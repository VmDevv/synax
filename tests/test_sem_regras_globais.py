"""Não existe regra global no ICMS nem no IPI.

Decisão do escritório (25/08/2026): ICMS e IPI seguem a mesma linha do CFOP —
regra é do tenant, com chave exata, sem curinga e sem chute do motor. Três
mecanismos violavam isso e esta suíte trava os três:

1. `is_global=True` / tenant fantasma "GLOBAL". O escopo era
   `tenant_id == meu OU is_global`, então 26 regras de ICMS e 6 de IPI semeadas
   sob um tenant que não é de ninguém decidiam crédito em TODAS as empresas.

2. Curinga na chave. O motor emitia `cst_ipi: "*"` na tela de curadoria e
   gravava isso no banco; como o mapa de IPI era só `{cfop: allow}`, o CST nem
   era consultado e uma resposta valia para qualquer CST daquele CFOP. No CFOP,
   a rota do admin gravava "TODOS" em três campos da chave — regra que o motor
   nunca acha, porque a busca é exata.

3. Chute do motor. Uma lista de 10 CFOPs fixa no código decidia glosa quando não
   havia regra: acusação de divergência que nenhum admin aprovou.
"""
import os, sys, uuid, types, io, json, tempfile
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_global_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_global_test.db"):
    os.remove("_global_test.db")

from sqlalchemy import types as _satypes
import sqlalchemy.dialects.postgresql as pg
class _UUIDShim(_satypes.TypeDecorator):
    impl = _satypes.Uuid; cache_ok = True
    def __init__(self, *a, **kw):
        kw.pop('as_uuid', None); super().__init__()
    def process_bind_param(self, value, dialect):
        return uuid.UUID(value) if isinstance(value, str) else value
pg.UUID = _UUIDShim

import backend.app.infrastructure.auth.supabase_client as sb
TOKENS = {}
class _Auth:
    def get_user(self, token):
        u = TOKENS.get(token.replace("Bearer ", "").strip())
        if not u: raise Exception("invalid")
        return types.SimpleNamespace(user=u)
class _Client:
    auth = _Auth()
sb.get_supabase_client = lambda: _Client()
sb.get_supabase_admin_client = lambda: _Client()
import backend.app.api.dependencies.auth as dep_auth
dep_auth.get_supabase_client = lambda: _Client()

from backend.app.infrastructure.database.session import engine as _eng
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit("ABORTADO: engine nao e SQLite")

import backend.app.main as m
from backend.app.infrastructure.database.session import SessionLocal, engine
from backend.app.domain.models.base import Base
from backend.app.domain.models.auth_models import Tenant, UserProfile, UserTenant
from backend.app.domain.models.audit import AuditTask
from backend.app.domain.models.rules import (
    CfopCreditRule, CfopClassification, CteCfopClassification,
    IpiCreditRule, PisCofinsCreditRule, IcmsAnticipationRule,
)
from backend.app.infrastructure.repositories.rules_repository import RulesRepository
from backend.app.services.audit_engine.cross_checker import execute_audit, permite_credito_icms
from fastapi.testclient import TestClient

Base.metadata.create_all(bind=engine)

db = SessionLocal()
ut = uuid.uuid4(); t = str(ut)
db.add(Tenant(id=ut, name="Empresa X", cnpj="34449753000109"))
u_adm, u_ope = uuid.uuid4(), uuid.uuid4()
db.add_all([
    UserProfile(id=u_adm, tenant_id=None, role="admin", name="Admin"),
    UserProfile(id=u_ope, tenant_id=ut, role="operador", name="Operador"),
])
db.add(UserTenant(user_id=u_ope, tenant_id=ut))

