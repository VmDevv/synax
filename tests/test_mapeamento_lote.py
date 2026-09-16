"""Mapeamento em lote por CFOP de origem (POST /api/mapping/cfop/bulk_by_cfop).

Cobre o que o lote passou a garantir:
  - CFOP de origem e obrigatorio; CNPJ e opcional e apenas estreita o lote.
  - O CNPJ casa como a tela casa (substring, sem pontuacao).
  - A regra em lote nasce com a chave completa, igual a manual — nada de "*".
  - Resposta nunca verde quando nada foi gravado.
  - Literais interpolados no <script> da resposta sao escapados.
"""
import os, sys, uuid, types
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_lote_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_lote_test.db"):
    os.remove("_lote_test.db")

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

EMPRESA = "11111111111111"
FORN_A  = "88888888888888"
FORN_B  = "77777777777777"

db = SessionLocal()
tA = uuid.uuid4()
db.add(Tenant(id=tA, name="Empresa A", cnpj=EMPRESA))
u_adm = uuid.uuid4()
db.add(UserProfile(id=u_adm, tenant_id=tA, role="admin", name="Admin"))
db.add(UserTenant(user_id=u_adm, tenant_id=tA))

# unmapped_items da auditoria:
#   1) 5405 / FORN_A / Q1   -> entra no lote
#   2) 5405 / FORN_B / Q2   -> entra no lote quando nao ha filtro de CNPJ
#   3) 5102 / FORN_A / Q3   -> outro CFOP, nunca entra
#   4) 5405 / 66..66 / ""   -> produto vazio, tem de ser ignorado
#   5) 5405 / 55..55 / "*"  -> curinga, tem de ser ignorado
ITENS = (
    '{"supplier_cfop": "5405", "supplier_cnpj": "%s", "company_cnpj": "%s",'
    ' "product_code": "Q1", "product_description": "PECA Y", "current_sped_cfop": "1403"},'
    '{"supplier_cfop": "5405", "supplier_cnpj": "%s", "company_cnpj": "%s",'
    ' "product_code": "Q2", "product_description": "PECA Z", "current_sped_cfop": "1403"},'
    '{"supplier_cfop": "5102", "supplier_cnpj": "%s", "company_cnpj": "%s",'
    ' "product_code": "Q3", "product_description": "PECA W", "current_sped_cfop": "1102"},'
    '{"supplier_cfop": "5405", "supplier_cnpj": "66666666666666", "company_cnpj": "%s",'
    ' "product_code": "", "product_description": "SEM CODIGO", "current_sped_cfop": "1403"},'
    '{"supplier_cfop": "5405", "supplier_cnpj": "55555555555555", "company_cnpj": "%s",'
    ' "product_code": "*", "product_description": "CURINGA", "current_sped_cfop": "1403"}'
) % (FORN_A, EMPRESA, FORN_B, EMPRESA, FORN_A, EMPRESA, EMPRESA, EMPRESA)

db.add(AuditTask(
    id="task-lote", tenant_id=str(tA), status="concluido", user_id=str(u_adm),
    file_name="sped.txt",
    result_data='{"company_cnpj": "%s", "cfops_desconhecidos": [], "unmapped_items": [%s],'
                ' "unmapped_cte_items": [], "resumo": {"notas_processadas": 1}}' % (EMPRESA, ITENS)))
db.commit(); db.close()
TOKENS["adm"] = types.SimpleNamespace(id=str(u_adm), email="admin@synax.com")

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

def lote(**campos):
    dados = {"task_id": "task-lote", "tenant_id": str(tA)}
    dados.update(campos)
    return as_("adm").post("/api/mapping/cfop/bulk_by_cfop", data=dados)

def regras():
    s = SessionLocal()
    try:
        return s.query(CfopClassification).filter_by(tenant_id=str(tA)).all()
    finally:
        s.close()

results = []
def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("  OK  " if cond else " FALHA") + f" | {name}" + (f"  [{detail}]" if detail and not cond else ""))

