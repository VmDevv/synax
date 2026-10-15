# -*- coding: utf-8 -*-
"""Janela de novidades depois do login.

O que esta suíte protege:

1. **Só aparece o que a pessoa ainda não viu**, no máximo três por vez, e o
   "já vi" é do usuário no banco — não volta em outro computador.
2. **Cada papel vê só o que é dele**: cliente não vê ferramenta interna.
3. **Arquivo de novidades ruim não derruba a tela principal**, e link externo
   não entra (o arquivo vai para a tela de todo mundo).
4. **A coluna nova chega ao banco que já existe** (`COLUNAS_NOVAS`): sem isso
   ela nasce no SQLite dos testes e quebra só em produção.
"""
import json
import os
import sys
import tempfile
import types
import uuid as _uuid
from datetime import date

sys.path.insert(0, os.path.abspath('.'))

from backend.app.services import novidades as nv  # noqa: E402

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


def item(data, titulo, papeis=("admin", "operador"), link="/x"):
    return {"data": data, "titulo": titulo, "texto": "Texto prático.",
            "link": link, "papeis": list(papeis)}


ITENS = [item("2026-10-15", "D"), item("2026-10-10", "C"),
         item("2026-10-05", "B"), item("2026-09-01", "A"),
         item("2026-10-12", "Só cliente", papeis=("cliente",))]
ITENS = sorted(ITENS, key=lambda i: i["data"], reverse=True)
HOJE = date(2026, 10, 15)


# ============================================================================
print("\n=== 1. O que cada um vê ===")

p = nv.pendentes("operador", "2026-10-06", HOJE, ITENS)
check("só o que veio depois do último visto", [i["titulo"] for i in p] == ["D", "C"], p)
p = nv.pendentes("operador", "2026-08-01", HOJE, ITENS)
check("no máximo três por vez, os mais recentes",
      [i["titulo"] for i in p] == ["D", "C", "B"], p)
check("nada pendente: lista vazia",
      nv.pendentes("operador", "2026-10-15", HOJE, ITENS) == [])
check("cliente não vê novidade de ferramenta interna",
      [i["titulo"] for i in nv.pendentes("cliente", "2026-01-01", HOJE, ITENS)]
      == ["Só cliente"])
p = nv.pendentes("operador", None, HOJE, ITENS)
check("quem nunca viu nada recebe só o último mês",
      "A" not in [i["titulo"] for i in p] and "D" in [i["titulo"] for i in p], p)
check("o mais recente do papel é o que marca o 'já vi'",
      nv.mais_recente("operador", ITENS) == "2026-10-15"
      and nv.mais_recente("cliente", ITENS) == "2026-10-12")


# ============================================================================
print("\n=== 2. O arquivo de novidades ===")

reais = nv.carregar()
check("o arquivo do sistema carrega e tem novidade",
      len(reais) >= 1 and all(i["data"] and i["titulo"] for i in reais))
check("as novidades atuais são de ferramenta interna (cliente não vê)",
      nv.do_papel("cliente", reais) == [])
check("todas com texto curto (até 3 frases práticas)",
      all(i["texto"].count(". ") <= 3 for i in reais),
      [i["titulo"] for i in reais if i["texto"].count(". ") > 3])

with tempfile.TemporaryDirectory() as d:
    ruim = os.path.join(d, "ruim.json")
    open(ruim, "w", encoding="utf-8").write("{ isto não é json")
    check("arquivo inválido vira lista vazia, sem exceção", nv.carregar(ruim) == [])
    misto = os.path.join(d, "misto.json")
    json.dump({"novidades": [item("2026-10-15", "ok"),
                             item("15/10/2026", "data errada"),
                             {"data": "2026-10-15", "titulo": "sem texto",
                              "papeis": ["admin"]},
                             item("2026-10-15", "externo",
                                  link="https://exemplo.com")]},
              open(misto, "w", encoding="utf-8"))
    check("só a novidade válida passa: data, campos e link interno",
          [i["titulo"] for i in nv.carregar(misto)] == ["ok"])


# ============================================================================
print("\n=== 3. A janela na tela principal ===")

import dotenv  # noqa: E402
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main  # noqa: E402
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_novidades_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_novidades_test.db"):
    os.remove("_novidades_test.db")

from sqlalchemy import types as _satypes  # noqa: E402
import sqlalchemy.dialects.postgresql as pg  # noqa: E402


class _UUIDShim(_satypes.TypeDecorator):
    impl = _satypes.Uuid
    cache_ok = True

    def __init__(self, *a, **kw):
        kw.pop('as_uuid', None)
        super().__init__()

    def process_bind_param(self, value, dialect):
        return _uuid.UUID(value) if isinstance(value, str) else value


pg.UUID = _UUIDShim

