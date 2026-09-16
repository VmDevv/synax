"""Edição e exclusão de usuários — exclusivo de admin.

Foco nas travas: sem elas, um clique pode deixar o sistema sem nenhum
administrador, e não há como recriar um pela interface (a interface exige admin).
"""
import os, sys, uuid, types
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_crud_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_crud_test.db"):
    os.remove("_crud_test.db")

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
DELETADOS = []
FALHAR_SUPABASE = {"on": False}

EMAILS = {}          # uid(str) -> email atual no "Supabase"
RESETS = []          # e-mails que receberam link de redefinicao

class _AdminAuth:
    def delete_user(self, uid):
        if FALHAR_SUPABASE["on"]:
            raise Exception("Supabase indisponivel")
        DELETADOS.append(uid)
        EMAILS.pop(str(uid), None)
        return None

    def get_user_by_id(self, uid):
        if FALHAR_SUPABASE["on"]:
            raise Exception("Supabase indisponivel")
        email = EMAILS.get(str(uid))
        if not email:
            raise Exception("nao encontrado")
        return types.SimpleNamespace(user=types.SimpleNamespace(id=str(uid), email=email))

    def update_user_by_id(self, uid, attrs):
        if "email" in attrs:
            novo = attrs["email"].strip().lower()
            # o Supabase real recusa e-mail ja usado por outra conta
            for outro, mail in EMAILS.items():
                if outro != str(uid) and mail.lower() == novo:
                    raise Exception("A user with this email address has already been registered")
            EMAILS[str(uid)] = novo
        return types.SimpleNamespace(user=types.SimpleNamespace(id=str(uid)))

class _Auth:
    admin = _AdminAuth()
    def get_user(self, token):
        u = TOKENS.get(token.replace("Bearer ", "").strip())
        if not u: raise Exception("invalid")
        return types.SimpleNamespace(user=u)
    def reset_password_email(self, email, options=None):
        if FALHAR_SUPABASE["on"]:
            raise Exception("Supabase indisponivel")
        RESETS.append((email, options))
        return None

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
    raise SystemExit("ABORTADO: engine nao e SQLite")

import backend.app.main as m
from backend.app.infrastructure.database.session import SessionLocal, engine
from backend.app.domain.models.base import Base
from backend.app.domain.models.auth_models import Tenant, UserProfile, UserTenant
from fastapi.testclient import TestClient

Base.metadata.create_all(bind=engine)
db = SessionLocal()
tA, tB = uuid.uuid4(), uuid.uuid4()
db.add_all([
    Tenant(id=tA, name="Empresa A", cnpj="11111111111111"),
    Tenant(id=tB, name="Empresa B", cnpj="22222222222222"),
])
adm1, adm2, oper, cli = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
db.add_all([
    UserProfile(id=adm1, tenant_id=None, role="admin", name="Admin Um"),
    UserProfile(id=adm2, tenant_id=None, role="admin", name="Admin Dois"),
    UserProfile(id=oper, tenant_id=tA, role="operador", name="Operador"),
    UserProfile(id=cli,  tenant_id=tA, role="cliente",  name="Cliente"),
])
db.add(UserTenant(user_id=oper, tenant_id=tA))
db.commit(); db.close()
for k, u in [("adm1", adm1), ("adm2", adm2), ("oper", oper), ("cli", cli)]:
    TOKENS[k] = types.SimpleNamespace(id=str(u), email=f"{k}@synax.com")
    EMAILS[str(u)] = f"{k}@synax.com"

from backend.app.api.dependencies.db import get_rls_db as _real_rls
def _fake_rls():
    s = SessionLocal()
    try: yield s
    finally: s.close()
m.app.dependency_overrides[_real_rls] = _fake_rls

c = TestClient(m.app, raise_server_exceptions=False)
def as_(tok):
    c.cookies.clear()
    if tok: c.cookies.set("access_token", f"Bearer {tok}")
    return c

results = []
def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("  OK  " if cond else " FALHA") + f" | {name}" + (f"  [{detail}]" if detail and not cond else ""))

def perfil(uid):
    """Snapshot do perfil. Devolve dados simples porque o relacionamento
    `allowed_tenants` é lazy e não pode ser lido com a sessão já fechada."""
    s = SessionLocal()
    try:
        p = s.query(UserProfile).filter(UserProfile.id == uid).first()
        if not p:
            return None
        return types.SimpleNamespace(
            id=p.id, name=p.name, role=p.role, tenant_id=p.tenant_id,
            n_empresas=len(p.allowed_tenants or []),
        )
    finally:
        s.close()

