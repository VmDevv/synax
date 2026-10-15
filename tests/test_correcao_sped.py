# -*- coding: utf-8 -*-
"""Da divergência apontada ao arquivo corrigido: a ponte e a tela.

O motor de reescrita já tinha suíte própria (test_sped_rewriter.py). O que se
prova aqui é o caminho que liga a auditoria a ele — e é onde moram os erros que
o motor sozinho não pega, porque ele confia no patch que recebe:

1. **A proposta é conferida contra o ARQUIVO, não contra o `result_data`.** O
   "de" que a pessoa lê na tela tem de ser o que está no arquivo hoje. Auditoria
   de semanas atrás, arquivo trocado no meio: a proposta sai impedida, com o
   motivo, em vez de gravar por cima de um número que ninguém conferiu.

2. **O que não dá para automatizar aparece dizendo por quê.** Divergência
   apurada na soma de várias linhas do mesmo produto não tem como ser repartida
   entre elas; crédito sem a base do XML não pode ser gravado sozinho (o Guia
   manda informar valor, base e alíquota em conjunto). Nos dois casos, sumir
   com a linha deixaria a tela dizendo que a nota está resolvida.

3. **Duas correções no mesmo alvo não se atropelam.** A mesma linha costuma
   receber o CFOP que a curadoria mapeou E o crédito que a auditoria apontou.
   Antes desta suíte, a validação do documento via só uma das duas (`dict`
   por item, a última ganhando) e a aplicação do frete procurava o D190 pelo
   CFOP DEPOIS de já ter trocado esse CFOP — estouro no meio do download.

4. **Ferramenta interna, escopo por empresa.** Quem gera arquivo de retificação
   é a equipe do escritório; cliente não vê a aba nem a rota.

5. **Nada é gravado sem prévia, e nada sai verde com zero.** Sem correção
   aplicável não há botão de gerar: baixar uma cópia do próprio SPED como se
   fosse a versão corrigida é pior do que o erro na tela.
"""
import io
import json
import os
import re
import sys
import types
import uuid

sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_correcao_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
# Nenhuma suite fala com API de modelo: sem chave, `resumir` usa o texto local,
# e os testes do caminho com IA trocam a funcao de chamada.
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
if os.path.exists("_correcao_test.db"):
    os.remove("_correcao_test.db")

from sqlalchemy import types as _satypes
import sqlalchemy.dialects.postgresql as pg


class _UUIDShim(_satypes.TypeDecorator):
    impl = _satypes.Uuid
    cache_ok = True

    def __init__(self, *a, **kw):
        kw.pop('as_uuid', None)
        super().__init__()

    def process_bind_param(self, value, dialect):
        return uuid.UUID(value) if isinstance(value, str) else value


pg.UUID = _UUIDShim

import backend.app.infrastructure.auth.supabase_client as sb  # noqa: E402
TOKENS = {}


class _Auth:
    def get_user(self, token):
        u = TOKENS.get(token.replace("Bearer ", "").strip())
        if not u:
            raise Exception("invalid")
        return types.SimpleNamespace(user=u)


class _Client:
    auth = _Auth()


sb.get_supabase_client = lambda: _Client()
sb.get_supabase_admin_client = lambda: _Client()
import backend.app.api.dependencies.auth as dep_auth  # noqa: E402
dep_auth.get_supabase_client = lambda: _Client()

from backend.app.infrastructure.database.session import engine as _eng  # noqa: E402
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit("ABORTADO: engine nao e SQLite")

from decimal import Decimal  # noqa: E402

from backend.app.services.sped_rewriter import (  # noqa: E402
    aplicar, ler_texto, montar, montar_patch, propor)
from backend.app.services.sped_rewriter import (  # noqa: E402
    texto as texto_do_relatorio)

OK = FALHAS = 0
_falhou = []


def _pode_cp1252(texto) -> bool:
    """O console do Windows e o cp1252. Fora dele, a impressao inteira morre."""
    try:
        str(texto).encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def check(nome, condicao, detalhe=""):
    global OK, FALHAS
    if condicao:
        OK += 1
        print(f"  OK   | {nome}")
    else:
        FALHAS += 1
        _falhou.append(nome)
        print(f" FALHA | {nome}  [{detalhe}]")


# ============================================================================
# Um SPED de entrada com o que interessa: três itens de mercadoria e um CT-e.
# Os contadores saem certos para o arquivo poder ser reescrito e conferido.
# ============================================================================
CHAVE_NF = "35260827865757000102550010000123451000012345"
CHAVE_CTE = "35260812345678000190570010001498311000149831"


def linha(reg, campos, total):
    valores = [""] * total
    valores[0] = reg
    for numero, valor in campos.items():
        valores[numero - 1] = valor
    return "|" + "|".join(valores) + "|"


def c170(num_item, cod, vl_item, cst, cfop, aliq="", bc="", icms="",
         cst_ipi="", bc_ipi="", aliq_ipi="", ipi=""):
    return linha("C170", {2: num_item, 3: cod, 5: "10,00000", 6: "UN",
                          7: vl_item, 9: "0", 10: cst, 11: cfop, 13: bc,
                          14: aliq, 15: icms, 20: cst_ipi, 22: bc_ipi,
                          23: aliq_ipi, 24: ipi}, 38)


def c190(cst, cfop, aliq, opr, bc, icms, ipi="0,00"):
    return linha("C190", {2: cst, 3: cfop, 4: aliq, 5: opr, 6: bc, 7: icms,
                          8: "0,00", 9: "0,00", 10: "0,00", 11: ipi}, 12)


def e110(debitos="500,00", creditos="180,00", saldo="320,00",
         recolher="320,00", credor="0,00", deducoes="0,00"):
    """A apuração do ICMS, coerente com os documentos da fixture.

    Os numeros fecham pela formula do Guia — (02+03+04+05) − (06+07+08+09+10) —
    de proposito: e essa coerencia que o motor confere ANTES de reescrever, e um
    E110 que ja nao fecha faria o teste passar pelo motivo errado.
    """
    return linha("E110", {2: debitos, 3: "0,00", 4: "0,00", 5: "0,00",
                          6: creditos, 7: "0,00", 8: "0,00", 9: "0,00",
                          10: "0,00", 11: saldo, 12: deducoes, 13: recolher,
                          14: credor, 15: "0,00"}, 15)


