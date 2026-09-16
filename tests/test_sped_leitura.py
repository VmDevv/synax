# -*- coding: utf-8 -*-
"""Leitura do SPED: o que os dois leitores fazem com arquivo fora do padrão.

Dois leitores abrem o mesmo arquivo — `audit_engine/sped_parser.py` (a
auditoria) e `sped_rewriter/documento.py` (a reescrita) — e precisam concordar
sobre o que está escrito. O que esta suíte protege:

1. **BOM não some com o cabeçalho.** Arquivo salvo por editor de texto sai em
   UTF-8 com BOM; lido como latin-1, o |0000| deixava de começar com pipe e
   CNPJ, nome e competência viravam vazio, sem aviso. Na reescrita o BOM sai
   do arquivo e vira aviso.
2. **Ponto decimal lê igual nos dois motores.** "1234.56" era 123456.0 na
   auditoria e 1234.56 na reescrita.
3. **Linha que não é registro sai avisada** e fica fora do arquivo corrigido,
   em vez de virar um "tipo de registro" no 9900.
4. **Item sem documento (C170 antes de qualquer C100) é avisado**, e o aviso
   chega à prévia.
5. **NUM_ITEM com espaço não recusa a correção.**
6. **E110: dedução maior que o saldo vira aviso**, nunca número inventado.
7. **O rateio do VL_OPR nunca perde centavo.**
"""
import io
import os
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, os.path.abspath('.'))

from backend.app.services.audit_engine.sped_parser import (  # noqa: E402
    clean_float, parse_sped)
from backend.app.services.sped_rewriter.cascata import (  # noqa: E402
    _ratear, aplicar)
from backend.app.services.sped_rewriter.documento import ler, ler_texto  # noqa: E402
from backend.app.services.sped_rewriter.patch import (  # noqa: E402
    CorrecaoItem, Patch, texto_para_decimal)
from backend.app.services.sped_rewriter.previa import prever  # noqa: E402

OK = FALHAS = 0
_falhou = []


def check(nome, condicao, detalhe=""):
    global OK, FALHAS
    if condicao:
        OK += 1
        print(f"  OK   | {nome}")
    else:
        FALHAS += 1
        _falhou.append(nome)
        print(f" FALHA | {nome}  [{detalhe}]")


def linha(reg, campos, total):
    valores = [""] * total
    valores[0] = reg
    for numero, valor in campos.items():
        valores[numero - 1] = valor
    return "|" + "|".join(valores) + "|"


CHAVE = "35260812345678000190550010000027771000002777"
EMPRESA = "27865757000102"
NOME = "AÇÚCAR & CIA LTDA"


def montar(num_item="1", vl_icms="120,00", e110=None, extras_pos_0990=()):
    """Um SPED mínimo e coerente: uma nota, um item, um analítico, apuração."""
    e110 = e110 or {2: "500,00", 3: "0,00", 4: "0,00", 5: "0,00", 6: "120,00",
                    7: "0,00", 8: "0,00", 9: "0,00", 10: "0,00", 11: "380,00",
                    12: "0,00", 13: "380,00", 14: "0,00", 15: "0,00"}
    linhas = [
        linha("0000", {2: "020", 3: "0", 4: "01042026", 5: "30042026", 6: NOME,
                       7: EMPRESA, 9: "ES", 14: "A", 15: "1"}, 15),
        linha("0001", {2: "0"}, 2),
        linha("0150", {2: "F1", 3: "FORNECEDOR LTDA", 4: "1058",
                       5: "12345678000190"}, 13),
        linha("0990", {2: "4"}, 2),
        *extras_pos_0990,
        linha("C001", {2: "0"}, 2),
        linha("C100", {2: "0", 3: "1", 4: "F1", 5: "55", 6: "00", 7: "1",
                       8: "2777", 9: CHAVE, 10: "12042026", 11: "12042026",
                       12: "1000,00", 13: "1", 14: "0,00", 16: "1000,00",
                       17: "9", 18: "0,00", 19: "0,00", 20: "0,00",
                       21: "1000,00", 22: vl_icms, 23: "0,00", 24: "0,00",
                       25: "0,00", 26: "0,00", 27: "0,00"}, 29),
        linha("C170", {2: num_item, 3: "CAFE500", 5: "10,00000", 6: "UN",
                       7: "1000,00", 8: "0,00", 9: "0", 10: "000", 11: "1102",
                       13: "1000,00", 14: "12,00", 15: vl_icms, 16: "0,00",
                       18: "0,00", 25: "99", 31: "99"}, 38),
        linha("C190", {2: "000", 3: "1102", 4: "12,00", 5: "1000,00",
                       6: "1000,00", 7: vl_icms, 8: "0,00", 9: "0,00",
                       10: "0,00", 11: "0,00"}, 12),
        linha("C990", {2: "5"}, 2),
        linha("E001", {2: "0"}, 2),
        linha("E100", {2: "01042026", 3: "30042026"}, 4),
        linha("E110", e110, 15),
        linha("E990", {2: "4"}, 2),
        linha("9001", {2: "0"}, 2),
        linha("9900", {2: "0000", 3: "1"}, 3),
        linha("9900", {2: "9999", 3: "1"}, 3),
        linha("9990", {2: "4"}, 2),
        linha("9999", {2: "19"}, 2),
    ]
    return "\r\n".join(linhas) + "\r\n"


