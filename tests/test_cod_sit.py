# -*- coding: utf-8 -*-
"""COD_SIT: só cancelado/denegado/inutilizado fica de fora da apuração.

Relato do Sandro em 01/09/2026: o CT-e 211146 (Transportes Mobiline) tinha
R$ 30,45 de crédito de ICMS no XML, a empresa escriturou sem crédito, e a tela
não acusou nada.

A causa não estava na conferência — estava no PARSER. O filtro era
`sit == "00"`, então o documento, escriturado com **COD_SIT 06 (Complementar)**,
nunca chegava a `d100_entries`. Ele não era conferido e nem aparecia como
faltante: sumia inteiro, calado.

Tabela 4.1.2 do SPED — o que cada código significa e o que o motor faz agora:

    00  Documento regular                        CONFERE
    01  Extemporâneo regular                     CONFERE
    02  Documento cancelado                      ignora
    03  Extemporâneo cancelado                   ignora
    04  NF-e/CT-e denegado                       ignora
    05  Numeração inutilizada                    ignora
    06  Documento Fiscal COMPLEMENTAR            CONFERE  <- o caso do relato
    07  Complementar extemporâneo                CONFERE
    08  Regime especial / norma específica       CONFERE

Os quatro ignorados não têm crédito a conferir. Todo o resto tem imposto e
precisa ser olhado — inclusive porque documento complementar existe justamente
para acertar valor ou imposto de outro documento.
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('backend'))

from app.services.audit_engine.sped_parser import parse_sped, SITUACOES_SEM_EFEITO  # noqa: E402

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


EMPRESA = "11222333000181"
CONFEREM = ("00", "01", "06", "07", "08")
IGNORAM = ("02", "03", "04", "05")


def sped_com(sit_nfe: str, sit_cte: str) -> str:
    """Um SPED com uma NF-e e um CT-e, cada um na situação pedida."""
    chave_nfe = "1" * 44
    chave_cte = "2" * 44
    linhas = [
        f"|0000|017|0|01072026|31072026|EMPRESA TESTE LTDA|{EMPRESA}|ES|||",
        "|0150|9001|FORNECEDOR|1058|55444333000122||||||",
        "|0200|A1|PRODUTO|||UN|00|||||",
        f"|C100|0|1|9001|55|{sit_nfe}|1|500|{chave_nfe}|18072026|18072026|"
        "100,00|0|0,00|0,00|100,00|0|0,00|0,00|0,00|0,00|12,00|0,00|0,00|0,00|",
        "|" + "|".join([
            "C170", "1", "A1", "", "1,00", "UN", "100,00", "0,00", "0", "00",
            "1102", "", "100,00", "12,00", "12,00", "0,00", "0,00", "0,00", "0",
            "", "", "0,00", "0,00", "0,00", "50", "", "1,65", "", "", "0,00",
            "50", "", "7,60", "", "", "0,00", "", "0,00",
        ]) + "|",
        # Layout D100: |REG|IND_OPER|IND_EMIT|COD_PART|COD_MOD|COD_SIT|SER|SUB|
        #              NUM_DOC|CHV|DT_DOC|DT_A_P|TP_CT|CHV_REF|VL_DOC|...
        f"|D100|0|1|9001|57|{sit_cte}|1||211146|{chave_cte}|31072026|13082026|0||"
        "435,00|0|1|435,00|435,00|30,45|0|||4206652|3203205|",
        "|D190|000|2353|7,00|435,00|435,00|30,45|0,00||",
    ]
    fd, caminho = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    io.open(caminho, "w", encoding="latin1").write("\n".join(linhas) + "\n")
    return caminho


def contar(sit_nfe, sit_cte):
    caminho = sped_com(sit_nfe, sit_cte)
    try:
        d = parse_sped(caminho)
        return len(d.get("c100", {})), len(d.get("d100", {}))
    finally:
        os.remove(caminho)


# ============================================================================
print("\n=== 1. A lista do que fica de fora ===")

check("só cancelado, extemp. cancelado, denegado e inutilizado",
      SITUACOES_SEM_EFEITO == {"02", "03", "04", "05"}, SITUACOES_SEM_EFEITO)
for sit in CONFEREM:
    check(f"COD_SIT {sit} NÃO está na lista de descarte",
          sit not in SITUACOES_SEM_EFEITO)


# ============================================================================
print("\n=== 2. Documento válido entra na apuração ===")

for sit in CONFEREM:
    nfe, cte = contar(sit, sit)
    rotulo = {"00": "regular", "01": "extemporâneo", "06": "COMPLEMENTAR",
              "07": "complementar extemp.", "08": "regime especial"}[sit]
    check(f"NF-e COD_SIT {sit} ({rotulo}) é lida", nfe == 1, f"c100={nfe}")
    check(f"CT-e COD_SIT {sit} ({rotulo}) é lido", cte == 1, f"d100={cte}")


# ============================================================================
print("\n=== 3. Documento sem efeito continua de fora ===")

for sit in IGNORAM:
    nfe, cte = contar(sit, sit)
    rotulo = {"02": "cancelado", "03": "extemp. cancelado",
              "04": "denegado", "05": "inutilizado"}[sit]
    check(f"NF-e COD_SIT {sit} ({rotulo}) fica de fora", nfe == 0, f"c100={nfe}")
    check(f"CT-e COD_SIT {sit} ({rotulo}) fica de fora", cte == 0, f"d100={cte}")


# ============================================================================
print("\n=== 4. O caso do relato: CT-e complementar com crédito ===")

# Exatamente o 211146: COD_SIT 06, D190 com ICMS, e o XML com R$ 30,45.
caminho = sped_com("00", "06")
try:
    dados = parse_sped(caminho)
    cte = dados["d100"].get("2" * 44)
    check("o CT-e complementar chega ao motor", cte is not None)
    if cte:
        check("com o número certo", cte["num"] == "211146", cte.get("num"))
        check("e o analítico D190 lido", cte.get("tem_d190") is True, cte.get("tem_d190"))
        check("com o CFOP do D190", "2353" in cte.get("cfops", []), cte.get("cfops"))
        check("e o ICMS do D190", abs(cte.get("vl_icms", 0) - 30.45) < 0.01,
              cte.get("vl_icms"))
finally:
    os.remove(caminho)

# A NF-e e o CT-e são filtrados de forma independente: um cancelado não pode
# levar o outro junto.
nfe, cte = contar("02", "06")
check("NF-e cancelada não derruba o CT-e complementar da mesma apuração",
      (nfe, cte) == (0, 1), (nfe, cte))
nfe, cte = contar("06", "02")
check("CT-e cancelado não derruba a NF-e complementar",
      (nfe, cte) == (1, 0), (nfe, cte))


# ============================================================================
print("\n=== 5. O filtro é o mesmo nos dois registros ===")

FONTE = io.open(os.path.join("backend", "app", "services", "audit_engine",
                             "sped_parser.py"), encoding="utf-8").read()
# Procura a FORMA DE CÓDIGO, não a menção: o comentário do módulo cita o filtro
# antigo de propósito, para explicar o bug do CT-e 211146.
check("nenhum lugar volta a exigir COD_SIT == '00'",
      'and sit == "00":' not in FONTE, "o filtro restritivo voltou")
check("C100 e D100 usam a mesma lista",
      FONTE.count('sit not in SITUACOES_SEM_EFEITO') == 2,
      FONTE.count('sit not in SITUACOES_SEM_EFEITO'))


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

sys.exit(1 if FALHAS else 0)