def montar_sped(analiticos_cte=None, cst_item3="000", bloco_e=None,
                vl_doc_c100="1800,00"):
    bloco_0 = ["|0000|018|0|01042026|30042026|COMERCIAL MASSUCATTI LTDA|"
               "27865757000102|ES|123456789|3205309|||A|1|",
               "|0001|0|",
               "|0150|FORN001|DIST LAURENCINI LTDA|1058|12345678000190||||||",
               "|0150|TRANSP01|TRANSPORTES MOBILINE LTDA|1058|"
               "34992472000270||||||"]
    bloco_0.append(f"|0990|{len(bloco_0) + 1}|")

    c100 = linha("C100", {2: "0", 3: "1", 4: "FORN001", 5: "55", 6: "00",
                          7: "1", 8: "2777", 9: CHAVE_NF, 10: "12042026",
                          11: "12042026", 12: vl_doc_c100, 13: "0", 14: "0,00",
                          15: "0,00", 16: "1800,00", 17: "9", 18: "0,00",
                          19: "0,00", 20: "0,00", 21: "1500,00", 22: "180,00",
                          23: "0,00", 24: "0,00", 25: "0,00", 26: "0,00",
                          27: "0,00", 28: "0,00", 29: "0,00"}, 29)
    itens = [
        # Item 1: creditou 120,00 e o XML tem 120,00 — nada a corrigir.
        c170("1", "CAFE500", "1000,00", "000", "1102", "12,00", "1000,00", "120,00"),
        # Item 2: creditou IPI que o CFOP nao permite -> glosa.
        c170("2", "ACUCAR1", "500,00", "000", "1102", "12,00", "500,00",
             "60,00", cst_ipi="00", bc_ipi="500,00", aliq_ipi="5,00", ipi="25,00"),
        # Item 3: entrou como 1556 sem credito nenhum. A curadoria aprovou 1102,
        # e com o CFOP certo o credito de 36,00 do XML e devido.
        c170("3", "OLEO900", "300,00", cst_item3, "1556"),
    ]
    analiticos = [
        c190("000", "1102", "12,00", "1525,00", "1500,00", "180,00", "25,00"),
        c190(cst_item3, "1556", "0,00", "300,00", "0,00", "0,00"),
    ]
    bloco_c = ["|C001|0|", c100] + itens + analiticos
    bloco_c.append(f"|C990|{len(bloco_c) + 1}|")

    d100 = linha("D100", {2: "0", 3: "1", 4: "TRANSP01", 5: "57", 6: "00",
                          7: "1", 8: "0", 9: "211146", 10: CHAVE_CTE,
                          11: "15042026", 12: "15042026", 13: "0",
                          15: "253,75", 16: "0,00", 17: "1", 18: "253,75",
                          19: "0,00", 20: "0,00", 21: "0,00"}, 25)
    d190s = analiticos_cte or [linha("D190", {2: "090", 3: "2352", 4: "0",
                                              5: "253,75", 6: "0,00",
                                              7: "0,00", 8: "0,00"}, 9)]
    bloco_d = ["|D001|0|", d100] + d190s
    bloco_d.append(f"|D990|{len(bloco_d) + 1}|")

    # Bloco E: a apuracao do periodo. `bloco_e=[]` monta um arquivo SEM ele, que
    # e um caso real (SPED so de entradas) e precisa de resposta propria.
    if bloco_e is None:
        bloco_e = ["|E001|0|", "|E100|01042026|30042026|", e110()]
    if bloco_e:
        bloco_e = list(bloco_e)
        bloco_e.append(f"|E990|{len(bloco_e) + 1}|")

    tipos = {}
    for l in bloco_0 + bloco_c + bloco_d + bloco_e:
        reg = l.split("|")[1]
        tipos[reg] = tipos.get(reg, 0) + 1
    tipos["9001"] = 1
    total_9900 = len(tipos) + 3
    tipos["9900"], tipos["9990"], tipos["9999"] = total_9900, 1, 1
    bloco_9 = ["|9001|0|"]
    bloco_9 += [f"|9900|{reg}|{qtd}|" for reg, qtd in tipos.items()]
    bloco_9.append(f"|9990|{1 + total_9900 + 1 + 1}|")

    linhas = bloco_0 + bloco_c + bloco_d + bloco_e + bloco_9
    linhas.append(f"|9999|{len(linhas) + 1}|")
    return "\r\n".join(linhas) + "\r\n"


SPED = montar_sped()

# O que a auditoria apontou neste arquivo, no formato real do `result_data`.
RESULTADO = {
    "cfop_classifications_issues": [{
        "key": CHAVE_NF, "num": "2777", "emit_name": "DIST LAURENCINI LTDA",
        "num_item": "3", "product_code": "OLEO900",
        "product_description": "OLEO DE SOJA 900ML",
        "supplier_cfop": "5102", "sped_cfop": "1556", "expected_cfop": "1102",
    }],
    "icms_issues": [
        {   # credito na mesa: com o CFOP corrigido, os 36,00 do XML sao devidos
            "key": CHAVE_NF, "num": "2777", "emit_name": "DIST LAURENCINI LTDA",
            "num_item": "3", "descr": "OLEO DE SOJA 900ML", "cfop": "1102",
            "reason": "ICMS nao aproveitado: o CFOP 1102 permite credito",
            "acao": "ajustar", "sped_icms": 0.0, "xml_icms": 36.0,
            "xml_bc_icms": 300.0, "xml_aliq_icms": 12.0,
        },
        {   # divergencia medida na SOMA de duas linhas do mesmo produto
            "key": CHAVE_NF, "num": "2777", "emit_name": "DIST LAURENCINI LTDA",
            "num_item": "1", "descr": "CAFE TORRADO 500G", "cfop": "1102",
            "reason": "Divergencia de credito de ICMS - soma das 2 linhas",
            "acao": "ajustar", "sped_icms": 120.0, "xml_icms": 132.0,
            "xml_bc_icms": 1100.0, "xml_aliq_icms": 12.0, "itens_agrupados": 2,
        },
        {   # frete: o CT-e tem 30,45 de ICMS e o SPED nao creditou nada
            "key": CHAVE_CTE, "num": "211146",
            "emit_name": "TRANSPORTES MOBILINE LTDA", "num_item": "CT-e",
            "descr": "Conhecimento de Transporte (Frete)", "cfop": "1353",
            "reason": "ICMS do frete nao aproveitado",
            "acao": "ajustar", "sped_cfop": "2352", "sped_icms": 0.0,
            "xml_icms": 30.45, "xml_bc_icms": 253.75,
        },
    ],
    "ipi_issues": [{
        "key": CHAVE_NF, "num": "2777", "emit_name": "DIST LAURENCINI LTDA",
        "num_item": "2", "descr": "ACUCAR CRISTAL 1KG", "cfop": "1102",
        "cst_ipi": "00", "reason": "CFOP 1102 com CST 00 nao permite credito de IPI",
        "acao": "glosar", "sped_val": 25.0, "xml_val": 25.0,
    }],
    # Divergencia de valor: o total escriturado nao bate com o documento. As
    # tres formas que o motor produz estao aqui — a com XML (corrigivel), a
    # interna (sem alvo) e a do frete.
    "value_discrepancies": [{
        "type": "NF-e", "key": CHAVE_NF, "num": "2777",
        "emit_name": "DIST LAURENCINI LTDA",
        "sped_val": 1800.0, "xml_val": 1825.0, "diff": 25.0,
        "reason": "Total da NF-e divergente (C100: R$ 1800,00 vs XML: R$ 1825,00).",
    }],
    "cte_cfop_classifications_issues": [{
        "key": CHAVE_CTE, "num": "211146",
        "emit_name": "TRANSPORTES MOBILINE LTDA", "emit_uf": "ES",
        "supplier_cfop": "5352", "sped_cfop": "2352", "expected_cfop": "1353",
    }],
    "resumo": {"notas_processadas": 2, "inconsistencias": 5},
}


def por_id(propostas, opcao, alvo=""):
    for p in propostas:
        if p.opcao == opcao and (not alvo or alvo in p.alvo):
            return p
    return None


# ============================================================================
print("\n=== 1. A ponte le o ARQUIVO, nao so o relatorio ===")

propostas = propor(RESULTADO, SPED)

cfop = por_id(propostas, "cfop_produto")
check("divergencia de CFOP vira proposta", cfop is not None)
check("com o 'de' lido do arquivo, nao do result_data",
      cfop and cfop.mudancas == [("CFOP", "1556", "1102")], cfop and cfop.mudancas)
check("e o campo do patch e o CFOP", cfop and cfop.campos == {"CFOP": "1102"},
      cfop and cfop.campos)
check("a proposta e aplicavel", cfop and cfop.aplicavel)

icms = por_id(propostas, "credito_icms_mercadoria", "Item 3")
check("credito nao aproveitado vira proposta", icms is not None)
check("com os TRES campos (valor, base e aliquota) juntos",
      icms and set(icms.campos) == {"VL_BC_ICMS", "ALIQ_ICMS", "VL_ICMS"},
      icms and set(icms.campos))
check("a aliquota vai como texto com virgula, nao como float",
      icms and icms.campos.get("ALIQ_ICMS") == "12,00",
      icms and repr(icms.campos.get("ALIQ_ICMS")))
check("e o delta mostra quanto de credito entra",
      icms and icms.delta == Decimal("36.00"), icms and icms.delta)

