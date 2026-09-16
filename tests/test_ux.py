"""Valida os fluxos novos de UX: data grid inline da curadoria e criação de usuário
com grade de empresas. Mesmo isolamento do test_rbac (dotenv neutralizado + trava SQLite)."""
import os, sys, uuid, types
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_ux_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

if os.path.exists("_ux_test.db"):
    os.remove("_ux_test.db")

from sqlalchemy import types as _satypes
import sqlalchemy.dialects.postgresql as pg

class _UUIDShim(_satypes.TypeDecorator):
    impl = _satypes.Uuid
    cache_ok = True
    def __init__(self, *a, **kw):
        kw.pop('as_uuid', None); super().__init__()
    def process_bind_param(self, value, dialect):
        return uuid.UUID(value) if isinstance(value, str) else value

pg.UUID = _UUIDShim

import backend.app.infrastructure.auth.supabase_client as sb
TOKENS = {}
CREATED_AUTH_USERS = []

class _AdminAuth:
    def create_user(self, payload):
        if payload["email"] == "duplicado@synax.com":
            raise Exception("User already registered")
        nid = str(uuid.uuid4())
        CREATED_AUTH_USERS.append(payload["email"])
        return types.SimpleNamespace(user=types.SimpleNamespace(id=nid, email=payload["email"]))

class _Auth:
    admin = _AdminAuth()
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
import backend.app.api.routes.admin as admin_mod
admin_mod.get_supabase_admin_client = lambda: _Client()

from backend.app.infrastructure.database.session import engine as _eng
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit(f"ABORTADO: engine aponta para {_eng.url.get_backend_name()}, não SQLite.")

import backend.app.main as m
from backend.app.infrastructure.database.session import SessionLocal, engine
from backend.app.domain.models.base import Base
from backend.app.domain.models.auth_models import Tenant, UserProfile, UserTenant
from backend.app.domain.models.rules import CfopClassification, PisCofinsCreditRule
from fastapi.testclient import TestClient

Base.metadata.create_all(bind=engine)
db = SessionLocal()

u1, u2, u3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
t1, t2, t3 = str(u1), str(u2), str(u3)
db.add_all([
    Tenant(id=u1, name="Alfa Comercio", cnpj="11111111111111"),
    Tenant(id=u2, name="Beta Industria", cnpj="22222222222222"),
    Tenant(id=u3, name="Gama Servicos", cnpj="33333333333333"),
])
ua = uuid.uuid4()
db.add(UserProfile(id=ua, tenant_id=u1, role="admin", name="Admin"))

# fila de curadoria
db.add_all([
    CfopClassification(tenant_id=t1, company_cnpj="11111111111111", supplier_cnpj="99999999999999",
                       supplier_cfop="5102", product_code="*", entry_cfop="1102",
                       status="pendente_revisao", created_by="operador@synax.com"),
    CfopClassification(tenant_id=t1, company_cnpj="11111111111111", supplier_cnpj="88888888888888",
                       supplier_cfop="5405", product_code="ABC", entry_cfop="1403",
                       status="pendente_revisao", created_by="operador@synax.com"),
    PisCofinsCreditRule(tenant_id=t1, cfop="5102", cst_expected="50", allow_credit=False,
                        status="pendente_revisao", created_by="operador@synax.com"),
])
db.commit()
TOKENS["tok_admin"] = types.SimpleNamespace(id=str(ua), email="admin@synax.com")

from backend.app.api.dependencies.db import get_rls_db as _real_rls
def _fake_rls():
    s = SessionLocal()
    try: yield s
    finally: s.close()
m.app.dependency_overrides[_real_rls] = _fake_rls

c = TestClient(m.app, raise_server_exceptions=False)
c.cookies.set("access_token", "Bearer tok_admin")

results = []
def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("  OK  " if cond else " FALHA") + f" | {name}" + (f"  [{detail}]" if detail and not cond else ""))

KEY1 = f"{t1}|11111111111111|99999999999999|5102|*"
KEY2 = f"{t1}|11111111111111|88888888888888|5405|ABC"
KEY_PC = f"{t1}|5102|50"

