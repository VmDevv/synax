from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.app.domain.models import rules
from dotenv import load_dotenv
import os

load_dotenv('.env')
database_url = os.environ.get('DATABASE_URL')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)

engine = create_engine(database_url)
Session = sessionmaker(bind=engine)
session = Session()

# Check Credit Rules
credit_rules = session.query(rules.CfopCreditRule).all()
print(f'--- Credit Rules ({len(credit_rules)}) ---')
for r in credit_rules:
    print(f'CFOP: {r.cfop}, Credit: {r.allow_credit}, Status: {r.status}')

# Check CFOP Mappings
cfop_mappings = session.query(rules.CfopClassification).all()
print(f'\n--- CFOP Mappings ({len(cfop_mappings)}) ---')
for r in cfop_mappings:
    print(f'Supplier CFOP: {r.supplier_cfop} -> Entry CFOP: {r.entry_cfop}, Status: {r.status}, Tenant: {r.tenant_id}')

# Check CTE Mappings
cte_mappings = session.query(rules.CteCfopClassification).all()
print(f'\n--- CTE Mappings ({len(cte_mappings)}) ---')
for r in cte_mappings:
    print(f'Supplier CFOP: {r.supplier_cfop} (UF: {r.emitter_uf}) -> Entry CFOP: {r.entry_cfop}, Status: {r.status}')