ipi = por_id(propostas, "credito_ipi")
check("glosa de IPI pede UM campo so: zerar o valor",
      ipi and ipi.campos == {"VL_IPI": Decimal("0")}, ipi and ipi.campos)
check("e o delta e negativo (credito sai)",
      ipi and ipi.delta == Decimal("-25.00"), ipi and ipi.delta)

frete_cfop = por_id(propostas, "cfop_frete")
check("CFOP de frete casa o D190 pelo CFOP escriturado",
      frete_cfop and frete_cfop.mudancas == [("CFOP", "2352", "1353")],
      frete_cfop and frete_cfop.mudancas)

frete_icms = por_id(propostas, "credito_icms_frete")
check("credito de ICMS do frete vira proposta no D190",
      frete_icms and frete_icms.campos.get("VL_ICMS") == Decimal("30.45"),
      frete_icms and frete_icms.campos)
check("com a base do CT-e junto",
      frete_icms and frete_icms.campos.get("VL_BC_ICMS") == Decimal("253.75"))

check("todo id e unico", len({p.id for p in propostas}) == len(propostas),
      [p.id for p in propostas])


# ============================================================================
print("\n=== 2. O que nao da para automatizar aparece, com o motivo ===")

agrupada = por_id(propostas, "credito_icms_mercadoria", "Item 1")
check("divergencia apurada na soma de N linhas NAO vira correcao",
      agrupada is not None and not agrupada.aplicavel)
check("e o motivo diz que a diferenca e do conjunto",
      agrupada and "soma de 2 linhas" in agrupada.impedimento,
      agrupada and agrupada.impedimento)

antigo = dict(RESULTADO)
antigo["icms_issues"] = [{
    "key": CHAVE_NF, "num": "2777", "num_item": "3", "descr": "OLEO",
    "cfop": "1102", "reason": "ICMS nao aproveitado", "acao": "ajustar",
    "sped_icms": 0.0, "xml_icms": 36.0,   # sem xml_bc_icms: auditoria antiga
}]
sem_base = por_id(propor(antigo, SPED), "credito_icms_mercadoria")
check("credito sem a base do XML nao e gravado sozinho",
      sem_base is not None and not sem_base.aplicavel)
check("e o motivo manda rodar a auditoria de novo",
      sem_base and "rode a auditoria de novo" in sem_base.impedimento,
      sem_base and sem_base.impedimento)

fantasma = {"cfop_classifications_issues": [
    {"key": "3" * 44, "num": "999", "num_item": "1", "expected_cfop": "1102"},
    {"key": CHAVE_NF, "num": "2777", "num_item": "99", "expected_cfop": "1102"},
]}
fora = propor(fantasma, SPED)
check("documento que nao esta no arquivo sai impedido",
      not fora[0].aplicavel and "nao encontrado" in
      fora[0].impedimento.replace("ã", "a").replace("é", "e"),
      fora[0].impedimento)
check("item que nao existe no documento tambem",
      not fora[1].aplicavel and "99" in fora[1].impedimento,
      fora[1].impedimento)

ja_feito = {"cfop_classifications_issues": [
    {"key": CHAVE_NF, "num": "2777", "num_item": "1", "expected_cfop": "1102"}]}
check("correcao que o arquivo ja tem sai como 'ja esta assim', nao aplicavel",
      not propor(ja_feito, SPED)[0].aplicavel)

# CT-e com dois analiticos e sem o CFOP na divergencia (auditoria antiga).
sped_dois_d190 = montar_sped(analiticos_cte=[
    linha("D190", {2: "090", 3: "2352", 4: "0", 5: "100,00", 6: "0,00",
                   7: "0,00", 8: "0,00"}, 9),
    linha("D190", {2: "000", 3: "1352", 4: "12,00", 5: "153,75", 6: "153,75",
                   7: "18,45", 8: "0,00"}, 9),
])
ambiguo = {"icms_issues": [{
    "key": CHAVE_CTE, "num": "211146", "num_item": "CT-e", "cfop": "1353",
    "reason": "ICMS do frete", "acao": "ajustar", "sped_icms": 0.0,
    "xml_icms": 30.45, "xml_bc_icms": 253.75,
}]}
p_amb = propor(ambiguo, sped_dois_d190)[0]
check("CT-e com dois D190 e sem o CFOP escriturado nao e adivinhado",
      not p_amb.aplicavel and "2 registros D190" in p_amb.impedimento,
      p_amb.impedimento)


# ============================================================================
print("\n=== 3. Do patch ao arquivo: as duas correcoes do mesmo alvo ===")

# O item 3 recebe CFOP (curadoria) E credito (auditoria); o CT-e recebe as duas
# tambem. Antes da correcao em patch.py, a validacao via so uma das duas e a
# aplicacao do frete estourava ao procurar o D190 pelo CFOP ja trocado.
todos = [p.id for p in propostas if p.aplicavel]
patch = montar_patch(propostas, todos, aprovada_por="joao@vm.com.br")
check("o patch leva as correcoes de item e de frete separadas",
      len(patch.correcoes) == 3 and len(patch.fretes) == 2,
      (len(patch.correcoes), len(patch.fretes)))

doc = ler_texto(SPED)
resultado = aplicar(doc, patch)
check("nenhuma recusa", not resultado.recusas,
      [(r.documento[:12], r.motivo) for r in resultado.recusas])
saida = doc.serializar()
linhas_saida = [l for l in saida.split("\r\n") if l]


def campos_de(prefixo, indice=0):
    achadas = [l for l in linhas_saida if l.startswith(prefixo)]
    return achadas[indice].split("|") if achadas else []


item3 = [l.split("|") for l in linhas_saida if l.startswith("|C170|3|")][0]
check("o item 3 saiu com CFOP 1102 e credito de 36,00",
      (item3[11], item3[15]) == ("1102", "36,00"), (item3[11], item3[15]))
check("e com base e aliquota preenchidas junto",
      (item3[13], item3[14]) == ("300,00", "12,00"), (item3[13], item3[14]))

item2 = [l.split("|") for l in linhas_saida if l.startswith("|C170|2|")][0]
check("o IPI glosado zerou valor, base e aliquota",
      (item2[22], item2[23], item2[24]) == ("0,00", "0,00", "0,00"),
      (item2[22], item2[23], item2[24]))
check("o CST do IPI acompanhou a glosa (49 = outras entradas)",
      item2[20] == "49", item2[20])
check("e os 25,00 migraram para o valor da mercadoria (custo)",
      item2[7] == "525,00", item2[7])

d190 = campos_de("|D190|")
check("o frete saiu com o CFOP novo E o credito no mesmo registro",
      (d190[3], d190[6], d190[7]) == ("1353", "253,75", "30,45"),
      (d190[3], d190[6], d190[7]))
d100 = campos_de("|D100|")
check("e o D100 pai foi espelhado", (d100[19], d100[20]) == ("253,75", "30,45"),
      (d100[19], d100[20]))

c190s = [l.split("|") for l in linhas_saida if l.startswith("|C190|")]
check("o C190 do 1556 sumiu (nao restou item na combinacao)",
      not any(c[3] == "1556" for c in c190s), [c[3] for c in c190s])
check("e o do 1102 somou o item que chegou",
      any(c[3] == "1102" and c[7] == "216,00" for c in c190s),
      [(c[3], c[7]) for c in c190s])

# Dar credito a um item cujo CST diz que nao ha imposto e contradicao dentro do
# proprio arquivo. Desde 09/09/2026 o motor ajusta o CST sozinho: exigir que a
# pessoa corrija isso item a item e microgerencia que o PVA cobra na entrega.
sped_st = montar_sped(cst_item3="060")
so_credito = {"icms_issues": [i for i in RESULTADO["icms_issues"]
                              if i["num_item"] == "3"]}
props_st = propor(so_credito, sped_st)
doc_st = ler_texto(sped_st)
res_st = aplicar(doc_st, montar_patch(props_st, [p.id for p in props_st]))
item3_st = [l for l in doc_st.serializar().splitlines()
            if l.startswith("|C170|3|")][0].split("|")
