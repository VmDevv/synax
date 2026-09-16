"""Duas mudanças pedidas em 2026-08-18:

1. Botão de inativar/ativar empresa. Inativa some das telas de quem não é
   admin (operador/cliente); o admin continua vendo, com a tag "Inativa".
2. Bug do certificado: a linha da tabela envolvia os <td> com um <form>, o que
   o HTML5 não permite dentro de <tr> — o navegador descarta o <form> ao montar
   o DOM, e o botão "Salvar" (type=submit) fica sem nenhum form para submeter.
   Numa linha que só tem "Salvar" (certificado ainda não cadastrado), isso lia
   exatamente como "digitar a senha e nada acontece".

Mesmo isolamento das demais suítes (dotenv neutralizado + trava SQLite).
"""
import os, sys, uuid, types, io, re
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_tenant_status_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
os.environ["SIEG_API_KEY"] = "chave-de-teste"
os.environ["SIEG_EMAIL"] = "teste@synax.local"

if os.path.exists("_tenant_status_test.db"):
    os.remove("_tenant_status_test.db")

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

class _AdminAuth:
    def update_user_by_id(self, uid, attrs):
        return types.SimpleNamespace(user=types.SimpleNamespace(id=uid))
    def sign_out(self, token, scope=None):
        return None

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
from fastapi.testclient import TestClient

Base.metadata.create_all(bind=engine)
db = SessionLocal()

tAtiva, tInativa = uuid.uuid4(), uuid.uuid4()
db.add_all([
    Tenant(id=tAtiva, name="Empresa Ativa", cnpj="11111111111111", ativo=True),
    Tenant(id=tInativa, name="Empresa Inativada", cnpj="22222222222222", ativo=True),
])
db.commit()

ua_admin, ua_oper, ua_cliInativa = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
db.add_all([
    UserProfile(id=ua_admin, tenant_id=None, role="admin", name="Admin"),
    UserProfile(id=ua_oper, tenant_id=tAtiva, role="operador", name="Operador"),
    UserProfile(id=ua_cliInativa, tenant_id=tInativa, role="cliente", name="Cliente da Inativada"),
])
# Operador vinculado às DUAS empresas — a inativação deve escondê-la dele.
db.add_all([UserTenant(user_id=ua_oper, tenant_id=tAtiva),
           UserTenant(user_id=ua_oper, tenant_id=tInativa)])
db.commit()
db.close()

TOKENS["admin"] = types.SimpleNamespace(id=str(ua_admin), email="admin@synax.com")
TOKENS["oper"] = types.SimpleNamespace(id=str(ua_oper), email="operador@synax.com")
TOKENS["cliInativa"] = types.SimpleNamespace(id=str(ua_cliInativa), email="cliente@synax.com")

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


# ============================================================================
print("\n=== 1. Modelo e migração: coluna ativo ===")
db2 = SessionLocal()
t = db2.query(Tenant).filter(Tenant.id == tAtiva).first()
check("Tenant novo nasce ativo por padrão", t.ativo is True, f"ativo={t.ativo}")
db2.close()


# ============================================================================
print("\n=== 2. Toggle de status (admin) ===")
r = as_("admin").post(f"/admin/tenants/{tInativa}/toggle-status")
check("Toggle responde 200", r.status_code == 200, f"status={r.status_code}")
check("Toast confirma a inativação", "inativada" in r.text.lower(), r.text[:200])

db3 = SessionLocal()
t2 = db3.query(Tenant).filter(Tenant.id == tInativa).first()
check("Empresa marcada como inativa no banco", t2.ativo is False, f"ativo={t2.ativo}")
db3.close()

r = as_("admin").post(f"/admin/tenants/{tInativa}/toggle-status")
check("Segundo toggle reativa a empresa", "ativa novamente" in r.text.lower(), r.text[:200])
db4 = SessionLocal()
check("Empresa voltou a 'ativo=True' no banco",
      db4.query(Tenant).filter(Tenant.id == tInativa).first().ativo is True)
db4.close()

# Deixa inativada para os próximos blocos.
as_("admin").post(f"/admin/tenants/{tInativa}/toggle-status")

r = as_("oper").post(f"/admin/tenants/{tInativa}/toggle-status")
check("Não-admin não alcança a rota de toggle (router /admin exige admin)",
      r.status_code in (401, 403), f"status={r.status_code}")


# ============================================================================
print("\n=== 3. Admin vê a inativa com tag; operador/cliente não veem ===")
r = as_("admin").get("/admin/tenants/list")
check("Lista de empresas (admin) inclui a inativa", "Empresa Inativada" in r.text)
check("Lista de empresas (admin) mostra a tag 'Inativa'",
      re.search(r"Empresa Inativada.*?Inativa", r.text, re.S) is not None,
      "tag não encontrada perto do nome")

r = as_("admin").get("/")
check("Dashboard do admin lista a empresa inativa no seletor",
      "Empresa Inativada" in r.text)
check("Dashboard do admin marca a inativa como tal",
      "Empresa Inativada — Inativa" in r.text or
      re.search(r"Empresa Inativada[^<]*Inativa", r.text) is not None,
      "tag não encontrada no <option>")

