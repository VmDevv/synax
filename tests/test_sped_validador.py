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
      and "REGRA_VALIDA_IE_0000_V2" in base.nao_conferidas,
      base.nao_conferidas.most_common(3))
check("e regra implementada não entra na conta de não conferidas",
      "REGRA_MAIOR_ZERO" not in base.nao_conferidas
      and "REGRA_QTD_LIN_BLOCOC" not in base.nao_conferidas
      and "REGRA_VL_ICMS_NO_REGISTRO_ITEM" not in base.nao_conferidas)
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
# Achado em SPED real: E316 com DIFAL para a BA e receita GNRE 100102, que
# está na GENERICA SEM o prefixo "UF" — e era acusada como inexistente.
check("código nacional da GENERICA (receita GNRE 100102) vale para qualquer UF",
      t.consultar("REC_REF_OBRIGACAO", "100102", "BA", "20260930") == "vigente")
check("'UF' literal no arquivo não é código de estado nenhum",
      t.consultar("AJ_APUR_DED", "UF009999", "ES", "20260430") == "inexistente")


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


# Com o CT-e, o crédito do período passa a 170 + 12 = 182 (E110 campo 06).
E110_COM_FRETE = ("|E110|0,00|0,00|0,00|0,00|182,00|0,00|0,00|0,00|0,00|0,00|"
                  "0,00|0,00|182,00|0,00|")
assert E110_COM_FRETE != E110 and E110_COM_FRETE.count("|") == E110.count("|")


def com_frete(d100=D100, d190=D190):
    b = blocos_base()
    b["D"] = ["|D001|0|", d100, d190]
    b["E"][2] = E110_COM_FRETE
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
print("\n=== 12b. Regras de conta: itens, documento e apuração ===")


def e110(**campos):
    """E110 com os campos pelo nome; o que não vier é 0,00."""
    nomes = ("VL_TOT_DEBITOS", "VL_AJ_DEBITOS", "VL_TOT_AJ_DEBITOS",
             "VL_ESTORNOS_CRED", "VL_TOT_CREDITOS", "VL_AJ_CREDITOS",
             "VL_TOT_AJ_CREDITOS", "VL_ESTORNOS_DEB", "VL_SLD_CREDOR_ANT",
             "VL_SLD_APURADO", "VL_TOT_DED", "VL_ICMS_RECOLHER",
             "VL_SLD_CREDOR_TRANSPORTAR", "DEB_ESP")
    return "|E110|" + "|".join(campos.get(n, "0,00") for n in nomes) + "|"


check("o E110 montado pelo helper é o da fixture",
      e110(VL_TOT_CREDITOS="170,00", VL_SLD_CREDOR_TRANSPORTAR="170,00") == E110)

res = validar(com("C", 3, trocar_campo(C190, 7, "171,00")))
item = achar(res, "REGRA_VL_ICMS_NO_REGISTRO_ITEM", "C190", "VL_ICMS")
check("C190 com ICMS diferente da soma dos C170", bool(item))
check("a mensagem traz os dois valores",
      item and "171,00" in item[0].mensagem and "170,00" in item[0].mensagem)
check("e o C100 contra os C190 também diverge",
      achar(res, "REGRA_VL_ICMS_ANALIT", "C100", "VL_ICMS"))
res = validar(com("C", 3, trocar_campo(C190, 6, "900,00")))
check("C190 com base diferente da soma dos C170",
      achar(res, "REGRA_VL_BC_ICMS_NO_REGISTRO_ITEM", "C190", "VL_BC_ICMS"))
res = validar(com("C", 3, trocar_campo(C190, 9, "5,00")))
check("C190 com ICMS ST sem ST nos itens",
      achar(res, "REGRA_VL_ICMS_ST_NO_REGISTRO_ITEM", "C190", "VL_ICMS_ST"))
check("C100 com ICMS ST diferente da soma dos C190",
      achar(res, "REGRA_VL_ICMS_ST_ANALIT", "C100", "VL_ICMS_ST"))
res = validar(com("C", 1, trocar_campo(C100, 25, "10,00")))
check("C100 com IPI que os C190 não somam",
      achar(res, "REGRA_VL_IPI_C190", "C100", "VL_IPI"))

b = blocos_base()
b["C"].append("|C190|000|1403|0,00|0,00|0,00|0,00|0,00|0,00|0,00|0,00||")
res = validar(b)
check("C190 com combinação que nenhum C170 tem",
      achar(res, "REGRA_CHAVE_EXISTE_NO_REGISTRO_ITEM_A8E1V1", "C190"))

b = blocos_base()
b["C"] = ["|C001|0|", C100, trocar_campo(C190, 7, "999,00")]
res = validar(b)
check("nota SEM C170 (NF-e própria dispensa): o C190 não é somado contra itens",
      not achar(res, "REGRA_VL_ICMS_NO_REGISTRO_ITEM"), regras(res))

res = com_frete(d190=trocar_campo(D190, 7, "13,00"))
check("D100 com ICMS diferente da soma dos D190",
      achar(res, "REGRA_VL_ICMS_ANALIT", "D100", "VL_ICMS"))

b = blocos_base()
b["E"][2] = e110(VL_TOT_CREDITOS="170,00", VL_SLD_CREDOR_TRANSPORTAR="100,00")
res = validar(b)
check("saldo credor que não fecha com a fórmula do Guia",
      achar(res, "REGRA_SLD_CREDOR_E110", "E110", "VL_SLD_CREDOR_TRANSPORTAR"))

# Ajuste de débito de 500 (campo 03, sem regra nesta versão): 500 - 170 = 330.
DEVE = dict(VL_AJ_DEBITOS="500,00", VL_TOT_CREDITOS="170,00",
            VL_SLD_APURADO="330,00", VL_ICMS_RECOLHER="330,00")
E116 = "|E116|000|330,00|10052026|100013|||||042026|"


def apuracao(e110_linha, *filhos):
    b = blocos_base()
    b["E"] = ["|E001|0|", "|E100|01042026|30042026|", e110_linha, *filhos]
    return validar(b)


res = apuracao(e110(**DEVE), E116)
check("apuração com saldo devedor e guia que fecha: sem acusação de conta",
      not [i for i in res.inconsistencias if i.registro == "E110"], regras(res))
res = apuracao(e110(**DEVE))
check("ICMS a recolher sem nenhum E116",
      achar(res, "REGRA_ICMS_REC_E116", "E110"))
res = apuracao(e110(**{**DEVE, "VL_SLD_APURADO": "300,00", "VL_ICMS_RECOLHER": "300,00"}),
               E116.replace("330,00", "300,00"))
check("saldo devedor que não fecha com a fórmula",
      achar(res, "REGRA_SLD_DEVEDOR_E110", "E110", "VL_SLD_APURADO"))