# O tenant fantasma que o seeder usava, com uma regra de cada tipo.
GLOBAL = "GLOBAL"
db.add_all([
    CfopCreditRule(cfop="1556", tenant_id=GLOBAL, allow_credit=False,
                   description="global ICMS", status="aprovado", is_global=True),
    IpiCreditRule(cfop="1101", cst_expected="00", tenant_id=GLOBAL, allow_credit=True,
                  description="global IPI", status="aprovado", is_global=True),
    PisCofinsCreditRule(cfop="1101", cst_expected="50", tenant_id=GLOBAL, allow_credit=True,
                        description="global PIS", status="aprovado", is_global=True),
    IcmsAnticipationRule(uf_origem="PR", uf_destino="SP", cfop="2102", tenant_id=GLOBAL,
                         mva_percentage=0.0, aliquota_interna_percentage=18.0,
                         aliquota_interestadual_percentage=12.0,
                         description="global antecipacao", status="aprovado", is_global=True),
    CfopClassification(company_cnpj="1", supplier_cnpj="2", supplier_cfop="5102",
                       product_code="P", tenant_id=GLOBAL, entry_cfop="1102",
                       status="aprovado", is_global=True),
    CteCfopClassification(company_cnpj="1", supplier_cfop="5353", emitter_uf="SP",
                          tenant_id=GLOBAL, entry_cfop="1353",
                          status="aprovado", is_global=True),
])
db.commit(); db.close()

TOKENS["adm"] = types.SimpleNamespace(id=str(u_adm), email="admin@synax.com")
TOKENS["ope"] = types.SimpleNamespace(id=str(u_ope), email="oper@synax.com")

from backend.app.api.dependencies.db import get_rls_db as _real_rls
def _fake_rls():
    s = SessionLocal()
    try: yield s
    finally: s.close()
m.app.dependency_overrides[_real_rls] = _fake_rls

c = TestClient(m.app, raise_server_exceptions=False)
def as_(tok):
    c.cookies.clear(); c.cookies.set("access_token", f"Bearer {tok}")
    return c

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  OK  " if cond else " FALHA") + f" | {name}" + (f"  [{detail}]" if detail and not cond else ""))


# ============================================================================
print("\n=== 1. Regra is_global nao vaza para nenhum tenant ===")
s = SessionLocal()
repo = RulesRepository(s, t)
check("ICMS: regra global invisivel",
      [r.cfop for r in repo.credit_rules.get_all()] == [], f"{[r.cfop for r in repo.credit_rules.get_all()]}")
check("IPI: regra global invisivel", repo.ipi_credit_rules.get_all() == [])
check("PIS/COFINS: regra global invisivel", repo.pis_cofins_credit_rules.get_all() == [])
check("Antecipacao: regra global invisivel", repo.icms_anticipation_rules.get_all() == [])
check("CFOP: regra global invisivel", repo.cfop_classifications.get_all() == [])
check("CT-e: regra global invisivel", repo.cte_classifications.get_all() == [])
check("consulta unitaria de ICMS tambem nao acha a global",
      repo.get_credit_rule("1556") is None)
check("consulta unitaria de CFOP tambem nao acha a global",
      repo.get_cfop_classification("1", "2", "5102", "P") is None)
check("consulta unitaria de CT-e tambem nao acha a global",
      repo.get_cte_classification("1", "5353", "SP") is None)

# O dono continua enxergando o que e dele: o filtro e por tenant, nao um bloqueio cego.
repo_g = RulesRepository(s, GLOBAL)
check("o proprio tenant dono continua enxergando a regra dele",
      [r.cfop for r in repo_g.credit_rules.get_all()] == ["1556"])
s.close()


# ============================================================================
print("\n=== 2. Motor nao chuta: sem regra, nao alega nada ===")
check("sem regra devolve None, nao False", permite_credito_icms("1556", {}) is None)
check("sem regra devolve None, nao True", permite_credito_icms("1102", {}) is None)
check("com regra True, libera", permite_credito_icms("1102", {"1102": True}) is True)
check("com regra False, bloqueia", permite_credito_icms("1102", {"1102": False}) is False)

fonte_motor = io.open("backend/app/services/audit_engine/cross_checker.py", encoding="utf-8").read()
check("a lista fixa de CFOPs sem credito saiu do codigo",
      "DEFAULT_NO_CREDIT_CFOPS" not in fonte_motor)
check("o motor nao grava palpite no mapa de regras em memoria",
      "icms_rules_map[sped_cfop] =" not in fonte_motor)


# ============================================================================
print("\n=== 3. Chave do IPI e (CFOP, CST), nao so o CFOP ===")
check("o mapa de IPI usa a chave inteira",
      '(r.cfop, str(r.cst_expected or ""))' in fonte_motor)
