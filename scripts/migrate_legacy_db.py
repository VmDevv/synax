import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Importa a engine de destino configurada (SQLite ou Supabase via .env)
from backend.app.infrastructure.database.session import engine as dest_engine
from backend.app.domain.models.rules import CfopCreditRule, CfopClassification, CteCfopClassification
from backend.app.domain.models.base import Base

# Engine legado (antigo audit_rules.db)
LEGACY_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "audit_rules.db")
legacy_engine = create_engine(f"sqlite:///{LEGACY_DB_PATH}")

# Garante que as tabelas existem no Supabase antes de inserir
Base.metadata.create_all(bind=dest_engine)

DestSession = sessionmaker(bind=dest_engine)

DEFAULT_TENANT_ID = "admin-tenant-id"

def migrate_data():
    print(f"Iniciando migração de {LEGACY_DB_PATH} para o banco de destino...")
    
    legacy_conn = legacy_engine.raw_connection()
    c_legacy = legacy_conn.cursor()
    
    session_new = DestSession()
    
    # 1. Migrar cfop_credit_rules
    try:
        c_legacy.execute("SELECT cfop, allow_credit, description FROM cfop_credit_rules")
        rows = c_legacy.fetchall()
        rules_to_insert = []
        for row in rows:
            rules_to_insert.append(CfopCreditRule(
                cfop=row[0],
                tenant_id=DEFAULT_TENANT_ID,
                allow_credit=bool(row[1]),
                description=row[2],
                status="aprovado"
            ))
        if rules_to_insert:
            session_new.bulk_save_objects(rules_to_insert)
            session_new.commit()
        print(f"Migrados {len(rows)} registros para cfop_credit_rules.")
    except Exception as e:
        print(f"Erro ao migrar cfop_credit_rules: {e}")
        session_new.rollback()

    # 2. Migrar cfop_classifications
    try:
        c_legacy.execute("SELECT company_cnpj, supplier_cnpj, supplier_cfop, product_code, product_description, entry_cfop FROM cfop_classifications")
        rows = c_legacy.fetchall()
        rules_to_insert = []
        for row in rows:
            rules_to_insert.append(CfopClassification(
                company_cnpj=row[0],
                supplier_cnpj=row[1],
                supplier_cfop=row[2],
                product_code=row[3],
                tenant_id=DEFAULT_TENANT_ID,
                product_description=row[4],
                entry_cfop=row[5],
                status="aprovado"
            ))
        # Quebrar em lotes para evitar sobrecarga de memória se for gigante
        batch_size = 5000
        for i in range(0, len(rules_to_insert), batch_size):
            session_new.bulk_save_objects(rules_to_insert[i:i+batch_size])
            session_new.commit()
            print(f"Lote {i} a {i+batch_size} inserido.")
        print(f"Migrados {len(rows)} registros para cfop_classifications.")
    except Exception as e:
        print(f"Erro ao migrar cfop_classifications: {e}")
        session_new.rollback()

    # 3. Migrar cte_cfop_classifications
    try:
        c_legacy.execute("SELECT company_cnpj, supplier_cfop, emitter_uf, entry_cfop FROM cte_cfop_classifications")
        rows = c_legacy.fetchall()
        rules_to_insert = []
        for row in rows:
            rules_to_insert.append(CteCfopClassification(
                company_cnpj=row[0],
                supplier_cfop=row[1],
                emitter_uf=row[2],
                tenant_id=DEFAULT_TENANT_ID,
                entry_cfop=row[3],
                status="aprovado"
            ))
        if rules_to_insert:
            session_new.bulk_save_objects(rules_to_insert)
            session_new.commit()
        print(f"Migrados {len(rows)} registros para cte_cfop_classifications.")
    except Exception as e:
        print(f"Erro ao migrar cte_cfop_classifications: {e}")
        session_new.rollback()

    legacy_conn.close()
    session_new.close()
    print("Migração concluída com sucesso!")

if __name__ == "__main__":
    migrate_data()