check("item que ganha credito sai com CST tributado (060 -> 000)",
      item3_st[10] == "000", item3_st[10])
check("e a troca vem marcada como cascata, com o motivo",
      any(a.campo == "CST_ICMS" and a.origem == "cascata"
          and "crédito de ICMS" in a.motivo for a in res_st.alteracoes),
      [(a.campo, a.de, a.para) for a in res_st.alteracoes if "CST" in a.campo])
check("com o CST alinhado, o item entra no C190 que ja existia",
      len([l for l in doc_st.serializar().splitlines()
           if l.startswith("|C190|000|1102|")]) == 1)

check("o 9999 fecha com o total de linhas do arquivo",
      campos_de("|9999|")[2] == str(len(linhas_saida)),
      (campos_de("|9999|")[2], len(linhas_saida)))

impedidos = [p.id for p in propostas if not p.aplicavel]
check("proposta impedida NAO entra no patch nem se vier marcada no POST",
      not montar_patch(propostas, impedidos).correcoes
      and not montar_patch(propostas, impedidos).fretes)
check("id que nao existe e ignorado",
      not montar_patch(propostas, ["cfop_produto|inventado|1"]).correcoes)


# ============================================================================
print("\n=== 4. A tela: escopo, previa e download ===")

import backend.app.main as m  # noqa: E402
from backend.app.infrastructure.database.session import SessionLocal  # noqa: E402
from backend.app.domain.models.base import Base  # noqa: E402
from backend.app.domain.models.auth_models import (  # noqa: E402
    Tenant, UserProfile, UserTenant)
from backend.app.domain.models.audit import AuditTask  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

Base.metadata.create_all(bind=_eng)
db = SessionLocal()
tA, tB = uuid.uuid4(), uuid.uuid4()
db.add_all([Tenant(id=tA, name="Empresa A", cnpj="27865757000102"),
            Tenant(id=tB, name="Empresa B", cnpj="22222222222222")])
u_ope, u_cli, u_cliB = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
db.add_all([
    UserProfile(id=u_ope, tenant_id=tA, role="operador", name="Operador"),
    UserProfile(id=u_cli, tenant_id=tA, role="cliente", name="Cliente A"),
    UserProfile(id=u_cliB, tenant_id=tB, role="cliente", name="Cliente B"),
])
db.add(UserTenant(user_id=u_ope, tenant_id=tA))

TASK = "11111111-2222-3333-4444-555555555555"
TASK_SEM_ARQUIVO = "99999999-8888-7777-6666-555555555555"
db.add(AuditTask(id=TASK, tenant_id=str(tA), status="concluido",
                 user_id=str(u_ope), file_name="SPED_042026.txt",
                 result_data=json.dumps(RESULTADO)))
db.add(AuditTask(id=TASK_SEM_ARQUIVO, tenant_id=str(tA), status="concluido",
                 user_id=str(u_ope), file_name="SPED_032026.txt",
                 result_data=json.dumps(RESULTADO)))
db.commit()
db.close()

TOKENS["ope"] = types.SimpleNamespace(id=str(u_ope), email="oper@synax.com")
TOKENS["cli"] = types.SimpleNamespace(id=str(u_cli), email="cli@synax.com")
TOKENS["cliB"] = types.SimpleNamespace(id=str(u_cliB), email="cliB@synax.com")

from backend.app.api.dependencies.db import get_rls_db as _real_rls  # noqa: E402


def _fake_rls():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


m.app.dependency_overrides[_real_rls] = _fake_rls

# O SPED desta auditoria fica em temp_uploads/<task_id>/sped.txt — e o
# `execute_audit` o deixa la de proposito, justamente para esta tela.
PASTA = os.path.join(os.getcwd(), "temp_uploads", TASK)
os.makedirs(PASTA, exist_ok=True)
CAMINHO = os.path.join(PASTA, "sped.txt")
with io.open(CAMINHO, "w", encoding="latin-1", newline="") as f:
    f.write(SPED)

c = TestClient(m.app, raise_server_exceptions=False)


def as_(token):
    c.cookies.clear()
    if token:
        c.cookies.set("access_token", f"Bearer {token}")
    return c


r = as_("ope").get(f"/api/audit/{TASK}/correcao")
check("operador da empresa abre a lista", r.status_code == 200, r.status_code)
check("as correcoes aplicaveis aparecem agrupadas por tipo",
      "CFOP de produtos" in r.text and "Crédito de IPI" in r.text)
check("com o de -> para de cada campo", "1556" in r.text and "1102" in r.text)
check("o que nao da para corrigir aparece com o motivo",
      "não dá para corrigir por aqui" in r.text
      and "soma de 2 linhas" in r.text)
check("a tela oferece a caixa da apuracao, marcada",
      'name="atualizar_e110"' in r.text and "checked" in r.text)
# O texto e comparado com o espaco normalizado: quebra de linha no template nao
# pode reprovar um teste que fala sobre o que a frase DIZ.
_liso = " ".join(re.sub(r"<[^>]+>", " ", r.text).split())
check("e diz, na propria caixa, que isso muda imposto a recolher",
      "muda o imposto a recolher" in _liso)
check("e que a apuracao e conferida antes de ser gravada",
      "fecha pela f" in _liso and "Ajustes (E111)" in _liso)

r_cli = as_("cli").get(f"/api/audit/{TASK}/correcao")
check("cliente da propria empresa e barrado (ferramenta interna)",
      r_cli.status_code == 403, r_cli.status_code)
r_outro = as_("cliB").get(f"/api/audit/{TASK}/correcao")
check("usuario de outra empresa nao ve a auditoria",
      r_outro.status_code in (403, 404), r_outro.status_code)
r_anon = as_(None).get(f"/api/audit/{TASK}/correcao")
check("anonimo barrado", r_anon.status_code in (401, 403), r_anon.status_code)

r_sem = as_("ope").get(f"/api/audit/{TASK_SEM_ARQUIVO}/correcao")
check("auditoria cujo arquivo ja saiu do disco explica o que houve",
      r_sem.status_code == 200 and "não está mais no servidor" in r_sem.text,
      r_sem.status_code)

ids_tela = re.findall(r'name="aceitas" value="([^"]+)"', r.text)
check("a tela oferece uma caixa por correcao aplicavel",
      len(ids_tela) == len(todos), (len(ids_tela), len(todos)))

r_vazia = as_("ope").post(f"/api/audit/{TASK}/correcao/previa", data={})
check("previa sem nada marcado responde 200 com mensagem, nao 4xx",
      r_vazia.status_code == 200 and "Nenhuma correção marcada" in r_vazia.text,
      r_vazia.status_code)

r_prev = as_("ope").post(f"/api/audit/{TASK}/correcao/previa",
                         data={"aceitas": ids_tela})
check("a previa lista o que sera aplicado", r_prev.status_code == 200
      and "Serão aplicadas" in r_prev.text, r_prev.status_code)
check("mostra o que muda junto por obrigacao do leiaute",
      "Muda junto" in r_prev.text and "C190" in r_prev.text)
check("mostra o impacto na apuracao", "Impacto na apuração" in r_prev.text)
check("e o botao de gerar so existe aqui, depois da previa",
      "correcao/gerar" in r_prev.text and "correcao/gerar" not in r.text)

r_txt = as_("ope").post(f"/api/audit/{TASK}/correcao/gerar",
                        data={"aceitas": ids_tela})
check("o download responde 200", r_txt.status_code == 200, r_txt.status_code)
check("com nome tirado do 0000 do proprio arquivo",
      "Sped_Corrigido_COMERCIAL MASSUCATTI LTDA.txt"
      in r_txt.headers.get("content-disposition", ""),
      r_txt.headers.get("content-disposition"))
corrigido = r_txt.content.decode("latin-1")
check("o arquivo baixado tem as correcoes",
      "|C170|3|OLEO900|" in corrigido and "|1102|" in corrigido
      and "|36,00|" in corrigido)