check("a consulta de IPI usa a chave inteira",
      'ipi_rules_map.get((cfop_regra, sped_ipi_cst))' in fonte_motor)
check("o motor nao emite mais cst_ipi curinga", '"cst_ipi": "*"' not in fonte_motor)

check("a chave da antecipacao inclui a UF de destino",
      "(r.uf_origem, r.uf_destino, r.cfop)" in fonte_motor)
check("a consulta da antecipacao usa a UF de destino",
      "anticipation_rules_map.get((emit_uf, dest_uf, sped_cfop))" in fonte_motor)

fonte_xml = io.open("backend/app/services/audit_engine/xml_parser.py", encoding="utf-8").read()
check("o parser de NF-e le a UF do destinatario", '"dest_uf": dest_uf' in fonte_xml)


# ============================================================================
print("\n=== 4. Rotas recusam curinga em vez de gravar regra morta ===")
r = as_("adm").post("/api/mapping/ipi_rule",
                    data={"cfop": "5102", "cst_ipi": "*", "allow_credit": "true", "tenant_id": t})
check("IPI com CST '*' responde 200 (htmx nao troca alvo em 4xx)", r.status_code == 200,
      f"status={r.status_code}")
check("IPI com CST '*' nao grava", SessionLocal().query(IpiCreditRule).filter_by(
    tenant_id=t, cfop="5102").first() is None)

r = as_("adm").post("/api/mapping/cfop",
                    data={"cfop": "5102", "mapped_cfop": "1102", "tenant_id": t,
                          "cnpj_emitente": "99999999999999", "company_cnpj": "11111111111111",
                          "product_code": "*"})
check("CFOP com product_code '*' responde 200", r.status_code == 200, f"status={r.status_code}")
s = SessionLocal()
check("CFOP com product_code '*' nao grava",
      s.query(CfopClassification).filter_by(tenant_id=t, supplier_cfop="5102").first() is None)
s.close()
# A linha de recusa tambem precisa ficar marcada como cfop-row -- senao a
# paginacao da aba "Novos CFOPs" para de contar essa linha e ela fica presa
# visivel em toda pagina, igual ao bug da linha de sucesso (14/09/2026).
check("a linha de recusa mantem cfop-row (mesma razao da linha de sucesso)",
      'cfop-row' in r.text and "data-cfop='5102'" in r.text, r.text)

r = as_("adm").post("/api/mapping/cfop",
                    data={"cfop": "5103", "mapped_cfop": "1102", "tenant_id": t,
                          "cnpj_emitente": "99999999999999", "company_cnpj": "11111111111111"})
check("CFOP sem product_code responde 200", r.status_code == 200, f"status={r.status_code}")
s = SessionLocal()
check("CFOP sem product_code nao grava",
      s.query(CfopClassification).filter_by(tenant_id=t, supplier_cfop="5103").first() is None)
s.close()
check("a linha de recusa (sem product_code) mantem cfop-row",
      'cfop-row' in r.text and "data-cfop='5103'" in r.text, r.text)

# A rota do admin gravava "TODOS" em tres campos da chave.
r = as_("adm").post("/admin/rules/cfop/add",
                    data={"tenant_id": t, "cfop_origem": "5405", "cfop_entrada": "1403"})
s = SessionLocal()
check("admin: regra de CFOP sem os campos da chave nao grava",
      s.query(CfopClassification).filter_by(tenant_id=t, supplier_cfop="5405").first() is None,
      f"status={r.status_code}")
check("nenhuma regra com 'TODOS' na chave existe",
      s.query(CfopClassification).filter_by(company_cnpj="TODOS").first() is None)
s.close()

r = as_("adm").post("/admin/rules/cfop/add",
                    data={"tenant_id": t, "cfop_origem": "5406", "cfop_entrada": "1403",
                          "company_cnpj": "11111111111111", "supplier_cnpj": "99999999999999",
                          "product_code": "PROD-Z"})
s = SessionLocal()
regra_ok = s.query(CfopClassification).filter_by(tenant_id=t, supplier_cfop="5406").first()
check("admin: com a chave completa, grava normalmente", regra_ok is not None,
      f"status={r.status_code}")
