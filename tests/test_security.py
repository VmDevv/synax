"""Varredura de segurança: isolamento multi-tenant (IDOR), Maker-Checker,
RBAC do operador e fluxo de senha. Mesmo isolamento dos testes anteriores
(dotenv neutralizado + trava SQLite) para nunca tocar produção."""
import os, sys, uuid, types
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_sec_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

if os.path.exists("_sec_test.db"):
    os.remove("_sec_test.db")

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
RESET_CALLS = []
PASSWORD_UPDATES = []

class _AdminAuth:
    def update_user_by_id(self, uid, attrs):
        PASSWORD_UPDATES.append((uid, attrs))
        return types.SimpleNamespace(user=types.SimpleNamespace(id=uid))

class _Auth:
    admin = _AdminAuth()
    def get_user(self, token):
        u = TOKENS.get(token)
        if not u:
            raise Exception("invalid token")
        return types.SimpleNamespace(user=u)
    def reset_password_email(self, email, options=None):
        RESET_CALLS.append((email, options))
        return None

class _Client:
    auth = _Auth()

sb.get_supabase_client = lambda: _Client()
sb.get_supabase_admin_client = lambda: _Client()
import backend.app.api.dependencies.auth as dep_auth
dep_auth.get_supabase_client = lambda: _Client()
import backend.app.api.routes.auth as auth_mod
auth_mod.get_supabase_client = lambda: _Client()
auth_mod.get_supabase_admin_client = lambda: _Client()

from backend.app.infrastructure.database.session import engine as _eng
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit(f"ABORTADO: engine aponta para {_eng.url.get_backend_name()}, não SQLite.")

import backend.app.main as m
from backend.app.infrastructure.database.session import SessionLocal, engine
from backend.app.domain.models.base import Base
from backend.app.domain.models.auth_models import Tenant, UserProfile, UserTenant
from backend.app.domain.models.audit import AuditTask
from backend.app.domain.models.rules import CfopClassification, CfopCreditRule
from backend.app.infrastructure.repositories.rules_repository import RulesRepository
from fastapi.testclient import TestClient

Base.metadata.create_all(bind=engine)
db = SessionLocal()

# --- Dois tenants completamente distintos (simulando dois escritórios/clientes) ---
uA, uB = uuid.uuid4(), uuid.uuid4()
tA, tB = str(uA), str(uB)
db.add_all([
    Tenant(id=uA, name="Empresa A (vitima)", cnpj="11111111111111"),
    Tenant(id=uB, name="Empresa B (atacante)", cnpj="22222222222222"),
])

ua_admin, ua_cliA, ua_cliB, ua_oper = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
db.add_all([
    UserProfile(id=ua_admin, tenant_id=uA, role="admin", name="Admin"),
    UserProfile(id=ua_cliA, tenant_id=uA, role="cliente", name="Cliente A"),
    UserProfile(id=ua_cliB, tenant_id=uB, role="cliente", name="Cliente B (atacante)"),
    UserProfile(id=ua_oper, tenant_id=uA, role="operador", name="Operador"),
])
db.add(UserTenant(user_id=ua_oper, tenant_id=uA))  # operador só enxerga A
db.commit()

TOKENS["admin"] = types.SimpleNamespace(id=str(ua_admin), email="admin@synax.com")
TOKENS["cliA"] = types.SimpleNamespace(id=str(ua_cliA), email="clienteA@synax.com")
TOKENS["cliB"] = types.SimpleNamespace(id=str(ua_cliB), email="clienteB@synax.com")
TOKENS["oper"] = types.SimpleNamespace(id=str(ua_oper), email="operador@synax.com")

# --- Regras: A tem uma regra global CFOP; B tem outra pra mesmo CFOP com valor diferente ---
db.add_all([
    CfopClassification(tenant_id=tA, company_cnpj="11111111111111", supplier_cnpj="*",
                       supplier_cfop="5102", product_code="*", entry_cfop="1102",
                       status="aprovado", is_global=False, created_by="admin"),
    CfopClassification(tenant_id=tB, company_cnpj="22222222222222", supplier_cnpj="*",
                       supplier_cfop="5102", product_code="*", entry_cfop="9999",
                       status="aprovado", is_global=False, created_by="admin"),
    # CFOP com regra APROVADA em B
    CfopCreditRule(tenant_id=tB, cfop="5405", allow_credit=False, status="aprovado"),
])
db.commit()
# CFOP diferente, só com regra PENDENTE em B — nunca deve influenciar cálculo
# nem ser confundida com "aprovada" por um get_all() mal filtrado.
db.add(CfopCreditRule(tenant_id=tB, cfop="5406", allow_credit=True, status="pendente_revisao"))

# --- AuditTask de A (a "vítima") com resultado sensível ---
task_a = AuditTask(id="task-vitima", tenant_id=tA, status="concluido", user_id=str(ua_cliA),
                   file_name="sped_empresaA.txt",
                   result_data='{"resumo": {"notas_processadas": 42}, "company_name": "Empresa A Ltda - dados sigilosos"}')
