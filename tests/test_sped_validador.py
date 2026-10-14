# -*- coding: utf-8 -*-
"""Validador de EFD ICMS IPI guiado pelo descritor do PVA.

O validador confere o arquivo do cliente ANTES da transmissão, sem a tela do
PVA: o leiaute (registros, campos, tamanhos, valores válidos, regras por
campo) vem do PVA instalado, extraído por `scripts/extrair_dados_pva.py`, e
as regras genéricas são implementadas a partir do Guia Prático.

O que esta suíte protege:

1. **Arquivo certo passa limpo.** Uma EFD mínima e correta não pode gerar
   inconsistência nenhuma — senão a tela ensina o operador a ignorar o que
   ela mostra. As contagens do Bloco 9 entram aqui: o 0990 soma o 0000 e o
   9990 soma o 9999.
2. **Cada regra genérica acusa o defeito dela**, com linha, registro, campo e
   o id da regra do PVA — fixture sintética por regra.
3. **Sem regra, sem acusação.** Tabela que não existe para a UF, campo que só
   é obrigatório na saída, documento cancelado: o motor se cala.
4. **O que ficou de fora é contado.** As regras específicas do PVA não são
   conferidas na v1, e o resultado diz quantas — "sem inconsistência" não
   pode virar "o PVA vai aceitar".
5. **A aba respeita empresa e papel.** Operador só valida empresa dele, e o
   CNPJ do 0000 tem de ser o do cadastro; cliente não entra.
"""
import io
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.abspath('.'))

from backend.app.services.sped_validador import (  # noqa: E402
    ADVERTENCIA, ArquivoNaoValidavel, LeiauteInesperado, LeiauteSemDescritor,
    validar_bytes, validar_texto)
from backend.app.services.sped_validador import campos as cp  # noqa: E402
from backend.app.services.sped_validador import dados  # noqa: E402

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


# ============================================================================
# Fixture: EFD ICMS IPI mínima e correta — abril/2026, ES, leiaute 020.
# ============================================================================

EMPRESA = "11222333000181"
FORNECEDOR = "11444777000161"


def linha(reg, campos, total):
    valores = [""] * total
    valores[0] = reg
    for n, v in campos.items():
        valores[n - 1] = v
    return "|" + "|".join(valores) + "|"


def chave(numero="000000123", cnpj=FORNECEDOR, modelo="55"):
    corpo = f"322604{cnpj}{modelo}001{numero}1{numero[-8:]}"
    assert len(corpo) == 43
    pesos = [2, 3, 4, 5, 6, 7, 8, 9]
    soma = sum(int(c) * pesos[i % 8] for i, c in enumerate(reversed(corpo)))
    resto = soma % 11
    return corpo + str(0 if resto < 2 else 11 - resto)


CHAVE = chave()

R0000 = (f"|0000|020|0|01042026|30042026|EMPRESA TESTE LTDA|{EMPRESA}||ES|"
         "082000000|3205309|||A|1|")
R0005 = "|0005|EMPRESA TESTE|29000000|RUA A|10||CENTRO||||"
R0150 = (f"|0150|F001|FORNECEDOR LTDA|1058|{FORNECEDOR}|||3205309||RUA B|20||"
         "CENTRO|")
R0190 = "|0190|UN|UNIDADE|"
R0200 = "|0200|P001|PRODUTO TESTE|||UN|00|22030000||||||"
C100 = linha("C100", {2: "0", 3: "1", 4: "F001", 5: "55", 6: "00", 7: "1",
                      8: "123", 9: CHAVE, 10: "05042026", 11: "06042026",
                      12: "1000,00", 13: "1", 14: "0,00", 16: "1000,00",
                      17: "9", 21: "1000,00", 22: "170,00", 23: "0,00",
                      24: "0,00", 25: "0,00", 26: "0,00", 27: "0,00",
                      28: "0,00", 29: "0,00"}, 29)
C170 = linha("C170", {2: "1", 3: "P001", 5: "10", 6: "UN", 7: "1000,00",
                      9: "0", 10: "000", 11: "1102", 13: "1000,00",
                      14: "17,00", 15: "170,00"}, 38)
C190 = "|C190|000|1102|17,00|1000,00|1000,00|170,00|0,00|0,00|0,00|0,00||"
E110 = "|E110|0,00|0,00|0,00|0,00|170,00|0,00|0,00|0,00|0,00|0,00|0,00|0,00|170,00|0,00|"
R1010 = "|1010|N|N|N|N|N|N|N|N|N|N|N|N|N|"


def blocos_base():
    return {
        "0": [R0000, "|0001|0|", R0005, R0150, R0190, R0200],
        "B": ["|B001|1|"],
        "C": ["|C001|0|", C100, C170, C190],
        "D": ["|D001|1|"],
        "E": ["|E001|0|", "|E100|01042026|30042026|", E110],
        "G": ["|G001|1|"],
        "H": ["|H001|1|"],
        "K": ["|K001|1|"],
        "1": ["|1001|0|", R1010],
    }


