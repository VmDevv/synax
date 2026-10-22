# -*- coding: utf-8 -*-
"""Aviso de reinício pendente na Administração.

O --reload não recarrega o servidor desde 13/10/2026: código gravado fica no
disco e o processo segue com o antigo. O que esta suíte protege:

1. **Conta só o que muda de verdade.** Conteúdo diferente, arquivo novo ou
   removido; regravar um arquivo IGUAL (cópia, checkout) mexe na data e não
   pede reinício.
2. **Conta só o que o processo guarda na memória**: `.py` e os dados do
   validador de SPED. Template e `novidades.json` já valem na hora.
3. **O aviso é do admin**, e some quando não há nada pendente.
"""
import os
import sys
import tempfile
import time
import types
import uuid as _uuid

sys.path.insert(0, os.path.abspath('.'))

from backend.app.core import reinicio  # noqa: E402

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


def gravar(raiz, rel, texto, quando=None):
    caminho = os.path.join(raiz, *rel.split("/"))
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(texto)
    if quando is not None:
        os.utime(caminho, (quando, quando))


# ============================================================================
print("\n=== 1. O que pede reinício ===")

check("todo .py pede", reinicio.pede_reinicio("api/routes/admin.py"))
check("os dados do validador de SPED pedem (ficam na memória)",
      reinicio.pede_reinicio("services/sped_validador/dados/leiaute_020.json"))
check("novidades.json não pede (é lido a cada acesso)",
      not reinicio.pede_reinicio("services/novidades.json"))
check("template não pede", not reinicio.pede_reinicio("templates/pagina.html"))

with tempfile.TemporaryDirectory() as raiz:
    antes = time.time() - 100
    gravar(raiz, "main.py", "a = 1\n", antes)
    gravar(raiz, "api/rotas.py", "b = 2\n", antes)
    gravar(raiz, "services/sped_validador/dados/leiaute.json", "{}", antes)
    gravar(raiz, "services/novidades.json", "{}", antes)
    gravar(raiz, "templates/x.html", "<p>", antes)
    gravar(raiz, "velho.py", "c = 3\n", antes)
    gravar(raiz, "__pycache__/main.cpython-314.py", "lixo", antes)
    foto = reinicio.fotografar(raiz)
    inicio = time.time() - 50
    depois = time.time() - 10

    check("o retrato ignora __pycache__ e o que não pede reinício",
          sorted(foto) == ["api/rotas.py", "main.py",
                           "services/sped_validador/dados/leiaute.json", "velho.py"],
          sorted(foto))
    check("nada mudou: nada pendente", reinicio.alterados(raiz, foto, inicio) == [])

    gravar(raiz, "main.py", "a = 2\n", depois)
    gravar(raiz, "api/rotas.py", "b = 2\n", depois)            # mesmo conteúdo
    gravar(raiz, "services/sped_validador/dados/leiaute.json", '{"x": 1}', depois)
    gravar(raiz, "services/novidades.json", '{"y": 1}', depois)
    gravar(raiz, "templates/x.html", "<p>novo", depois)
    gravar(raiz, "novo.py", "d = 4\n", depois)
    os.remove(os.path.join(raiz, "velho.py"))
    pend = {a.caminho: a.situacao for a in reinicio.alterados(raiz, foto, inicio)}
    check("conteúdo mudado depois da subida é pendente",
          pend.get("main.py") == "alterado", pend)
    check("regravado IGUAL não é pendente (a data sozinha não basta)",
          "api/rotas.py" not in pend, pend)
    check("os dados do validador mudados são pendentes",
          pend.get("services/sped_validador/dados/leiaute.json") == "alterado", pend)
    check("novidades.json e template mudados não são",
          "services/novidades.json" not in pend and "templates/x.html" not in pend, pend)
    check("arquivo novo e arquivo removido aparecem",
          pend.get("novo.py") == "novo" and pend.get("velho.py") == "removido", pend)

check("o retrato do sistema de verdade foi tirado na importação",
      "api/routes/admin.py" in reinicio.FOTO and "core/reinicio.py" in reinicio.FOTO
      and any(c.startswith("services/sped_validador/dados/") for c in reinicio.FOTO))


# ============================================================================
print("\n=== 2. O aviso na Administração ===")

import dotenv  # noqa: E402
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main  # noqa: E402
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_reinicio_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_reinicio_test.db"):
    os.remove("_reinicio_test.db")

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
u_adm, u_ope = _uuid.uuid4(), _uuid.uuid4()
db.add(Tenant(id=t, name="EMPRESA", cnpj="11222333000181", uf="ES"))
db.add_all([UserProfile(id=u_adm, tenant_id=t, role="admin", name="Adm"),
            UserProfile(id=u_ope, tenant_id=t, role="operador", name="Op")])
db.commit()
db.close()
TOKENS["adm"] = types.SimpleNamespace(id=str(u_adm), email="adm@synax.com")
TOKENS["ope"] = types.SimpleNamespace(id=str(u_ope), email="op@synax.com")

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


r = as_("adm").get("/admin")
check("a Administração pede o aviso e confere de novo a cada minuto",
      r.status_code == 200 and 'hx-get="/admin/reinicio-pendente"' in r.text
      and "every 60s" in r.text, r.status_code)

original = reinicio.alterados
from datetime import datetime  # noqa: E402
try:
    reinicio.alterados = lambda *a, **k: []
    r = as_("adm").get("/admin/reinicio-pendente")
    check("nada pendente: resposta vazia (o aviso some)",
          r.status_code == 200 and r.text == "", (r.status_code, r.text[:80]))

    reinicio.alterados = lambda *a, **k: [
        reinicio.Alterado("domain/constants/cfops_entrada.py", "alterado",
                          datetime(2026, 10, 20, 9, 5)),
        reinicio.Alterado("core/reinicio.py", "novo", datetime(2026, 10, 20, 9, 7))]
    r = as_("adm").get("/admin/reinicio-pendente")
    check("com código pendente, o admin vê o aviso com a contagem",
          r.status_code == 200 and "Reinício pendente" in r.text
          and "2 arquivos de código" in r.text, r.text[:300])
    check("...e a lista dos arquivos, com o que aconteceu e quando",
          "domain/constants/cfops_entrada.py" in r.text and "alterado em 20/10 09:05" in r.text
          and "core/reinicio.py" in r.text)

    r = as_("ope").get("/admin/reinicio-pendente")
    check("operador não vê o aviso", r.status_code != 200 or "Reinício pendente" not in r.text,
          r.status_code)
    r = as_(None).get("/admin/reinicio-pendente")
    check("anônimo não vê o aviso", r.status_code != 200 or "Reinício pendente" not in r.text,
          r.status_code)
finally:
    reinicio.alterados = original

_eng.dispose()
if os.path.exists("_reinicio_test.db"):
    try:
        os.remove("_reinicio_test.db")
    except OSError:
        pass


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
for f in _falhou:
    print("  -", f)
sys.exit(1 if FALHAS else 0)
