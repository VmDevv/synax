# -*- coding: utf-8 -*-
"""IPI devolvido (vIPIDevol) entra na conferência de IPI.

Relato do Sandro (27/08/2026): "veio com o valor do IPI do XML zerado sendo que
tem valor do ipi dentro do ipi devolvido no xml".

O XML traz o IPI em DUAS tags, de grupos diferentes:

    <imposto><IPI><vIPI>              IPI destacado na venda
    <impostoDevol><IPI><vIPIDevol>    IPI que volta numa devolução

A conferência lia só `vIPI`. Nota de devolução chegava com IPI ZERO, e daí saíam
os dois erros opostos: quem creditou certo era acusado ("SPED R$ 50,00 vs XML
R$ 0,00") e quem NÃO creditou não era avisado de nada — crédito na mesa, em
silêncio. É a mesma doença do `vICMSOutraUF` no CT-e.

Quem decide o crédito continua sendo a regra do tenant (CFOP + CST). O que mudou
é o motor ENXERGAR o valor que está no documento.

Duas travas de apresentação, pedidas junto:
  - a conta aberta acompanha a linha (total, destacado e devolvido), porque o
    total não bate com o `vIPI` que a pessoa vê ao abrir o XML;
  - essas linhas vão para o TOPO da lista — no fim de uma tabela paginada de 20
    em 20 elas não seriam vistas.
"""
import io
import os
import sys
import unicodedata

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('backend'))

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


def sem_acento(txt):
    return "".join(c for c in unicodedata.normalize("NFD", str(txt))
                   if unicodedata.category(c) != "Mn").lower()


# ============================================================================
print("\n=== 1. O parser lê as duas tags separadamente ===")

from app.services.audit_engine.xml_parser import parse_nfe_xml  # noqa: E402

NS = "http://www.portalfiscal.inf.br/nfe"


def montar_xml(v_prod=1000.0, v_ipi=None, v_ipi_devol=None):
    """NF-e de um item. `v_ipi` no grupo imposto, `v_ipi_devol` no impostoDevol."""
    bloco_ipi = ""
    if v_ipi is not None:
        bloco_ipi = (f"<IPI><cEnq>999</cEnq><IPITrib><CST>50</CST>"
                     f"<vBC>{v_prod:.2f}</vBC><pIPI>5.00</pIPI>"
                     f"<vIPI>{v_ipi:.2f}</vIPI></IPITrib></IPI>")
    bloco_devol = ""
    if v_ipi_devol is not None:
        bloco_devol = (f"<impostoDevol><pDevol>100.00</pDevol>"
                       f"<IPI><vIPIDevol>{v_ipi_devol:.2f}</vIPIDevol></IPI>"
                       f"</impostoDevol>")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="{NS}"><NFe><infNFe Id="NFe{'9' * 44}">
 <ide><nNF>777</nNF><serie>1</serie></ide>
 <emit><CNPJ>55444333000122</CNPJ><xNome>FORNECEDOR LTDA</xNome>
   <enderEmit><UF>ES</UF></enderEmit></emit>
 <dest><CNPJ>11222333000181</CNPJ><xNome>EMPRESA TESTE</xNome>
   <enderDest><UF>ES</UF></enderDest></dest>
 <det nItem="1">
  <prod><cProd>ABC</cProd><xProd>PRODUTO DEVOLVIDO</xProd><NCM>84212300</NCM>
    <CFOP>1202</CFOP><qCom>1.0000</qCom><vUnCom>{v_prod:.2f}</vUnCom>
    <vProd>{v_prod:.2f}</vProd></prod>
  <imposto>
    <ICMS><ICMS00><CST>00</CST><vBC>{v_prod:.2f}</vBC><pICMS>12.00</pICMS>
      <vICMS>120.00</vICMS></ICMS00></ICMS>
    {bloco_ipi}
  </imposto>
  {bloco_devol}
 </det>