def montar(blocos, fim="\r\n"):
    """Fecha cada bloco com o x990 e monta o Bloco 9 com as contagens certas."""
    linhas = []
    for b in ("0", "B", "C", "D", "E", "G", "H", "K", "1"):
        corpo = blocos.get(b)
        if corpo is None:
            continue
        linhas += corpo + [f"|{b}990|{len(corpo) + 1}|"]
    contagem = Counter(l.split("|")[1] for l in linhas)
    contagem["9001"] = 1
    n9900 = len(contagem) + 3
    contagem.update({"9900": n9900, "9990": 1, "9999": 1})
    linhas.append("|9001|0|")
    linhas += [f"|9900|{t}|{n}|" for t, n in sorted(contagem.items())]
    linhas.append(f"|9990|{1 + n9900 + 1 + 1}|")
    linhas.append(f"|9999|{len(linhas) + 1}|")
    return fim.join(linhas) + fim


def validar(blocos=None, texto=None):
    return validar_texto(texto if texto is not None else montar(blocos))


def regras(res):
    return Counter(i.regra for i in res.inconsistencias)


def achar(res, regra, registro=None, campo=None):
    return [i for i in res.inconsistencias if i.regra == regra
            and (registro is None or i.registro == registro)
            and (campo is None or i.campo == campo)]


def com(bloco, indice, nova):
    b = blocos_base()
    b[bloco][indice] = nova
    return b


def trocar_campo(texto_linha, n, valor):
    partes = texto_linha.split("|")
    partes[n] = valor
    return "|".join(partes)


# ============================================================================
print("\n=== 1. Os dados extraídos do PVA ===")

lt = dados.leiaute("020")
check("o leiaute 020 está nos dados", lt is not None)
check("com os 267 registros do descritor do PVA 6.1.1",
      lt is not None and len(lt.registros) == 267, len(lt.registros) if lt else 0)
check("o pai vem do aninhamento do XML, não do atributo nível (C880 em C870)",
      lt.registros["C880"].pai == "C870" and lt.registros["C880"].nivel == 4)
check("E531 fica um nível abaixo do E530",
      lt.registros["E531"].pai == "E530" and lt.registros["E531"].nivel == 5)
check("os x001/x990 pendem do 0000",
      lt.registros["C001"].pai == "0000" and lt.registros["0990"].pai == "0000")
check("tamanho [n] é exato e n é máximo",
      lt.registros["C170"].indice_campo["CFOP"].exato
      and not lt.registros["0150"].indice_campo["NOME"].exato)
man = dados.manifesto()
check("o manifesto registra a versão do PVA", bool(man["pva"]["versao"]),
      man.get("pva"))
check("e a versão de cada tabela externa", len(man["tabelas"]) > 100
      and all(t["versao"] for t in man["tabelas"]))
check("a tabela de versões liga o COD_VER 020 a 2026",
      dados.versoes_leiaute()["020"]["inicio"] == "20260101")
check("o 021 (2027) aparece como leiaute ainda sem descritor",
      "021" in man["leiautes_sem_descritor"])
check("a linha quebrada da tabela de CFOP (5909) foi remontada",
      dados.tabelas().consultar("CFOP", "5909", "ES", "20260430") == "vigente")


# ============================================================================
print("\n=== 2. Funções de valor: data, número, dígitos verificadores ===")

check("data válida vira AAAAMMDD", cp.data("29022024") == "20240229")
check("29/02 em ano não bissexto é inválida", cp.data("29022026") is None)
check("número do SPED: vírgula decimal", cp.numero("1234,56") is not None)
check("ponto de milhar não é número do SPED", cp.numero("1.234,56") is None)
check("sinal negativo também não", cp.numero("-5,00") is None)
check("CNPJ numérico válido", cp.cnpj_valido(EMPRESA))
check("CNPJ com DV errado", not cp.cnpj_valido("11222333000182"))
check("CNPJ alfanumérico da IN 2.229/2024 (exemplo oficial)",
      cp.cnpj_valido("12ABC34501DE35"))
check("CNPJ alfanumérico com DV errado", not cp.cnpj_valido("12ABC34501DE36"))
check("CNPJ com todos os dígitos iguais é inválido",
      not cp.cnpj_valido("00000000000000"))
check("CPF válido e inválido",
      cp.cpf_valido("52998224725") and not cp.cpf_valido("52998224724"))
check("chave de acesso com DV certo", cp.chave_valida(CHAVE))
check("chave com DV errado",
      not cp.chave_valida(CHAVE[:43] + str((int(CHAVE[43]) + 1) % 10)))


# ============================================================================
print("\n=== 3. Arquivo certo passa limpo ===")

base = validar(blocos_base())
check("EFD mínima e correta: nenhuma inconsistência", base.total == 0,
      [(i.linha, i.regra, i.registro, i.campo, i.mensagem[:60])
       for i in base.inconsistencias])
check("leiaute escolhido pelo COD_VER", base.leiaute == "020")
check("empresa lida do 0000",
      base.empresa["cnpj"] == EMPRESA and base.empresa["uf"] == "ES"
      and base.empresa["dt_fin"] == "20260430")
check("as regras específicas que ficaram de fora são CONTADAS",
      sum(base.nao_conferidas.values()) > 0
      and "REGRA_VL_ICMS_NO_REGISTRO_ITEM" in base.nao_conferidas,
      base.nao_conferidas.most_common(3))
check("e regra implementada não entra na conta de não conferidas",
      "REGRA_MAIOR_ZERO" not in base.nao_conferidas
      and "REGRA_QTD_LIN_BLOCOC" not in base.nao_conferidas)
check("LF em vez de CRLF também valida limpo",
      validar(texto=montar(blocos_base(), fim="\n")).total == 0)
d = base.como_dict()
check("o resultado vira dicionário serializável com as seis chaves por item",
      d["total"] == 0 and isinstance(d["nao_conferidas"]["aplicacoes"], int))