SPED = montar()
PASTA = tempfile.mkdtemp(prefix="synax_leitura_")


def gravar(nome, conteudo_bytes):
    caminho = os.path.join(PASTA, nome)
    with open(caminho, "wb") as f:
        f.write(conteudo_bytes)
    return caminho


# ============================================================================
print("\n=== 1. BOM e codificacao: o cabecalho nao some ===")

com_bom = gravar("bom.txt", b"\xef\xbb\xbf" + SPED.encode("utf-8"))
latin = gravar("latin.txt", SPED.encode("latin-1"))
utf8 = gravar("utf8.txt", SPED.encode("utf-8"))

for rotulo, caminho in (("UTF-8 com BOM", com_bom), ("latin-1", latin),
                        ("UTF-8 sem BOM", utf8)):
    r = parse_sped(caminho)
    check(f"auditoria le o 0000 de arquivo {rotulo}: CNPJ, nome e competencia",
          (r["cnpj"], r["name"], r["periodo_inicio"]) == (EMPRESA, NOME, "2026-04-01"),
          (r["cnpj"], r["name"], r["periodo_inicio"]))
    check(f"  e acha a nota ({rotulo})", CHAVE in r["c100"]
          and len(r["c100"][CHAVE]["items"]) == 1)

doc = ler(com_bom)
check("reescrita: com BOM o 0000 continua sendo o primeiro registro",
      doc.registros[0].reg == "0000" and len(doc.por_reg("0000")) == 1,
      doc.registros[0].reg)
check("o BOM sai do arquivo gravado e vira aviso",
      not doc.serializar().startswith("\u00ef") and doc.serializar().startswith("|0000|")
      and any("BOM" in a for a in doc.avisos), doc.avisos)
check("ler_texto reconhece o BOM tambem quando o texto veio decodificado como UTF-8",
      ler_texto("\ufeff" + SPED).registros[0].reg == "0000")
check("sem BOM nao ha aviso nenhum", ler_texto(SPED).avisos == [])
check("a hierarquia sobrevive: C170 pendurado no C100",
      len(doc.filhos_reg(doc.por_reg("C100")[0], "C170")) == 1)


# ============================================================================
print("\n=== 2. Ponto decimal: os dois motores leem o mesmo numero ===")

casos = {"1234,56": 1234.56, "1.234,56": 1234.56, "1234.56": 1234.56,
         "12": 12.0, "": 0.0, "abc": 0.0, "0,00": 0.0}
for texto, esperado in casos.items():
    check(f"clean_float({texto!r}) == {esperado}", clean_float(texto) == esperado,
          clean_float(texto))
for texto in ("1234,56", "1.234,56", "1234.56", "12", "", "0,00"):
    check(f"auditoria e reescrita concordam em {texto!r}",
          Decimal(str(clean_float(texto))) == texto_para_decimal(texto),
          (clean_float(texto), texto_para_decimal(texto)))


# ============================================================================
print("\n=== 3. Linha que nao e registro: fora do arquivo, com aviso ===")

torto = SPED.replace("|C990|", "lixo sem pipe\r\n||\r\n|C990|", 1)
doc_torto = ler_texto(torto)
check("as duas linhas tortas nao viram registro",
      len(doc_torto.registros) == len(ler_texto(SPED).registros),
      (len(doc_torto.registros), len(ler_texto(SPED).registros)))
check("cada uma sai avisada com o numero da linha",
      sum("ignorada" in a for a in doc_torto.avisos) == 2, doc_torto.avisos)
