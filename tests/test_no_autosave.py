"""Prova que: (1) o motor nao grava regra nenhuma no banco; (2) o status ao
confirmar respeita o papel; (3) a descricao real do produto e capturada."""
import os, sys, uuid, types, json, inspect
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_nas_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_nas_test.db"):
    os.remove("_nas_test.db")

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
from backend.app.domain.models.rules import CfopClassification, CfopCreditRule, CteCfopClassification
from backend.app.core.authz import rule_status_for
from fastapi.testclient import TestClient

Base.metadata.create_all(bind=engine)
db = SessionLocal()
ut = uuid.uuid4(); t = str(ut)
db.add(Tenant(id=ut, name="Empresa X", cnpj="11111111111111"))
u_adm, u_ope, u_cli = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
db.add_all([
    UserProfile(id=u_adm, tenant_id=None, role="admin", name="Admin"),
    UserProfile(id=u_ope, tenant_id=ut, role="operador", name="Operador"),
    UserProfile(id=u_cli, tenant_id=ut, role="cliente", name="Cliente"),
])
db.add(UserTenant(user_id=u_ope, tenant_id=ut))
db.commit(); db.close()
TOKENS["adm"] = types.SimpleNamespace(id=str(u_adm), email="admin@synax.com")
TOKENS["ope"] = types.SimpleNamespace(id=str(u_ope), email="oper@synax.com")
TOKENS["cli"] = types.SimpleNamespace(id=str(u_cli), email="cli@synax.com")

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
    results.append((name, cond, detail))
    print(("  OK  " if cond else " FALHA") + f" | {name}" + (f"  [{detail}]" if detail and not cond else ""))

print("\n=== 1. Motor nao persiste heuristica ===")
from backend.app.services.audit_engine import cross_checker as cc
src = inspect.getsource(cc)
check("nenhum merge() de regra no motor", "db_session.merge(" not in src)
check("nao instancia CfopClassification no motor", "CfopClassification(" not in src)
check("nao instancia CfopCreditRule no motor", "CfopCreditRule(" not in src)
check("nao instancia CteCfopClassification no motor", "CteCfopClassification(" not in src)
check("acumuladores de regra removidos",
      "pending_cfop_rules" not in src and "pending_icms_rules" not in src and "pending_cte_rules" not in src)
check("log de notas nao lancadas preservado", "pending_missing_logs" in src)
# a sugestao continua indo para a UI
check("payload ainda expoe unmapped_items", 'results["unmapped_items"].append' in src)
check("payload inclui a descricao do produto", '"product_description": xml_item["descr"]' in src)
check("payload ainda expoe unmapped_icms_cfops", 'results["unmapped_icms_cfops"].append' in src)
check("payload ainda expoe unmapped_cte_items", 'results["unmapped_cte_items"].append' in src)

print("\n=== 2. Status por papel (helper) ===")
check("admin -> aprovado", rule_status_for(types.SimpleNamespace(role='admin')) == 'aprovado')
check("operador -> pendente_revisao", rule_status_for(types.SimpleNamespace(role='operador')) == 'pendente_revisao')
check("cliente -> pendente_revisao", rule_status_for(types.SimpleNamespace(role='cliente')) == 'pendente_revisao')

print("\n=== 3. Confirmacao grava com o status certo e a descricao real ===")
DESC = "PARAFUSO SEXTAVADO M8 X 40MM ZINCADO"
payload = {
    "cfop": "5102", "mapped_cfop": "1102", "tenant_id": t,
    "cnpj_emitente": "99999999999999", "company_cnpj": "11111111111111",
    "product_code": "PROD-1",
    "product_description": DESC,
}
r = as_("adm").post("/api/mapping/cfop", data=payload)
check("admin confirma com sucesso", r.status_code == 200, f"status={r.status_code}")
db = SessionLocal()
rule = db.query(CfopClassification).filter_by(tenant_id=t, supplier_cfop="5102").first()
check("regra criada", rule is not None)
check("admin grava APROVADO", rule and rule.status == "aprovado", f"status={getattr(rule,'status',None)}")
check("descricao real gravada (nao sintetica)", rule and rule.product_description == DESC,
      f"desc={getattr(rule,'product_description',None)}")
check("nao contem texto gerado pelo sistema",
      rule and "One-Click" not in (rule.product_description or ""))
check("autoria registrada", rule and rule.created_by == "admin@synax.com")
db.close()