check("o SPED ORIGINAL em disco continua intocado",
      io.open(CAMINHO, encoding="latin-1", newline="").read() == SPED)

r_xls = as_("ope").post(f"/api/audit/{TASK}/correcao/relatorio",
                        data={"aceitas": ids_tela})
check("o relatorio de modificacoes baixa em Excel",
      r_xls.status_code == 200 and r_xls.content[:2] == b"PK", r_xls.status_code)

r_nada = as_("ope").post(f"/api/audit/{TASK}/correcao/gerar",
                         data={"aceitas": impedidos})
check("gerar so com correcao impedida devolve erro, nao uma copia do original",
      r_nada.status_code == 409, r_nada.status_code)

r_cli_gerar = as_("cli").post(f"/api/audit/{TASK}/correcao/gerar",
                              data={"aceitas": ids_tela})
check("cliente nao gera arquivo nem pela rota direta",
      r_cli_gerar.status_code == 403, r_cli_gerar.status_code)


# ============================================================================
print("\n=== 5. O download e forcado, e o SPED nao aparece na tela ===")

cd = r_txt.headers.get("content-disposition", "")
check("a resposta manda SALVAR o arquivo", cd.startswith("attachment;"), cd)
check("e o tipo nao convida o navegador a renderizar",
      r_txt.headers.get("content-type", "") == "application/octet-stream",
      r_txt.headers.get("content-type"))
check("com nosniff, para nao adivinhar o tipo pelo conteudo",
      r_txt.headers.get("x-content-type-options") == "nosniff")
check("o form de gerar abre em aba nova (a tela de resultado fica de pe)",
      'target="_blank"' in r_prev.text)

from backend.app.api.routes.correcao_sped import _cabecalhos_download  # noqa: E402
com_acento = _cabecalhos_download("Sped_Corrigido_ORGANIZA\u00c7\u00c3O ABC.txt")
check("nome com acento vai em ASCII e em UTF-8 (RFC 5987)",
      'filename="Sped_Corrigido_ORGANIZACAO ABC.txt"'
      in com_acento["Content-Disposition"]
      and "filename*=UTF-8''" in com_acento["Content-Disposition"],
      com_acento["Content-Disposition"])
check("e o cabecalho e ASCII puro, que e o que o HTTP aceita",
      all(ord(c) < 128 for c in com_acento["Content-Disposition"]))


# ============================================================================
print("\n=== 6. Resumo executivo: a IA escreve, o motor calcula ===")

from backend.app.services.sped_rewriter import resumo_ia  # noqa: E402

doc_r = ler_texto(SPED)
res_r = aplicar(doc_r, montar_patch(propostas, todos))
rel_r = montar(res_r, doc_r, SPED, origem="SPED_042026.txt")
f = resumo_ia.fatos(rel_r)

check("os fatos contam as alteracoes por CAMPO, nao so o total",
      f["alteracoes_por_campo"].get("CFOP") == 2, f["alteracoes_por_campo"])
check("e trazem o impacto em reais ja formatado",
      f["icms_diferenca"] == "66,45", f["icms_diferenca"])

local = resumo_ia.resumo_local(f)
check("o resumo local cita os numeros do motor",
      "66,45" in local and "documento(s)" in local, local)

check("numero que ninguem enviou reprova o texto do modelo",
      not resumo_ia._so_usa_os_numeros_dados(
          "Houve um aumento de 37% no credito.", f))
check("e o texto que so usa os numeros dados passa",
      resumo_ia._so_usa_os_numeros_dados(
          "O credito de ICMS subiu R$ " + f["icms_diferenca"] + ".", f))

texto_r, da_ia, motivo = resumo_ia.resumir(rel_r)
check("sem OPENAI_API_KEY o resumo sai igual, marcado como do sistema",
      texto_r == local and da_ia is False)
check("e o motivo diz o que falta, em vez de so 'nao deu'",
      "OPENAI_API_KEY" in motivo, motivo)

_original = resumo_ia._chamar_modelo
resumo_ia._chamar_modelo = lambda fatos: (
    "Foram corrigidos " + str(fatos["correcoes_aceitas"])
    + " itens, e o credito de ICMS subiu R$ " + fatos["icms_diferenca"] + ".")
texto_ia, da_ia, motivo = resumo_ia.resumir(rel_r)
check("com o modelo respondendo, o texto dele e usado e marcado",
      da_ia is True and "credito de ICMS subiu" in texto_ia, texto_ia)
check("e sem motivo, porque nao houve falha", motivo == "", motivo)

resumo_ia._chamar_modelo = lambda fatos: (
    "A correcao gerou um incremento de R$ 9.999,99 nos creditos.")
texto_falso, da_ia, motivo = resumo_ia.resumir(rel_r)
check("modelo que inventa numero e DESCARTADO, vale o resumo local",
      da_ia is False and texto_falso == local, texto_falso)
check("e o motivo diz que o texto trouxe numero de fora",
      "n\u00famero" in motivo, motivo)


def _explode(fatos):
    raise RuntimeError("API fora do ar")


resumo_ia._chamar_modelo = _explode
texto_erro, da_ia, motivo = resumo_ia.resumir(rel_r)
check("API fora do ar nao derruba o relatorio",
      da_ia is False and texto_erro == local)
check("e o motivo generico e dito, nao engolido", bool(motivo), motivo)

# Conta sem credito e a falha MAIS COMUM em producao (aconteceu em 09/09/2026,
# no primeiro uso real). Nao e defeito: e configuracao, e a tela tem de dizer
# isso em vez de deixar o traceback no log do servidor. Na OpenAI ela chega como
# 429 com "insufficient_quota" — o MESMO codigo de um pico de uso, e as duas
# situacoes pedem coisas diferentes de quem le.
import httpx2  # noqa: E402
import openai  # noqa: E402


def _erro_openai(classe, mensagem, status=429):
    return classe(
        message=mensagem,
        response=httpx2.Response(
            status, request=httpx2.Request("POST", "http://x")),
        body=None)


_erro_credito = _erro_openai(
    openai.RateLimitError,
    "You exceeded your current quota, please check your plan and billing details.")
check("sem credito vira uma frase que diz onde resolver",
      "cr\u00e9ditos" in resumo_ia._motivo_da_falha(_erro_credito)
      and "Billing" in resumo_ia._motivo_da_falha(_erro_credito),
      resumo_ia._motivo_da_falha(_erro_credito))
check("e pico de uso NAO e confundido com falta de credito",
      "limite de uso" in resumo_ia._motivo_da_falha(_erro_openai(
          openai.RateLimitError, "Rate limit reached for gpt-5.1")),
      resumo_ia._motivo_da_falha(_erro_openai(
          openai.RateLimitError, "Rate limit reached for gpt-5.1")))
check("chave revogada tambem tem frase propria",
      "revogada" in resumo_ia._motivo_da_falha(_erro_openai(
          openai.AuthenticationError, "Incorrect API key provided", 401)))


def _sem_credito(fatos):
    raise _erro_credito


resumo_ia._chamar_modelo = _sem_credito
texto_sc, da_ia, motivo = resumo_ia.resumir(rel_r)
check("e o relatorio sai assim mesmo, com o motivo na mao",
      da_ia is False and texto_sc == local and "cr\u00e9ditos" in motivo, motivo)
resumo_ia._chamar_modelo = _original

r_res = as_("ope").post("/api/audit/" + TASK + "/correcao/resumo",
                        data={"aceitas": ids_tela})
check("a rota do resumo responde 200", r_res.status_code == 200, r_res.status_code)
check("e marca que o texto veio do sistema, nao da IA",
      "gerado pelo sistema" in r_res.text)
check("e diz na TELA por que a IA nao escreveu",
      "A IA n\u00e3o escreveu este resumo" in r_res.text
      and "OPENAI_API_KEY" in r_res.text)