check("e grava os valores reais, nao 'TODOS'",
      regra_ok is not None and regra_ok.company_cnpj == "11111111111111"
      and regra_ok.product_code == "PROD-Z")
s.close()

r = as_("adm").post("/admin/rules/ipi/add",
                    data={"tenant_id": t, "cfop": "1101", "cst_expected": "", "allow_credit": "on"})
s = SessionLocal()
check("admin: regra de IPI sem CST nao grava",
      s.query(IpiCreditRule).filter_by(tenant_id=t, cfop="1101").first() is None,
      f"status={r.status_code}")
s.close()


# ============================================================================
print("\n=== 5. A tela nao manda mais curinga ===")
tpl = io.open("frontend/templates/partials/curation/curation_results.html", encoding="utf-8").read()
check("a curadoria nao envia product_code '*'",
      'or "*"' not in tpl and "or '*'" not in tpl)
modal = io.open("frontend/templates/partials/admin/modals/modal_add_cfop.html", encoding="utf-8").read()
for campo in ("company_cnpj", "supplier_cnpj", "product_code"):
    check(f"o modal de nova regra de CFOP pede {campo}", f'name="{campo}"' in modal)

fonte_seed = io.open("scripts/seed_tax_rules.py", encoding="utf-8").read()
codigo_seed = [l for l in fonte_seed.splitlines() if not l.strip().startswith("#")]
check("o seeder nao cria mais regra global",
      not any("is_global=True" in l for l in codigo_seed))
check("o seeder nao usa mais o tenant fantasma", 'GLOBAL_TENANT_ID = "GLOBAL"' not in fonte_seed)


# ============================================================================
print("\n=== 6. Ponta a ponta: sem regra o motor manda para a curadoria e cala ===")
CHAVE = "35230134449753000109550010000010011000010012"

XML = """<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe%s">
 <ide><nNF>1001</nNF><serie>1</serie></ide>
 <emit><CNPJ>86527934000100</CNPJ><xNome>FORNECEDOR</xNome>
  <enderEmit><UF>PR</UF></enderEmit></emit>
 <dest><CNPJ>34449753000109</CNPJ><enderDest><UF>SP</UF></enderDest></dest>
 <det nItem="1"><prod><cProd>P1</cProd><xProd>PARAFUSO</xProd><CFOP>5102</CFOP>
   <NCM>73181500</NCM><vProd>100.00</vProd><qCom>1</qCom><vUnCom>100.00</vUnCom></prod>
  <imposto><ICMS><ICMS00><CST>00</CST><vBC>100.00</vBC><pICMS>18.00</pICMS>
    <vICMS>18.00</vICMS></ICMS00></ICMS>
   <IPI><cEnq>999</cEnq><IPITrib><CST>00</CST><vIPI>5.00</vIPI></IPITrib></IPI></imposto></det>
 <total><ICMSTot><vNF>100.00</vNF><vST>0.00</vST></ICMSTot></total>
</infNFe></NFe></nfeProc>""" % CHAVE


def montar_sped(vl_icms_sped, vl_ipi_sped):
    fd, p = tempfile.mkstemp(suffix=".txt"); os.close(fd)
    linhas = [
        "|0000|017|0|01072026|31072026|EMPRESA X|34449753000109|SP|||",
        "|0150|F1|FORNECEDOR|1058|86527934000100||||||",
        f"|C100|0|1|F1|55|00|1|1001|{CHAVE}|18072026|18072026|100,00|0|0,00|0,00|"
        "100,00|0|0,00|0,00|0,00|18,00|0,00|0,00|0,00|0,00|",
        "|C170|1|P1|PARAFUSO|1|UN|100,00|0,00|0|00|1102||100,00|18,00|"
        f"{str(vl_icms_sped).replace('.', ',')}|0,00|0,00|0,00|0|00||0,00|0,00|"
        f"{str(vl_ipi_sped).replace('.', ',')}|50|100,00|1,65|1,65|50|100,00|7,60|7,60|",
    ]
    io.open(p, "w", encoding="latin1").write("\n".join(linhas) + "\n")
    return p


