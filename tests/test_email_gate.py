"""Valida que o gerador de e-mail some da tela do CLIENTE e continua disponivel
para a equipe interna (admin/operador) — na interface E na rota."""
import os, sys, uuid, types
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_gate_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_gate_test.db"):
    os.remove("_gate_test.db")

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
from fastapi.testclient import TestClient

Base.metadata.create_all(bind=engine)
db = SessionLocal()
ut = uuid.uuid4(); t = str(ut)
db.add(Tenant(id=ut, name="Empresa Cliente", cnpj="11111111111111"))
u_admin, u_oper, u_cli = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
db.add_all([
    UserProfile(id=u_admin, tenant_id=None, role="admin", name="Admin"),
    UserProfile(id=u_oper, tenant_id=ut, role="operador", name="Operador"),
    UserProfile(id=u_cli, tenant_id=ut, role="cliente", name="Cliente"),
])
db.add(UserTenant(user_id=u_oper, tenant_id=ut))
db.add(AuditTask(
    id="task-1", tenant_id=t, status="concluido", user_id=str(u_cli),
    file_name="sped.txt",
    result_data='{"resumo": {"notas_processadas": 5}, "icms_issues": [], "relatorio_geral": []}'))
db.commit(); db.close()

TOKENS["adm"] = types.SimpleNamespace(id=str(u_admin), email="admin@synax.com")
TOKENS["ope"] = types.SimpleNamespace(id=str(u_oper), email="oper@synax.com")
TOKENS["cli"] = types.SimpleNamespace(id=str(u_cli), email="cliente@empresa.com")

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

print("\n=== Tela de resultados: CLIENTE ===")
r = as_("cli").get("/api/status/task-1")
check("tela abre normalmente", r.status_code == 200, f"status={r.status_code}")
check("botao 'E-mail para Cliente' NAO aparece", 'data-tab="tab-email-generator"' not in r.text)
check("painel do gerador NAO aparece", 'panel-tab-email-generator' not in r.text)
check("formulario de e-mail NAO aparece", 'email-generate' not in r.text)
check("resto da auditoria continua visivel", 'panel-tab-overview' in r.text or 'tab-overview' in r.text)

print("\n=== Tela de resultados: OPERADOR (equipe interna) ===")
r = as_("ope").get("/api/status/task-1")
check("tela abre", r.status_code == 200, f"status={r.status_code}")
check("botao aparece", 'data-tab="tab-email-generator"' in r.text)
check("painel aparece", 'panel-tab-email-generator' in r.text)

print("\n=== Tela de resultados: ADMIN ===")
r = as_("adm").get("/api/status/task-1")
check("tela abre", r.status_code == 200, f"status={r.status_code}")
check("botao aparece", 'data-tab="tab-email-generator"' in r.text)
check("painel aparece", 'panel-tab-email-generator' in r.text)

print("\n=== Rota de geracao (nao basta esconder o botao) ===")
payload = {"recipient_name": "Fulano", "period": "07/2026", "include_summary": "true"}
r = as_("cli").post("/api/audit/task-1/email-generate", data=payload)
check("CLIENTE recebe 403 na rota direta", r.status_code == 403, f"status={r.status_code}")
check("nao vaza conteudo do e-mail no corpo", "Equipe Synax" not in r.text)

r = as_("ope").post("/api/audit/task-1/email-generate", data=payload)
check("OPERADOR consegue gerar", r.status_code == 200, f"status={r.status_code}")

r = as_("adm").post("/api/audit/task-1/email-generate", data=payload)
check("ADMIN consegue gerar", r.status_code == 200, f"status={r.status_code}")

r = TestClient(m.app, raise_server_exceptions=False).post("/api/audit/task-1/email-generate", data=payload)
check("anonimo barrado", r.status_code in (401, 403), f"status={r.status_code}")

engine.dispose()
if os.path.exists("_gate_test.db"):
    try: os.remove("_gate_test.db")
    except Exception: pass

fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