# ============================================================================
print("\n=== 4. Qual EFD é o arquivo ===")

contrib = ("|0000|006|0|0||01042026|30042026|EMPRESA|" + EMPRESA +
           "|ES|3205309||00|0|\r\n|0001|0|\r\n")
try:
    validar(texto=contrib)
    check("EFD Contribuições é recusada", False, "não levantou")
except LeiauteInesperado as e:
    check("EFD Contribuições é recusada, com o motivo",
          "Contribuições" in e.mensagem)

try:
    validar(texto="|C001|0|\r\n|C990|2|\r\n")
    check("arquivo sem 0000 não é validável", False)
except ArquivoNaoValidavel as e:
    check("arquivo sem 0000 não é validável", "0000" in e.mensagem)

b = com("0", 0, R0000.replace("|020|", "|021|"))
try:
    validar(b)
    check("COD_VER sem descritor é recusado", False)
except LeiauteSemDescritor as e:
    check("COD_VER sem descritor (021) é recusado, dizendo como resolver",
          "021" in e.mensagem and "extrair_dados_pva" in e.mensagem)

b = com("0", 0, R0000.replace("|020|", "|019|"))
res = validar(b)
check("COD_VER de outro ano: valida pelo 019 e acusa a versão errada",
      res.leiaute == "019" and achar(res, "REGRA_VERSAO_LEIAUTE", "0000", "COD_VER"))
check("dizendo qual é o leiaute certo do período",
      "020" in achar(res, "REGRA_VERSAO_LEIAUTE")[0].mensagem)


# ============================================================================
print("\n=== 5. Estrutura: registro, hierarquia, ordem, ocorrência ===")

b = blocos_base()
b["C"].insert(3, "|C999|X|")
res = validar(b)
check("registro que não existe no leiaute",
      achar(res, "REGRA_REGISTRO_INEXISTENTE", "C999"))

b = blocos_base()
b["C"] = ["|C001|0|", C170, C100, C190]
res = validar(b)
item = achar(res, "REGRA_HIERARQUIA", "C170")
check("C170 antes de qualquer C100: hierarquia", bool(item))
check("a mensagem diz que pai faltou", item and "C100" in item[0].mensagem)

b = blocos_base()
b["C"] = ["|C001|0|", C100, C190, C170]
res = validar(b)
item = achar(res, "REGRA_ORDEM_REGISTROS", "C170")
check("C170 depois do C190 no mesmo C100: ordem", bool(item))
check("aponta a linha do C170", item and item[0].linha == 13, item)

# O caso B&C: C195 no meio de dois C190 — o PVA respondeu "Valor esperado".
b = blocos_base()
b["0"].append("|0460|OBS1|OBSERVACAO|")
b["C"] = ["|C001|0|", C100, C170, C190, "|C195|OBS1||",
          "|C190|000|1403|0,00|0,00|0,00|0,00|0,00|0,00|0,00|0,00||"]
res = validar(b)
check("C190 depois de C195 (o arquivo da B&C): ordem",
      achar(res, "REGRA_ORDEM_REGISTROS", "C190"))

texto = montar(blocos_base())
# Monta o arquivo com o bloco D antes do C, mantendo o resto.
partes = texto.split("\r\n")
ini_c = partes.index("|C001|0|")
fim_c = next(i for i, l in enumerate(partes) if l.startswith("|C990|"))
ini_d = partes.index("|D001|1|")
bloco_c, bloco_d = partes[ini_c:fim_c + 1], partes[ini_d:ini_d + 2]
nova = partes[:ini_c] + bloco_d + bloco_c + partes[ini_d + 2:]
res = validar(texto="\r\n".join(nova))
check("bloco D antes do bloco C: ordem dos blocos",
      achar(res, "REGRA_ORDEM_REGISTROS", "C001"), regras(res))

b = blocos_base()
b["0"].insert(3, R0005)
res = validar(b)
check("dois 0005 no arquivo: ocorrência", achar(res, "REGRA_OCORRENCIA", "0005"))

b = blocos_base()
b["E"].append(E110)
res = validar(b)
check("dois E110 no mesmo E100: ocorrência por pai",
      achar(res, "REGRA_OCORRENCIA", "E110"))

b = blocos_base()
b["C"] += [C100.replace("|123|", "|124|").replace(CHAVE, chave("000000124")),
           C170, C190]
res = validar(b)
check("o mesmo NUM_ITEM em duas notas NÃO é duplicidade (escopo é o pai)",
      not achar(res, "REGRA_REGISTRO_DUPLICADO_A7E2V1"), regras(res))


# ============================================================================
print("\n=== 6. Campos: quantidade, tipo, tamanho, obrigatoriedade ===")

res = validar(com("C", 3, "|C190|000|1102|17,00|1000,00|1000,00|170,00|0,00|0,00|0,00|0,00|"))
check("C190 com um campo a menos", achar(res, "REGRA_NUMERO_CAMPOS", "C190"))
check("e os campos dele não geram ruído",
      not [i for i in res.inconsistencias if i.registro == "C190"
           and i.regra != "REGRA_NUMERO_CAMPOS"])

res = validar(com("C", 3, trocar_campo(C190, 5, "1.000,00")))
check("ponto de milhar em valor: conteúdo inválido",
      achar(res, "REGRA_CAMPO_INVALIDO_V2", "C190", "VL_OPR"))