print("\n=== 1. Sem CFOP de origem o lote recusa e nao grava ===")
r = lote(supplier_cfop="", entry_cfop="1102")
check("responde 200 (htmx precisa trocar o alvo)", r.status_code == 200, f"status={r.status_code}")
check("nao anuncia sucesso", "Sucesso" not in r.text)
check("diz o que fazer", "Selecione um CFOP de origem" in r.text)
check("nao pinta de verde", "emerald" not in r.text)
check("nada foi gravado", len(regras()) == 0, f"{len(regras())} regras")

print("\n=== 1b. Sem CFOP de entrada recusa com mensagem, nao com 422 mudo ===")
r = lote(supplier_cfop="5405", entry_cfop="")
check("responde 200, nao 422", r.status_code == 200, f"status={r.status_code}")
check("diz o que fazer", "Selecione o CFOP de entrada" in r.text)
check("nada foi gravado", len(regras()) == 0, f"{len(regras())} regras")

print("\n=== 2. CNPJ parcial casa como a tela casa ===")
r = lote(supplier_cfop="5405", entry_cfop="1556", cnpj_emitente="8888")
check("responde 200", r.status_code == 200, f"status={r.status_code}")
check("anuncia sucesso", "Sucesso" in r.text)
todas = regras()
check("gravou exatamente 1 regra", len(todas) == 1, f"{len(todas)} regras")
if todas:
    g = todas[0]
    check("fornecedor certo", g.supplier_cnpj == FORN_A, g.supplier_cnpj)
    check("CFOP de entrada certo", g.entry_cfop == "1556", g.entry_cfop)
    check("chave completa, sem curinga",
          all([g.company_cnpj, g.supplier_cnpj, g.supplier_cfop, g.product_code])
          and "*" not in (g.company_cnpj, g.supplier_cnpj, g.supplier_cfop, g.product_code),
          f"{g.company_cnpj}/{g.supplier_cnpj}/{g.supplier_cfop}/{g.product_code}")
    check("admin grava aprovado", g.status == "aprovado", g.status)

print("\n=== 3. Sem CNPJ o lote pega todos os fornecedores daquele CFOP ===")
r = lote(supplier_cfop="5405", entry_cfop="1556")
todas = regras()
check("agora sao 2 regras (Q1 e Q2)", len(todas) == 2, f"{len(todas)} regras")
check("nao criou regra para outro CFOP",
      all(g.supplier_cfop == "5405" for g in todas),
      str(sorted(g.supplier_cfop for g in todas)))
check("avisa os itens ignorados", "ignorados" in r.text.lower())

print("\n=== 4. Regra generica nunca e gravada ===")
todas = regras()
check("nenhum product_code vazio", all(g.product_code for g in todas))
check("nenhum campo de chave com '*'",
      all("*" not in (g.company_cnpj, g.supplier_cnpj, g.supplier_cfop, g.product_code) for g in todas),
      str([(g.supplier_cnpj, g.product_code) for g in todas]))
check("produtos gravados sao Q1 e Q2",
      sorted(g.product_code for g in todas) == ["Q1", "Q2"],
      str(sorted(g.product_code for g in todas)))

print("\n=== 5. Lote que nao casa nada nao mente ===")
antes = len(regras())
r = lote(supplier_cfop="5405", entry_cfop="1556", cnpj_emitente="12345678901234")
check("nao anuncia sucesso", "Sucesso" not in r.text)
check("nao pinta de verde", "emerald" not in r.text)
check("explica o que nao casou", "Nada foi gravado" in r.text)
check("banco intacto", len(regras()) == antes, f"{antes} -> {len(regras())}")

print("\n=== 6. Literais do <script> sao escapados ===")
r = lote(supplier_cfop="5405", entry_cfop="1102'+alert(1)+'", cnpj_emitente="8888")
check("aspa nao escapa do literal JS", "'+alert(1)+'" not in r.text.replace('"1102\'+alert(1)+\'"', ""))
r = lote(supplier_cfop="5405", entry_cfop="</script><script>alert(1)</script>", cnpj_emitente="8888")
check("fecha-script nao passa cru", "</script><script>" not in r.text)
check("veio escapado como \\u003c", "u003c" in r.text)

engine.dispose()
if os.path.exists("_lote_test.db"):
    try: os.remove("_lote_test.db")
    except Exception: pass

fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