import backend.app.infrastructure.auth.supabase_client as sb  # noqa: E402
TOKENS = {}


class _Auth:
    def get_user(self, token):
        u = TOKENS.get(token.replace("Bearer ", "").strip())
        if not u:
            raise Exception("invalid")
        return types.SimpleNamespace(user=u)


class _Client:
    auth = _Auth()


sb.get_supabase_client = lambda: _Client()
sb.get_supabase_admin_client = lambda: _Client()
import backend.app.api.dependencies.auth as dep_auth  # noqa: E402
dep_auth.get_supabase_client = lambda: _Client()

from backend.app.infrastructure.database.session import engine as _eng  # noqa: E402
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit("ABORTADO: engine nao e SQLite")

import backend.app.main as m  # noqa: E402
from backend.app.infrastructure.database.session import SessionLocal  # noqa: E402
from backend.app.domain.models.base import Base  # noqa: E402
from backend.app.domain.models.auth_models import Tenant, UserProfile  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

Base.metadata.create_all(bind=_eng)
db = SessionLocal()
t = _uuid.uuid4()
u_ope, u_cli = _uuid.uuid4(), _uuid.uuid4()
db.add(Tenant(id=t, name="EMPRESA", cnpj="11222333000181", uf="ES"))
# "Já vi" no começo do ano: o teste não depende da data de hoje.
db.add_all([UserProfile(id=u_ope, tenant_id=t, role="operador", name="Op",
                        novidades_vistas_ate="2026-01-01"),
            UserProfile(id=u_cli, tenant_id=t, role="cliente", name="Cli",
                        novidades_vistas_ate="2026-01-01")])
db.commit()
db.close()
TOKENS["ope"] = types.SimpleNamespace(id=str(u_ope), email="op@synax.com")
TOKENS["cli"] = types.SimpleNamespace(id=str(u_cli), email="cli@synax.com")

from backend.app.api.dependencies.db import get_rls_db as _real_rls  # noqa: E402


def _fake_rls():
    ss = SessionLocal()
    try:
        yield ss
    finally:
        ss.close()


m.app.dependency_overrides[_real_rls] = _fake_rls
c = TestClient(m.app, raise_server_exceptions=False)


def as_(token):
    c.cookies.clear()
    if token:
        c.cookies.set("access_token", f"Bearer {token}")
    return c


r = as_("ope").get("/")
check("a tela principal pede a janela de novidades",
      r.status_code == 200 and 'hx-get="/api/novidades"' in r.text, r.status_code)

primeira = reais[0]["titulo"]
r = as_("ope").get("/api/novidades")
check("operador com novidade pendente recebe a janela",
      r.status_code == 200 and "Novidades no Synax" in r.text and primeira in r.text)
check("com o botão Entendi e o link da novidade",
      "Entendi" in r.text and 'hx-post="/api/novidades/vistas"' in r.text
      and reais[0]["link"] in r.text)

r = as_("ope").post("/api/novidades/vistas")
check("marcar como visto responde 200", r.status_code == 200, r.status_code)
ss = SessionLocal()
visto = ss.query(UserProfile.novidades_vistas_ate).filter(UserProfile.id == u_ope).scalar()
ss.close()
check("e grava no perfil a data da novidade mais recente",
      visto == nv.mais_recente("operador"), visto)
r = as_("ope").get("/api/novidades")
check("depois de visto, a janela não volta", r.status_code == 200 and r.text == "")

r = as_("cli").get("/api/novidades")
check("cliente não recebe novidade de ferramenta interna",
      r.status_code == 200 and r.text == "")
check("anônimo não recebe nada",
      as_(None).get("/api/novidades").status_code in (401, 403))


# ============================================================================
print("\n=== 4. A coluna chega ao banco que já existe ===")

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from backend.app.infrastructure.database import schema_upgrades  # noqa: E402

check("a coluna está registrada em COLUNAS_NOVAS",
      ("novidades_vistas_ate", "VARCHAR(10)") in
      schema_upgrades.COLUNAS_NOVAS.get("user_profiles", []))
antigo = create_engine("sqlite://")
with antigo.begin() as conn:
    conn.execute(text("CREATE TABLE user_profiles (id VARCHAR(36) PRIMARY KEY, "
                      "role VARCHAR(50), name VARCHAR(255))"))
schema_upgrades.aplicar(antigo)
check("o startup acrescenta a coluna na tabela antiga",
      "novidades_vistas_ate" in {c["name"] for c in inspect(antigo).get_columns("user_profiles")})
antigo.dispose()

_eng.dispose()
if os.path.exists("_novidades_test.db"):
    try:
        os.remove("_novidades_test.db")
    except OSError:
        pass


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
for f in _falhou:
    print("  -", f)
sys.exit(1 if FALHAS else 0)
