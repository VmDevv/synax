"""XSS armazenado via descrição de produto.

Cenário real: a descrição do item vem do XML da NF-e emitida por um FORNECEDOR
(terceiro, fora do nosso controle). Se o Jinja renderizar sem escape, um
`<script>` no campo xProd executa no navegador do admin que abre a Curadoria —
com a sessão dele, que enxerga todas as empresas.
"""
import os, sys, uuid, types
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_xss_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_xss_test.db"):
    os.remove("_xss_test.db")

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
from backend.app.domain.models.auth_models import Tenant, UserProfile
from backend.app.domain.models.rules import CfopClassification
from fastapi.testclient import TestClient

PAYLOAD = '<script>alert("xss")</script>'
PAYLOAD_ATTR = '" onmouseover="alert(1)'

Base.metadata.create_all(bind=engine)
db = SessionLocal()
ut = uuid.uuid4(); t = str(ut)
db.add(Tenant(id=ut, name="Empresa X", cnpj="11111111111111"))
u_adm = uuid.uuid4()
db.add(UserProfile(id=u_adm, tenant_id=None, role="admin", name="Admin"))
# Regra pendente com descricao maliciosa, como viria de um XML de fornecedor
db.add(CfopClassification(
    tenant_id=t, company_cnpj="11111111111111", supplier_cnpj="99999999999999",
    supplier_cfop="5102", product_code="P1", entry_cfop="1102",
    product_description=PAYLOAD, status="pendente_revisao", created_by="fornecedor"))
db.add(CfopClassification(
    tenant_id=t, company_cnpj="11111111111111", supplier_cnpj="88888888888888",
    supplier_cfop="5405", product_code="P2", entry_cfop="1403",
    product_description=PAYLOAD_ATTR, status="pendente_revisao", created_by="fornecedor"))
db.commit(); db.close()
TOKENS["adm"] = types.SimpleNamespace(id=str(u_adm), email="admin@synax.com")

from backend.app.api.dependencies.db import get_rls_db as _real_rls
def _fake_rls():
    s = SessionLocal()
    try: yield s
    finally: s.close()
m.app.dependency_overrides[_real_rls] = _fake_rls

c = TestClient(m.app, raise_server_exceptions=False)
c.cookies.set("access_token", "Bearer adm")

results = []
def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("  OK  " if cond else " FALHA") + f" | {name}" + (f"  [{detail}]" if detail and not cond else ""))

print("\n=== Autoescape ativo no ambiente Jinja ===")
from backend.app.core.templating import build_env
env = build_env()
check("Environment com autoescape", bool(env.autoescape))
check("escapa string com script",
      "<script>" not in env.from_string("{{ x }}").render(x=PAYLOAD))

print("\n=== Curadoria: descricao vinda de XML de terceiro ===")
r = c.get("/api/curation/list?rule_type=cfop")
check("tela responde 200", r.status_code == 200, f"status={r.status_code}")
check("NAO injeta <script> executavel", "<script>alert(" not in r.text)
check("payload aparece escapado", "&lt;script&gt;" in r.text or "&amp;lt;" in r.text)
check("NAO permite quebrar atributo title", 'onmouseover="alert(1)"' not in r.text)

print("\n=== Regras por Empresa (painel admin) ===")
r = c.get(f"/admin/rules/cfop?tenant_id={t}&status=Todos")
if r.status_code == 200:
    check("NAO injeta <script> executavel", "<script>alert(" not in r.text)
    check("payload escapado", "&lt;script&gt;" in r.text)
else:
    check("tela de regras respondeu", False, f"status={r.status_code}")

print("\n=== Cookie de sessao ===")
import inspect
from backend.app.api.routes import auth as auth_mod
src = inspect.getsource(auth_mod)
check("cookie usa flag secure", "secure=" in src)
check("secure condicionado a https", 'request.url.scheme == "https"' in src)
check("cookie continua httponly", "httponly=True" in src)

print("\n=== Preview de e-mail continua renderizando HTML de proposito ===")
tpl = open('frontend/templates/partials/admin/email_preview.html', encoding='utf-8').read()
check("email_preview mantem | safe explicito", "html_body | safe" in tpl)

engine.dispose()
if os.path.exists("_xss_test.db"):
    try: os.remove("_xss_test.db")
    except Exception: pass

fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