check("e o arquivo gravado nao as carrega",
      "lixo" not in doc_torto.serializar()
      and all(l != "||" for l in doc_torto.serializar().split("\r\n")))
check("linha sem o pipe final mantem o ultimo campo",
      ler_texto("|C190|000|1102|12,00|1000,00|1000,00|120,00|0,00|0,00|0,00|0,00|OBS")
      .registros[0].campo(12) == "OBS")


# ============================================================================
print("\n=== 4. Item sem documento: avisado, e o aviso chega a previa ===")

orfao = montar(extras_pos_0990=(
    linha("C170", {2: "9", 3: "PERDIDO", 7: "1,00", 10: "000", 11: "1102"}, 38),))
doc_orfao = ler_texto(orfao)
check("o C170 antes de qualquer C100 fica fora da arvore",
      all("PERDIDO" not in i.campo(3)
          for i in doc_orfao.filhos_reg(doc_orfao.por_reg("C100")[0], "C170")))
check("mas continua no arquivo (nao e o motor que decide apaga-lo)",
      any(r.campo(3) == "PERDIDO" for r in doc_orfao.por_reg("C170")))
check("e sai avisado, com registro e linha",
      any("C170" in a and "sem o registro pai" in a for a in doc_orfao.avisos),
      doc_orfao.avisos)
previa = prever(orfao, Patch())
check("a previa repassa o aviso da leitura",
      any("sem o registro pai" in a for a in previa.avisos), previa.avisos)


# ============================================================================
print("\n=== 5. NUM_ITEM com espaco nao recusa a correcao ===")

com_espaco = ler_texto(montar(num_item=" 1"))
patch = Patch(correcoes=[CorrecaoItem(documento=CHAVE, num_item="1",
                                      campos={"VL_ICMS": Decimal("100.00")})])
res = aplicar(com_espaco, patch)
check("nenhuma recusa", not res.recusas, [r.motivo for r in res.recusas])
check("o VL_ICMS do item foi gravado",
      any(a.campo == "VL_ICMS" and a.para == "100,00" for a in res.alteracoes),
      [(a.campo, a.para) for a in res.alteracoes][:4])


# ============================================================================
print("\n=== 6. E110: deducao maior que o saldo e aviso, nao chute ===")

# 500 de debito, 120 de credito, 300 de deducoes: saldo 380, a recolher 80.
# A correcao sobe o credito para 320: saldo 180, e as deducoes (300) passam
# a ser maiores que ele.
apuracao = {2: "500,00", 3: "0,00", 4: "0,00", 5: "0,00", 6: "120,00",
            7: "0,00", 8: "0,00", 9: "0,00", 10: "0,00", 11: "380,00",
            12: "300,00", 13: "80,00", 14: "0,00", 15: "0,00"}
doc_e110 = ler_texto(montar(e110=apuracao))
res = aplicar(doc_e110, Patch(
    correcoes=[CorrecaoItem(documento=CHAVE, num_item="1",
                            campos={"VL_ICMS": Decimal("320.00")})],
    atualizar_apuracao=True))
e110 = doc_e110.por_reg("E110")[0]
check("o credito entrou no campo 06", e110.campo(6) == "320,00", e110.campo(6))
check("o saldo apurado foi refeito", e110.campo(11) == "180,00", e110.campo(11))
check("ICMS a recolher nao fica negativo", e110.campo(13) == "0,00", e110.campo(13))
check("as deducoes NAO foram tocadas", e110.campo(12) == "300,00", e110.campo(12))
check("e o excesso de deducao sai como aviso",
      any("dedu" in a and "300,00" in a for a in res.avisos), res.avisos)


# ============================================================================
print("\n=== 7. O rateio nunca perde centavo ===")

for extra, bases in ((Decimal("0.05"), [Decimal(1), Decimal(1)]),
                     (Decimal("1.00"), [Decimal(1)] * 8),
                     (Decimal("-0.07"), [Decimal("3"), Decimal("7")]),
                     (Decimal("65.00"), [Decimal("1170")])):
    parcelas = _ratear(extra, bases)
    check(f"soma das parcelas de {extra} sobre {len(bases)} base(s) == {extra}",
          sum(parcelas, Decimal(0)) == extra, parcelas)


# ============================================================================
for nome in os.listdir(PASTA):
    os.remove(os.path.join(PASTA, nome))
os.rmdir(PASTA)

print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
for f in _falhou:
    print("  -", f)
sys.exit(1 if FALHAS else 0)