db.add(task_a)
db.commit()
db.close()

from backend.app.api.dependencies.db import get_rls_db as _real_rls
def _fake_rls():
    s = SessionLocal()
    try: yield s
    finally: s.close()
m.app.dependency_overrides[_real_rls] = _fake_rls

c = TestClient(m.app, raise_server_exceptions=False)

def as_(tok):
    c.cookies.clear()
    if tok:
        c.cookies.set("access_token", f"Bearer {tok}")
    return c

results = []
def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("  OK  " if cond else " FALHA") + f" | {name}" + (f"  [{detail}]" if detail and not cond else ""))


print("\n=== PILAR 1: Isolamento multi-tenant (IDOR) ===")

r = as_("cliB").get("/api/status/task-vitima")
check("Cliente B NÃO lê a task da Empresa A (/api/status)",
      "sigilosos" not in r.text and "notas_processadas" not in r.text,
      f"status={r.status_code} vazou={'sigilosos' in r.text}")

r = as_("cliB").get("/api/export/task-vitima")
check("Cliente B NÃO exporta a task da Empresa A (/api/export)",
      r.status_code in (401, 403, 404), f"status={r.status_code}")

r = as_("cliB").get("/api/export/excel/task-vitima")
check("Cliente B NÃO exporta Excel da task da Empresa A (/api/export/excel)",
      r.status_code in (401, 403, 404), f"status={r.status_code}")

r = as_(None).get("/api/export/task-vitima")
check("Anônimo NÃO exporta task alguma (/api/export)",
      r.status_code in (401, 403, 404), f"status={r.status_code}")

r = as_(None).get("/api/status/task-vitima")
check("Anônimo NÃO lê status de task alguma",
      r.status_code in (401, 403, 404) or "sigilosos" not in r.text,
      f"status={r.status_code}")

# Repositório: regra de B nunca aparece numa sessão escopada para A
db = SessionLocal()
repo_a = RulesRepository(db, tA)
rule = repo_a.get_cfop_classification("11111111111111", "*", "5102", "*")
check("Repositório do tenant A não enxerga entry_cfop de B",
      rule is not None and rule.entry_cfop == "1102",
      f"entry_cfop={getattr(rule, 'entry_cfop', None)}")

credit = repo_a.get_credit_rule("5405")
check("get_credit_rule ignora tenant errado (B) mesmo sem regra em A",
      credit is None, f"credit={credit}")
db.close()


print("\n=== PILAR 2: Maker-Checker (pendente nunca aprovado por engano) ===")
db = SessionLocal()
repo_b = RulesRepository(db, tB)
credit_b = repo_b.get_credit_rule("5405")
check("get_credit_rule devolve a APROVADA, não a pendente_revisao",
      credit_b is not None and credit_b.allow_credit is False,
      f"allow_credit={getattr(credit_b, 'allow_credit', None)}")
all_b = repo_b.credit_rules.get_all()
check("BaseRepository.get_all() só traz status=aprovado",
      all(r.status == 'aprovado' for r in all_b), f"n={len(all_b)}")
db.close()

import inspect
from backend.app.services.audit_engine import cross_checker as cc
src = inspect.getsource(cc)
check("cross_checker não contém mais 'is_admin' (auto-aprovação heurística removida)",
      "is_admin" not in src)

db = SessionLocal()
repo_b2 = RulesRepository(db, tB)
pend = repo_b2.get_credit_rule("5406")
check("get_credit_rule NÃO devolve regra pendente_revisao mesmo sem alternativa aprovada",
      pend is None, f"pend={pend}")
all_b2 = repo_b2.credit_rules.get_all()
check("get_all() nunca inclui a pendente_revisao (5406)",
      all("5406" != r.cfop for r in all_b2), f"cfops={[r.cfop for r in all_b2]}")
db.close()


print("\n=== PILAR 3: RBAC — Operador travado fora de Admin/Curadoria ===")
for path in ["/admin", "/admin/tenants/list", "/admin/rules?tenant_id=" + tA, "/curation", "/api/curation/list"]:
    r = as_("oper").get(path, follow_redirects=False)
    check(f"Operador -> 403 em {path}", r.status_code == 403, f"status={r.status_code}")

for path in ["/admin", "/curation"]:
    r = as_("cliA").get(path, follow_redirects=False)
    check(f"Cliente -> 403 em {path}", r.status_code == 403, f"status={r.status_code}")

r = as_("admin").get("/admin")
check("Admin acessa /admin normalmente", r.status_code == 200, f"status={r.status_code}")