r_res_vazio = as_("ope").post("/api/audit/" + TASK + "/correcao/resumo", data={})
check("sem correcao marcada, o resumo explica em vez de inventar",
      r_res_vazio.status_code == 200 and "ao menos uma" in r_res_vazio.text)


# ============================================================================
print("\n=== 7. Trilha: quem gerou, quando, sobre qual arquivo ===")

from backend.app.domain.models.audit import SpedCorrectionRun  # noqa: E402

ss = SessionLocal()
linhas_trilha = ss.query(SpedCorrectionRun).filter_by(task_id=TASK).all()
check("gerar o arquivo deixou registro", len(linhas_trilha) == 1,
      len(linhas_trilha))
reg = linhas_trilha[0] if linhas_trilha else None
check("com o e-mail de quem gerou", reg and reg.user_email == "oper@synax.com",
      reg and reg.user_email)
check("o nome do arquivo entregue",
      reg and reg.file_name == "Sped_Corrigido_COMERCIAL MASSUCATTI LTDA.txt",
      reg and reg.file_name)
check("os dois hashes, que amarram o registro aos arquivos",
      reg and len(reg.hash_origem or "") == 64 and len(reg.hash_destino or "") == 64)
check("hashes DIFERENTES entre origem e destino (o arquivo mudou mesmo)",
      reg and reg.hash_origem != reg.hash_destino)
check("a competencia lida do 0000", reg and reg.competencia == "2026-04",
      reg and reg.competencia)
check("o impacto no credito de ICMS",
      reg and Decimal(str(reg.icms_diferenca)) == Decimal("66.45"),
      reg and reg.icms_diferenca)
check("e o relatorio inteiro, para provar o que foi entregue",
      reg and '"total_alteracoes"' in (reg.detalhe or "")
      and '"hash_destino"' in (reg.detalhe or ""))
check("o tenant vai junto (a trilha e por empresa)",
      reg and reg.tenant_id == str(tA))
ss.close()

r_lista = as_("ope").get("/api/audit/" + TASK + "/correcao")
check("a tela avisa que este SPED ja foi corrigido",
      "j\u00e1 foi corrigido" in r_lista.text and "oper@synax.com" in r_lista.text)

from backend.app.api.routes.correcao_sped import _geracoes_anteriores  # noqa: E402
ss = SessionLocal()
linhas = _geracoes_anteriores(ss, TASK)
ss.close()
check("a consulta da tela NAO carrega o JSON do relatorio",
      bool(linhas) and not hasattr(linhas[0], "detalhe"))


# ============================================================================
print("\n=== 8. A aba na tela de resultado ===")

TELA = io.open(os.path.join("frontend", "templates", "partials", "curation",
                            "curation_results.html"), encoding="utf-8").read()
check("existe a aba 'Corrigir SPED'", "tab-correcao-sped" in TELA)
def dentro_de_is_internal(texto, marcador):
    """Toda ocorrencia do marcador esta sob um `{% if is_internal %}` aberto."""
    pos = 0
    for _ in range(texto.count(marcador)):
        pos = texto.index(marcador, pos)
        antes = texto[:pos]
        if antes.rfind("{% if is_internal %}") < antes.rfind("{% endif %}"):
            return False
        pos += len(marcador)
    return True


# Confere os ELEMENTOS de tela, nao toda mencao ao id: ele aparece tambem na
# lista `sempreVisiveis` do JavaScript, que nao e elemento e nao precisa (nem
# deve) ficar sob `is_internal`.
check("e ela e interna: botao do menu, atalho e painel sob is_internal",
      dentro_de_is_internal(TELA, 'data-tab="tab-correcao-sped"')
      and dentro_de_is_internal(TELA, 'id="panel-tab-correcao-sped"'))
check("o botao esta no TOPO do menu, antes das abas de dado",
      TELA.index('data-tab="tab-correcao-sped"')
      < TELA.index('data-tab="tab-general-report"'))
check("e a rotina que dobra abas vazias nao o esconde",
      "'tab-correcao-sped'" in TELA.split("var sempreVisiveis")[1][:200],
      TELA.split("var sempreVisiveis")[1][:120])
check("a Visao Geral tem o atalho para corrigir",
      "Gerar SPED corrigido" in TELA)
# Até 30/09/2026 o gatilho era "click once from:[data-tab=...]": o atalho verde
# da Visão Geral trocava de painel SEM clicar na aba, e a pessoa ficava olhando
# o "Conferindo…" girar para sempre — nenhum pedido chegava ao servidor.
_painel = TELA.split('id="panel-tab-correcao-sped"')[1][:1500]
check("a lista carrega quando o painel APARECE (qualquer caminho), nao no load",
      'hx-trigger="intersect once"' in _painel and 'hx-trigger="load' not in _painel,
      _painel[:200])
check("e nao depende mais do clique na aba lateral",
      "click once from:[data-tab='tab-correcao-sped']" not in TELA)

OPCOES = io.open(os.path.join("frontend", "templates", "partials", "curation",
                              "_correcao_sped.html"), encoding="utf-8").read()
PREVIA = io.open(os.path.join("frontend", "templates", "partials", "curation",
                              "_correcao_previa.html"), encoding="utf-8").read()
BASE = io.open(os.path.join("frontend", "templates", "layout", "base.html"),
               encoding="utf-8").read()
check("(o <body> tem hx-boost, que e o que torna a trava abaixo obrigatoria)",
      'hx-boost="true"' in BASE)
check("o form de download fica FORA do boost, senao o SPED e injetado na tela",
      'hx-boost="false"' in PREVIA
      and 'action="/api/audit/{{ task_id }}/correcao/gerar"' in PREVIA)
check("e o form da lista tambem", 'hx-boost="false"' in OPCOES)

check("o hx-include aponta para o form por id, nao para [name=aceitas]",
      'hx-include="#form-correcao-sped"' in OPCOES
      and "[name='aceitas']" not in OPCOES)
check("e o 'marcar todas' so mexe nas caixas desta tela",
      "form.querySelectorAll('.correcao-check')" in OPCOES)


# ============================================================================
print("\n=== 9. Valor do documento: so corrige o total quando os itens fecham ===")

# Na fixture, os analiticos somam 1.825,00 (1.525,00 + 300,00) e o C100 declara
# 1.800,00 — o XML confirma 1.825,00. Aqui o total e que esta errado, e e o
# unico caso em que gravar o VL_DOC resolve em vez de esconder.
p_valor = por_id(propostas, "valor_documento")
check("divergencia de valor com o XML vira proposta",
      p_valor is not None and p_valor.aplicavel,
      p_valor and p_valor.impedimento)
check("que grava o VL_DOC, e so ele",
      p_valor and p_valor.campos == {"VL_DOC": Decimal("1825.00")},
      p_valor and p_valor.campos)
check("com o de -> para lido do arquivo",
      p_valor and p_valor.mudancas == [("VL_DOC", "1800,00", "1825,00")],
      p_valor and p_valor.mudancas)

# Nota que NAO fecha: os analiticos somam 1.825,00 e o documento vale 2.000,00.
# Ate 10/09/2026 isso era recusado; o Sandro pediu que fechasse, e o desenho e o
# unico que fecha sem inventar valor de item — o rateio passa a mirar o total e
# o residuo vira despesa acessoria.
nao_fecha = {"value_discrepancies": [{
    "type": "NF-e", "key": CHAVE_NF, "num": "2777", "emit_name": "X",
    "sped_val": 1800.0, "xml_val": 2000.0, "diff": 200.0,
}]}
p_nf = propor(nao_fecha, SPED)[0]
check("total que os itens nao sustentam AGORA e corrigivel",
      p_nf.aplicavel, p_nf.impedimento)
check("e o motivo avisa que a nota sera fechada, sem mexer nos itens",
      "resíduo declarado em outras despesas" in p_nf.motivo
      and "itens não são alterados" in p_nf.motivo, p_nf.motivo)