res = apuracao(e110(**{**DEVE, "VL_ICMS_RECOLHER": "320,00"}), E116)
check("ICMS a recolher diferente de saldo - deduções",
      achar(res, "REGRA_ICMS_RECOLHER", "E110", "VL_ICMS_RECOLHER"))
res = apuracao(e110(**{**DEVE, "VL_TOT_DED": "400,00", "VL_ICMS_RECOLHER": "0,00"}))
check("deduções maiores que o saldo devedor",
      achar(res, "REGRA_VALIDA_VL_TOT_DED_E110", "E110", "VL_TOT_DED"))

res = apuracao(e110(**DEVE), "|E111|ES000001||10,00|", E116)
check("E111 de outros débitos (3º 0, 4º 0) sem o E110 campo 04",
      achar(res, "REGRA_VL_TOT_AJ_DEBITOS_E110", "E110", "VL_TOT_AJ_DEBITOS"))
res = apuracao(e110(**{**DEVE, "VL_TOT_AJ_DEBITOS": "10,00",
                       "VL_SLD_APURADO": "340,00", "VL_ICMS_RECOLHER": "340,00"}),
               "|E111|ES000001||10,00|", E116.replace("330,00", "340,00"))
check("com o campo 04 somando o E111, a apuração fecha",
      not [i for i in res.inconsistencias if i.registro == "E110"], regras(res))

res = apuracao(e110(VL_TOT_CREDITOS="100,00", VL_SLD_CREDOR_TRANSPORTAR="100,00"))
item = achar(res, "REGRA_VL_TOT_CREDITOS_E110_17002_V1", "E110", "VL_TOT_CREDITOS")
check("crédito do E110 diferente da soma dos analíticos de entrada", bool(item))
check("dizendo a soma certa", item and "170,00" in item[0].mensagem)

extemporanea = linha("C100", {2: "1", 3: "0", 4: "F001", 5: "55", 6: "01", 7: "1",
                              8: "130", 9: chave("000000130", EMPRESA),
                              10: "05032026", 12: "100,00", 13: "0", 17: "9",
                              21: "100,00", 22: "50,00"}, 29)
b = blocos_base()
b["C"] += [extemporanea, "|C190|000|5102|50,00|100,00|100,00|50,00|0,00|0,00|0,00|0,00||"]
res = validar(b)
check("saída EXTEMPORÂNEA fica fora do total de débitos (vai para o DEB_ESP)",
      not achar(res, "REGRA_VL_TOT_DEBITOS_E110_17002_V1"), regras(res))

cancelada_com_valor = linha("C100", {2: "1", 3: "0", 5: "55", 6: "02", 7: "1",
                                     8: "131", 9: chave("000000131", EMPRESA),
                                     22: "99,00"}, 29)
b = blocos_base()
b["C"].append(cancelada_com_valor)
res = validar(b)
check("documento cancelado não é somado contra analíticos",
      not achar(res, "REGRA_VL_ICMS_ANALIT"), regras(res))

b = blocos_base()
b["E"] = ["|E001|0|", "|E100|01042026|15042026|", E110,
          "|E100|16042026|30042026|", E110]
res = validar(b)
check("com dois E110 (apuração por período), o total por CFOP não é conferido",
      not achar(res, "REGRA_VL_TOT_CREDITOS_E110_17002_V1")
      and res.nao_conferidas["REGRA_VL_TOT_CREDITOS_E110_17002_V1"] == 2,
      res.nao_conferidas["REGRA_VL_TOT_CREDITOS_E110_17002_V1"])


# ============================================================================
print("\n=== 12c. Gabarito do PVA (LIESA, jul/2026): o que faltava ===")
# O PVA apontou 5 erros e 1 advertência no arquivo real; o Synax pegava 3. Os
# três casos abaixo são os que faltavam, reproduzidos em fixture sintética.


def com_0205(dt_ini, dt_fim):
    b = blocos_base()
    b["0"].insert(6, f"|0205|PRODUTO ANTIGO|{dt_ini}|{dt_fim}|P000|")
    return validar(b)


res = com_0205("01013000", "01013000")
check("0205 com fim de uso em 3000 (o do gabarito)",
      achar(res, "REGRA_DATA_MENOR_DT_FIN_0000", "0205", "DT_FIM"))
res = com_0205("01032026", "30042026")
check("fim de uso NO último dia do período também não (é 'menor que')",
      achar(res, "REGRA_DATA_MENOR_DT_FIN_0000", "0205", "DT_FIM"))
res = com_0205("01032026", "31032026")
check("fim de uso antes do período fechar: aceito",
      not achar(res, "REGRA_DATA_MENOR_DT_FIN_0000"), regras(res))
res = com_0205("31032026", "01032026")
check("0205 com início depois do fim",
      achar(res, "REGRA_DATA_MENOR_IGUAL_DT_FIN_DT_INI_DT_FIM", "0205", "DT_INI"))

INDUSTRIAL = R0000[:-3] + "|0|"
assert INDUSTRIAL.endswith("|A|0|") and R0000.endswith("|A|1|")
E500 = ["|E500|0|01042026|30042026|",
        "|E520|0,00|0,00|0,00|0,00|0,00|0,00|0,00|"]
res = validar(com("0", 0, INDUSTRIAL))
item = achar(res, "REGRA_EXISTE_APURACAO_IPI", "0000", "IND_ATIV")
check("industrial (IND_ATIV 0) sem E500: erro, como no gabarito",
      item and item[0].gravidade == "erro")
b = com("0", 0, INDUSTRIAL)
b["E"] += E500
res = validar(b)
check("industrial com E500: aceito", not achar(res, "REGRA_EXISTE_APURACAO_IPI"))
b = blocos_base()
b["E"] += E500
res = validar(b)
check("não industrial (IND_ATIV 1) com E500",
      achar(res, "REGRA_NAO_EXISTE_APURACAO_IPI", "0000"))

C170_020 = trocar_campo(C170, 10, "020")
C190_020 = trocar_campo(trocar_campo(C190, 2, "020"), 10, "0,00")
res = validar({**blocos_base(), "C": ["|C001|0|", C100, C170_020, C190_020]})
item = achar(res, "REGRA_CST_ICMS_RED_BC_COM_COD_SIT_C190", "C190", "VL_RED_BC")
check("CST 020 sem redução de base: ADVERTÊNCIA (gravidade confirmada no PVA)",
      item and item[0].gravidade == ADVERTENCIA, item)
check("e advertência não conta como erro",
      res.erros == 0, [(i.regra, i.registro) for i in res.inconsistencias])
res = validar({**blocos_base(), "C": ["|C001|0|", C100, C170_020,
                                      trocar_campo(C190_020, 10, "150,00")]})
check("CST 020 com redução informada: nada",
      not achar(res, "REGRA_CST_ICMS_RED_BC_COM_COD_SIT_C190"))
res = validar({**blocos_base(), "C": ["|C001|0|", trocar_campo(C100, 6, "06"),
                                      C170_020, C190_020]})