print("\n=== A. Data grid renderiza ===")
r = c.get("/api/curation/list")
check("lista abre", r.status_code == 200, f"status={r.status_code}")
check("linhas do grid presentes", 'class="cur-row' in r.text)
check("campo editável inline (entry_cfop)", 'name="value"' in r.text and 'curation/rule/update' in r.text)
check("botão aprovar inline", 'curation/rule/approve' in r.text)
check("botão descartar inline", 'curation/rule/discard' in r.text)
check("abas com contadores", 'id="curation-tabs"' in r.text)
check("dica de atalho Enter", 'Enter' in r.text)

print("\n=== B. Edição inline grava e devolve só a linha ===")
r = c.post("/api/curation/rule/update", data={
    "rule_type": "cfop", "key": KEY1, "field": "entry_cfop", "value": "1556"})
check("update responde 200", r.status_code == 200, f"status={r.status_code}")
check("devolve <tr>, não a página toda", r.text.strip().startswith("<tr") or "cur-row" in r.text)
check("não devolve o container inteiro", 'id="curation-list-container"' not in r.text)
db.expire_all()
row = db.query(CfopClassification).filter_by(supplier_cfop="5102", tenant_id=t1).first()
check("entry_cfop gravado", row.entry_cfop == "1556", f"valor={row.entry_cfop}")
check("continua pendente (edição não aprova)", row.status == "pendente_revisao", f"status={row.status}")

print("\n=== C. Allow-list de campos editáveis ===")
r = c.post("/api/curation/rule/update", data={
    "rule_type": "cfop", "key": KEY1, "field": "status", "value": "aprovado"})
check("bloqueia campo fora da allow-list", r.status_code == 400, f"status={r.status_code}")
db.expire_all()
row = db.query(CfopClassification).filter_by(supplier_cfop="5102", tenant_id=t1).first()
check("status não foi alterado por via travessa", row.status == "pendente_revisao", f"status={row.status}")

r = c.post("/api/curation/rule/update", data={
    "rule_type": "cfop", "key": KEY1, "field": "tenant_id", "value": t2})
check("bloqueia troca de tenant_id", r.status_code == 400, f"status={r.status_code}")
db.expire_all()
row = db.query(CfopClassification).filter_by(supplier_cfop="5102").first()
check("tenant_id preservado", str(row.tenant_id) == t1)

print("\n=== D. Toggle de crédito (PIS/COFINS) ===")
r = c.post("/api/curation/rule/update", data={
    "rule_type": "pis_cofins", "key": KEY_PC, "field": "allow_credit", "value": "true"})
db.expire_all()
pc = db.query(PisCofinsCreditRule).filter_by(cfop="5102", tenant_id=t1).first()
check("allow_credit virou True", pc.allow_credit is True, f"valor={pc.allow_credit}")
check("linha devolvida mostra Credita", "Credita" in r.text)

print("\n=== E. Enter = salva e aprova numa requisição só ===")
r = c.post("/api/curation/rule/approve", data={
    "rule_type": "cfop", "key": KEY2, "field": "entry_cfop", "value": "1999"})
check("approve responde 200", r.status_code == 200, f"status={r.status_code}")
db.expire_all()
row2 = db.query(CfopClassification).filter_by(supplier_cfop="5405", tenant_id=t1).first()
check("valor editado foi gravado", row2.entry_cfop == "1999", f"valor={row2.entry_cfop}")
check("e a regra foi aprovada", row2.status == "aprovado", f"status={row2.status}")
check("linha sai do grid (resposta sem <tr>)", "<tr" not in r.text)
check("abas atualizadas via OOB", 'hx-swap-oob="true"' in r.text and 'id="curation-tabs"' in r.text)

print("\n=== F. Descarte inline ===")
before = db.query(CfopClassification).filter_by(status="pendente_revisao").count()
r = c.post("/api/curation/rule/discard", data={"rule_type": "cfop", "key": KEY1})
db.expire_all()
after = db.query(CfopClassification).filter_by(status="pendente_revisao").count()
check("regra removida do banco", after == before - 1, f"antes={before} depois={after}")
check("abas atualizadas via OOB", 'hx-swap-oob="true"' in r.text)