# A linha de sucesso precisa continuar marcada como cfop-row, com o MESMO
# CFOP/CNPJ da linha original -- senao a paginacao JS da aba "Novos CFOPs"
# (applyTableState, em curation_results.html) para de contar essa linha e ela
# fica visivel em TODAS as paginas para sempre, em vez de so na pagina onde
# foi aprovada. Bug relatado pelo Sandro em 14/09/2026.
check("linha de sucesso mantem a classe cfop-row (senao vaza pra outras paginas)",
      'class="cfop-row bg-emerald-50"' in r.text, r.text)
check("com o data-cfop da linha aprovada", 'data-cfop="5102"' in r.text, r.text)
check("e o data-cnpj da linha aprovada", 'data-cnpj="99999999999999"' in r.text, r.text)

r = as_("ope").post("/api/mapping/cfop", data={**payload, "cfop": "5405", "mapped_cfop": "1403",
                                               "product_description": "OLEO LUBRIFICANTE 20W50"})
check("operador confirma com sucesso", r.status_code == 200, f"status={r.status_code}")
db = SessionLocal()
r_ope = db.query(CfopClassification).filter_by(tenant_id=t, supplier_cfop="5405").first()
check("operador grava PENDENTE_REVISAO", r_ope and r_ope.status == "pendente_revisao",
      f"status={getattr(r_ope,'status',None)}")
check("descricao do operador gravada", r_ope and r_ope.product_description == "OLEO LUBRIFICANTE 20W50")
db.close()

r = as_("cli").post("/api/mapping/cfop", data={**payload, "cfop": "5910", "mapped_cfop": "1910",
                                               "product_description": "BRINDE"})
db = SessionLocal()
r_cli = db.query(CfopClassification).filter_by(tenant_id=t, supplier_cfop="5910").first()
check("cliente grava PENDENTE_REVISAO (nao aprova sozinho)",
      r_cli is None or r_cli.status == "pendente_revisao", f"status={getattr(r_cli,'status',None)}")
db.close()

print("\n=== 4. Descricao vazia nao vira texto artificial ===")
r = as_("adm").post("/api/mapping/cfop", data={**payload, "cfop": "5949", "mapped_cfop": "1949",
                                               "product_description": ""})
db = SessionLocal()
r_vazio = db.query(CfopClassification).filter_by(tenant_id=t, supplier_cfop="5949").first()
check("descricao vazia fica NULL", r_vazio is not None and not r_vazio.product_description,
      f"desc={getattr(r_vazio,'product_description',None)}")
db.close()

print("=== 4b. As TRES respostas de /api/mapping/cfop preservam cfop-row ===")
import backend.app.api.routes.uploads as uploads_mod
fonte_map_cfop = inspect.getsource(uploads_mod.map_cfop_inline)
check("sao exatamente 3 respostas HTMLResponse na rota (sucesso/validacao/excecao)",
      fonte_map_cfop.count("return HTMLResponse(") == 3, fonte_map_cfop.count("return HTMLResponse("))
check("as 3 tem a classe cfop-row (nenhuma escapa da paginacao)",
      fonte_map_cfop.count("cfop-row") >= 3, fonte_map_cfop.count("cfop-row"))
check("as 3 levam data-cfop (para a paginacao filtrar/contar certo)",
      fonte_map_cfop.count("data-cfop") >= 3, fonte_map_cfop.count("data-cfop"))
check("valores interpolados vao escapados (html.escape), nao crus",
      fonte_map_cfop.count("html.escape(") >= 6, fonte_map_cfop.count("html.escape("))

print("\n=== 5. Templates exibem a descricao ===")
row_tpl = open('frontend/templates/partials/curation/_row.html', encoding='utf-8').read()
check("Curadoria exibe product_description", "r.product_description" in row_tpl)
adm_tpl = open('frontend/templates/partials/admin/tables/cfop.html', encoding='utf-8').read()
check("Regras por Empresa exibe descricao", "r.product_description" in adm_tpl)
check("Regras por Empresa trata descricao vazia", "Sem descrição" in adm_tpl)
res_tpl = open('frontend/templates/partials/curation/curation_results.html', encoding='utf-8').read()
check("Botao de confirmar envia product_description", 'product_description' in res_tpl)

engine.dispose()
if os.path.exists("_nas_test.db"):
    try: os.remove("_nas_test.db")
    except Exception: pass

fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