check("nota complementar (COD_SIT 06) com CST 020 sem redução: não acusa",
      not achar(res, "REGRA_CST_ICMS_RED_BC_COM_COD_SIT_C190"), regras(res))


# ============================================================================
print("\n=== 12d. Documento: chave, operação/CFOP e CST do C190 ===")


def nota(c100=C100, itens=(C170,), analiticos=(C190,)):
    b = blocos_base()
    b["C"] = ["|C001|0|", c100, *itens, *analiticos]
    return validar(b)


res = nota(trocar_campo(C100, 5, "65"))
check("modelo do C100 (65) diferente do da chave (55)",
      achar(res, "REGRA_COMPARA_CODIGO_MODELO", "C100", "CHV_NFE"))
res = nota(trocar_campo(C100, 7, "2"))
check("série do C100 (2) diferente da série da chave (1)",
      achar(res, "REGRA_VALIDA_SERIE_CHAVE", "C100", "CHV_NFE"))
res = nota(trocar_campo(C100, 7, "001"))
check("série '001' é a mesma série 1 da chave",
      not achar(res, "REGRA_VALIDA_SERIE_CHAVE"))
res = nota(trocar_campo(C100, 8, "124"))
check("número do C100 (124) diferente do número da chave (123)",
      achar(res, "REGRA_COMPARA_NUM_DOC_NFE_CTE_C100_V4", "C100", "CHV_NFE"))

b = blocos_base()
b["0"].insert(4, R0150.replace("|F001|", "|F002|").replace(FORNECEDOR, "11222333000181"))
b["C"][1] = trocar_campo(C100, 4, "F002")
res = validar(b)
check("terceiros: CNPJ da chave não é o do participante do 0150",
      achar(res, "REGRA_COMPARA_CNPJ_NFE_PARTICIPANTE_12001_V1", "C100", "CHV_NFE"))

propria = trocar_campo(trocar_campo(C100, 2, "1"), 3, "0")
res = nota(propria)
check("emissão própria com o CNPJ do fornecedor na chave",
      achar(res, "REGRA_COMPARA_CNPJ_NFE_CTE_12001_V1", "C100", "CHV_NFE"))
chave_rj = "33" + chave("000000123", EMPRESA)[2:43]
pesos = [2, 3, 4, 5, 6, 7, 8, 9]
resto = sum(int(x) * pesos[i % 8] for i, x in enumerate(reversed(chave_rj))) % 11
chave_rj += str(0 if resto < 2 else 11 - resto)
res = nota(trocar_campo(propria, 9, chave_rj),
           itens=(trocar_campo(C170, 11, "5102"),),
           analiticos=(trocar_campo(C190, 3, "5102"),))
check("chave válida da UF 33 (RJ) em emissão própria de informante do ES",
      achar(res, "REGRA_COMPARA_UF_NFE_CTE_13001_V1", "C100", "CHV_NFE"), regras(res))
check("e o CNPJ dela é o do informante: essa regra não acusa",
      not achar(res, "REGRA_COMPARA_CNPJ_NFE_CTE_12001_V1"))

res = nota(trocar_campo(C100, 9, ""))
check("NF-e de terceiros sem chave",
      achar(res, "REGRA_OBRIGATORIO_CHV_NFE_CTE_TERCEIROS_12001_V1", "C100", "CHV_NFE"))
res = nota(trocar_campo(propria, 9, ""),
           itens=(trocar_campo(C170, 11, "5102"),),
           analiticos=(trocar_campo(C190, 3, "5102"),))
check("NF-e de emissão própria sem chave",
      achar(res, "REGRA_OBRIGATORIO_CHV_NFE_CTE_A13E1V2", "C100", "CHV_NFE"))
res = nota(trocar_campo(C100, 5, "01"))
check("chave informada em nota modelo 01 (papel)",
      achar(res, "REGRA_CHVDFE_MODELO_12001_V1", "C100", "CHV_NFE"))

res = nota(trocar_campo(C100, 2, "1"))
check("documento de terceiros como saída (IND_OPER 1)",
      achar(res, "REGRA_IND_EMIT", "C100", "IND_EMIT"))
res = nota(trocar_campo(trocar_campo(C100, 2, "1"), 6, "08"))
check("regime especial (COD_SIT 08): saída de terceiros é admitida",
      not achar(res, "REGRA_IND_EMIT"), regras(res))
res = nota(C100, itens=(trocar_campo(C170, 11, "5102"),),
           analiticos=(trocar_campo(C190, 3, "5102"),))
check("CFOP de saída (5102) em nota de entrada: no item e no analítico",
      achar(res, "REGRA_CFOP_C100", "C170", "CFOP")
      and achar(res, "REGRA_CFOP_C100", "C190", "CFOP"))
dois = trocar_campo(trocar_campo(C170, 2, "2"), 11, "2102")
res = nota(C100, itens=(C170, dois),
           analiticos=(C190, trocar_campo(C190, 3, "2102")))
check("itens da mesma nota com CFOP 1xxx e 2xxx",
      achar(res, "REGRA_COERENCIA_CFOP_C170", "C100", "CFOP"))

nfce = linha("C100", {2: "0", 3: "1", 5: "65", 6: "00", 7: "1", 8: "126",
                      9: chave("000000126", EMPRESA, "65"), 10: "05042026",
                      12: "10,00", 13: "0", 17: "9"}, 29)
res = nota(nfce, itens=(),
           analiticos=("|C190|000|1102|0,00|10,00|0,00|0,00|0,00|0,00|0,00|0,00||",))
check("NFC-e como entrada e de terceiros: operação, emitente e CFOP",
      achar(res, "REGRA_IND_OPER_NFCE_12001_V1", "C100", "IND_OPER")
      and achar(res, "REGRA_IND_EMIT_NFCE", "C100", "IND_EMIT")
      and achar(res, "REGRA_CFOP_NFCE", "C190", "CFOP"))
res = nota(trocar_campo(nfce, 9, "").replace("|0|1|", "|1|0|", 1), itens=(),
           analiticos=("|C190|000|5102|0,00|10,00|0,00|0,00|0,00|0,00|0,00|0,00||",))
check("NFC-e sem chave", achar(res, "REGRA_OBRIGATORIO_CHVE_NFCE_A7E2V1", "C100"))
check("e a regra genérica de chave própria não acusa a mesma coisa de novo",
      not achar(res, "REGRA_OBRIGATORIO_CHV_NFE_CTE_A13E1V2"))

saida_61 = trocar_campo(trocar_campo(C190, 2, "061"), 3, "5102")
res = nota(propria, itens=(), analiticos=(trocar_campo(saida_61, 9, "5,00"),))
check("CST 61 (monofásico cobrado antes) com ICMS ST na saída",
      achar(res, "REGRA_VALIDA_CST_VL_ICMS_ST_C190", "C190", "VL_ICMS_ST"))