doc_f = ler_texto(SPED)
res_f = aplicar(doc_f, montar_patch(propor(nao_fecha, SPED), [p_nf.id]))
linhas_f = doc_f.serializar().splitlines()
c100_f = [l for l in linhas_f if l.startswith("|C100|")][0].split("|")
opr_f = sum(Decimal(l.split("|")[5].replace(".", "").replace(",", "."))
            for l in linhas_f if l.startswith("|C190|"))
check("o VL_DOC vai para o valor do documento", c100_f[12] == "2000,00",
      c100_f[12])
check("a soma dos VL_OPR fecha com ele (a 1a conta do PVA)",
      opr_f == Decimal("2000.00"), opr_f)
componentes = (Decimal(c100_f[16].replace(",", "."))       # VL_MERC
               + Decimal(c100_f[18].replace(",", "."))     # VL_FRT
               + Decimal(c100_f[19].replace(",", "."))     # VL_SEG
               + Decimal(c100_f[20].replace(",", "."))     # VL_OUT_DA
               + Decimal(c100_f[24].replace(",", "."))     # VL_ICMS_ST
               + Decimal(c100_f[25].replace(",", "."))     # VL_IPI
               - Decimal(c100_f[14].replace(",", ".")))    # VL_DESC
check("e os componentes do C100 tambem (a 2a conta do PVA)",
      componentes == Decimal("2000.00"), componentes)
check("o residuo foi declarado como outras despesas acessorias",
      c100_f[20] == "175,00", c100_f[20])
check("e a linha aparece no relatorio, com o motivo",
      any(a.campo == "VL_OUT_DA" and "resíduo" in a.motivo
          for a in res_f.alteracoes),
      [(a.campo, a.motivo[:40]) for a in res_f.alteracoes])
check("NENHUM item foi tocado",
      [l for l in linhas_f if l.startswith("|C170|")]
      == [l for l in SPED.splitlines() if l.startswith("|C170|")])

# O inverso continua recusado: itens valendo MAIS que o documento so fechariam
# diminuindo mercadoria ou imposto, e isso e inventar.
maior = {"value_discrepancies": [{
    "type": "NF-e", "key": CHAVE_NF, "num": "2777", "emit_name": "X",
    "sped_val": 1800.0, "xml_val": 1000.0, "diff": 800.0,
}]}
p_maior = propor(maior, SPED)[0]
check("itens somando MAIS que o documento seguem recusados",
      not p_maior.aplicavel and "acima do total" in p_maior.impedimento,
      p_maior.impedimento)

interno = {"value_discrepancies": [{
    "type": "NF-e (SPED C100 vs C190)", "key": CHAVE_NF, "num": "2777",
    "emit_name": "X", "sped_val": 1825.0, "xml_val": 1800.0, "diff": 25.0,
}]}
p_int = propor(interno, SPED)[0]
check("divergencia interna do SPED nao tem alvo: sai impedida",
      not p_int.aplicavel and "Sem o XML" in p_int.impedimento,
      p_int.impedimento)

frete_valor = {"value_discrepancies": [{
    "type": "CT-e", "key": CHAVE_CTE, "num": "211146", "emit_name": "MOBILINE",
    "sped_val": 253.75, "xml_val": 300.0, "diff": 46.25,
}]}
p_cte = propor(frete_valor, SPED)[0]
check("valor do CT-e vira proposta no D100",
      p_cte.aplicavel and p_cte.campos == {"VL_DOC": Decimal("300.00")},
      p_cte.impedimento or p_cte.campos)

doc_v = ler_texto(SPED)
aplicar(doc_v, montar_patch(propor(frete_valor, SPED), [p_cte.id]))
d190_v = [l for l in doc_v.serializar().splitlines()
          if l.startswith("|D190|")][0].split("|")
check("e o VL_OPR do analitico unico acompanha o valor novo",
      d190_v[5] == "300,00", d190_v[5])


# ============================================================================
print("\n=== 10. Apuracao do ICMS (E110): so com a caixa marcada ===")

TODAS = [p.id for p in propostas if p.aplicavel]


def apurar(texto_sped, marcar=True, ids=None):
    d = ler_texto(texto_sped)
    r = aplicar(d, montar_patch(propor(RESULTADO, texto_sped),
                                ids if ids is not None else TODAS,
                                atualizar_apuracao=marcar))
    e110 = [l for l in d.serializar().splitlines() if l.startswith("|E110|")]
    return d, r, (e110[0].split("|") if e110 else [])


_d, _r, campos_e110 = apurar(SPED, marcar=False)
check("sem marcar, o E110 sai byte a byte igual ao original",
      [l for l in _d.serializar().splitlines() if l.startswith("|E110|")]
      == [l for l in SPED.splitlines() if l.startswith("|E110|")])
check("e nenhuma alteracao de apuracao e registrada",
      not [a for a in _r.alteracoes if a.origem == "apura\u00e7\u00e3o"])

_d, _r, campos_e110 = apurar(SPED)
check("marcada, o credito do E110 recebe a DIFERENCA (180,00 + 66,45)",
      campos_e110[6] == "246,45", campos_e110[6:7])
check("o saldo devedor e refeito (500,00 - 246,45)",
      campos_e110[11] == "253,55", campos_e110[11:12])
check("o ICMS a recolher acompanha (campo 11 - 12)",
      campos_e110[13] == "253,55", campos_e110[13:14])
check("e nao ha saldo credor a transportar",
      campos_e110[14] == "0,00", campos_e110[14:15])
check("os debitos NAO sao tocados — a correcao e de entradas",
      campos_e110[2] == "500,00", campos_e110[2:3])
check("as alteracoes do E110 vem marcadas como apuracao",
      all(a.registro == "E110" for a in _r.alteracoes
          if a.origem == "apura\u00e7\u00e3o")
      # Sao TRES, nao quatro: o saldo credor a transportar ja estava em
      # 0,00 e continua. `Registro.set` so marca alteracao quando o texto
      # muda de verdade — reescrever campo com o mesmo valor sujaria o
      # relatorio com uma linha que nao aconteceu.
      and len([a for a in _r.alteracoes if a.origem == "apura\u00e7\u00e3o"]) == 3,
      [(a.campo, a.de, a.para) for a in _r.alteracoes
       if a.origem == "apura\u00e7\u00e3o"])

# Credito maior que o debito: a apuracao vira CREDORA, e os tres campos do saldo
# trocam de papel. Errar aqui declara imposto a recolher que nao existe.
sped_credor = montar_sped(bloco_e=["|E001|0|", "|E100|01042026|30042026|",
                                   e110(debitos="100,00", saldo="0,00",
                                        recolher="0,00", credor="80,00")])
_d, _r, campos_cr = apurar(sped_credor)
check("apuracao que fecha credora zera o saldo devedor",
      campos_cr[11] == "0,00" and campos_cr[13] == "0,00",
      (campos_cr[11], campos_cr[13]))
check("e leva a diferenca para o saldo credor a transportar",
      campos_cr[14] == "146,45", campos_cr[14:15])

# O arquivo cuja apuracao nao fecha pela formula segue OUTRA conta: o motor nao
# reescreve por cima dela.
sped_torto = montar_sped(bloco_e=["|E001|0|", "|E100|01042026|30042026|",
                                  e110(saldo="999,99", recolher="999,99")])
_d, _r, campos_torto = apurar(sped_torto)
check("E110 que nao fecha pela formula do Guia NAO e reescrito",
      campos_torto[6] == "180,00" and campos_torto[11] == "999,99",
      (campos_torto[6], campos_torto[11]))
check("e o motivo aparece como aviso, com os dois numeros",
      any("n\u00e3o fecha pela f\u00f3rmula" in a for a in _r.avisos), _r.avisos)

sem_e = montar_sped(bloco_e=[])
_d, _r, campos_sem = apurar(sem_e)
check("arquivo sem E110 nao quebra a correcao", not campos_sem and _d.serializar())
check("e avisa que a diferenca ficou so nos documentos",
      any("n\u00e3o tem registro E110" in a for a in _r.avisos), _r.avisos)