print("\n=== 1. Somente admin acessa ===")
for tok, papel in [("oper", "operador"), ("cli", "cliente"), (None, "anonimo")]:
    r = as_(tok).get(f"/admin/modals/user/edit/{oper}")
    check(f"{papel} barrado no modal de edicao", r.status_code in (401, 403), f"status={r.status_code}")
    r = as_(tok).post(f"/admin/users/{cli}/delete")
    check(f"{papel} barrado na exclusao", r.status_code in (401, 403), f"status={r.status_code}")

print("\n=== 2. Edicao funciona ===")
r = as_("adm1").get(f"/admin/modals/user/edit/{oper}")
check("modal abre", r.status_code == 200, f"status={r.status_code}")
check("traz o nome atual", "Operador" in r.text)
check("empresa vinculada vem marcada", str(tA) in r.text)

r = as_("adm1").post(f"/admin/users/{oper}/edit",
                     data={"name": "Operador Renomeado", "role": "operador",
                           "tenant_ids": [str(tA), str(tB)]})
check("edicao responde 200", r.status_code == 200, f"status={r.status_code}")
p = perfil(oper)
check("nome atualizado", p and p.name == "Operador Renomeado", f"nome={getattr(p,'name',None)}")
check("agora com 2 empresas", p and p.n_empresas == 2, f"n={p.n_empresas if p else 0}")

# reduzir vinculos deve remover o antigo
r = as_("adm1").post(f"/admin/users/{oper}/edit",
                     data={"name": "Operador Renomeado", "role": "operador", "tenant_ids": [str(tB)]})
p = perfil(oper)
check("revinculo remove empresa desmarcada", p and p.n_empresas == 1, f"n={p.n_empresas if p else 0}")

print("\n=== 3. Validacoes da edicao ===")
r = as_("adm1").post(f"/admin/users/{cli}/edit",
                     data={"name": "Cliente", "role": "cliente", "tenant_ids": []})
check("nao-admin sem empresa e rejeitado", perfil(cli).role == "cliente")
r = as_("adm1").post(f"/admin/users/{cli}/edit",
                     data={"name": "X", "role": "superadmin", "tenant_ids": [str(tA)]})
check("papel invalido rejeitado", perfil(cli).role == "cliente", f"role={perfil(cli).role}")
r = as_("adm1").post(f"/admin/users/{cli}/edit",
                     data={"name": "X", "role": "cliente", "tenant_ids": [str(uuid.uuid4())]})
check("empresa inexistente rejeitada", perfil(cli).name == "Cliente", f"nome={perfil(cli).name}")

print("\n=== 3b. Alteracao de e-mail ===")
r = as_("adm1").get(f"/admin/modals/user/edit/{oper}")
check("modal mostra o e-mail atual", "oper@synax.com" in r.text)
check("modal tem campo de e-mail", 'name="email"' in r.text)
check("modal tem botao de redefinir senha", "/reset-password" in r.text)

r = as_("adm1").post(f"/admin/users/{oper}/edit",
                     data={"name": "Operador Renomeado", "role": "operador",
                           "email": "novo.operador@synax.com", "tenant_ids": [str(tB)]})
check("edicao com novo e-mail responde 200", r.status_code == 200, f"status={r.status_code}")
check("e-mail alterado no provedor", EMAILS[str(oper)] == "novo.operador@synax.com",
      f"email={EMAILS.get(str(oper))}")
check("demais campos preservados", perfil(oper).name == "Operador Renomeado")

# e-mail invalido
r = as_("adm1").post(f"/admin/users/{oper}/edit",
                     data={"name": "X", "role": "operador", "email": "sem-arroba",
                           "tenant_ids": [str(tB)]})
check("e-mail invalido rejeitado", EMAILS[str(oper)] == "novo.operador@synax.com",
      f"email={EMAILS.get(str(oper))}")
check("nome nao mudou apos rejeicao", perfil(oper).name == "Operador Renomeado")

# e-mail ja em uso por outra conta
r = as_("adm1").post(f"/admin/users/{oper}/edit",
                     data={"name": "Y", "role": "operador", "email": "adm1@synax.com",
                           "tenant_ids": [str(tB)]})
check("e-mail duplicado rejeitado", EMAILS[str(oper)] == "novo.operador@synax.com",
      f"email={EMAILS.get(str(oper))}")