check("CST 61 com base de ICMS",
      achar(res, "REGRA_VALIDA_CST_VL_BC_ICMS_C190", "C190", "VL_BC_ICMS"))
check("CST 61 na saída com alíquota e ICMS",
      achar(res, "REGRA_CST_ICMS_CAMPOS_ICMS_C190", "C190", "CST_ICMS"))
saida_02 = trocar_campo(trocar_campo(C190, 2, "002"), 3, "5102")
res = nota(propria, itens=(), analiticos=(saida_02,))
item = achar(res, "REGRA_CST_ICMS_CAMPOS_ICMS_C190", "C190")
check("CST 02 na saída com ICMS: só ADVERTÊNCIA (o PVA pede ao Simples que "
      "desconsidere)", item and item[0].gravidade == ADVERTENCIA, item)


# ============================================================================
print("\n=== 12e. Documento: datas, situação, valores × itens e duplicidade ===")

res = nota(trocar_campo(C100, 11, "04042026"))
item = achar(res, "REGRA_DT_E_S_MAIOR_IGUAL_DT_DOC", "C100", "DT_E_S")
check("entrada (04/04) antes da emissão (05/04): erro",
      item and item[0].gravidade == "erro", item)
res = nota(trocar_campo(trocar_campo(C100, 11, "04042026"), 6, "08"))
item = achar(res, "REGRA_DT_E_S_MAIOR_IGUAL_DT_DOC", "C100", "DT_E_S")
check("no regime especial (08) o Guia admite: só ADVERTÊNCIA (Exceção 5)",
      item and item[0].gravidade == ADVERTENCIA, item)
res = nota(trocar_campo(C100, 11, "02052026"))
check("entrada depois do fim do período",
      achar(res, "REGRA_DATA_MENOR_IGUAL_DT_FIN_0000_DT_ES", "C100", "DT_E_S"))
fora = trocar_campo(trocar_campo(C100, 10, "01052026"), 11, "02052026")
res = nota(fora)
check("emissão depois do fim do período",
      achar(res, "REGRA_DATA_MENOR_IGUAL_DT_FIN_0000_DT_DOC_C100", "C100", "DT_DOC"))
res = nota(trocar_campo(fora, 6, "08"))
check("...mas não no regime especial, em que a emissão pode vir depois",
      not achar(res, "REGRA_DATA_MENOR_IGUAL_DT_FIN_0000_DT_DOC_C100"), regras(res))

saida = trocar_campo(trocar_campo(propria, 10, "01052026"), 11, "")
res = nota(saida, itens=(), analiticos=(trocar_campo(C190, 3, "5102"),))
check("saída emitida depois do período sem a data de saída",
      achar(res, "REGRA_OBRIGATORIO_DT_E_S", "C100", "DT_E_S"))
res = nota(trocar_campo(propria, 11, ""), itens=(),
           analiticos=(trocar_campo(C190, 3, "5102"),))
check("saída emitida no período: a data de saída é opcional",
      not achar(res, "REGRA_OBRIGATORIO_DT_E_S")
      and not achar(res, "REGRA_CAMPO_OBRIGATORIO", "C100", "DT_E_S"), regras(res))

antes = trocar_campo(trocar_campo(C100, 10, "30032026"), 11, "31032026")
res = nota(antes)
check("regular (00) com entrada em março num arquivo de abril: é extemporâneo",
      achar(res, "REGRA_DOCUMENTO_NORMAL_DT_ES", "C100", "COD_SIT"))
res = nota(trocar_campo(antes, 6, "01"))
check("o mesmo documento como extemporâneo (01) passa",
      not achar(res, "REGRA_DOCUMENTO_NORMAL_DT_ES")
      and not achar(res, "REGRA_DOCUMENTO_EXTEMPORANEO_DT_E_S"), regras(res))
res = nota(trocar_campo(C100, 6, "01"))
check("extemporâneo (01) com entrada dentro do período: é regular",
      achar(res, "REGRA_DOCUMENTO_EXTEMPORANEO_DT_E_S", "C100", "COD_SIT"))
res = nota(trocar_campo(trocar_campo(C100, 11, ""), 10, "30032026"))
check("sem DT_E_S, a data que decide é a emissão (Guia, E110 campo 02)",
      achar(res, "REGRA_DOCUMENTO_NORMAL_DT_ES", "C100", "COD_SIT"), regras(res))
res = com_frete(trocar_campo(trocar_campo(D100, 11, "30032026"), 12, "31032026"))
check("CT-e regular com aquisição em março: DT_A_P no lugar da DT_E_S",
      achar(res, "REGRA_DOCUMENTO_NORMAL_DT_AP", "D100", "COD_SIT"))

# Valores do documento contra os itens
res = nota(trocar_campo(C100, 14, "10,00"))
check("desconto no C100 que os C170 não têm",
      achar(res, "REGRA_VALIDA_DESC_DOC_ITEM", "C100", "VL_DESC"))
res = nota(trocar_campo(C100, 14, "10,00"), itens=(trocar_campo(C170, 8, "10,00"),))
check("desconto igual à soma dos itens passa",
      not achar(res, "REGRA_VALIDA_DESC_DOC_ITEM"), regras(res))
res = nota(trocar_campo(C100, 15, "5,00"))
check("abatimento não tributado sem os itens",
      achar(res, "REGRA_VALIDA_VL_ABAT_NT", "C100", "VL_ABAT_NT"))
res = nota(trocar_campo(propria, 14, "10,00"), itens=(trocar_campo(C170, 11, "5102"),),
           analiticos=(trocar_campo(C190, 3, "5102"),))
check("NF-e própria: os C170 são parciais, o desconto não é somado",
      not achar(res, "REGRA_VALIDA_DESC_DOC_ITEM"), regras(res))

modelo_01 = trocar_campo(trocar_campo(C100, 5, "01"), 9, "")
res = nota(trocar_campo(modelo_01, 16, "900,00"))
check("nota modelo 01 de terceiros com mercadoria abaixo da soma dos itens",
      achar(res, "REGRA_VL_MERC_C170", "C100", "VL_MERC"))
res = nota(trocar_campo(modelo_01, 16, "1100,00"))
check("mercadoria ACIMA da soma: o Guia diz igual e o PVA diz maior ou igual; "
      "sem acordo, sem acusação", not achar(res, "REGRA_VL_MERC_C170"))
res = nota(trocar_campo(C100, 16, "900,00"))
check("NF-e (55) fica fora da regra do VL_MERC", not achar(res, "REGRA_VL_MERC_C170"))

C195 = "|C195|001||"
C197 = "|C197|ES10000001||P001|1000,00|17,00|1000,00|0,00|"
res = nota(C100, analiticos=(C190, C195, C197))
check("ICMS dos ajustes do documento igual ao valor da nota",
      achar(res, "REGRA_VL_ICMS_MAIOR_VL_DOC_C100", "C100", "VL_DOC"))
