"""Seletor de CFOP de entrada no lugar do campo de digitação livre.

Valida a montagem dos grupos (sugerido / histórico da empresa / tabela padrão),
o escopo por empresa e que o payload do lote continua chegando ao backend.
"""
import os, sys, uuid, types, re
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_cfop_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_cfop_test.db"):
    os.remove("_cfop_test.db")

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
from backend.app.domain.models.rules import CfopClassification
from fastapi.testclient import TestClient

Base.metadata.create_all(bind=engine)
db = SessionLocal()
tA, tB = uuid.uuid4(), uuid.uuid4()
db.add_all([
    Tenant(id=tA, name="Empresa A", cnpj="11111111111111"),
    Tenant(id=tB, name="Empresa B", cnpj="22222222222222"),
])
u_ope, u_cliB = uuid.uuid4(), uuid.uuid4()
db.add_all([
    UserProfile(id=u_ope,  tenant_id=tA, role="operador", name="Operador"),
    UserProfile(id=u_cliB, tenant_id=tB, role="cliente",  name="Cliente B"),
])
db.add(UserTenant(user_id=u_ope, tenant_id=tA))

# Historico aprovado da Empresa A: 1152 mais usado que 1556
for i in range(3):
    db.add(CfopClassification(
        tenant_id=str(tA), company_cnpj="11111111111111", supplier_cnpj=f"9999999999999{i}",
        supplier_cfop="5102", product_code=f"P{i}", entry_cfop="1152",
        status="aprovado", created_by="admin"))
db.add(CfopClassification(
    tenant_id=str(tA), company_cnpj="11111111111111", supplier_cnpj="88888888888888",
    supplier_cfop="5405", product_code="Q1", entry_cfop="1556",
    status="aprovado", created_by="admin"))
# Regra de OUTRA empresa: nao pode vazar para a lista da Empresa A
db.add(CfopClassification(
    tenant_id=str(tB), company_cnpj="22222222222222", supplier_cnpj="77777777777777",
    supplier_cfop="5102", product_code="Z1", entry_cfop="1234",
    status="aprovado", created_by="admin"))

db.add(AuditTask(
    id="task-a", tenant_id=str(tA), status="concluido", user_id=str(u_ope), file_name="sped.txt",
    result_data='{"cfops_desconhecidos": [{"cfop": "5102", "cfop_sugerido": "1102",'
                ' "cnpj_emitente": "99999999999999", "company_cnpj": "11111111111111",'
                ' "descricao": "PECA X"}],'
                ' "unmapped_items": [{"supplier_cfop": "5405", "current_sped_cfop": "1403",'
                ' "company_cnpj": "11111111111111", "supplier_cnpj": "88888888888888",'
                ' "product_code": "Q1", "product_description": "PECA Y"}],'
                ' "unmapped_cte_items": [], "resumo": {"notas_processadas": 1}}'))
db.commit(); db.close()
TOKENS["ope"] = types.SimpleNamespace(id=str(u_ope), email="oper@synax.com")
TOKENS["cliB"] = types.SimpleNamespace(id=str(u_cliB), email="cliB@synax.com")

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

print("\n=== 1. Endpoint devolve os grupos ===")
r = as_("ope").get("/api/audit/task-a/entry-cfops")
check("responde 200", r.status_code == 200, f"status={r.status_code}")
check("tem grupo de sugeridos", 'label="Sugerido nesta auditoria"' in r.text)
check("tem grupo do historico", 'label="Já usados nesta empresa"' in r.text)
check("tem grupo da tabela padrao", 'label="Tabela de CFOPs de entrada"' in r.text)

check("sugestao do motor presente (1102)", 'value="1102"' in r.text)
check("sugestao do motor presente (1403)", 'value="1403"' in r.text)
check("historico da empresa presente (1152)", 'value="1152"' in r.text)
check("descricao concatenada", "Compra para comercialização" in r.text)

print("\n=== 2. Escopo por empresa ===")
check("NAO vaza CFOP de outra empresa (1234)", 'value="1234"' not in r.text)

grupos = re.split(r'<optgroup label="([^"]+)">', r.text)
def bloco(nome):
    for i, g in enumerate(grupos):
        if g == nome:
            return grupos[i + 1]
    return ""
hist = bloco("Já usados nesta empresa")
check("1152 (3 usos) vem antes de 1556 (1 uso)",
      hist.find('value="1152"') != -1 and hist.find('value="1152"') < hist.find('value="1556"'),
      "ordenacao por frequencia real de uso")