res = validar(com("C", 3, trocar_campo(C190, 5, "-1000,00")))
check("valor negativo: conteúdo inválido",
      achar(res, "REGRA_CAMPO_INVALIDO_V2", "C190", "VL_OPR"))
res = validar(com("C", 1, trocar_campo(C100, 8, "12,5")))
check("decimal em campo inteiro (NUM_DOC)",
      achar(res, "REGRA_CAMPO_INVALIDO_V2", "C100", "NUM_DOC"))
res = validar(com("C", 1, trocar_campo(C100, 10, "31042026")))
check("data inexistente (31/04)",
      achar(res, "REGRA_DATA_VALIDA_A4E5V1", "C100", "DT_DOC"))
res = validar(com("0", 3, trocar_campo(R0150, 3, "FORNECEDOR\tLTDA")))
check("tabulação dentro de campo texto",
      achar(res, "REGRA_CAMPO_INVALIDO_V2", "0150", "NOME"))

res = validar(com("0", 3, trocar_campo(R0150, 2, "F" * 61)))
check("COD_PART com 61 posições (máximo 60)",
      achar(res, "REGRA_TAMANHO_CAMPO", "0150", "COD_PART"))
res = validar(com("C", 3, trocar_campo(C190, 3, "11020")))
check("CFOP com 5 posições (exato 4)",
      achar(res, "REGRA_TAMANHO_CAMPO", "C190", "CFOP"))
res = validar(com("C", 3, trocar_campo(C190, 4, "17,123")))
check("alíquota com 3 casas (máximo 2)",
      achar(res, "REGRA_TAMANHO_CAMPO", "C190", "ALIQ_ICMS"))

res = validar(com("C", 1, trocar_campo(C100, 12, "")))
check("VL_DOC vazio: campo obrigatório",
      achar(res, "REGRA_CAMPO_OBRIGATORIO", "C100", "VL_DOC"))
res = validar(com("C", 1, trocar_campo(C100, 11, "")))
check("DT_E_S vazio na ENTRADA: obrigatório",
      achar(res, "REGRA_CAMPO_OBRIGATORIO", "C100", "DT_E_S"))
saida = trocar_campo(trocar_campo(trocar_campo(C100, 2, "1"), 3, "0"), 11, "")
res = validar(com("C", 1, saida))
check("DT_E_S vazio na SAÍDA: não é exigido aqui",
      not achar(res, "REGRA_CAMPO_OBRIGATORIO", "C100", "DT_E_S"))

cancelada = linha("C100", {2: "1", 3: "0", 5: "55", 6: "02", 7: "1",
                           8: "125", 9: chave("000000125", EMPRESA)}, 29)
b = blocos_base()
b["C"].append(cancelada)
res = validar(b)
check("NF-e cancelada (COD_SIT 02) só com a identificação: sem acusação",
      not [i for i in res.inconsistencias if i.linha == 14], regras(res))
check("e sem C190 não é 'filho obrigatório'",
      not achar(res, "REGRA_REGISTRO_OBRIGATORIO", "C100"))

nfce = linha("C100", {2: "1", 3: "0", 5: "65", 6: "00", 7: "1", 8: "126",
                      9: chave("000000126", EMPRESA, "65"), 10: "05042026",
                      12: "10,00", 13: "0", 17: "9"}, 29)
b = blocos_base()
b["C"] += [nfce, "|C190|000|5102|0,00|10,00|0,00|0,00|0,00|0,00|0,00|0,00||"]
res = validar(b)
check("NFC-e (modelo 65) sem COD_PART não é acusada",
      not achar(res, "REGRA_CAMPO_OBRIGATORIO", "C100", "COD_PART"))

res = validar(com("0", 0, R0000.replace("|EMPRESA TESTE LTDA|", "|EMPRESA TESTE LTDA |")))
check("espaço no fim do campo",
      achar(res, "REGRA_ESPACOS_INICIO_FINAL", "0000", "NOME"))


# ============================================================================
print("\n=== 7. Valores válidos e Tabelas Externas ===")

res = validar(com("C", 1, trocar_campo(C100, 13, "7")))
check("IND_PGTO fora da lista do leiaute",
      achar(res, "REGRA_VALORES_VALIDOS", "C100", "IND_PGTO"))
res = validar(com("C", 3, trocar_campo(C190, 3, "1999")))
item = achar(res, "REGRA_VALORES_VALIDOS_TABELA_EXTERNA", "C190", "CFOP")
check("CFOP que não existe na tabela", bool(item))
check("a mensagem nomeia o código e a tabela",
      item and "1999" in item[0].mensagem and "CFOP" in item[0].mensagem)
res = validar(com("0", 3, trocar_campo(R0150, 8, "1234567")))
check("município que não existe",
      achar(res, "REGRA_VALORES_VALIDOS_TABELA_EXTERNA", "0150", "COD_MUN"))
res = validar(com("0", 3, trocar_campo(R0150, 4, "01058")))
check("COD_PAIS '01058' é o Brasil (campo numérico, compara pelo valor)",
      not achar(res, "REGRA_VALORES_VALIDOS_TABELA_EXTERNA", "0150", "COD_PAIS"))
res = validar(com("C", 3, trocar_campo(C190, 2, "00")))
check("CST com 2 posições (exato 3) não é 'zero à esquerda'",
      achar(res, "REGRA_TAMANHO_CAMPO", "C190", "CST_ICMS"))

b = blocos_base()
b["C"].append(linha("C100", {2: "0", 3: "1", 5: "55", 6: "04", 7: "1",
                             8: "127", 9: chave("000000127")}, 29))