res = nota(C100, analiticos=(C190, C195, trocar_campo(C197, 7, "170,00")))
check("ajuste menor que o valor da nota passa",
      not achar(res, "REGRA_VL_ICMS_MAIOR_VL_DOC_C100"))

# Situação do documento
cancelada = linha("C100", {2: "0", 3: "1", 5: "55", 6: "02", 7: "1", 8: "123",
                           9: CHAVE}, 29)
res = nota(cancelada, itens=(), analiticos=())
check("cancelada só com a identificação e a chave passa",
      not achar(res, "REGRA_NOTA_FISCAL_CANCELADA_14001_V1")
      and not achar(res, "REGRA_CAMPO_OBRIGATORIO"), regras(res))
res = nota(trocar_campo(trocar_campo(cancelada, 10, "05042026"), 12, "1000,00"))
check("cancelada com data e valor: cada campo acusado",
      achar(res, "REGRA_NOTA_FISCAL_CANCELADA_14001_V1", "C100", "DT_DOC")
      and achar(res, "REGRA_NOTA_FISCAL_CANCELADA_14001_V1", "C100", "VL_DOC"))
check("e os filhos dela também (C170 e C190)",
      achar(res, "REGRA_NOTA_FISCAL_CANCELADA_14001_V1", "C170")
      and achar(res, "REGRA_NOTA_FISCAL_CANCELADA_14001_V1", "C190"))
res = nota(trocar_campo(trocar_campo(cancelada, 6, "05"), 12, "1,00"),
           itens=(), analiticos=())
check("numeração inutilizada (05) com valor",
      achar(res, "REGRA_NOTA_FISCAL_INUTILIZADA_A12E1V2", "C100", "VL_DOC"))
res = nota(trocar_campo(trocar_campo(cancelada, 6, "04"), 5, "01")
           .replace(CHAVE, ""), itens=(), analiticos=())
check("denegada (04) só existe para NF-e e NFC-e",
      achar(res, "REGRA_VALIDA_SITUACAO_PARA_NFE_A7E2V1", "C100", "COD_SIT"))

nfce_saida = linha("C100", {2: "1", 3: "0", 5: "65", 6: "00", 7: "1", 8: "126",
                            9: chave("000000126", EMPRESA, "65"), 10: "05042026",
                            12: "10,00", 13: "0", 17: "9"}, 29)
C190_NFCE = "|C190|000|5102|0,00|10,00|0,00|0,00|0,00|0,00|0,00|0,00||"
res = nota(nfce_saida, itens=(), analiticos=(C190_NFCE,))
check("NFC-e de saída correta não acusa o modificador",
      not achar(res, "REGRA_NFE_CONSUMIDOR_14001V1"), regras(res))
res = nota(trocar_campo(nfce_saida, 26, "0,10"), itens=(), analiticos=(C190_NFCE,))
check("NFC-e com PIS informado",
      achar(res, "REGRA_NFE_CONSUMIDOR_14001V1", "C100", "VL_PIS"))
res = nota(trocar_campo(trocar_campo(nfce_saida, 6, "08"), 13, ""), itens=(),
           analiticos=(C190_NFCE,))
check("NFC-e em regime especial (08) segue a obrigatoriedade normal",
      achar(res, "REGRA_CAMPO_OBRIGATORIO", "C100", "IND_PGTO"), regras(res))
res = nota(trocar_campo(trocar_campo(C100, 6, "08"), 13, ""))
check("...e a NF-e em regime especial não exige o IND_PGTO",
      not achar(res, "REGRA_CAMPO_OBRIGATORIO", "C100", "IND_PGTO"), regras(res))
check("NF-e de terceiros em regime especial sem o C110 da norma",
      achar(res, "REGRA_OBRIGATORIO_C110_14001_V1", "C100"))
res = nota(trocar_campo(propria, 6, "08"), itens=(),
           analiticos=(trocar_campo(C190, 3, "5102"),))
check("NF-e de emissão própria em regime especial dispensa o C110",
      not achar(res, "REGRA_OBRIGATORIO_C110_14001_V1"), regras(res))

res = nota(propria, itens=(trocar_campo(C170, 11, "5102"),),
           analiticos=(trocar_campo(C190, 3, "5102"),))
check("C170 em NF-e de emissão própria sem C176/C177/C180/C181",
      achar(res, "REGRA_COMPLEMENTO_NFE_EMITIDA_15001_V1_C176_C177_C180_C181", "C170"))
C176 = linha("C176", {}, len(dados.leiaute("020").registros["C176"].campos))
res = nota(propria, itens=(trocar_campo(C170, 11, "5102"), C176),
           analiticos=(trocar_campo(C190, 3, "5102"),))
check("...com o C176 (ressarcimento) o item é admitido",
      not achar(res, "REGRA_COMPLEMENTO_NFE_EMITIDA_15001_V1_C176_C177_C180_C181"))

base = validar(blocos_base())
check("os modificadores do C100 saem da conta das não conferidas",
      not any(k in base.nao_conferidas for k in (
          "REGRA_NOTA_FISCAL_CANCELADA_14001_V1", "REGRA_NOTA_FISCAL_INUTILIZADA_A12E1V2",
          "REGRA_NOTA_FISCAL_COMPLEMENTAR_17001_V2", "REGRA_NOTA_FISCAL_ESP_14001V1",
          "REGRA_NFE_EMITIDA_15001_V1", "REGRA_NFE_CONSUMIDOR_14001V1")),
      base.nao_conferidas.most_common(8))

# Duplicidade
b = blocos_base()
b["C"] = ["|C001|0|", C100, C170, C190, C100, C170, C190]
res = validar(b)
item = achar(res, "REGRA_DUPLICIDADE_NO_REGISTRO_C100_EMISSAO_TERCEIROS", "C100")
check("a mesma NF-e de terceiros duas vezes: acusada na segunda",
      len(item) == 1 and "linha" in item[0].mensagem, item)
b = blocos_base()
segunda = trocar_campo(propria, 4, "F002")
b["C"] = ["|C001|0|", propria, trocar_campo(C190, 3, "5102"),
          segunda, trocar_campo(C190, 3, "5102")]
res = validar(b)
check("emissão própria: o participante não distingue a nota",
      achar(res, "REGRA_DUPLICIDADE_NO_REGISTRO_C100_EMISSAO_PROPRIA", "C100"))
b = blocos_base()
b["C"] = ["|C001|0|", modelo_01, C170, C190, trocar_campo(modelo_01, 5, "1B"), C170, C190]
res = validar(b)
item = achar(res, "REGRA_MULTIPLAS_OCORRENCIAS_C100", "C100")
check("mesma nota em papel com modelo 01 e 1B: ADVERTÊNCIA, não duplicidade",
      item and item[0].gravidade == ADVERTENCIA
      and not achar(res, "REGRA_DUPLICIDADE_NO_REGISTRO_C100_EMISSAO_TERCEIROS"),
      regras(res))