check("mensagem indica e-mail em uso", "já está em uso" in r.text)
check("alteracao local NAO aplicada quando o e-mail falha",
      perfil(oper).name == "Operador Renomeado", f"nome={perfil(oper).name}")

# manter o mesmo e-mail nao deve chamar o provedor
antes = dict(EMAILS)
r = as_("adm1").post(f"/admin/users/{oper}/edit",
                     data={"name": "Operador Final", "role": "operador",
                           "email": "NOVO.OPERADOR@synax.com", "tenant_ids": [str(tB)]})
check("mesmo e-mail (case diferente) nao quebra", perfil(oper).name == "Operador Final")
check("e-mail permanece igual", EMAILS[str(oper)] == antes[str(oper)])

print("\n=== 3c. Envio de redefinicao de senha ===")
RESETS.clear()
r = as_("adm1").post(f"/admin/users/{oper}/reset-password")
check("envio responde 200", r.status_code == 200, f"status={r.status_code}")
check("provedor chamado uma vez", len(RESETS) == 1, f"n={len(RESETS)}")
check("enviado para o e-mail do usuario", RESETS and RESETS[0][0] == "novo.operador@synax.com",
      f"destino={RESETS[0][0] if RESETS else None}")
check("redirect_to aponta para /reset-password",
      RESETS and RESETS[0][1].get("redirect_to", "").endswith("/reset-password"))
check("confirmacao menciona o destino", "novo.operador@synax.com" in r.text)

for tok, papel in [("oper", "operador"), ("cli", "cliente"), (None, "anonimo")]:
    RESETS.clear()
    r = as_(tok).post(f"/admin/users/{oper}/reset-password")
    check(f"{papel} nao dispara redefinicao", r.status_code in (401, 403) and not RESETS,
          f"status={r.status_code}")

print("\n=== 4. TRAVA: nao excluir a si mesmo ===")
r = as_("adm1").post(f"/admin/users/{adm1}/delete")
check("admin nao exclui a propria conta", perfil(adm1) is not None)
check("nada removido do Supabase", str(adm1) not in DELETADOS)

print("\n=== 5. Exclusao funciona ===")
r = as_("adm1").post(f"/admin/users/{cli}/delete")
check("exclusao responde 200", r.status_code == 200, f"status={r.status_code}")
check("perfil local removido", perfil(cli) is None)
check("login removido do Supabase", str(cli) in DELETADOS)
s = SessionLocal()
check("vinculos user_tenants limpos",
      s.query(UserTenant).filter(UserTenant.user_id == cli).count() == 0)
s.close()

print("\n=== 6. TRAVA: nunca ficar sem administrador ===")
r = as_("adm1").post(f"/admin/users/{adm2}/delete")
check("exclui admin quando ha outro", perfil(adm2) is None)
# agora adm1 e o unico admin; adm2 tenta rebaixar/excluir via outro caminho
r = as_("adm2").post(f"/admin/users/{adm1}/edit",
                     data={"name": "Admin Um", "role": "cliente", "tenant_ids": [str(tA)]}) \
    if perfil(adm2) else None
p = perfil(adm1)
check("ultimo admin NAO pode ser rebaixado", p and p.role == "admin", f"role={getattr(p,'role',None)}")

print("\n=== 7. Falha no Supabase nao deixa estado inconsistente ===")
s = SessionLocal()
novo = uuid.uuid4()
s.add(UserProfile(id=novo, tenant_id=tA, role="cliente", name="Temporario"))
s.commit(); s.close()
FALHAR_SUPABASE["on"] = True
r = as_("adm1").post(f"/admin/users/{novo}/delete")
FALHAR_SUPABASE["on"] = False
check("perfil preservado quando o Supabase falha", perfil(novo) is not None,
      "rollback deve devolver o perfil")
check("nada foi removido no Supabase", str(novo) not in DELETADOS)

print("\n=== 8. Admins aparecem na listagem (tenant_id nulo) ===")
r = as_("adm1").get("/admin/tenants/list")
check("listagem responde 200", r.status_code == 200, f"status={r.status_code}")
check("secao de acesso global existe", "Acesso global" in r.text)
check("admin sem empresa aparece", "Admin Um" in r.text)
check("tem botao de editar o admin", f"/admin/modals/user/edit/{adm1}" in r.text)

engine.dispose()
if os.path.exists("_crud_test.db"):
    try: os.remove("_crud_test.db")
    except Exception: pass

fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
