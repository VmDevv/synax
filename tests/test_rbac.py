"""Valida o RBAC e o maker-checker de ponta a ponta com Supabase e banco simulados.

ISOLAMENTO: vários módulos chamam load_dotenv(override=True), o que sobrescreveria
o DATABASE_URL de teste pelo do .env (Supabase de produção). Neutralizamos o dotenv
antes de qualquer import do backend e travamos a execução se o engine não for SQLite.
"""
import os, sys, uuid, types
sys.path.insert(0, os.path.abspath('.'))

# 1) dotenv vira no-op para o .env de produção nunca ser carregado
import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

# 2) ambiente de teste
os.environ["DATABASE_URL"] = "sqlite:///./_rbac_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

if os.path.exists("_rbac_test.db"):
    os.remove("_rbac_test.db")

# 3) UUID do Postgres não compila em SQLite. Mapeamos para o tipo genérico e
#    coagimos str -> uuid.UUID, que é o que o psycopg2 faz sozinho em produção.
from sqlalchemy import types as _satypes
import sqlalchemy.dialects.postgresql as pg

class _UUIDShim(_satypes.TypeDecorator):
    impl = _satypes.Uuid
    cache_ok = True
    def __init__(self, *a, **kw):
        kw.pop('as_uuid', None)
        super().__init__()
    def process_bind_param(self, value, dialect):
        if isinstance(value, str):
            return uuid.UUID(value)
        return value

pg.UUID = _UUIDShim

# --- stub do Supabase antes de importar o app ---
import backend.app.infrastructure.auth.supabase_client as sb

TOKENS = {}

class _Auth:
    def get_user(self, token):
        u = TOKENS.get(token)
        if not u:
            raise Exception("invalid token")
        return types.SimpleNamespace(user=u)

class _Client:
    auth = _Auth()

sb.get_supabase_client = lambda: _Client()
sb.get_supabase_admin_client = lambda: _Client()

import backend.app.api.dependencies.auth as dep_auth
dep_auth.get_supabase_client = lambda: _Client()

# TRAVA DE SEGURANÇA: aborta antes de tocar em qualquer banco que não seja o SQLite de teste.
from backend.app.infrastructure.database.session import engine as _eng
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit(f"ABORTADO: engine aponta para {_eng.url.get_backend_name()}, não SQLite.")

import backend.app.main as m
from backend.app.infrastructure.database.session import SessionLocal, engine
from backend.app.domain.models.base import Base
from backend.app.domain.models.auth_models import Tenant, UserProfile, UserTenant
from backend.app.domain.models.rules import CfopClassification
from fastapi.testclient import TestClient

Base.metadata.create_all(bind=engine)
db = SessionLocal()

# Tenant.id / UserProfile.id são UUID; rules.tenant_id é String(36) -> str(uuid)
u1, u2, u3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
t1, t2, t3 = str(u1), str(u2), str(u3)
db.add_all([
    Tenant(id=u1, name="Empresa A", cnpj="11111111111111"),
    Tenant(id=u2, name="Empresa B", cnpj="22222222222222"),
    Tenant(id=u3, name="Empresa C", cnpj="33333333333333"),
])
ua, uo, uc = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
admin_id, oper_id, cli_id = str(ua), str(uo), str(uc)
db.add_all([
    UserProfile(id=ua, tenant_id=u1, role="admin", name="Admin"),
    UserProfile(id=uo, tenant_id=u1, role="operador", name="Operador"),
    UserProfile(id=uc, tenant_id=u3, role="cliente", name="Cliente"),
])
# operador vinculado às empresas A e B (não C)
db.add_all([
    UserTenant(user_id=uo, tenant_id=u1),
    UserTenant(user_id=uo, tenant_id=u2),
])
db.commit()

TOKENS["tok_admin"] = types.SimpleNamespace(id=admin_id, email="admin@synax.com")
TOKENS["tok_oper"] = types.SimpleNamespace(id=oper_id, email="operador@synax.com")
TOKENS["tok_cli"] = types.SimpleNamespace(id=cli_id, email="cliente@synax.com")

# get_rls_db usa SELECT set_config(), exclusivo do Postgres. Em SQLite trocamos por
# uma sessão simples: o que estamos testando aqui é o RBAC da aplicação, não o RLS do banco.
from backend.app.api.dependencies.db import get_rls_db as _real_rls
def _fake_rls():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
m.app.dependency_overrides[_real_rls] = _fake_rls

c = TestClient(m.app, raise_server_exceptions=False)

def as_(tok):
    c.cookies.clear()
    c.cookies.set("access_token", f"Bearer {tok}")
    return c

results = []
def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("  OK  " if cond else " FALHA") + f" | {name}" + (f"  [{detail}]" if detail and not cond else ""))

print("\n=== 1. Operador não acessa Curadoria / Admin / Regras ===")
# O painel do Radar cruza auditorias, pessoas e pendências de TODAS as empresas.
# É a tela do sistema com a visão mais ampla — operador nenhum pode abri-la.
for path in ["/curation", "/admin", "/admin/tenants/list", "/admin/rules?tenant_id=" + t1,
             "/admin/activity", "/admin/activity/painel"]:
    r = as_("tok_oper").get(path, follow_redirects=False)
    check(f"operador GET {path} bloqueado", r.status_code == 403, f"status={r.status_code}")

print("\n=== 2. Cliente também não acessa ===")
for path in ["/curation", "/admin", "/admin/activity/painel"]:
    r = as_("tok_cli").get(path, follow_redirects=False)
    check(f"cliente GET {path} bloqueado", r.status_code == 403, f"status={r.status_code}")

