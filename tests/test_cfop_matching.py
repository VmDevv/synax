"""Regra de casamento de CFOP: chave EXATA de 4 campos, sem fallback genérico.

Decisão de negócio (12/08/2026): não existe regra genérica. A regra é sempre
unitária — empresa + fornecedor + CFOP de origem + produto. Se não houver regra
para aquele produto exato, o item volta para a fila de mapeamento.

O que isso protege: antes, uma regra criada para um produto passava a valer para
QUALQUER produto do mesmo fornecedor+CFOP. Um item novo era mapeado sozinho,
sem nunca passar por decisão humana.
"""
import os, sys, uuid, types, inspect
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_match_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_match_test.db"):
    os.remove("_match_test.db")

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
class _Auth:
    def get_user(self, token): raise Exception("nao usado")
class _Client:
    auth = _Auth()
sb.get_supabase_client = lambda: _Client()
sb.get_supabase_admin_client = lambda: _Client()

from backend.app.infrastructure.database.session import engine as _eng
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit("ABORTADO: engine nao e SQLite")

from backend.app.infrastructure.database.session import SessionLocal, engine
from backend.app.domain.models.base import Base
from backend.app.domain.models.rules import CfopClassification
from backend.app.infrastructure.repositories.rules_repository import RulesRepository

Base.metadata.create_all(bind=engine)
T = str(uuid.uuid4())
EMP, FORN = "11111111111111", "99999999999999"

db = SessionLocal()
# Produtos mapeados individualmente
for prod in ("P1", "P2"):
    db.add(CfopClassification(
        tenant_id=T, company_cnpj=EMP, supplier_cnpj=FORN, supplier_cfop="5102",
        product_code=prod, entry_cfop="1102", status="aprovado"))
# Mesmo fornecedor e CFOP, produto com destino DIFERENTE (exceção legítima)
db.add(CfopClassification(
    tenant_id=T, company_cnpj=EMP, supplier_cnpj=FORN, supplier_cfop="5102",
    product_code="P4", entry_cfop="1556", status="aprovado"))
# Regra ainda pendente: nunca pode influenciar o cálculo
db.add(CfopClassification(
    tenant_id=T, company_cnpj=EMP, supplier_cnpj=FORN, supplier_cfop="5656",
    product_code="P9", entry_cfop="1653", status="pendente_revisao"))
db.commit(); db.close()

# Reproduz exatamente como o motor monta o mapa
db = SessionLocal()
regras = RulesRepository(db, T).cfop_classifications.get_all()
cfop_mappings = {
    (r.company_cnpj, r.supplier_cnpj, r.supplier_cfop, r.product_code): r.entry_cfop
    for r in regras if getattr(r, 'status', 'aprovado') == 'aprovado'
}
db.close()

def resolver(cfop, produto):
    """Mesma expressão usada pelo motor: busca única, chave completa."""
    return cfop_mappings.get((EMP, FORN, cfop, produto))

results = []
def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("  OK  " if cond else " FALHA") + f" | {name}" + (f"  [{detail}]" if detail and not cond else ""))

print("\n=== 1. Produto mapeado casa a própria regra ===")
check("P1 -> 1102", resolver("5102", "P1") == "1102", f"={resolver('5102','P1')}")
check("P2 -> 1102", resolver("5102", "P2") == "1102", f"={resolver('5102','P2')}")

print("\n=== 2. Exceção por produto é respeitada ===")
check("P4 -> 1556 (destino próprio, não herda 1102)",
      resolver("5102", "P4") == "1556", f"={resolver('5102','P4')}")

print("\n=== 3. Produto NOVO volta para a fila (o ponto da mudança) ===")
check("P3 nunca mapeado fica pendente", resolver("5102", "P3") is None,
      f"herdou {resolver('5102','P3')} — vazamento entre produtos")
check("produto de CFOP conhecido mas inédito fica pendente",
      resolver("5102", "PZ") is None)

print("\n=== 4. Regra pendente não influencia cálculo ===")
check("P9 pendente não casa", resolver("5656", "P9") is None, f"={resolver('5656','P9')}")

print("\n=== 5. Sem vazamento por fornecedor/CFOP ===")
check("CFOP não cadastrado fica pendente", resolver("9999", "P1") is None)

print("\n=== 6. O motor implementa exatamente isso ===")
from backend.app.services.audit_engine import cross_checker as cc
src = inspect.getsource(cc)
check("busca única, sem fallback genérico",
      "mapped_cfop = cfop_mappings.get(mapping_key)" in src)
check("fallback antigo removido (nao ha cfop_map.get)", "cfop_map.get(" not in src)
check("fallback antigo removido (nao ha cfop_map = {})", "cfop_map = {}" not in src)

print("\n=== 7. Nenhum endpoint grava regra genérica ===")
from backend.app.api.routes import uploads as up
src_up = inspect.getsource(up)
check("nenhum product_code='*' gravado", 'product_code="*"' not in src_up,
      "regra generica nunca seria encontrada pelo motor")

engine.dispose()
if os.path.exists("_match_test.db"):
    try: os.remove("_match_test.db")
    except Exception: pass

fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