def rodar(regras):
    """Roda uma auditoria com as regras dadas e devolve o result_data."""
    xml_dir = tempfile.mkdtemp(prefix="synax_global_")
    io.open(os.path.join(xml_dir, f"{CHAVE}.xml"), "w", encoding="utf-8").write(XML)
    sped = montar_sped(18.00, 5.00)

    s = SessionLocal()
    tid, task_id = str(uuid.uuid4()), str(uuid.uuid4())
    r = RulesRepository(s, tid)
    r.audit_tasks.add(AuditTask(id=task_id, status="pendente", user_id="teste", file_name="sped.txt"))
    for regra in regras:
        regra.tenant_id = tid
        s.add(regra)
    s.commit()
    execute_audit(task_id, tid, sped, xml_dir, s, sped_only_mode=False, ignore_saidas=True)
    task = RulesRepository(s, tid).audit_tasks.get_by_id(task_id)
    dados = json.loads(task.result_data or "{}")
    s.close()

    import shutil
    shutil.rmtree(xml_dir, ignore_errors=True)
    if os.path.exists(sped):
        os.remove(sped)
    return dados


sem_regra = rodar([])
check("sem regra de ICMS, o CFOP vira pendencia",
      "1102" in sem_regra.get("unmapped_icms_cfops", []),
      f"{sem_regra.get('unmapped_icms_cfops')}")
check("sem regra de ICMS, NENHUMA divergencia e alegada",
      sem_regra.get("icms_issues", []) == [], f"{sem_regra.get('icms_issues')}")
check("sem regra de IPI, o par (CFOP, CST) vira pendencia",
      any(x.get("cfop") == "1102" and x.get("cst_ipi") == "00"
          for x in sem_regra.get("unmapped_ipi_rules", [])),
      f"{sem_regra.get('unmapped_ipi_rules')}")
check("a pendencia de IPI leva o CST real, nunca '*'",
      all(x.get("cst_ipi") != "*" for x in sem_regra.get("unmapped_ipi_rules", [])))
check("sem regra de IPI, NENHUMA divergencia e alegada",
      sem_regra.get("ipi_issues", []) == [], f"{sem_regra.get('ipi_issues')}")

# A mesma nota, agora com regra do proprio tenant glosando os dois impostos.
com_regra = rodar([
    CfopCreditRule(cfop="1102", tenant_id="", allow_credit=False,
                   description="teste", status="aprovado", is_global=False),
    IpiCreditRule(cfop="1102", cst_expected="00", tenant_id="", allow_credit=False,
                  description="teste", status="aprovado", is_global=False),
])
check("com regra do tenant, a glosa de ICMS aparece",
      any("nao permite" in i.get("reason", "").replace("ã", "a")
          for i in com_regra.get("icms_issues", [])),
      f"{com_regra.get('icms_issues')}")
check("com regra do tenant, a glosa de IPI aparece",
      any("nao permite" in i.get("reason", "").replace("ã", "a")
          for i in com_regra.get("ipi_issues", [])),
      f"{com_regra.get('ipi_issues')}")
check("e o CFOP sai da fila de pendencias de ICMS",
      "1102" not in com_regra.get("unmapped_icms_cfops", []))

# Duas regras de IPI para o MESMO CFOP com CSTs diferentes tem que coexistir:
# antes, o mapa era {cfop: allow} e a segunda apagava a primeira.
dois_cst = rodar([
    CfopCreditRule(cfop="1102", tenant_id="", allow_credit=True,
                   description="teste", status="aprovado", is_global=False),
    IpiCreditRule(cfop="1102", cst_expected="49", tenant_id="", allow_credit=False,
                  description="CST que nao e o da nota", status="aprovado", is_global=False),
    IpiCreditRule(cfop="1102", cst_expected="00", tenant_id="", allow_credit=True,
                  description="CST da nota", status="aprovado", is_global=False),
])
check("a regra do CST certo (00) e que vale, nao a do CST 49",
      dois_cst.get("ipi_issues", []) == [], f"{dois_cst.get('ipi_issues')}")
check("com regra para o CST da nota, nada fica pendente no IPI",
      dois_cst.get("unmapped_ipi_rules", []) == [], f"{dois_cst.get('unmapped_ipi_rules')}")


# ============================================================================
engine.dispose()
if os.path.exists("_global_test.db"):
    try: os.remove("_global_test.db")
    except Exception: pass

fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
if fails:
    print("FALHAS:")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("OK")