b = blocos_base()
b["D"] = ["|D001|0|", D100, D190, D100, D190]
b["E"][2] = E110_COM_FRETE
res = validar(b)
check("o mesmo CT-e duas vezes",
      achar(res, "REGRA_DUPLICIDADE_NO_REGISTRO_D100_EMISSAO_TERCEIROS_A9E2V1", "D100"))


# ============================================================================
print("\n=== 12f. Cadastros: participante (0150) e item (0200) ===")

lt20 = dados.leiaute("020")
check("posição do campo vem da ORDEM do descritor, não do número dele",
      all([c.n for c in d.campos] == list(range(1, len(d.campos) + 1))
          for d in lt20.registros.values()))
check("0220: o código de barras é o 4º campo (o descritor do PVA diz 3)",
      lt20.registros["0220"].indice_campo["COD_BARRA"].n == 4)
check("C890: VL_ICMS é o 7º e COD_OBS o 8º (o descritor diz 6 para os dois)",
      lt20.registros["C890"].indice_campo["VL_ICMS"].n == 7
      and lt20.registros["C890"].indice_campo["COD_OBS"].n == 8)


def com_cadastro(*, r0150=R0150, r0200=R0200, extras_0=(), r0000=R0000, filhos_c170=()):
    b = blocos_base()
    b["0"] = [r0000, "|0001|0|", R0005, r0150, R0190, r0200, *extras_0]
    b["C"] = ["|C001|0|", C100, C170, *filhos_c170, C190]
    return validar(b)


exterior = trocar_campo(trocar_campo(R0150, 4, "2496"), 8, "")
res = com_cadastro(r0150=exterior)
check("participante do exterior com CNPJ",
      achar(res, "REGRA_NAO_PREENCHER_BRA_CNPJ", "0150", "CNPJ"))
res = com_cadastro(r0150=trocar_campo(trocar_campo(exterior, 5, ""), 7, "123"))
check("participante do exterior com IE", achar(res, "REGRA_NAO_PREENCHER_BRA_IE", "0150", "IE")
      and not achar(res, "REGRA_NAO_PREENCHER_BRA_CNPJ"))
check("...e do exterior não precisa de município", not achar(
    res, "REGRA_OBRIGATORIO_PARA_BRASIL_A10E3V4"), regras(res))
res = com_cadastro(r0150=trocar_campo(R0150, 6, "52998224725"))
check("CNPJ e CPF no mesmo participante", achar(res, "REGRA_CNPJ_EXCLUDENTE", "0150", "CNPJ"))
res = com_cadastro(r0150=trocar_campo(R0150, 5, ""))
check("participante do Brasil sem CNPJ nem CPF",
      achar(res, "REGRA_OBRIGATORIO_CPF_BRA_V2", "0150", "CNPJ"))
for municipio in ("", "9999999"):
    res = com_cadastro(r0150=trocar_campo(R0150, 8, municipio))
    check(f"participante do Brasil com município '{municipio}'",
          achar(res, "REGRA_OBRIGATORIO_PARA_BRASIL_A10E3V4", "0150", "COD_MUN"))
res = com_cadastro(r0150=trocar_campo(R0150, 4, "01058"))
check("Brasil escrito 01058 é o mesmo 1058", res.total == 0,
      [(i.regra, i.campo) for i in res.inconsistencias])

res = com_cadastro(r0150=trocar_campo(R0150, 9, "200187414"))
check("SUFRAMA com dígito certo passa", not achar(res, "REGRA_DIGITO_SUFRAMA"))
res = com_cadastro(r0150=trocar_campo(R0150, 9, "200187415"))
check("SUFRAMA com dígito errado", achar(res, "REGRA_DIGITO_SUFRAMA", "0150", "SUFRAMA"))

res = com_cadastro(extras_0=(R0150.replace("|F001|", "|F002|"),))
item = achar(res, "REGRA_REFERENCIADO_COD_PART_17002_V1", "0150", "COD_PART")
check("participante que nenhum registro usa", len(item) == 1 and item[0].valor == "F002",
      item)
P002 = R0200.replace("|P001|", "|P002|")
res = com_cadastro(extras_0=(P002,))
check("item que nenhum registro usa",
      [i.valor for i in achar(res, "REGRA_REFERENCIADO_COD_ITEM_17002_V1")] == ["P002"])
res = com_cadastro(extras_0=(P002, "|0220|UN|12||"))
check("...com fator de conversão (0220) o item sem movimento é admitido",
      not achar(res, "REGRA_REFERENCIADO_COD_ITEM_17002_V1"), regras(res))
check("e o 0220 sem código de barras não exige o do 0200 (lido no campo certo)",
      not achar(res, "REGRA_OBRIGATORIO_COD_BARRA"), regras(res))
res = com_cadastro(extras_0=("|0220|UN|12|7891000315507|",))
check("0220 com código de barras exige o do 0200",
      achar(res, "REGRA_OBRIGATORIO_COD_BARRA", "0200", "COD_BARRA"))

industrial = R0000[:-3] + "|0|"
assert industrial.count("|") == R0000.count("|")
res = com_cadastro(r0000=industrial, r0200=trocar_campo(R0200, 8, ""))
check("industrial (IND_ATIV 0) com mercadoria sem NCM",
      achar(res, "REGRA_OBRIGATORIO_NCM", "0200", "COD_NCM"))
res = com_cadastro(r0000=industrial, r0200=trocar_campo(trocar_campo(R0200, 8, ""), 7, "07"))
check("...material de uso e consumo (07) dispensa o NCM",
      not achar(res, "REGRA_OBRIGATORIO_NCM"))
res = com_cadastro(r0200=trocar_campo(R0200, 8, ""))
check("comércio (IND_ATIV 1) não precisa de NCM", not achar(res, "REGRA_OBRIGATORIO_NCM"))
res = com_cadastro(r0200=trocar_campo(R0200, 8, "99999999"))
check("NCM 99999999", achar(res, "REGRA_VALIDA_COD_NCM", "0200", "COD_NCM"))
res = com_cadastro(r0200=trocar_campo(R0200, 5, "ANTIGO1"))
check("código anterior no 0200 (vai no 0205)",
      achar(res, "REGRA_NAO_INFORMAR_COD_ANT_ITEM_0200", "0200", "COD_ANT_ITEM"))
res = com_cadastro(r0200=trocar_campo(R0200, 11, "1401"))
check("serviço da LC 116 sem o ponto", achar(res, "REGRA_VALIDA_COD_LST", "0200", "COD_LST"))
res = com_cadastro(r0200=trocar_campo(R0200, 11, "14.01"))
check("...no formato NN.NN passa", not achar(res, "REGRA_VALIDA_COD_LST"))

res = com_cadastro(r0200=R0200, filhos_c170=())
check("item na unidade do estoque não pede fator de conversão",
      not achar(res, "REGRA_VALIDA_UNID_CONVERSAO_17001_V1"))
