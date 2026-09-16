# -*- coding: utf-8 -*-
"""Par SPED×XML aprovado vira regra — não se aprova o mesmo item duas vezes.

Pedido do Sandro em 31/08/2026: "guardar as regras e itens que forem casados e
aprovados no banco para que na próxima auditoria não precise aprovar nem mapear
novamente, vire um item certo".

O ciclo que este teste percorre inteiro, contra banco de verdade:

  1ª auditoria — o motor não casa o item, a IA sugere, a pessoa aprova.
                 O par é gravado como `ItemMatchRule`.
  2ª auditoria — o MESMO par aparece noutra nota. O motor casa sozinho, o item
                 não vai para a tela e a divergência sai marcada.

As travas:

- A chave é EXATA e tem cinco campos (empresa, fornecedor, cód. SPED, cód. XML,
  tenant). Curinga aqui casaria produtos de fornecedores diferentes — é a mesma
  proibição que vale para `CfopClassification`.
- A regra NÃO desfaz casamento determinístico: se o motor já casou aquela linha
  pelo código, a regra não é consultada.
- O status segue o maker-checker (`rule_status_for`): operador grava
  `pendente_revisao` e o motor ignora até um admin aprovar na Curadoria.
"""
import io
import os
import sys
import uuid

sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_item_match_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

if os.path.exists("_item_match_test.db"):
    os.remove("_item_match_test.db")

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

import backend.app.infrastructure.auth.supabase_client as sb
sb.get_supabase_client = lambda: type("C", (), {"auth": None})()
sb.get_supabase_admin_client = sb.get_supabase_client

from backend.app.infrastructure.database.session import engine as _eng
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit(f"ABORTADO: engine aponta para {_eng.url.get_backend_name()}.")

from backend.app.infrastructure.database.session import SessionLocal, engine
from backend.app.domain.models.base import Base
from backend.app.domain.models import rules, audit  # noqa: F401
from backend.app.domain.models.rules import ItemMatchRule
from backend.app.infrastructure.repositories.rules_repository import RulesRepository
from backend.app.services.audit_engine.cross_checker import (
    aplicar_pares_conhecidos, chave_item_match, levantar_nao_conferidos)

Base.metadata.create_all(bind=engine)

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
FORNEC = "55444333000122"
TID = str(uuid.uuid4())


# ============================================================================
print("\n=== 1. A chave é exata e normaliza o CNPJ ===")

check("CNPJ formatado e cru dão a mesma chave",
      chave_item_match(EMPRESA, "55.444.333/0001-22", "A1", "X1")
      == chave_item_match(EMPRESA, "55444333000122", "A1", "X1"))
check("o código é comparado sem caixa/espaço",
      chave_item_match(EMPRESA, FORNEC, " a1 ", "x1") == chave_item_match(EMPRESA, FORNEC, "A1", "X1"))
check("fornecedor diferente é chave diferente",
      chave_item_match(EMPRESA, "99888777000100", "A1", "X1")
      != chave_item_match(EMPRESA, FORNEC, "A1", "X1"))

# A chave da TABELA tem company_cnpj; a do mapa em memória precisa ter também.
# Um tenant que audita matriz e filial (CNPJs diferentes) teria as duas regras
# colidindo no mesmo dicionário, e a de uma empresa casaria item da outra.
FILIAL = "11222333000262"
check("empresa diferente é chave diferente (matriz x filial)",
      chave_item_match(FILIAL, FORNEC, "A1", "X1")
      != chave_item_match(EMPRESA, FORNEC, "A1", "X1"))
check("a chave tem os quatro campos",
      len(chave_item_match(EMPRESA, FORNEC, "A1", "X1")) == 4,
      chave_item_match(EMPRESA, FORNEC, "A1", "X1"))

# E na prática: a regra da matriz não pode casar item da filial.
_regra_matriz = ItemMatchRule(
    company_cnpj=EMPRESA, supplier_cnpj=FORNEC, sped_cod="M1", xml_cod="XM1",
    tenant_id=TID, sped_descr="x", xml_descr="x", origem="ia", confianca="alta",
    justificativa="x", status="aprovado", created_by="teste")
_mapa_matriz = {chave_item_match(EMPRESA, FORNEC, "M1", "XM1"): _regra_matriz}


# ============================================================================
print("\n=== 2. A regra casa o par, sem desfazer o determinístico ===")


def item_sped(cod, valor=100.0):
    return {"cod_item": cod, "vl_item": valor, "qtd": 1.0}


def item_xml(cod, valor=100.0):
    return {"cod_item": cod, "vl_item": valor, "descr": f"PROD {cod}", "vl_unit": valor,
            "qtd": 1.0}