print("\n=== 3. Admin acessa normalmente ===")
for path in ["/curation", "/admin", "/api/curation/list",
             "/admin/activity", "/admin/activity/painel"]:
    r = as_("tok_admin").get(path, follow_redirects=False)
    check(f"admin GET {path} permitido", r.status_code == 200, f"status={r.status_code}")

print("\n=== 4. Maker-Checker: operador gera pendente_revisao ===")
r = as_("tok_oper").post("/api/mapping/cfop", data={
    "cfop": "5102", "mapped_cfop": "1102", "tenant_id": t1,
    "cnpj_emitente": "99999999999999", "company_cnpj": "11111111111111",
    "product_code": "PROD-A",
})
row = db.query(CfopClassification).filter_by(supplier_cfop="5102", tenant_id=t1).first()
db.expire_all()
row = db.query(CfopClassification).filter_by(supplier_cfop="5102", tenant_id=t1).first()
check("regra do operador criada", row is not None, f"resp={r.status_code}")
check("status = pendente_revisao", row is not None and row.status == "pendente_revisao",
      f"status={getattr(row,'status',None)}")
check("created_by = e-mail real do operador", row is not None and row.created_by == "operador@synax.com",
      f"created_by={getattr(row,'created_by',None)}")

print("\n=== 5. Maker-Checker: admin grava direto como aprovado ===")
as_("tok_admin").post("/api/mapping/cfop", data={
    "cfop": "5405", "mapped_cfop": "1403", "tenant_id": t1,
    "cnpj_emitente": "88888888888888", "company_cnpj": "11111111111111",
    "product_code": "PROD-B",
})
db.expire_all()
row_a = db.query(CfopClassification).filter_by(supplier_cfop="5405", tenant_id=t1).first()
check("regra do admin = aprovado", row_a is not None and row_a.status == "aprovado",
      f"status={getattr(row_a,'status',None)}")

print("\n=== 6. Isolamento: operador não mapeia empresa não vinculada ===")
r = as_("tok_oper").post("/api/mapping/cfop", data={
    "cfop": "6102", "mapped_cfop": "2102", "tenant_id": t3,
    "cnpj_emitente": "77777777777777", "company_cnpj": "33333333333333",
    "product_code": "PROD-C",
})
db.expire_all()
leaked = db.query(CfopClassification).filter_by(tenant_id=t3).first()
check("operador barrado no tenant C", leaked is None, f"vazou={leaked is not None}")

print("\n=== 7. Curadoria: batch-approve por checkbox (era 404) ===")
db.expire_all()
pend = db.query(CfopClassification).filter_by(status="pendente_revisao").first()
key = "|".join([str(pend.tenant_id), pend.company_cnpj, pend.supplier_cnpj, pend.supplier_cfop, pend.product_code])
r = as_("tok_admin").post("/api/curation/batch-approve", data={
    "rule_type": "cfop", "rule_keys": [key], "page": 1,
})
check("endpoint existe (não 404)", r.status_code == 200, f"status={r.status_code}")
db.expire_all()
after = db.query(CfopClassification).filter_by(
    tenant_id=pend.tenant_id, supplier_cfop=pend.supplier_cfop).first()
check("regra aprovada via checkbox", after.status == "aprovado", f"status={after.status}")
check("resposta re-renderiza a tabela", 'id="curation-list-container"' in r.text)
check("toast fora de banda presente", 'hx-swap-oob' in r.text)

print("\n=== 8. Operador não aprova nada na curadoria ===")
r = as_("tok_oper").post("/api/curation/batch-approve", data={"rule_type": "cfop", "rule_keys": [key]})
check("operador barrado no batch-approve", r.status_code == 403, f"status={r.status_code}")
r = as_("tok_oper").post("/api/curation/bulk-approve-filtered", data={"rule_type": "cfop"})
check("operador barrado no bulk-approve", r.status_code == 403, f"status={r.status_code}")
r = as_("tok_oper").post("/api/curation/bulk-discard-filtered", data={"rule_type": "cfop"})
check("operador barrado no bulk-discard", r.status_code == 403, f"status={r.status_code}")

print("\n=== 9. Anônimo bloqueado ===")
c.cookies.clear()
for path in ["/curation", "/admin"]:
    r = c.get(path, follow_redirects=False)
    check(f"anônimo GET {path} bloqueado", r.status_code in (401, 403), f"status={r.status_code}")
r = c.post("/api/curation/bulk-discard-filtered", data={"rule_type": "cfop"})
check("anônimo barrado no bulk-discard (era 500)", r.status_code in (401, 403), f"status={r.status_code}")

print("\n=== 10. Endpoints órfãos do sistema antigo removidos ===")
for p in ["/api/curation/approve-all", "/api/curation/bulk-reject", "/api/rules/approve"]:
    r = c.post(p, data={})
    check(f"{p} removido", r.status_code == 404, f"status={r.status_code}")

print("\n=== 11. Dashboard do operador lista só empresas vinculadas ===")
r = as_("tok_oper").get("/")
check("dashboard do operador abre", r.status_code == 200, f"status={r.status_code}")
check("vê Empresa A (vinculada)", "Empresa A" in r.text)
check("vê Empresa B (vinculada)", "Empresa B" in r.text)
check("NÃO vê Empresa C", "Empresa C" not in r.text)
check("operador não vê menu Curadoria", 'href="/curation"' not in r.text)
check("operador não vê menu Administração", 'href="/admin"' not in r.text)

print("\n=== 12. Admin vê os menus (era hardcoded por e-mail) ===")
r = as_("tok_admin").get("/")
check("admin vê link da Curadoria", 'href="/curation"' in r.text)
check("admin vê link da Administração", 'href="/admin"' in r.text)

db.close()
engine.dispose()

fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
if fails:
    print("FALHAS:")
    for f in fails:
        print("  -", f)
sys.exit(1 if fails else 0)