print("\n=== 3. Sem duplicar entre grupos ===")
todos = re.findall(r'<option value="(\d{4})"', r.text)
check("nenhum CFOP repetido", len(todos) == len(set(todos)),
      f"repetidos={[x for x in set(todos) if todos.count(x) > 1]}")

print("\n=== 4. Escopo de acesso ===")
r_out = as_("cliB").get("/api/audit/task-a/entry-cfops")
check("usuario de outra empresa barrado", r_out.status_code in (403, 404), f"status={r_out.status_code}")
r_anon = as_(None).get("/api/audit/task-a/entry-cfops")
check("anonimo barrado", r_anon.status_code in (401, 403), f"status={r_anon.status_code}")

print("\n=== 5. Tela: campo livre virou seletor ===")
r = as_("ope").get("/api/status/task-a")
check("tela abre", r.status_code == 200, f"status={r.status_code}")
check("nao ha mais input de texto para entry_cfop",
      'type="text" name="entry_cfop"' not in r.text)
check("nao ha mais input de texto para mapped_cfop",
      'type="text" name="mapped_cfop"' not in r.text)
check("select de entry_cfop existe", 'name="entry_cfop"' in r.text and "<select" in r.text)
check("select do lote carrega via HTMX", "/entry-cfops" in r.text)
check("select por linha ja vem renderizado (sem 1 req por linha)",
      r.text.count('name="mapped_cfop"') >= 1 and 'name="mapped_cfop"' in r.text)
check("sugestao da linha vem pre-selecionada",
      re.search(r'value="1102"[^>]*selected', r.text) is not None)

print("\n=== 6. Lote continua funcionando com o valor do seletor ===")
r = as_("ope").post("/api/mapping/cfop/bulk_by_cfop", data={
    "task_id": "task-a", "supplier_cfop": "5405", "entry_cfop": "1556",
    "tenant_id": str(tA), "cnpj_emitente": "88888888888888"})
check("aplicar lote responde 200", r.status_code == 200, f"status={r.status_code}")
s = SessionLocal()
regra = s.query(CfopClassification).filter_by(
    tenant_id=str(tA), supplier_cfop="5405", product_code="Q1").first()
check("regra gravada com o CFOP escolhido", regra and regra.entry_cfop == "1556",
      f"entry={getattr(regra,'entry_cfop',None)}")
check("operador gera pendente_revisao", regra and regra.status == "pendente_revisao",
      f"status={getattr(regra,'status',None)}")
s.close()

# ---------------------------------------------------------------------------
# Dá para DESFAZER a escolha do CFOP de entrada.
#
# Relato do Sandro em 01/09/2026: escolhido um CFOP no seletor, não havia como
# voltar atrás para escolher outro. O placeholder "Selecione o CFOP de entrada"
# era `disabled`, então a opção vazia ficava inalcançável — quem clicasse errado
# ficava preso naquele valor. Submeter vazio segue barrado, mas pelo `required`
# do <select>, que é onde essa trava tem de estar.
import io as _io  # noqa: E402

_OPCOES = _io.open(os.path.join("frontend", "templates", "partials", "curation",
                                "_cfop_entrada_options.html"), encoding="utf-8").read()
_placeholder = [l for l in _OPCOES.splitlines()
                if "Selecione o CFOP de entrada" in l and l.strip().startswith("<option")]
results.append((
    "o placeholder do CFOP de entrada existe",
    len(_placeholder) == 1, _placeholder))
results.append((
    "e NAO e disabled (da para voltar atras)",
    bool(_placeholder) and "disabled" not in _placeholder[0], _placeholder))

_TELA = _io.open(os.path.join("frontend", "templates", "partials", "curation",
                              "curation_results.html"), encoding="utf-8").read()
results.append((
    "o select de entrada continua required",
    'name="entry_cfop" required' in _TELA, ""))
results.append((
    "existe botao de limpar o lote",
    "limparFiltroCfopLote()" in _TELA and "function limparFiltroCfopLote" in _TELA, ""))
results.append((
    "o limpar zera CNPJ, CFOP de origem e CFOP de entrada",
    all(t in _TELA for t in ("cnpj.value = ''", "cfop.value = ''", "entrada.value = ''")), ""))
results.append((
    "e reaplica o filtro da tabela depois de limpar",
    "filterCfopTable();" in _TELA, ""))

for _nome, _ok, _det in results[-6:]:
    print(("  OK   | " if _ok else " FALHA | ") + _nome + ("" if _ok else f"  [{_det}]"))

engine.dispose()
if os.path.exists("_cfop_test.db"):
    try: os.remove("_cfop_test.db")
    except Exception: pass

fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