print("\n=== G. Ações inline exigem admin ===")
c2 = TestClient(m.app, raise_server_exceptions=False)
for path in ["/api/curation/rule/update", "/api/curation/rule/approve", "/api/curation/rule/discard"]:
    r = c2.post(path, data={"rule_type": "cfop", "key": KEY_PC, "field": "entry_cfop", "value": "1"})
    check(f"anônimo barrado em {path}", r.status_code in (401, 403), f"status={r.status_code}")

print("\n=== H. Modal de usuário com grade de empresas ===")
r = c.get("/admin/modals/user")
check("abre sem tenant_id (Novo Usuário)", r.status_code == 200, f"status={r.status_code}")
check("lista as 3 empresas", all(n in r.text for n in ["Alfa Comercio", "Beta Industria", "Gama Servicos"]))
check("checkboxes tenant_ids", 'name="tenant_ids"' in r.text)
check("busca de empresa", 'x-model="search"' in r.text)
check("cards com Alpine", 'x-data="userForm()"' in r.text)
r = c.get(f"/admin/modals/user?tenant_id={t2}")
check("abre com empresa pré-marcada", f'value="{t2}"' in r.text and t2 in r.text)

print("\n=== I. Criação de usuário + vínculos em uma transação ===")
r = c.post("/admin/users", data={
    "name": "Maria Operadora", "email": "maria@synax.com", "password": "SenhaForte123",
    "role": "operador", "tenant_ids": [t1, t2]})
check("POST /admin/users responde 200", r.status_code == 200, f"status={r.status_code}")
prof = db.query(UserProfile).filter_by(name="Maria Operadora").first()
check("perfil criado", prof is not None)
check("role = operador", prof is not None and prof.role == "operador")
links = db.query(UserTenant).filter_by(user_id=prof.id).all() if prof else []
check("2 vínculos em user_tenants", len(links) == 2, f"vinculos={len(links)}")
check("tenant primário definido", prof is not None and str(prof.tenant_id) in (t1, t2))
check("allowed_tenants funciona", prof is not None and len(prof.allowed_tenants) == 2,
      f"n={len(prof.allowed_tenants) if prof else 0}")

print("\n=== J. Validações da criação ===")
n_before = db.query(UserProfile).count()
r = c.post("/admin/users", data={
    "name": "Sem Empresa", "email": "sem@synax.com", "password": "SenhaForte123",
    "role": "operador", "tenant_ids": []})
db.expire_all()
check("operador sem empresa é rejeitado", db.query(UserProfile).count() == n_before,
      f"perfis={db.query(UserProfile).count()} esperado={n_before}")
check("não criou login órfão no Supabase", "sem@synax.com" not in CREATED_AUTH_USERS)

r = c.post("/admin/users", data={
    "name": "Empresa Fantasma", "email": "fantasma@synax.com", "password": "SenhaForte123",
    "role": "operador", "tenant_ids": [str(uuid.uuid4())]})
db.expire_all()
check("empresa inexistente é rejeitada", db.query(UserProfile).count() == n_before)

r = c.post("/admin/users", data={
    "name": "Papel Invalido", "email": "papel@synax.com", "password": "SenhaForte123",
    "role": "superadmin", "tenant_ids": [t1]})
db.expire_all()
check("role inválido rejeitado", db.query(UserProfile).count() == n_before)

r = c.post("/admin/users", data={
    "name": "Admin Global", "email": "adminglobal@synax.com", "password": "SenhaForte123",
    "role": "admin", "tenant_ids": []})
db.expire_all()
check("admin sem empresa é permitido", db.query(UserProfile).filter_by(name="Admin Global").first() is not None)

print("\n=== K. Criação de usuário é exclusiva de admin ===")
r = c2.post("/admin/users", data={"name": "X", "email": "x@x.com", "password": "12345678",
                                  "role": "admin", "tenant_ids": []})
check("anônimo barrado em POST /admin/users", r.status_code in (401, 403), f"status={r.status_code}")

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