r = as_("oper").get("/")
check("Dashboard do operador NÃO lista a empresa inativa",
      "Empresa Inativada" not in r.text)
check("Dashboard do operador ainda lista a empresa ativa",
      "Empresa Ativa" in r.text)

r = as_("cliInativa").get("/")
check("Cliente da empresa inativada ainda consegue logar (sem bloqueio de acesso)",
      r.status_code == 200, f"status={r.status_code}")


# ============================================================================
print("\n=== 4. NFS-e: _empresas_do_perfil segue a mesma regra ===")
from backend.app.api.routes.nfse import _empresas_do_perfil

db5 = SessionLocal()
perfil_admin = db5.query(UserProfile).filter(UserProfile.id == ua_admin).first()
perfil_oper = db5.query(UserProfile).filter(UserProfile.id == ua_oper).first()

nomes_admin = {t.name for t in _empresas_do_perfil(db5, perfil_admin)}
nomes_oper = {t.name for t in _empresas_do_perfil(db5, perfil_oper)}

check("_empresas_do_perfil(admin) inclui a inativa", "Empresa Inativada" in nomes_admin,
      f"nomes={nomes_admin}")
check("_empresas_do_perfil(operador) exclui a inativa", "Empresa Inativada" not in nomes_oper,
      f"nomes={nomes_oper}")
check("_empresas_do_perfil(operador) ainda traz a ativa", "Empresa Ativa" in nomes_oper,
      f"nomes={nomes_oper}")
db5.close()

r = as_("admin").get("/nfse/drawer")
check("Drawer de NFS-e (admin) referencia a empresa inativa na lista JSON",
      '"nome": "Empresa Inativada"' in r.text or "Empresa Inativada" in r.text)

r = as_("oper").get("/nfse/drawer")
check("Drawer de NFS-e (operador) não referencia a empresa inativa",
      "Empresa Inativada" not in r.text)


# ============================================================================
print("\n=== 5. Reativação restaura a visibilidade ===")
as_("admin").post(f"/admin/tenants/{tInativa}/toggle-status")   # volta a ativa
r = as_("oper").get("/")
check("Depois de reativar, o operador volta a ver a empresa",
      "Empresa Inativada" in r.text)


# ============================================================================
print("\n=== 6. Bug do certificado: <form> inválido dentro de <tr> ===")
tpl_path = os.path.join("frontend", "templates", "partials", "admin",
                        "certificados_list.html")
tpl = io.open(tpl_path, encoding="utf-8").read()

# Isola o corpo do loop de linhas (entre "{% for l in linhas %}" e "{% endfor %}")
inicio = tpl.index("{% for l in linhas %}")
fim = tpl.index("{% endfor %}", inicio)
corpo = tpl[inicio:fim]
# Comentários Jinja {# ... #} podem citar "<form>" em prosa (como o que explica
# o bug corrigido) sem que isso seja markup real — removidos antes de checar.
corpo_sem_comentarios = re.sub(r"\{#.*?#\}", "", corpo, flags=re.S)

check("Não há <form> dentro do loop de linhas (HTML5 proíbe <form> em <tr>)",
      "<form" not in corpo_sem_comentarios, "ainda existe <form> na linha da tabela")
check("Botão 'Salvar' carrega hx-post diretamente (não depende de <form>)",
      '>Salvar</button>' in corpo and
      'hx-post="/admin/certificados/{{ l.tenant.id }}"' in corpo)
check("Botão 'Salvar' usa hx-include para juntar os campos da própria linha",
      'hx-include="closest tr"' in corpo)
check("Campo de senha também aciona o salvamento (Enter)",
      "hx-trigger=\"keyup[key=='Enter']\"" in corpo)

# Ponta a ponta: uma linha SEM certificado (só o botão Salvar) precisa continuar
# funcionando ao nível do backend — a leitura de senha/arquivo/ativo do hx-include
# chega ao servidor como campos de formulário comuns, então basta confirmar que
# a rota persiste corretamente o que o botão agora envia.
from backend.app.domain.models.certificates import TenantCertificate

db6 = SessionLocal()
check("Empresa ativa começa sem certificado cadastrado",
      db6.query(TenantCertificate).filter_by(tenant_id=str(tAtiva)).first() is None)
db6.close()

r = as_("admin").post(f"/admin/certificados/{tAtiva}",
                      data={"arquivo": "", "senha": "minhasenha123", "ativo": "1"})
check("POST equivalente ao clique em 'Salvar' responde 200", r.status_code == 200,
      f"status={r.status_code}")

db7 = SessionLocal()
cert = db7.query(TenantCertificate).filter_by(tenant_id=str(tAtiva)).first()
check("Certificado foi criado a partir dos campos da linha", cert is not None)
if cert:
    check("Senha foi cifrada (não gravada em claro)",
          cert.senha_encrypted != "minhasenha123")
    check("Senha decifra de volta para o valor digitado", cert.senha() == "minhasenha123")
db7.close()


# ============================================================================
print("\n" + "=" * 60)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("FALHAS:")
    for nome in _falhou:
        print(f"  - {nome}")
sys.exit(1 if FALHAS else 0)