</infNFe></NFe></nfeProc>"""


def ler(**kw):
    import tempfile
    fd, p = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    io.open(p, "w", encoding="utf-8").write(montar_xml(**kw))
    try:
        return parse_nfe_xml(p)["items"][0]
    finally:
        os.remove(p)


item = ler(v_ipi=50.0)
check("vIPI é lido", item["v_ipi"] == 50.0, item.get("v_ipi"))
check("sem impostoDevol, o devolvido é zero", item["v_ipi_devol"] == 0.0,
      item.get("v_ipi_devol"))

# O caso do relato: IPI SÓ no grupo impostoDevol.
item = ler(v_ipi_devol=50.0)
check("vIPIDevol é lido do grupo impostoDevol", item["v_ipi_devol"] == 50.0,
      item.get("v_ipi_devol"))
check("nesse caso vIPI fica zerado (é o sintoma relatado)",
      item["v_ipi"] == 0.0, item.get("v_ipi"))

# As duas na mesma linha continuam separadas no retorno do parser.
item = ler(v_ipi=30.0, v_ipi_devol=20.0)
check("as duas tags coexistem sem se sobrescrever",
      (item["v_ipi"], item["v_ipi_devol"]) == (30.0, 20.0),
      (item.get("v_ipi"), item.get("v_ipi_devol")))


# ============================================================================
print("\n=== 2. A conferência soma as duas tags ===")

FONTE = io.open(os.path.join("backend", "app", "services", "audit_engine",
                             "cross_checker.py"), encoding="utf-8").read()

bloco = FONTE[FONTE.find("# -- Sprint 3: IPI Compliance --"):]
bloco = bloco[:bloco.find("# -- Sprint 3: PIS/COFINS")]

check("o XML do IPI deixou de ser só v_ipi",
      'xml_ipi = xml_item.get("v_ipi", 0.0)' not in bloco)
check("xml_ipi soma o próprio e o devolvido",
      "xml_ipi = round(xml_ipi_proprio + xml_ipi_devol, 2)" in bloco, bloco[:600])
check("as parcelas ficam separadas na linha da divergência",
      '"xml_ipi_proprio": xml_ipi_proprio' in bloco
      and '"xml_ipi_devol": xml_ipi_devol' in bloco)
check("a conta aberta só aparece quando há devolvido",
      "if xml_ipi_devol > 0.01 else \"\"" in bloco, bloco[:1200])
check("a conta aberta entra nas duas mensagens (glosa e divergência)",
      bloco.count("detalhe_ipi") >= 3, bloco.count("detalhe_ipi"))


# ============================================================================
print("\n=== 3. Nota de devolução vai para o topo da lista ===")

check("ipi_issues é ordenado pelo devolvido",
      'results["ipi_issues"].sort(' in FONTE and "xml_ipi_devol" in
      FONTE[FONTE.find('results["ipi_issues"].sort('):][:300])

# Ordenação estável: quem tem devolvido primeiro, o resto na ordem de apuração.
linhas = [
    {"num": "A", "xml_ipi_devol": 0.0},
    {"num": "B", "xml_ipi_devol": 50.0},
    {"num": "C", "xml_ipi_devol": 0.0},
    {"num": "D", "xml_ipi_devol": 12.5},
]
linhas.sort(key=lambda i: 0 if float(i.get("xml_ipi_devol", 0.0) or 0.0) > 0.01 else 1)
check("devolvidos primeiro, resto na ordem original",
      [x["num"] for x in linhas] == ["B", "D", "A", "C"],
      [x["num"] for x in linhas])


# ============================================================================
print("\n=== 4. A nuvem na tela ===")

TPL = io.open(os.path.join("frontend", "templates", "partials", "curation",
                           "curation_results.html"), encoding="utf-8").read()

check("a coluna do IPI XML mostra o alerta quando há devolvido",
      "item.xml_ipi_devol|default(0)|float) > 0.01" in TPL)

# Auditoria gravada ANTES desta mudança não tem os campos novos no result_data.
# Sem o `default(0)`, abrir um resultado antigo derruba a tela inteira com 500 —
# aconteceu ao escrever esta feature, e a suíte pegou.
import jinja2  # noqa: E402

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.join("frontend", "templates")), autoescape=True)
_tpl = _env.get_template("partials/curation/curation_results.html")
_antigo = {
    "resumo": {"notas_processadas": 1, "inconsistencias": 1, "informativos": 0},
    "ipi_issues": [{"key": "9" * 44, "num": "777", "emit_name": "F", "num_item": "1",
                    "descr": "P", "cfop": "1202", "cst_ipi": "50",
                    "reason": "x", "sped_val": 0.0, "xml_val": 50.0}],
}
try:
    _tpl.render(result=_antigo, task_id="t", tenant_id="x", request=None)
    _renderizou = True
    _erro = ""
except Exception as exc:  # noqa: BLE001 — é exatamente o que se quer detectar
    _renderizou = False
    _erro = f"{type(exc).__name__}: {exc}"
check("resultado antigo, sem os campos novos, ainda abre a tela", _renderizou, _erro)

for _campo in ("xml_ipi_proprio", "xml_ipi_devol"):
    check(f"{_campo} é lido com default no template",
          f"item.{_campo}|default(0)|float" in TPL)
check("a nuvem destrincha as duas parcelas",
      "vIPIDevol" in TPL and "vIPI)" in TPL)
check("a nuvem mostra o total conferido", "Total conferido" in TPL)
check("a nuvem é fixed, para escapar do recorte da tabela",
      "tip-nuvem hidden fixed" in TPL)
check("existe o script que posiciona a nuvem",
      ".tip-ancora" in TPL and "getBoundingClientRect" in TPL)
check("a nuvem cai para baixo quando não há espaço acima",
      "r.bottom + 8" in TPL)


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

sys.exit(1 if FALHAS else 0)