regra = ItemMatchRule(
    company_cnpj=EMPRESA, supplier_cnpj=FORNEC, sped_cod="A1", xml_cod="X1",
    tenant_id=TID, sped_descr="OLEO HIDRAULICO", xml_descr="UNI L 10300 OLEO",
    origem="ia", confianca="alta", justificativa="Totais iguais.",
    status="aprovado", created_by="teste")
mapa = {chave_item_match(EMPRESA, FORNEC, "A1", "X1"): regra}

sped = [item_sped("A1")]
xml = [item_xml("X1")]
pares = {}
marcados = aplicar_pares_conhecidos(sped, xml, pares, EMPRESA, FORNEC, mapa)
check("o par conhecido é aplicado", pares.get(0) is xml[0], pares)
check("a linha é marcada com a regra", marcados.get(0) is regra, marcados)

# O motor já casou a linha 0 pelo código: a regra não pode mexer nela.
pares_ja = {0: xml[0]}
marcados = aplicar_pares_conhecidos(sped, xml, pares_ja, EMPRESA, FORNEC, mapa)
check("linha já casada pelo motor é deixada em paz", marcados == {}, marcados)
check("e o par determinístico permanece", pares_ja[0] is xml[0])

# Fornecedor diferente: a mesma dupla de códigos NÃO casa.
pares_outro = {}
aplicar_pares_conhecidos(sped, xml, pares_outro, EMPRESA, "99888777000100", mapa)
check("regra de outro fornecedor não vaza", pares_outro == {}, pares_outro)

# Um item do XML não pode servir a duas linhas do SPED.
mapa2 = {
    chave_item_match(EMPRESA, FORNEC, "A1", "X1"): regra,
    chave_item_match(EMPRESA, FORNEC, "A2", "X1"): regra,
}
pares3 = {}
aplicar_pares_conhecidos([item_sped("A1"), item_sped("A2")], [item_xml("X1")],
                         pares3, EMPRESA, FORNEC, mapa2)
check("o mesmo item do XML não é usado duas vezes", len(pares3) == 1, pares3)

check("sem regra nenhuma, nada acontece",
      aplicar_pares_conhecidos(sped, xml, {}, EMPRESA, FORNEC, {}) == {})

# Matriz x filial do MESMO tenant: a regra de uma não casa item da outra.
_pares_matriz = {}
aplicar_pares_conhecidos([item_sped("M1")], [item_xml("XM1")], _pares_matriz,
                         EMPRESA, FORNEC, _mapa_matriz)
check("a regra da matriz casa o item da matriz", len(_pares_matriz) == 1, _pares_matriz)

_pares_filial = {}
aplicar_pares_conhecidos([item_sped("M1")], [item_xml("XM1")], _pares_filial,
                         FILIAL, FORNEC, _mapa_matriz)
check("a regra da matriz NÃO casa item da filial", _pares_filial == {}, _pares_filial)


# ============================================================================
print("\n=== 3. Ciclo completo: aprova numa auditoria, casa na seguinte ===")

NS = "http://www.portalfiscal.inf.br/nfe"
CHAVE = "7" * 44