b = blocos_base()
b["C"][2] = trocar_campo(C170, 6, "CX")
res = validar(b)
check("item em CX com estoque em UN e sem 0220",
      achar(res, "REGRA_VALIDA_UNID_CONVERSAO_17001_V1", "C170", "UNID"))
b["0"].append("|0220|CX|12||")
res = validar(b)
check("...com o 0220 do item a unidade diferente é admitida",
      not achar(res, "REGRA_VALIDA_UNID_CONVERSAO_17001_V1"), regras(res))

C180 = linha("C180", {}, len(lt20.registros["C180"].campos))
res = com_cadastro(filhos_c170=(C180,))
check("item com ressarcimento de ST (C180) e sem alíquota interna no 0200",
      achar(res, "REGRA_OBRIGATORIO_ALIQ_RESSARC_ST", "0200", "ALIQ_ICMS"))
res = com_cadastro(filhos_c170=(C180,), r0200=trocar_campo(R0200, 12, "17,00"))
check("...com a alíquota informada passa", not achar(res, "REGRA_OBRIGATORIO_ALIQ_RESSARC_ST"))


# ============================================================================
print("\n=== 12g. Item (C170): CST × campos, CST do IPI e quantidade ===")


def item_c170(**campos):
    texto = C170
    for n, valor in campos.items():
        texto = trocar_campo(texto, int(n[1:]), valor)
    return texto


res = nota(C100, itens=(item_c170(c10="061"),))
check("entrada monofásica (CST 61) com base de ICMS",
      achar(res, "REGRA_VALIDA_CST_VL_BC_ICMS_C170", "C170", "VL_BC_ICMS"))
res = nota(C100, itens=(item_c170(c10="002", c13="0,00", c14="", c15="0,00"),))
achados = achar(res, "REGRA_VALIDA_CST_ALIQ_ICMS_C170", "C170", "ALIQ_ICMS")
check("entrada CST 02 com a alíquota VAZIA: acusada, e como advertência "
      "(o texto do PVA termina em '- ADVERTÊNCIA')",
      achados and achados[0].gravidade == ADVERTENCIA, achados)
res = nota(C100, itens=(item_c170(c10="015", c13="0,00", c14="17,00", c15="0,00"),))
check("entrada CST 15 sem alíquota de ST",
      achar(res, "REGRA_VALIDA_CST_ALIQ_ST_C170", "C170", "ALIQ_ST"))
res = nota(C100, itens=(item_c170(c10="061", c13="0,00", c14="", c15="0,00", c17="5,00"),))
check("entrada CST 61 com alíquota de ST",
      achar(res, "REGRA_VALIDA_CST_ALIQ_ST_C170", "C170", "ALIQ_ST"))
res = nota(C100, itens=(item_c170(c10="040"),))
check("o confronto de CST × ICMS da SAÍDA não se aplica à entrada",
      not achar(res, "REGRA_CST_ICMS_CAMPOS_ICMS_C170_V3"), regras(res))

saida_item = trocar_campo(item_c170(c10="040"), 11, "5102")
res = nota(propria, itens=(saida_item,), analiticos=(trocar_campo(C190, 3, "5102"),))
check("saída isenta (CST 40) com base, alíquota e ICMS",
      achar(res, "REGRA_CST_ICMS_CAMPOS_ICMS_C170_V3", "C170", "CST_ICMS"))
res = nota(propria, itens=(item_c170(c11="5102", c14="", c15="0,00"),),
           analiticos=(trocar_campo(C190, 3, "5102"),))
check("saída tributada (CST 00) com base e sem alíquota nem ICMS",
      achar(res, "REGRA_CST_ICMS_CAMPOS_ICMS_C170_V3", "C170", "CST_ICMS"))
res = nota(propria, itens=(item_c170(c10="020", c11="5102", c14="", c15="0,00"),),
           analiticos=(trocar_campo(C190, 3, "5102"),))
check("...mas a redução de base (CST 20) pode vir zerada",
      not achar(res, "REGRA_CST_ICMS_CAMPOS_ICMS_C170_V3"), regras(res))
res = nota(propria, itens=(item_c170(c10="061", c11="5102", c13="0,00", c14="", c15="0,00",
                                c18="3,00"),),
           analiticos=(trocar_campo(C190, 3, "5102"),))
check("saída CST 61 com ICMS ST",
      achar(res, "REGRA_CST_ICMS_CAMPOS_ST_COM_COD_SIT_C170_V2", "C170", "CST_ICMS"))

res = nota(C100, itens=(item_c170(c20="50"),))
check("CST do IPI de saída (50) em item de entrada",
      achar(res, "REGRA_VALIDA_CST_IPI", "C170", "CST_IPI"))
res = nota(C100, itens=(item_c170(c20="49"),))
check("CST do IPI de entrada (49) passa", not achar(res, "REGRA_VALIDA_CST_IPI"))
res = nota(C100, itens=(item_c170(c5="0"),))
check("item com quantidade zero",
      achar(res, "REGRA_MAIOR_ZERO_CONDICAO_COD_SIT", "C170", "QTD"))
res = nota(trocar_campo(C100, 6, "06"), itens=(item_c170(c5="0"),))
check("...no complementar (06) a quantidade zero é admitida",
      not achar(res, "REGRA_MAIOR_ZERO_CONDICAO_COD_SIT"), regras(res))


# ============================================================================
print("\n=== 12h. Transporte (D100/D190): CT-e, BP-e e datas ===")

b = blocos_base()
b["0"].insert(4, R0150.replace("|F001|", "|F002|").replace(FORNECEDOR, EMPRESA))
b["D"] = ["|D001|0|", trocar_campo(D100, 4, "F002"), D190]
b["E"][2] = E110_COM_FRETE
res = validar(b)
check("CT-e de terceiros com participante de outro CNPJ que o da chave",
      achar(res, "REGRA_COMPARA_CNPJ_CTE_PARTICIPANTE_14003_V5", "D100", "CHV_CTE"))
res = com_frete(trocar_campo(D100, 24, ""))
check("CT-e de entrada sem município de origem",
      achar(res, "REGRA_OBRIGATORIO_COD_MUN_DOCE_ORIG_V3", "D100", "COD_MUN_ORIG"))
res = com_frete(trocar_campo(D100, 12, "02052026"))
check("aquisição do frete depois do fim do período",
      achar(res, "REGRA_DATA_MENOR_IGUAL_DT_FIN_0000_DT_A_P", "D100", "DT_A_P"))
res = com_frete(trocar_campo(D100, 14, chave("000000556", FORNECEDOR, "57")))
check("chave referenciada num CT-e normal (tipo 0)",
      achar(res, "REGRA_VALIDA_CHV_CTE_REF", "D100", "CHV_CTE_REF"))
res = com_frete(trocar_campo(D100, 14, chave("000000555", FORNECEDOR, "57")))
check("chave referenciada igual à do próprio CT-e",
      achar(res, "REGRA_DIFERENTE_CTE_REF", "D100", "CHV_CTE_REF"))
