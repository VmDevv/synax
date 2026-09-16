"""Toasts somem sozinhos da tela.

Bug relatado (21/08/2026): na Curadoria de Regras, cada aprovação em lote
empilhava um aviso verde no canto e NENHUM saía. Depois de algumas ações, a
parede de "X regra(s) aprovada(s)" cobria metade da tela.

Causa: o auto-dispensar era responsabilidade de CADA tela. `tenant_list.html` e
`certificados_list.html` mandavam um `<script>setTimeout(...)</script>` junto do
toast; a Curadoria (montada em f-string em main.py, `_curation_response`) não
mandava nada — então o toast entrava por swap OOB e ficava para sempre.

Correção: o auto-dispensar virou responsabilidade única de `layout/base.html`,
que observa o `#toast-container` e remove qualquer `.alert` que apareça,
independentemente de quem inseriu. Nenhuma rota precisa injetar script — o que
também evita montar `<script>` em f-string no backend, prática que o CLAUDE.md
manda evitar.

O comportamento final é de navegador (setTimeout/MutationObserver), então aqui
se testa o CONTRATO que o torna possível:
  - a página serve o observador global;
  - as respostas que trazem toast NÃO dependem de script próprio;
  - o container existe e é o mesmo alvo em todas as telas.
"""
import os, sys, re, uuid, types, io
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_toast_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
os.environ["SIEG_API_KEY"] = "teste"
os.environ["SIEG_EMAIL"] = "teste@x.com"
os.environ.pop("ANTHROPIC_API_KEY", None)

if os.path.exists("_toast_test.db"):
    os.remove("_toast_test.db")

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

from backend.app.infrastructure.database.session import engine as _eng
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit(f"ABORTADO: engine aponta para {_eng.url.get_backend_name()}, não SQLite.")

import backend.app.main as m
from backend.app.infrastructure.database.session import SessionLocal, engine
from backend.app.domain.models.base import Base
from backend.app.domain.models.auth_models import Tenant, UserProfile
from backend.app.domain.models.rules import CfopClassification
from fastapi.testclient import TestClient

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


BASE = io.open(os.path.join("frontend", "templates", "layout", "base.html"),
               encoding="utf-8").read()


# ============================================================================
print("\n=== 1. O auto-dispensar global existe no layout ===")
check("layout tem o #toast-container", 'id="toast-container"' in BASE)
check("layout observa o container (MutationObserver)", "MutationObserver" in BASE)
check("layout agenda a remoção do toast", "setTimeout" in BASE and "remove()" in BASE)
check("há um teto de toasts visíveis (rajada não vira parede)", "MAX_VISIVEIS" in BASE)
check("dá para dispensar no clique", "Clique para dispensar" in BASE)


# ============================================================================
print("\n=== 2. Nenhuma tela depende mais do próprio setTimeout ===")
# Se uma tela voltar a mandar o script dela, some o toast "errado" (o último da
# lista, não o dela) e o comportamento fica dependendo de quem lembrou.
for rel in [("partials", "admin", "tenant_list.html"),
            ("partials", "admin", "certificados_list.html")]:
    caminho = os.path.join("frontend", "templates", *rel)
    conteudo = io.open(caminho, encoding="utf-8").read()
    # Comentário Jinja {# ... #} pode citar "setTimeout" em prosa (o comentário
    # que explica esta própria mudança faz isso) sem ser código.
    sem_comentarios = re.sub(r"\{#.*?#\}", "", conteudo, flags=re.S)
    check(f"{rel[-1]} não tem setTimeout de toast próprio",
          "setTimeout" not in sem_comentarios, "script de remoção voltou para a tela")

# main.py monta o toast da Curadoria em f-string: não pode injetar <script>.
main_src = io.open(os.path.join("backend", "app", "main.py"), encoding="utf-8").read()
trecho_toast = main_src[main_src.index("if toast:"):main_src.index("if toast:") + 700]
check("main.py não injeta <script> junto do toast", "<script" not in trecho_toast,
      trecho_toast[:200])
check("main.py escapa o texto do toast (f-string foge do autoescape)",
      "html_escape(toast)" in main_src)


# ============================================================================
print("\n=== 3. Respostas da Curadoria continuam trazendo o toast por OOB ===")
Base.metadata.create_all(bind=engine)
db = SessionLocal()
tA = uuid.uuid4()
db.add(Tenant(id=tA, name="Empresa Toast", cnpj="11222333000181", ativo=True))
ua_admin = uuid.uuid4()
db.add(UserProfile(id=ua_admin, tenant_id=tA, role="admin", name="Admin"))
for i in range(3):
    db.add(CfopClassification(
        tenant_id=str(tA), company_cnpj="11222333000181", supplier_cnpj="99999999999999",
        supplier_cfop="5102", product_code=f"P{i}", entry_cfop="1102",
        status="pendente_revisao", created_by="teste"))
db.commit()
db.close()
TOKENS["admin"] = types.SimpleNamespace(id=str(ua_admin), email="admin@synax.com")

from backend.app.api.dependencies.db import get_rls_db as _real_rls
def _fake_rls():
    s = SessionLocal()
    try: yield s
    finally: s.close()
m.app.dependency_overrides[_real_rls] = _fake_rls

c = TestClient(m.app, raise_server_exceptions=False)
c.cookies.set("access_token", "Bearer admin")

chave = "|".join([str(tA), "11222333000181", "99999999999999", "5102", "P0"])
r = c.post("/api/curation/batch-approve", data={
    "rule_type": "cfop", "rule_keys": [chave], "page": 1})
check("batch-approve responde 200", r.status_code == 200, f"status={r.status_code}")
check("toast vem por swap OOB", 'id="toast-container" hx-swap-oob="beforeend"' in r.text)
check("toast informa a quantidade aprovada", "regra(s) aprovada(s)" in r.text)
check("a resposta NÃO carrega script de remoção (quem remove é o layout)",
      "<script" not in r.text.split('id="toast-container"')[1], r.text[-300:])

# A página da Curadoria precisa herdar o layout — é de lá que vem o observador.
r_pagina = c.get("/curation")
check("página da Curadoria responde 200", r_pagina.status_code == 200, f"status={r_pagina.status_code}")
check("página da Curadoria serve o observador global",
      "MutationObserver" in r_pagina.text and 'id="toast-container"' in r_pagina.text)


# ============================================================================
print("\n=== 4. Muitas ações seguidas: cada resposta traz UM toast só ===")
# O empilhamento vinha de o toast nunca sair, não de a resposta trazer vários.
# Se uma resposta passar a trazer N toasts, o teto do layout mascararia o
# problema — então o contrato de "1 toast por resposta" também fica travado.
for i in (1, 2):
    ch = "|".join([str(tA), "11222333000181", "99999999999999", "5102", f"P{i}"])
    resp = c.post("/api/curation/batch-approve", data={
        "rule_type": "cfop", "rule_keys": [ch], "page": 1})
    n_containers = resp.text.count('id="toast-container"')
    check(f"ação {i}: exatamente 1 bloco de toast na resposta", n_containers == 1,
          f"={n_containers}")

engine.dispose()
if os.path.exists("_toast_test.db"):
    try:
        os.remove("_toast_test.db")
    except OSError:
        pass


# ============================================================================
print("\n" + "=" * 60)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("FALHAS:")
    for nome in _falhou:
        print(f"  - {nome}")
sys.exit(1 if FALHAS else 0)