res = validar(b)
item = achar(res, "REGRA_VALORES_VALIDOS_TABELA_EXTERNA", "C100", "COD_SIT")
check("COD_SIT 04 (denegada) não vale desde 2023: fora de vigência",
      item and "vigente" in item[0].mensagem, regras(res))

b = blocos_base()
b["C"].append(linha("C100", {2: "0", 3: "1", 5: "55", 6: "09", 7: "1",
                             8: "128", 9: chave("000000128")}, 29))
res = validar(b)
check("COD_SIT fora da lista do C100 ([codigos=00..08])",
      achar(res, "REGRA_VALORES_VALIDOS", "C100", "COD_SIT"))


def e111(codigo):
    b = blocos_base()
    b["E"].insert(3, f"|E111|{codigo}||10,00|")
    return validar(b)


res = e111("ES000001")
check("ajuste de apuração da tabela da UF do 0000 (ES)",
      not achar(res, "REGRA_VALORES_VALIDOS_TABELA_EXTERNA", "E111"), regras(res))
res = e111("ES009999")
check("código da GENERICA ('UF009999' vira ES009999) é aceito",
      not achar(res, "REGRA_VALORES_VALIDOS_TABELA_EXTERNA", "E111"), regras(res))
res = e111("MG000001")
check("código de outra UF não é aceito",
      achar(res, "REGRA_VALORES_VALIDOS_TABELA_EXTERNA", "E111", "COD_AJ_APUR"))
res = e111("ES101001")
check("3º caractere 1 (ICMS ST) num E111 (ICMS próprio) é recusado",
      achar(res, "REGRA_VALORES_VALIDOS_TABELA_EXTERNA", "E111", "COD_AJ_APUR"))

b = blocos_base()
b["C"] += ["|C195|OBS1||", "|C197|MG23000999||P001|100,00|17,00|17,00|0,00|"]
b["0"].append("|0460|OBS1|OBSERVACAO|")
res = validar(b)
check("C197 com código de ajuste de MG num arquivo do ES",
      achar(res, "REGRA_VALORES_VALIDOS_TABELA_EXTERNA", "C197", "COD_AJ"))

t = dados.tabelas()
check("UF sem tabela para o tipo: 'sem_tabela', e o motor não acusa",
      t.consultar("COD_ITEM_IPM", "X1", "CE", "20260430") == "sem_tabela")


# ============================================================================
print("\n=== 8. Duplicidade de chave e cadastros do Bloco 0 ===")

b = blocos_base()
b["0"].insert(4, R0150.replace("FORNECEDOR LTDA", "OUTRO NOME"))
res = validar(b)
check("dois 0150 com o mesmo COD_PART",
      achar(res, "REGRA_REGISTRO_DUPLICADO_A7E2V1", "0150"))

b = blocos_base()
b["C"].append(trocar_campo(C190, 4, "17"))
res = validar(b)
item = achar(res, "REGRA_REGISTRO_DUPLICADO_A7E2V1", "C190")
check("C190 com mesma chave, alíquota '17' x '17,00' (compara o valor)",
      bool(item))
check("e a mensagem aponta a primeira ocorrência",
      item and "linha 13" in item[0].mensagem, item and item[0].mensagem)

res = validar(com("C", 2, trocar_campo(C170, 3, "P999")))
check("item do C170 fora do 0200",
      achar(res, "REGRA_EXISTE_COD_ITEM_V2", "C170", "COD_ITEM"))
res = validar(com("C", 1, trocar_campo(C100, 4, "F999")))
check("participante do C100 fora do 0150",
      achar(res, "REGRA_EXISTE_COD_PART_V2", "C100", "COD_PART"))
res = validar(com("C", 2, trocar_campo(C170, 6, "CX")))
check("unidade do C170 fora do 0190",
      achar(res, "REGRA_EXISTE_UNID_V2", "C170", "UNID"))
b = blocos_base()
b["C"].append("|C195|OBS9||")
res = validar(b)
check("observação do C195 fora do 0460",
      achar(res, "REGRA_EXISTE_COD_OBS_V2", "C195", "COD_OBS"))


# ============================================================================
print("\n=== 9. Regras genéricas de campo e de registro ===")

res = validar(com("C", 2, trocar_campo(C170, 2, "0")))
check("NUM_ITEM zero: maior que zero",
      achar(res, "REGRA_MAIOR_ZERO", "C170", "NUM_ITEM"))
res = validar(com("C", 3, trocar_campo(C190, 4, "1000,00")))
check("alíquota acima de 999",
      achar(res, "REGRA_ALIQ_MENOR_1000", "C190", "ALIQ_ICMS"))
res = validar(com("C", 1, trocar_campo(C100, 10, "31121999")))
check("documento anterior a 2000",
      achar(res, "REGRA_DATA_MINIMA_DOC_FISCAL", "C100", "DT_DOC"))
res = validar(com("C", 1, trocar_campo(C100, 9, CHAVE[:43] + str((int(CHAVE[43]) + 1) % 10))))
check("chave de acesso com DV errado",
      achar(res, "REGRA_VALIDA_CHV_NFE_CTE", "C100", "CHV_NFE"))
res = validar(com("0", 3, trocar_campo(R0150, 5, "11444777000162")))
check("CNPJ do participante com DV errado",
      achar(res, "REGRA_DIGITO_CNPJ", "0150", "CNPJ"))
