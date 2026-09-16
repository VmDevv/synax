"""Dois bugs reportados em produção:

1) DELETE de regra de CFOP respondia 200 mas NÃO apagava. Havia duas rotas
   registradas no mesmo caminho; o FastAPI usava a primeira, que não tratava
   rule_type='classification'. A linha sumia da tela e voltava ao recarregar.

2) Botão de mapear IPI não reagia. O id da linha continha cst_ipi, que vale "*",
   gerando o seletor "#row-ipi-5102-*" — inválido em CSS, então o HTMX não
   encontrava o alvo mesmo com a gravação funcionando.
"""
import os, sys, uuid, types, re
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_bug_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_bug_test.db"):
    os.remove("_bug_test.db")

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
from backend.app.domain.models.rules import CfopClassification, IpiCreditRule
from fastapi.testclient import TestClient

Base.metadata.create_all(bind=engine)
db = SessionLocal()
ut = uuid.uuid4(); t = str(ut)
db.add(Tenant(id=ut, name="Empresa X", cnpj="34449753000109"))
u_adm, u_ope = uuid.uuid4(), uuid.uuid4()
db.add_all([
    UserProfile(id=u_adm, tenant_id=None, role="admin", name="Admin"),
    UserProfile(id=u_ope, tenant_id=ut, role="operador", name="Operador"),
])
db.add(UserTenant(user_id=u_ope, tenant_id=ut))
# regra igual a do log de producao (product_code = "*")
db.add(CfopClassification(
    tenant_id=t, company_cnpj="34449753000109", supplier_cnpj="86527934000100",
    supplier_cfop="6401", product_code="*", entry_cfop="1401",
    product_description="Regra de teste", status="aprovado", created_by="admin"))
# auditoria com IPI pendente, cst_ipi = "*" (como o motor gera)
db.add(AuditTask(
    id="task-ipi", tenant_id=t, status="concluido", user_id=str(u_ope), file_name="sped.txt",
    result_data='{"unmapped_ipi_rules": [{"cfop": "5102", "cst_ipi": "*", "descr": "PECA X"}],'
                ' "unmapped_pis_cofins_rules": [{"cfop": "5102", "cst_expected": "50", "descr": "PECA X"}],'
                ' "resumo": {"notas_processadas": 1}}'))
db.commit(); db.close()
TOKENS["adm"] = types.SimpleNamespace(id=str(u_adm), email="admin@synax.com")
TOKENS["ope"] = types.SimpleNamespace(id=str(u_ope), email="oper@synax.com")

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

def n_regras():
    s = SessionLocal()
    try: return s.query(CfopClassification).filter_by(tenant_id=t, supplier_cfop="6401").count()
    finally: s.close()

print("\n=== BUG 1: exclusao de regra de CFOP ===")
check("rota DELETE nao esta mais duplicada",
      open('backend/app/api/routes/admin.py', encoding='utf-8').read()
      .count('@router.delete("/rules/{rule_type}/delete"') == 1)
check("regra existe antes", n_regras() == 1, f"n={n_regras()}")

url = (f"/admin/rules/classification/delete?tenant_id={t}"
       f"&company_cnpj=34449753000109&supplier_cnpj=86527934000100"
       f"&supplier_cfop=6401&product_code=*")
r = as_("adm").delete(url)
check("DELETE responde 200", r.status_code == 200, f"status={r.status_code}")
check("regra REALMENTE apagada do banco", n_regras() == 0, f"ainda restam {n_regras()}")

# o sintoma antigo: 200 sem apagar. Garante que 200 so vem quando apaga.
r = as_("adm").delete(url)
check("segunda tentativa devolve 404 (nao mente 200)", r.status_code == 404, f"status={r.status_code}")

print("\n=== BUG 2: alvo do botao de IPI ===")
r = as_("ope").get("/api/status/task-ipi")
check("tela de resultados abre para o operador", r.status_code == 200, f"status={r.status_code}")

alvos = re.findall(r'hx-target="(#row-ipi-[^"]*)"', r.text)
check("botoes de IPI presentes", len(alvos) >= 2, f"n={len(alvos)}")
check("nenhum seletor contem '*'", all("*" not in a for a in alvos), f"alvos={alvos}")
ids = re.findall(r'<tr id="(row-ipi-[^"]*)"', r.text)
check("id da linha sem '*'", all("*" not in i for i in ids), f"ids={ids}")
check("hx-target casa com o id existente",
      all(a.lstrip("#") in ids for a in alvos), f"alvos={alvos} ids={ids}")

alvos_pc = re.findall(r'hx-target="(#row-piscofins-[^"]*)"', r.text)
ids_pc = re.findall(r'<tr id="(row-piscofins-[^"]*)"', r.text)
check("PIS/COFINS tambem casa id e alvo",
      all(a.lstrip("#") in ids_pc for a in alvos_pc), f"alvos={alvos_pc} ids={ids_pc}")

print("\n=== BUG 2b: o mapeamento de IPI grava mesmo (operador) ===")
r = as_("ope").post("/api/mapping/ipi_rule",
                    data={"cfop": "5102", "cst_ipi": "00", "allow_credit": "true", "tenant_id": t})
check("gravacao responde 200", r.status_code == 200, f"status={r.status_code}")
s = SessionLocal()
regra = s.query(IpiCreditRule).filter_by(tenant_id=t, cfop="5102", cst_expected="00").first()
check("regra de IPI criada", regra is not None)
check("operador gera PENDENTE_REVISAO", regra and regra.status == "pendente_revisao",
      f"status={getattr(regra,'status',None)}")
s.close()

print("\n=== BUG 2c: IPI com CST curinga e recusado ===")
# O motor consulta (CFOP, CST). Gravar "*" no CST fazia uma resposta valer para
# qualquer CST daquele CFOP -- regra global por outro nome.
for cst_ruim, rotulo in [("*", "asterisco"), ("", "vazio")]:
    r = as_("ope").post("/api/mapping/ipi_rule",
                        data={"cfop": "5999", "cst_ipi": cst_ruim, "allow_credit": "true", "tenant_id": t})
    check(f"CST {rotulo} responde 200 (htmx precisa trocar o alvo)", r.status_code == 200,
          f"status={r.status_code}")
    check(f"CST {rotulo} explica a recusa na tela", "nao gravada" in r.text or "n\u00e3o gravada" in r.text,
          f"corpo={r.text[:120]}")
s = SessionLocal()
check("nenhuma regra curinga de IPI foi gravada",
      s.query(IpiCreditRule).filter_by(tenant_id=t, cfop="5999").first() is None)
s.close()

engine.dispose()
if os.path.exists("_bug_test.db"):
    try: os.remove("_bug_test.db")
    except Exception: pass

fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