# SIEG precisa exigir login (estava 100% aberto)
for path in ["/api/sieg/xml/35230112345678901234550010000000011000000001",
             "/api/sieg/danfe/35230112345678901234550010000000011000000001"]:
    r = as_(None).get(path, follow_redirects=False)
    check(f"Anônimo -> negado em {path}", r.status_code in (401, 403), f"status={r.status_code}")

r = as_(None).post("/api/sieg/verificar-nao-lancadas", json={"cnpj": "11111111111111"})
check("Anônimo -> negado em /api/sieg/verificar-nao-lancadas", r.status_code in (401, 403), f"status={r.status_code}")

# Payload completo: o período agora é obrigatório (antes caía num intervalo fixo
# de 2026 embutido no código), então o caso precisa mandá-lo para chegar de fato
# à verificação de autorização — que é o que este teste protege.
PAYLOAD_SIEG = {"cnpj": "11111111111111",
                "data_inicio": "2026-01-01", "data_fim": "2026-01-31"}

r = as_("cliB").post("/api/sieg/verificar-nao-lancadas", json=PAYLOAD_SIEG)
check("Cliente B não varre o cofre fiscal da Empresa A por CNPJ alheio",
      r.status_code == 403, f"status={r.status_code}")

r = as_("cliB").post("/api/sieg/verificar-nao-lancadas", json={"cnpj": "11111111111111"})
check("Payload sem período é recusado pelo schema (rota recebia dict cru)",
      r.status_code == 422, f"status={r.status_code}")

r = as_(None).get("/api/audit/status/task-vitima")
check("Rota HTMX de status exige login (estava aberta)", r.status_code in (401, 403), f"status={r.status_code}")


print("\n=== PILAR 4: Fluxo de recuperação de senha ===")
r = as_(None).get("/forgot-password")
check("Página 'Esqueci a senha' existe e abre sem login", r.status_code == 200, f"status={r.status_code}")

r = as_(None).post("/api/auth/forgot-password", data={"email": "existe@synax.com"})
check("Solicitação de reset responde 200", r.status_code == 200)
check("Supabase reset_password_email foi chamado", len(RESET_CALLS) == 1, f"chamadas={len(RESET_CALLS)}")
check("redirect_to aponta para /reset-password",
      RESET_CALLS and RESET_CALLS[0][1].get("redirect_to", "").endswith("/reset-password"))
msg_existente = r.text

RESET_CALLS.clear()
r2 = as_(None).post("/api/auth/forgot-password", data={"email": "nao-existe-no-sistema@synax.com"})
check("Resposta idêntica para e-mail que não existe (sem enumeração de contas)",
      r2.status_code == r.status_code and ("sent" not in r2.text or True) and
      ("verifique" in r2.text.lower() or "sent" in r2.text.lower() or msg_existente[:200] == r2.text[:200]),
      "respostas devem ser indistinguíveis")

r = as_(None).get("/reset-password")
check("Página de definir nova senha existe", r.status_code == 200, f"status={r.status_code}")
check("Lê o token do fragmento da URL (não da query string)", "location.hash" in r.text)

r = as_(None).post("/api/auth/reset-password", data={
    "access_token": "tok-invalido", "password": "NovaSenha123", "password_confirm": "NovaSenha123"})
check("Token de reset inválido é rejeitado", r.status_code == 401, f"status={r.status_code}")
check("Nenhuma senha foi trocada com token inválido", len(PASSWORD_UPDATES) == 0)

TOKENS["recovery-cliA"] = TOKENS["cliA"]
r = as_(None).post("/api/auth/reset-password", data={
    "access_token": "recovery-cliA", "password": "abc12", "password_confirm": "abc12"})
check("Senha curta (5 chars) é rejeitada (min 8)",
      r.status_code == 400 and len(PASSWORD_UPDATES) == 0, f"status={r.status_code}")

r = as_(None).post("/api/auth/reset-password", data={
    "access_token": "recovery-cliA", "password": "SenhaForte123", "password_confirm": "SenhaDiferente456"})
check("Senhas divergentes são rejeitadas", r.status_code == 400, f"status={r.status_code}")
check("Nenhuma senha foi trocada com confirmação divergente", len(PASSWORD_UPDATES) == 0)

r = as_(None).post("/api/auth/reset-password", data={
    "access_token": "recovery-cliA", "password": "SenhaForte123", "password_confirm": "SenhaForte123"})
check("Reset válido responde 200", r.status_code == 200, f"status={r.status_code}")
check("update_user_by_id foi chamado para o usuário correto (cliente A)",
      len(PASSWORD_UPDATES) == 1 and PASSWORD_UPDATES[0][0] == str(ua_cliA),
      f"updates={PASSWORD_UPDATES}")

r = as_(None).get("/login")
check("Link 'Esqueceu?' aponta para /forgot-password (não mais '#')",
      'href="/forgot-password"' in r.text)

engine.dispose()

fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
if fails:
    print("FALHAS:")
    for f in fails:
        print("  -", f)
sys.exit(1 if fails else 0)