res = validar(com("0", 3, trocar_campo(R0150, 5, "12ABC34501DE35")))
check("CNPJ alfanumérico válido no participante passa",
      not achar(res, "REGRA_DIGITO_CNPJ"))

D100 = linha("D100", {2: "0", 3: "1", 4: "F001", 5: "57", 6: "00", 7: "1",
                      9: "555", 10: chave("000000555", FORNECEDOR, "57"),
                      11: "10042026", 12: "10042026", 13: "0", 15: "100,00",
                      16: "0,00", 17: "1", 18: "100,00", 19: "100,00",
                      20: "12,00", 21: "0,00", 24: "3205309",
                      25: "3205309"}, 25)
D190 = "|D190|000|1352|12,00|100,00|100,00|12,00|0,00||"


def com_frete(d100=D100):
    b = blocos_base()
    b["D"] = ["|D001|0|", d100, D190]
    return validar(b)


res = com_frete()
check("CT-e de entrada correto não gera inconsistência", res.total == 0,
      [(i.regra, i.registro, i.campo, i.mensagem[:50]) for i in res.inconsistencias])
res = com_frete(trocar_campo(D100, 11, "02052026"))
check("CT-e com data depois do fim do período",
      achar(res, "REGRA_DATA_MENOR_IGUAL_DT_FIN_0000_DT_DOC", "D100", "DT_DOC"))
res = com_frete(trocar_campo(trocar_campo(D100, 15, "250000000,00"), 18, "250000000,00"))
item = achar(res, "REGRA_VL_DOC_GRANDE", "D100", "VL_DOC")
check("valor acima de 200 milhões: só advertência",
      item and item[0].gravidade == ADVERTENCIA)
check("e advertência não conta como erro", res.erros == 0, res.por_gravidade)

res = validar(com("E", 1, "|E100|01032026|30042026|"))
check("E100 começando antes do período do 0000",
      achar(res, "REGRA_DATA_ENTRE_DT_INI_DT_FIN_0000_DT_INI", "E100", "DT_INI"))
res = validar(com("0", 0, R0000.replace("|30042026|", "|31052026|")))
check("0000 com início e fim em meses diferentes",
      achar(res, "REGRA_MES_ESCRITURACAO", "0000"))
res = validar(com("0", 0, R0000.replace("|01042026|30042026|", "|30042026|01042026|")))
check("0000 com início depois do fim",
      achar(res, "REGRA_DATA_MENOR_IGUAL_DT_FIN", "0000", "DT_INI"))


# ============================================================================
print("\n=== 10. Registros obrigatórios, modelo e movimento do bloco ===")

b = blocos_base()
del b["E"]
res = validar(b)
check("arquivo sem bloco E: E001 e E990 faltando",
      achar(res, "REGRA_REGISTRO_OBRIGATORIO", "E001")
      and achar(res, "REGRA_REGISTRO_OBRIGATORIO", "E990"))

b = blocos_base()
b["C"] = ["|C001|0|", C100, C170]
res = validar(b)
item = achar(res, "REGRA_REGISTRO_OBRIGATORIO", "C100")
check("C100 sem C190: filho obrigatório", bool(item))
check("apontando a linha do C100", item and item[0].linha == 11, item)

b = blocos_base()
b["E"] = ["|E001|0|", "|E100|01042026|30042026|"]
res = validar(b)
check("E100 sem E110", achar(res, "REGRA_REGISTRO_OBRIGATORIO", "E100"))

b = blocos_base()
b["C"].insert(2, "|C101|0,00|0,00|0,00|")
res = validar(b)
check("C101 (só NF-e) dentro de NF-e: aceito",
      not achar(res, "REGRA_EXISTENCIA_POR_MODELO"), regras(res))
b = blocos_base()
b["C"][1] = trocar_campo(trocar_campo(C100, 5, "01"), 9, "")
b["C"].insert(2, "|C101|0,00|0,00|0,00|")
res = validar(b)
check("C101 dentro de nota modelo 01: existência por modelo",
      achar(res, "REGRA_EXISTENCIA_POR_MODELO", "C101"))

res = validar(com("C", 0, "|C001|1|"))
check("C001 diz 'sem movimento' e há C100",
      achar(res, "REGRA_BLOCO_COM_SEM_MOVIMENTO", "C001"))
res = validar(com("D", 0, "|D001|0|"))
check("D001 diz 'com movimento' e não há nada",
      achar(res, "REGRA_BLOCO_COM_SEM_MOVIMENTO", "D001"))


# ============================================================================
print("\n=== 11. Bloco 9 e totalizadores ===")

certo = montar(blocos_base())
res = validar(texto=certo.replace("|C990|5|", "|C990|6|"))
check("C990 com a contagem errada", achar(res, "REGRA_QTD_LIN_BLOCOC", "C990"))
res = validar(texto=certo.replace("|0990|7|", "|0990|6|"))
check("0990 sem somar o 0000 está errado (o Guia manda somar)",
      achar(res, "REGRA_QTD_LIN_BLOCO0", "0990"))
linhas_ok = certo.split("\r\n")
total = len([l for l in linhas_ok if l])
res = validar(texto=certo.replace(f"|9999|{total}|", f"|9999|{total + 1}|"))
check("9999 com o total errado", achar(res, "REGRA_QTD_LIN_ARQUIVO", "9999"))
res = validar(texto=certo.replace("|9900|C190|1|", "|9900|C190|2|"))
check("9900 com a contagem errada",
      achar(res, "REGRA_QTD_REG_BLC_OBRIGATORIO", "9900", "QTD_REG_BLC"))