def montar_xml(caminho, cod_xml="X1", descr="UNI L 10300 OLEO LUBRIFICANTE"):
    io.open(caminho, "w", encoding="utf-8").write(f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="{NS}"><NFe><infNFe Id="NFe{CHAVE}">
 <ide><nNF>500</nNF><serie>1</serie></ide>
 <emit><CNPJ>{FORNEC}</CNPJ><xNome>FORNECEDOR TESTE</xNome>
   <enderEmit><UF>ES</UF></enderEmit></emit>
 <dest><CNPJ>{EMPRESA}</CNPJ><enderDest><UF>ES</UF></enderDest></dest>
 <det nItem="1">
  <prod><cProd>{cod_xml}</cProd><xProd>{descr}</xProd><NCM>27101932</NCM>
    <CFOP>5102</CFOP><qCom>5.0000</qCom><vUnCom>355.02</vUnCom>
    <vProd>1775.09</vProd></prod>
  <imposto><ICMS><ICMS00><CST>00</CST><vBC>1775.09</vBC><pICMS>12.00</pICMS>
    <vICMS>213.01</vICMS></ICMS00></ICMS></imposto>
 </det>
</infNFe></NFe></nfeProc>""")


def montar_sped(caminho, cod_sped="A1"):
    v = lambda x: f"{x:.2f}".replace(".", ",")  # noqa: E731
    linhas = [
        f"|0000|017|0|01072026|31072026|EMPRESA TESTE LTDA|{EMPRESA}|ES|||",
        f"|0150|9001|FORNECEDOR TESTE|1058|{FORNEC}||||||",
        f"|0200|{cod_sped}|OLEO HIDRAULICO 1LT FTA|||UN|00|||||",
        f"|C100|0|1|9001|55|00|1|500|{CHAVE}|18072026|18072026|"
        f"{v(1775.09)}|0|0,00|0,00|{v(1775.09)}|0|0,00|0,00|0,00|0,00|0,00|0,00|0,00|0,00|",
        # 120 unidades a 14,79 — a empresa desmembrou a caixa. Código, unitário e
        # quantidade divergem do XML; só o total (1775,09) sobrevive.
        "|" + "|".join([
            "C170", "1", cod_sped, "", "120,00", "UN", v(1775.09), "0,00",
            "0", "00", "1102", "", v(1775.09), "12,00", v(213.01),
            "0,00", "0,00", "0,00", "0", "", "", "0,00", "0,00", "0,00",
            "50", "", "1,65", "", "", "0,00",
            "50", "", "7,60", "", "", "0,00", "", "0,00",
        ]) + "|",
    ]
    io.open(caminho, "w", encoding="latin1").write("\n".join(linhas) + "\n")


import shutil  # noqa: E402
import tempfile  # noqa: E402

BASE = tempfile.mkdtemp(prefix="synax_item_match_")
xml_dir = os.path.join(BASE, "xmls")
os.makedirs(xml_dir)
sped_path = os.path.join(BASE, "sped.txt")
montar_sped(sped_path)
montar_xml(os.path.join(xml_dir, f"{CHAVE}.xml"))

# --- 1ª auditoria: sem regra, o item cai na tela de confirmação ---
notas = levantar_nao_conferidos(sped_path, xml_dir, ignore_saidas=True)
check("sem regra, o item é oferecido para confirmação", len(notas) == 1, notas)
if notas:
    check("a nota leva o CNPJ da empresa",
          notas[0].get("company_cnpj") == EMPRESA, notas[0].get("company_cnpj"))
    check("a nota leva o CNPJ do fornecedor",
          notas[0].get("supplier_cnpj") == FORNEC, notas[0].get("supplier_cnpj"))
    check("a descrição do SPED vem do 0200",
          "OLEO HIDRAULICO" in notas[0]["sped_itens"][0]["descr"],
          notas[0]["sped_itens"][0]["descr"])

# --- a pessoa aprovou: o par vira regra no banco ---
db = SessionLocal()
RulesRepository(db, TID).item_match_rules.add(ItemMatchRule(
    company_cnpj=EMPRESA, supplier_cnpj=FORNEC, sped_cod="A1", xml_cod="X1",
    tenant_id=TID, sped_descr="OLEO HIDRAULICO 1LT FTA",
    xml_descr="UNI L 10300 OLEO LUBRIFICANTE", origem="ia", confianca="alta",
    justificativa="Valores totais idênticos.", status="aprovado", created_by="admin"))
db.commit()

gravadas = db.query(ItemMatchRule).filter(ItemMatchRule.tenant_id == TID).all()
check("a regra ficou no banco", len(gravadas) == 1, gravadas)

# --- 2ª auditoria: o mesmo par, agora resolvido pela regra ---
conhecidos = {
    chave_item_match(r.company_cnpj, r.supplier_cnpj, r.sped_cod, r.xml_cod): r
    for r in RulesRepository(db, TID).item_match_rules.get_all()
    if getattr(r, "status", "aprovado") == "aprovado"
}
check("o mapa de regras foi montado", len(conhecidos) == 1, conhecidos)

notas2 = levantar_nao_conferidos(sped_path, xml_dir, ignore_saidas=True,
                                 pares_conhecidos=conhecidos)
check("com a regra, o item NÃO volta para a tela", notas2 == [], notas2)

# --- regra do operador (pendente_revisao) não vale até o admin aprovar ---
db.query(ItemMatchRule).filter(ItemMatchRule.tenant_id == TID).update(
    {"status": "pendente_revisao"})
db.commit()
pendentes = {
    chave_item_match(r.company_cnpj, r.supplier_cnpj, r.sped_cod, r.xml_cod): r
    for r in RulesRepository(db, TID).item_match_rules.get_all()
    if getattr(r, "status", "aprovado") == "aprovado"
}
check("regra pendente de revisão não entra no mapa", pendentes == {}, pendentes)
notas3 = levantar_nao_conferidos(sped_path, xml_dir, ignore_saidas=True,
                                 pares_conhecidos=pendentes)
check("e o item volta para a tela, como manda o maker-checker",
      len(notas3) == 1, notas3)

db.close()
shutil.rmtree(BASE, ignore_errors=True)


# ============================================================================
print("\n=== 4. A gravação da regra, pela rota ===")

ROTA = io.open(os.path.join("backend", "app", "api", "routes", "uploads.py"),
               encoding="utf-8").read()
bloco = ROTA[ROTA.find("def _gravar_regras_de_par"):]
bloco = bloco[:bloco.find('@router.post("/api/audit/{task_id}/aplicar-ia"')]

check("o status segue o maker-checker", "rule_status_for(profile)" in bloco)
check("sugestão sem CNPJ ou sem código é pulada",
      "if not (company and supplier and sped_cod and xml_cod):" in bloco, bloco[:1500])
check("o CNPJ é normalizado para só dígitos", "RE_SO_DIGITOS.sub" in bloco)

# Era um SELECT por sugestão. Cada ida ao Supabase custa ~140ms de rede: com 60
# itens o laço gastava ~8s antes da primeira gravação, e a tela ficava muda esse
# tempo todo — foi metade da razão de o botão parecer não fazer nada.
check("o que já existe é lido em UMA consulta, não uma por sugestão",
      "ja_existem = {" in bloco and "db_session.get(" not in bloco, bloco[:2000])
check("regra repetida não é gravada de novo", "if chave in ja_existem" in bloco)
check("sugestão repetida no mesmo envio também não duplica",
      "chave in vistos" in bloco and "vistos.add(chave)" in bloco)
check("falha ao gravar não derruba a auditoria",
      "logger.exception" in bloco and "rollback" in bloco)
check("só grava o que foi aprovado",
      "_gravar_regras_de_par(db_session, tenant_id, profile," in ROTA
      and "if selecionados:" in ROTA)

MOTOR = io.open(os.path.join("backend", "app", "services", "audit_engine",
                             "cross_checker.py"), encoding="utf-8").read()
check("o motor carrega só as regras aprovadas",
      'if getattr(r, "status", "aprovado") == "aprovado"' in MOTOR)
check("o motor aplica a regra antes da sugestão da IA",
      MOTOR.find("if not sped_only_mode and item_match_map:")
      < MOTOR.find("if not sped_only_mode and pares_ia:"))
check("o par vindo de regra também sai marcado na tela",
      '"ia_confianca": "aprovado"' in MOTOR)
check("a marca explica que veio de aprovação anterior",
      "Par aprovado em auditoria anterior" in MOTOR)


# ============================================================================
print("\n=== 4b. ISOLAMENTO: a regra de uma empresa não vaza para outra ===")

# É a trava mais importante da tabela. Um par aprovado pela empresa A não pode,
# em hipótese alguma, casar item na auditoria da empresa B — seriam produtos de
# outra escrituração decidindo crédito que ninguém de B aprovou.
TID_A, TID_B = str(uuid.uuid4()), str(uuid.uuid4())
db = SessionLocal()

RulesRepository(db, TID_A).item_match_rules.add(ItemMatchRule(
    company_cnpj=EMPRESA, supplier_cnpj=FORNEC, sped_cod="SEGREDO",
    xml_cod="XSEGREDO", tenant_id=TID_A, sped_descr="ITEM DA EMPRESA A",
    xml_descr="ITEM DA EMPRESA A", origem="ia", confianca="alta",
    justificativa="a", status="aprovado", created_by="admin"))

regras_a = RulesRepository(db, TID_A).item_match_rules.get_all()
regras_b = RulesRepository(db, TID_B).item_match_rules.get_all()
check("a empresa A enxerga a própria regra", len(regras_a) == 1, len(regras_a))
check("a empresa B NÃO enxerga a regra da A", regras_b == [], regras_b)

# O mapa que o motor monta para B tem de sair vazio.
mapa_b = {
    chave_item_match(r.company_cnpj, r.supplier_cnpj, r.sped_cod, r.xml_cod): r
    for r in RulesRepository(db, TID_B).item_match_rules.get_all()
}
check("o mapa do motor da empresa B sai vazio", mapa_b == {}, mapa_b)

pares_b = {}
aplicar_pares_conhecidos([item_sped("SEGREDO")], [item_xml("XSEGREDO")],
                         pares_b, EMPRESA, FORNEC, mapa_b)
check("e com ele nenhum item de B é casado", pares_b == {}, pares_b)

# O tenant_id é injetado pelo repositório: mesmo mandando o de outra empresa no
# objeto, ele é sobrescrito pelo escopo do repositório.
forjada = ItemMatchRule(
    company_cnpj=EMPRESA, supplier_cnpj=FORNEC, sped_cod="FORJADO",
    xml_cod="XFORJADO", tenant_id=TID_A, sped_descr="x", xml_descr="x",
    origem="ia", confianca="alta", justificativa="x", status="aprovado",
    created_by="teste")
RulesRepository(db, TID_B).item_match_rules.add_all([forjada])
check("tenant_id forjado no objeto é sobrescrito pelo repositório",
      forjada.tenant_id == TID_B, forjada.tenant_id)
check("e a regra forjada não aparece para a empresa A",
      all(r.sped_cod != "FORJADO"
          for r in RulesRepository(db, TID_A).item_match_rules.get_all()))

# get_all() também filtra status: regra pendente não vaza para o motor.
db.query(ItemMatchRule).filter(ItemMatchRule.tenant_id == TID_A).update(
    {"status": "pendente_revisao"})
db.commit()
check("regra pendente some do get_all(), sem depender de filtro no chamador",
      RulesRepository(db, TID_A).item_match_rules.get_all() == [])

db.query(ItemMatchRule).filter(ItemMatchRule.tenant_id.in_([TID_A, TID_B])).delete(
    synchronize_session=False)
db.commit()
db.close()

BASE_REPO = io.open(os.path.join("backend", "app", "infrastructure", "repositories",
                                 "base_repository.py"), encoding="utf-8").read()
check("get_all filtra por tenant no SQL",
      "self.model_class.tenant_id == self.tenant_id" in BASE_REPO)
check("add_all injeta o tenant do repositório",
      "obj.tenant_id = self.tenant_id" in BASE_REPO)

MOTOR_ESCOPO = MOTOR[MOTOR.find("item_match_map = {"):]
MOTOR_ESCOPO = MOTOR_ESCOPO[:MOTOR_ESCOPO.find("cte_classifications")]
check("o motor monta o mapa pelo repositório com escopo de tenant",
      "repo.item_match_rules.get_all()" in MOTOR_ESCOPO, MOTOR_ESCOPO[:400])

ROTA_ESCOPO = ROTA[ROTA.find("conhecidos = {"):]
ROTA_ESCOPO = ROTA_ESCOPO[:ROTA_ESCOPO.find("notas = levantar_nao_conferidos")]
check("a pré-análise também usa o repositório com escopo de tenant",
      "RulesRepository(bg_session, tenant_id).item_match_rules.get_all()" in ROTA_ESCOPO,
      ROTA_ESCOPO[:400])


# ============================================================================
print("\n=== 5. A Curadoria conhece o tipo (senão a regra do operador some) ===")

# Sem isto, a regra que um operador aprova fica em `pendente_revisao` sem tela
# onde ser liberada — e o item volta a ser perguntado todo mês, que é o oposto
# do pedido.
from backend.app.main import _rule_models, RULE_KEY_FIELDS, EDITABLE_FIELDS  # noqa: E402

modelos = _rule_models()
check("o tipo está registrado na Curadoria", "item_match" in modelos, list(modelos))
check("aponta para o model certo", modelos.get("item_match") is ItemMatchRule)
check("a chave da Curadoria é a chave real da tabela",
      RULE_KEY_FIELDS.get("item_match")
      == ('tenant_id', 'company_cnpj', 'supplier_cnpj', 'sped_cod', 'xml_cod'),
      RULE_KEY_FIELDS.get("item_match"))
for campo in RULE_KEY_FIELDS["item_match"]:
    check(f"o model tem o campo de chave {campo!r}", hasattr(ItemMatchRule, campo))
check("nada é editável inline (a decisão é aprovar ou descartar)",
      "item_match" not in EDITABLE_FIELDS)

ABAS = io.open(os.path.join("frontend", "templates", "partials", "curation",
                            "_tabs.html"), encoding="utf-8").read()
check("existe a aba na tela", "'item_match'" in ABAS)

LINHA = io.open(os.path.join("frontend", "templates", "partials", "curation",
                             "_row.html"), encoding="utf-8").read()
check("a linha mostra as duas descrições, que é o que se julga",
      "r.sped_descr" in LINHA and "r.xml_descr" in LINHA)
check("a linha mostra a confiança", "r.confianca" in LINHA)
check("a linha identifica o fornecedor", "r.supplier_cnpj" in LINHA)


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

try:
    engine.dispose()
    os.remove("_item_match_test.db")
except OSError:
    pass

sys.exit(1 if FALHAS else 0)