dois_e = montar_sped(bloco_e=["|E001|0|", "|E100|01042026|15042026|", e110(),
                              "|E100|16042026|30042026|", e110()])
_d, _r, _c = apurar(dois_e)
check("com DOIS E110 o motor nao escolhe: nao grava e avisa",
      any("2 registros E110" in a for a in _r.avisos), _r.avisos)
check("e os dois seguem intocados",
      [l for l in _d.serializar().splitlines() if l.startswith("|E110|")]
      == [l for l in dois_e.splitlines() if l.startswith("|E110|")])

_d, _r, _c = apurar(SPED)
rel_ap = montar(_r, _d, SPED, origem="SPED_042026.txt")
check("o relatorio separa a apuracao das linhas de documento",
      len(rel_ap.apuracao) == 3 and not any(
          a.registro == "E110" for d in rel_ap.documentos
          for a in d.aceitas + d.cascata),
      len(rel_ap.apuracao))
# O relatorio e impresso no console (script de linha de comando) e vai para o
# log. O console do Windows e cp1252, e um caractere fora dessa tabela derruba a
# impressao INTEIRA com UnicodeEncodeError — aconteceu em 10/09/2026 com o sinal
# de menos "verdadeiro" (U+2212) numa mensagem da apuracao. O "-" do teclado
# passa; o tipografico, nao.
try:
    texto_do_relatorio(rel_ap).encode("cp1252")
    _passa_no_console = True
except UnicodeEncodeError as _e:
    _passa_no_console = False
    _detalhe_console = str(_e)
check("o texto do relatorio com apuracao sobrevive ao console do Windows",
      _passa_no_console, "" if _passa_no_console else _detalhe_console)
check("e os avisos da apuracao tambem",
      all(_pode_cp1252(a) for a in _r.avisos), _r.avisos)

check("e o rodape do impacto para de dizer que o E110 nao foi alterado",
      "NÃO foi alterado" not in texto_do_relatorio(rel_ap)
      and "foi atualizado nesta corre" in texto_do_relatorio(rel_ap))
check("e o texto do relatorio para de dizer 'NAO gravado no Bloco E'",
      "GRAVADO no E110" in texto_do_relatorio(rel_ap)
      and "APURA\u00c7\u00c3O DO ICMS (E110)" in texto_do_relatorio(rel_ap))


# ============================================================================
print("\n=== 11. A apuracao pela tela: a escolha viaja ate o arquivo ===")

ids_todos = re.findall(r'name="aceitas" value="([^"]+)"', r_lista.text)
r_prev_e = as_("ope").post("/api/audit/" + TASK + "/correcao/previa",
                           data={"aceitas": ids_todos, "atualizar_e110": "true"})
check("a previa mostra o E110 que sera reescrito",
      "apura\u00e7\u00e3o do ICMS (E110) ser\u00e1 reescrita" in r_prev_e.text,
      r_prev_e.status_code)
check("com o campo 06 e o saldo, linha a linha",
      "VL_TOT_CREDITOS" in r_prev_e.text and "VL_SLD_APURADO" in r_prev_e.text)
check("e a escolha viaja escondida ate o botao de gerar",
      'name="atualizar_e110" value="true"' in r_prev_e.text)

r_prev_sem = as_("ope").post("/api/audit/" + TASK + "/correcao/previa",
                             data={"aceitas": ids_todos})
check("sem a caixa, a previa diz que a apuracao NAO foi reescrita",
      "n\u00e3o</strong> foi reescrita" in r_prev_sem.text)
check("e nao manda nada escondido para o gerar",
      'name="atualizar_e110"' not in r_prev_sem.text)

r_txt_e = as_("ope").post("/api/audit/" + TASK + "/correcao/gerar",
                          data={"aceitas": ids_todos, "atualizar_e110": "true"})
baixado = r_txt_e.content.decode("latin-1")
linha_e110 = [l for l in baixado.splitlines() if l.startswith("|E110|")][0]
check("o arquivo baixado sai com a apuracao corrigida",
      linha_e110.split("|")[6] == "246,45", linha_e110)

ss = SessionLocal()
ultimo = (ss.query(SpedCorrectionRun)
          .filter_by(task_id=TASK)
          .order_by(SpedCorrectionRun.created_at.desc()).first())
check("e a trilha registra que o imposto a recolher mudou",
      ultimo is not None and ultimo.apuracao_atualizada is True,
      ultimo and ultimo.apuracao_atualizada)
ss.close()


# ============================================================================
print("\n=== 12. Banco que JA existia: a coluna nova tem de chegar nele ===")

# Em 10/09/2026 o `apuracao_atualizada` entrou no model e o Postgres de producao
# ficou sem ele: `create_all` so cria tabela que FALTA, nunca altera tabela que
# ja existe. Resultado — o arquivo corrigido continuava saindo e a trilha morria
# no INSERT, em silencio para quem estava na tela. Este teste simula o banco
# velho: cria a tabela pelo model, ARRANCA as colunas registradas em
# `COLUNAS_NOVAS` e confere que o startup as devolve.
from sqlalchemy import create_engine, inspect as _inspect, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend.app.infrastructure.database import schema_upgrades  # noqa: E402

CAMINHO_VELHO = "./_correcao_schema_velho.db"
if os.path.exists(CAMINHO_VELHO):
    os.remove(CAMINHO_VELHO)
eng_velho = create_engine("sqlite:///" + CAMINHO_VELHO)
SpedCorrectionRun.__table__.create(bind=eng_velho)

registradas = [nome for nome, _ddl in
               schema_upgrades.COLUNAS_NOVAS.get("sped_correction_runs", [])]
check("a coluna da apuracao esta registrada no schema_upgrades",
      "apuracao_atualizada" in registradas, registradas)

with eng_velho.begin() as _conn:
    for _nome in registradas:
        _conn.execute(text(
            "ALTER TABLE sped_correction_runs DROP COLUMN " + _nome))
antes_upgrade = {c["name"] for c in
                 _inspect(eng_velho).get_columns("sped_correction_runs")}
check("o banco 'velho' realmente nao tem a coluna",
      "apuracao_atualizada" not in antes_upgrade)

schema_upgrades.aplicar(eng_velho)
depois_upgrade = {c["name"] for c in
                  _inspect(eng_velho).get_columns("sped_correction_runs")}
faltando = {c.name for c in SpedCorrectionRun.__table__.columns} - depois_upgrade
check("depois do startup, nenhuma coluna do model falta no banco antigo",
      not faltando, faltando)

# A prova real: gravar pelo ORM no banco que era velho.
Sessao = sessionmaker(bind=eng_velho)
sv = Sessao()
try:
    sv.add(SpedCorrectionRun(
        id="teste-schema", tenant_id="t", task_id="x",
        user_email="a@b.c", file_name="f.txt", correcoes_aceitas=1,
        alteracoes=1, recusas=0, apuracao_atualizada=True))
    sv.commit()
    gravou = sv.query(SpedCorrectionRun).filter_by(id="teste-schema").first()
    check("e a trilha grava, com a apuracao marcada",
          gravou is not None and gravou.apuracao_atualizada is True)
except Exception as _erro:
    check("e a trilha grava, com a apuracao marcada", False, str(_erro)[:160])
finally:
    sv.close()
    eng_velho.dispose()
    if os.path.exists(CAMINHO_VELHO):
        try:
            os.remove(CAMINHO_VELHO)
        except OSError:
            pass


# ============================================================================
try:
    os.remove(CAMINHO)
    os.rmdir(PASTA)
except OSError:
    pass
_eng.dispose()
if os.path.exists("_correcao_test.db"):
    try:
        os.remove("_correcao_test.db")
    except OSError:
        pass

print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
for f in _falhou:
    print("  -", f)
sys.exit(1 if FALHAS else 0)