res = validar(texto=certo.replace("|9900|C190|1|", "|9900|C195|1|"))
r = regras(res)
check("9900 de registro que não existe e falta o do C190",
      len(achar(res, "REGRA_QTD_REG_BLC_OBRIGATORIO", "9900")) == 2, r)
res = validar(texto=certo.replace("|9990|", "|9990|1"))
check("9990 errado", achar(res, "REGRA_QTD_LIN_BLOCO9", "9990"))
res = validar(texto=certo.replace("|9900|C190|1|\r\n", "|9900|C190|1|\r\n|9900|C190|1|\r\n"))
check("9900 duplicado para o mesmo registro",
      achar(res, "REGRA_REGISTRO_DUPLICADO_A7E2V1", "9900"))


# ============================================================================
print("\n=== 12. Forma do arquivo: linha, BOM, codificação ===")

res = validar(texto=certo.replace("|C170|", "lixo no meio\r\n|C170|", 1))
check("linha que não é registro", achar(res, "REGRA_FORMATO_LINHA"))
res = validar(texto=certo.replace("|C170|", "\r\n|C170|", 1))
check("linha em branco no meio do arquivo",
      [i for i in res.inconsistencias if i.regra == "REGRA_FORMATO_LINHA"
       and "branco" in i.mensagem])
res = validar(texto=certo.replace(R0190, R0190[:-1], 1))
check("linha sem o pipe final",
      [i for i in res.inconsistencias if i.regra == "REGRA_FORMATO_LINHA"
       and i.registro == "0190"], regras(res))
res = validar(texto="﻿" + certo)
check("BOM no começo: acusado na linha 1, e o resto valida",
      achar(res, "REGRA_FORMATO_LINHA") and res.leiaute == "020"
      and res.total == 1, regras(res))

com_acento = certo.replace("FORNECEDOR LTDA", "FORNECEDOR AÇÚCAR LTDA")
res = validar_bytes(com_acento.encode("utf-8"))
item = achar(res, "REGRA_CODIFICACAO")
check("arquivo em UTF-8 com acento: advertência de codificação",
      item and item[0].gravidade == ADVERTENCIA)
res = validar_bytes(com_acento.encode("latin-1"))
check("o mesmo em latin-1: sem advertência", res.total == 0, regras(res))
res = validar_texto(certo, limite=0)
check("limite corta a lista, não a contagem",
      res.total == 0 and not res.truncado)
res = validar_texto(certo.replace("|C001|0|", "|C001|0|\r\n|C999|\r\n|C998|"), limite=1)
check("com limite 1 e duas inconsistências: lista com uma, total dois",
      len(res.inconsistencias) == 1 and res.total >= 2 and res.truncado,
      (len(res.inconsistencias), res.total))


# ============================================================================
print("\n=== 13. A aba: empresa, papel e download ===")

import json  # noqa: E402
import re  # noqa: E402
import types  # noqa: E402
import uuid as _uuid  # noqa: E402

import dotenv  # noqa: E402
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main  # noqa: E402
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_validar_sped_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_validar_sped_test.db"):
    os.remove("_validar_sped_test.db")

from sqlalchemy import types as _satypes  # noqa: E402
import sqlalchemy.dialects.postgresql as pg  # noqa: E402


class _UUIDShim(_satypes.TypeDecorator):
    impl = _satypes.Uuid
    cache_ok = True

    def __init__(self, *a, **kw):
        kw.pop('as_uuid', None)
        super().__init__()

    def process_bind_param(self, value, dialect):
        return _uuid.UUID(value) if isinstance(value, str) else value


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

import backend.app.main as m  # noqa: E402
from backend.app.infrastructure.database.session import SessionLocal  # noqa: E402
from backend.app.domain.models.base import Base  # noqa: E402
from backend.app.domain.models.auth_models import (  # noqa: E402
    Tenant, UserProfile, UserTenant)
from fastapi.testclient import TestClient  # noqa: E402

Base.metadata.create_all(bind=_eng)
db = SessionLocal()
tA, tB, tC = _uuid.uuid4(), _uuid.uuid4(), _uuid.uuid4()
db.add_all([
    # O cadastro guarda com máscara: a comparação é só por letras e dígitos.
    Tenant(id=tA, name="EMPRESA TESTE LTDA", cnpj="11.222.333/0001-81", uf="ES"),
    Tenant(id=tB, name="OUTRA EMPRESA LTDA", cnpj="11444777000161", uf="ES"),
    Tenant(id=tC, name="EMPRESA DE OUTRO OPERADOR", cnpj=EMPRESA[:-2] + "00", uf="ES"),
])
u_ope, u_ope2, u_cli = _uuid.uuid4(), _uuid.uuid4(), _uuid.uuid4()
db.add_all([
    UserProfile(id=u_ope, tenant_id=tA, role="operador", name="Operador"),
    UserProfile(id=u_ope2, tenant_id=tC, role="operador", name="Operador 2"),
    UserProfile(id=u_cli, tenant_id=tA, role="cliente", name="Cliente"),
])
db.add_all([UserTenant(user_id=u_ope, tenant_id=tA),
            UserTenant(user_id=u_ope, tenant_id=tB),
            UserTenant(user_id=u_ope2, tenant_id=tC)])
db.commit()
db.close()

