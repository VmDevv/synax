import sqlite3
import uuid
import sys
import os

# Ajustar PYTHONPATH para conseguir importar o backend a partir do scripts
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.app.infrastructure.database.session import engine
from sqlalchemy.orm import Session
from backend.app.domain.models.auth_models import Tenant
from backend.app.domain.models.rules import CfopClassification

def run_migration():
    legacy_db_path = "audit_rules.db"
    
    if not os.path.exists(legacy_db_path):
        print(f"Banco legado {legacy_db_path} não encontrado.")
        return

    # 1. Connect to legacy DB
    legacy_conn = sqlite3.connect(legacy_db_path)
    legacy_conn.row_factory = sqlite3.Row
    cursor = legacy_conn.cursor()

    try:
        # Pega todas as regras de classificação antigas
        cursor.execute("SELECT * FROM cfop_classifications")
        legacy_rules = cursor.fetchall()
        
        if not legacy_rules:
            print("Nenhuma regra legada encontrada para migrar.")
            return

        # Identifica CNPJs únicos
        unique_cnpjs = set(row['company_cnpj'] for row in legacy_rules if row['company_cnpj'])
        print(f"Encontrados {len(unique_cnpjs)} CNPJs únicos.")

        # 2. Connect to new DB via SQLAlchemy
        db = Session(bind=engine)
        
        try:
            # Dicionário para mapear CNPJ -> Tenant ID
            cnpj_to_tenant = {}
            
            # Cria ou busca os Tenants
            for cnpj in unique_cnpjs:
                existing_tenant = db.query(Tenant).filter(Tenant.cnpj == cnpj).first()
                if not existing_tenant:
                    new_tenant = Tenant(
                        name=f"Empresa Migrada - {cnpj}",
                        cnpj=cnpj
                    )
                    db.add(new_tenant)
                    db.commit()
                    print(f"Tenant criado: {new_tenant.name} ({new_tenant.id})")
                    cnpj_to_tenant[cnpj] = str(new_tenant.id)
                else:
                    cnpj_to_tenant[cnpj] = str(existing_tenant.id)
                    
            # 3. Insere as regras antigas no novo schema otimizado
            rules_added = 0
            
            # Buscar todas as regras existentes para esses tenants para checar in memory
            existing_rules = db.query(CfopClassification).filter(
                CfopClassification.tenant_id.in_(list(cnpj_to_tenant.values()))
            ).all()
            
            existing_keys = set(
                (r.company_cnpj, r.supplier_cnpj, r.supplier_cfop, r.product_code, r.tenant_id)
                for r in existing_rules
            )
            
            new_objects = []
            for row in legacy_rules:
                row_dict = dict(row)
                cnpj = row_dict['company_cnpj']
                tenant_id = cnpj_to_tenant.get(cnpj)
                if not tenant_id:
                    continue
                    
                key = (
                    cnpj,
                    row_dict.get('supplier_cnpj', '*'),
                    row_dict.get('supplier_cfop', '*'),
                    row_dict.get('product_code', '*'),
                    tenant_id
                )
                
                if key not in existing_keys:
                    new_rule = CfopClassification(
                        company_cnpj=cnpj,
                        supplier_cnpj=row_dict.get('supplier_cnpj', '*'),
                        supplier_cfop=row_dict.get('supplier_cfop', '*'),
                        product_code=row_dict.get('product_code', '*'),
                        tenant_id=tenant_id,
                        product_description=row_dict.get('product_description', 'Migrado do Legado'),
                        entry_cfop=row_dict.get('entry_cfop', ''),
                        status=row_dict.get('status', 'aprovado'),
                        is_global=row_dict.get('is_global', 0) == 1,
                        created_by="migracao@legado"
                    )
                    new_objects.append(new_rule)
                    existing_keys.add(key) # Prevent duplicates in same batch
                    rules_added += 1
                    
            if new_objects:
                db.add_all(new_objects)
                db.commit()
            print(f"Migração concluída com sucesso! {rules_added} regras migradas.")
            
        except Exception as e:
            db.rollback()
            print("Erro durante a migração no SQLAlchemy:", e)
        finally:
            db.close()
            
    except Exception as e:
        print("Erro ao ler banco legado:", e)
    finally:
        legacy_conn.close()

if __name__ == "__main__":
    run_migration()