res = com_frete(trocar_campo(D100, 13, "3"))
check("CT-e substituto (tipo 3) sem a chave do substituído",
      achar(res, "REGRA_VALIDA_CHV_CTE_REF", "D100", "CHV_CTE_REF"))
res = com_frete(d190=trocar_campo(D190, 2, "100"))
check("CST de serviço com origem diferente de 0",
      achar(res, "REGRA_CST_ICMS_SERVICO", "D190", "CST_ICMS"))
res = com_frete(trocar_campo(trocar_campo(D100, 5, "07"), 10, ""))
check("nota de serviço de transporte (modelo 07) emitida depois de 2018",
      achar(res, "REGRA_DATA_MAXIMA_MODELO_DOC_A13E1V2", "D100", "DT_DOC"))
res = com_frete(trocar_campo(trocar_campo(D100, 5, "63"), 13, "1"))
check("BP-e (63) de entrada, de terceiros e de tipo 1: as três regras do BP-e",
      achar(res, "REGRA_BPE_SAIDA", "D100", "IND_OPER")
      and achar(res, "REGRA_BPE_EMISSAO_PROPRIA", "D100", "IND_EMIT")
      and achar(res, "REGRA_TIPO_BPE_V2", "D100", "TP_CT_E"))
res = com_frete(trocar_campo(trocar_campo(trocar_campo(D100, 2, "1"), 6, "08"), 10, ""))
check("CT-e de saída de terceiros em regime especial sem a chave",
      achar(res, "REGRA_CHAVE_REGIME_ESPECIAL_CTE", "D100", "CHV_CTE"))


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

# ============================================================================
print("\n=== 14. Histórico da validação e painel do mês ===")

from backend.app.domain.models.audit import SpedValidacao  # noqa: E402
from backend.app.services.sped_entregas import hash_do_corpo  # noqa: E402

ss = SessionLocal()
regs = (ss.query(SpedValidacao.tenant_id, SpedValidacao.competencia,
                 SpedValidacao.cnpj, SpedValidacao.erros, SpedValidacao.hash_corpo,
                 SpedValidacao.cod_fin, SpedValidacao.user_name)
        .order_by(SpedValidacao.created_at).all())
ss.close()
check("cada validação aceita fica registrada (3 de A; as recusadas não)",
      len(regs) == 3 and all(r.tenant_id == str(tA) for r in regs),
      [(r.tenant_id, r.erros) for r in regs])
check("com competência, CNPJ do 0000, original/substituta e quem validou",
      regs and regs[0].competencia == "2026-04" and regs[0].cnpj == EMPRESA
      and regs[0].cod_fin == "0" and regs[0].user_name == "Operador")
check("e o hash do corpo é o do arquivo enviado",
      regs and regs[0].hash_corpo == hash_do_corpo(ARQ))
check("o arquivo com erro ficou com o número de erros",
      regs and regs[-1].erros == 1)
check("a API devolve o hash e diz que registrou",
      corpo.get("registrada") is True and corpo.get("hash_corpo") == hash_do_corpo(errado))

r = enviar("ope", tA, ARQ, htmx=True)
check("validar de novo avisa que a competência já foi validada, com o MESMO arquivo",
      "já tinha" in r.text and "MESMO arquivo já tinha sido validado" in r.text,
      re.sub(r"\s+", " ", r.text)[:600])
r = enviar("ope", tA, certo.replace("|1|\r\n|0001|", "|1|\r\n|0001|", 1)
           .replace("RUA A", "RUA B").encode("latin-1"), htmx=True)
check("e, com outro conteúdo, que o arquivo é DIFERENTE",
      "DIFERENTE" in r.text, r.text[-400:])

r = as_("ope").get("/api/validar-sped/painel?competencia=2026-04")
check("painel do mês responde ao operador", r.status_code == 200, r.status_code)
check("mostra as empresas dele: a validada e a que falta",
      "EMPRESA TESTE LTDA" in r.text and "OUTRA EMPRESA LTDA" in r.text
      and "não validada" in r.text)
check("e não mostra empresa de outro operador",
      "EMPRESA DE OUTRO OPERADOR" not in r.text)
r = as_("ope2").get("/api/validar-sped/painel?competencia=2026-04")
check("o outro operador só vê a dele, sem a validação de A",
      "EMPRESA DE OUTRO OPERADOR" in r.text and "EMPRESA TESTE LTDA" not in r.text)
r = as_("ope").get("/api/validar-sped/painel?competencia=2026-03")
check("em outro mês, nada validado", "0</strong> de 2" in r.text, r.text[:300])
r = as_("ope").get("/api/validar-sped/painel?competencia=lixo")
check("competência inválida não derruba (vale a última fechada)", r.status_code == 200)
check("cliente não vê o painel",
      as_("cli").get("/api/validar-sped/painel").status_code == 403)
check("a aba traz o painel do mês",
      "/api/validar-sped/painel" in as_("ope").get("/validar-sped").text)


# ============================================================================
print("\n=== 15. Hash do corpo: a assinatura do PVA não muda, a edição muda ===")

# A ajuda do PVA: sem edição, "a assinatura do arquivo será alocada ao final
# do arquivo texto original". O bloco simulado aqui só precisa vir DEPOIS do 9999.
ASSINATURA = b"SBRCAAEPDR" + bytes(range(256)) * 4
h0 = hash_do_corpo(ARQ)
check("o hash é SHA-256 em hexadecimal", len(h0) == 64 and int(h0, 16) >= 0)
check("arquivo assinado (bloco depois do 9999) tem o mesmo hash do corpo",
      hash_do_corpo(ARQ + ASSINATURA) == h0)
check("assinatura grudada na linha do 9999 também",
      hash_do_corpo(ARQ.rstrip(b"\r\n") + ASSINATURA) == h0)
check("sem a quebra de linha final, o mesmo hash",
      hash_do_corpo(ARQ.rstrip(b"\r\n")) == h0)
check("BOM no começo não muda o hash", hash_do_corpo(b"\xef\xbb\xbf" + ARQ) == h0)
check("um valor alterado (edição no PVA) muda o hash",
      hash_do_corpo(ARQ.replace(b"|1000,00|1|", b"|1000,01|1|", 1)) != h0)
import tempfile  # noqa: E402
with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
    tmp.write(ARQ + ASSINATURA)
from backend.app.services.sped_entregas import hash_do_corpo_arquivo  # noqa: E402
check("lendo do disco em fluxo dá o mesmo hash",
      hash_do_corpo_arquivo(tmp.name) == h0)
os.remove(tmp.name)

import shutil as _shutil  # noqa: E402
ss = SessionLocal()
_processos = [r.id for r in ss.query(SpedValidacao.id).all()]
ss.close()
for p in _processos:
    _shutil.rmtree(os.path.join(os.getcwd(), "temp_uploads", p), ignore_errors=True)
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