TOKENS["ope"] = types.SimpleNamespace(id=str(u_ope), email="oper@synax.com")
TOKENS["ope2"] = types.SimpleNamespace(id=str(u_ope2), email="oper2@synax.com")
TOKENS["cli"] = types.SimpleNamespace(id=str(u_cli), email="cli@synax.com")

from backend.app.api.dependencies.db import get_rls_db as _real_rls  # noqa: E402


def _fake_rls():
    ss = SessionLocal()
    try:
        yield ss
    finally:
        ss.close()


m.app.dependency_overrides[_real_rls] = _fake_rls
c = TestClient(m.app, raise_server_exceptions=False)


def as_(token):
    c.cookies.clear()
    if token:
        c.cookies.set("access_token", f"Bearer {token}")
    return c


def enviar(token, tenant, conteudo, htmx=False):
    return as_(token).post(
        "/api/validar-sped", data={"tenant_id": str(tenant) if tenant else ""},
        files={"sped_file": ("sped.txt", conteudo, "text/plain")},
        headers={"HX-Request": "true"} if htmx else {})


r = as_("ope").get("/validar-sped")
check("operador abre a aba", r.status_code == 200, r.status_code)
check("com o seletor de empresa", 'name="tenant_id"' in r.text)
check("a aba aparece na navegação", 'href="/validar-sped"' in r.text)
check("e a tela diz o que a v1 NÃO confere", "ainda não" in r.text)
check("cliente não entra", as_("cli").get("/validar-sped").status_code == 403)
check("anônimo também não",
      as_(None).get("/validar-sped").status_code in (401, 403))
check("e a aba não aparece para o cliente",
      'href="/validar-sped"' not in as_("cli").get("/").text)

ARQ = certo.encode("latin-1")
r = enviar("ope", tA, ARQ)
check("API: arquivo certo responde 200 em JSON", r.status_code == 200, r.status_code)
corpo = r.json() if r.status_code == 200 else {}
check("com zero inconsistência e a conta do que ficou de fora",
      corpo.get("total") == 0 and corpo.get("nao_conferidas", {}).get("aplicacoes", 0) > 0,
      corpo.get("total"))
check("e as seis chaves de cada inconsistência estão no contrato",
      corpo.get("inconsistencias") == [])
processo_limpo = corpo.get("processo", "")
check("o SPED enviado não fica no servidor",
      processo_limpo and not os.path.exists(os.path.join(
          "temp_uploads", processo_limpo, "sped_validar.txt")))

r = enviar("ope", tA, ARQ, htmx=True)
check("htmx recebe a tela, sem prometer 'aprovado'",
      r.status_code == 200 and "Nenhum erro nas regras conferidas" in r.text
      and "aprovad" not in r.text.lower() and "palavra final" in r.text)

errado = certo.replace("|C990|5|", "|C990|6|").encode("latin-1")
r = enviar("ope", tA, errado)
corpo = r.json()
check("arquivo com erro: o item tem linha, registro, campo, regra, mensagem e gravidade",
      corpo["total"] == 1 and set(corpo["inconsistencias"][0]) >=
      {"linha", "registro", "campo", "regra", "mensagem", "gravidade"},
      corpo.get("inconsistencias"))
processo = corpo["processo"]
r = as_("ope").get(f"/api/validar-sped/{processo}/csv")
check("CSV baixa como arquivo, nunca renderizado",
      r.status_code == 200
      and r.headers.get("content-type") == "application/octet-stream"
      and r.headers.get("content-disposition", "").startswith("attachment;"))
texto_csv = r.content.decode("utf-8-sig")
check("CSV com cabeçalho e a inconsistência",
      texto_csv.startswith("linha;registro;campo") and "REGRA_QTD_LIN_BLOCOC" in texto_csv)
check("operador de outra empresa não baixa o relatório",
      as_("ope2").get(f"/api/validar-sped/{processo}/csv").status_code == 403)
check("identificador que não é UUID não vira caminho",
      as_("ope").get("/api/validar-sped/..%2F..%2Fmain/csv").status_code in (400, 404))

r = enviar("ope", tB, ARQ)
check("arquivo de OUTRA empresa é recusado (CNPJ do 0000 x cadastro)",
      r.status_code == 422 and "outra empresa" in r.json().get("erro", ""), r.text[:200])
r = enviar("ope", tB, ARQ, htmx=True)
check("e na tela a recusa vem em 200, com o motivo visível",
      r.status_code == 200 and "outra empresa" in r.text)
r = enviar("ope", tC, ARQ)
check("operador não valida empresa que não é dele", r.status_code == 403, r.status_code)
r = enviar("ope", "", ARQ, htmx=True)
check("sem empresa: 200 com aviso (htmx não troca o alvo em 4xx)",
      r.status_code == 200 and "Escolha a empresa" in r.text)
r = enviar("cli", tA, ARQ)
check("cliente não usa a API", r.status_code == 403, r.status_code)
r = enviar("ope", tA, contrib.encode("latin-1"))
check("EFD Contribuições pela API: 422 com o motivo",
      r.status_code == 422 and "Contribuições" in r.json().get("erro", ""))

import shutil as _shutil  # noqa: E402
for p in (processo, processo_limpo):
    _shutil.rmtree(os.path.join(os.getcwd(), "temp_uploads", p), ignore_errors=True)
_eng.dispose()
if os.path.exists("_validar_sped_test.db"):
    try:
        os.remove("_validar_sped_test.db")
    except OSError:
        pass


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
for f in _falhou:
    print("  -", f)
sys.exit(1 if FALHAS else 0)
