import os
import sys

# Adicionar a raiz do projeto ao path para importar modulos do backend
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.infrastructure.database.session import SessionLocal
from backend.app.domain.models.rules import CfopCreditRule, PisCofinsCreditRule
from backend.app.domain.models.auth_models import Tenant

def seed_rules():
    """Exceções de um cliente específico. NAO semeia regra global — ver comentário abaixo."""
    db = SessionLocal()
    try:
        # NAO semear regra global. As seções 1 a 4 deste script gravavam 26 regras
        # de ICMS, 6 de IPI, 6 de PIS/COFINS e 2 de antecipação sob um tenant
        # fantasma "GLOBAL" com is_global=True, e o motor as enxergava em TODAS as
        # empresas: crédito decidido por regra que ninguém daquela empresa aprovou.
        # Decisão do escritório (25/08/2026): cada tenant cadastra as suas pela
        # Curadoria. Para apagar o que ficou no banco: scripts/limpar_regras_globais.py

        # 5. Exceções do Cliente (ICMS, IPI, PIS/COFINS, Antecipação)
        print("Verificando Tenant Específico (50421644000121)...")
        target_cnpj = "50421644000121"
        tenant = db.query(Tenant).filter(Tenant.cnpj == target_cnpj).first()
        
        if tenant:
            print(f"Tenant encontrado! ID: {tenant.id}. Inserindo exceções...")
            
            # ICMS Exceções
            exceptions = [
                ("1949", False, "Outra entrada de mercadoria ou prestação de serviço não especificada (Exceção sem crédito)"),
                ("2949", False, "Outra entrada de mercadoria ou prestação de serviço não especificada (Interestadual, sem crédito)")
            ]
            for cfop, allow, desc in exceptions:
                exists = db.query(CfopCreditRule).filter(CfopCreditRule.cfop == cfop, CfopCreditRule.tenant_id == str(tenant.id)).first()
                if not exists:
                    rule = CfopCreditRule(cfop=cfop, tenant_id=str(tenant.id), allow_credit=allow, description=desc, status="aprovado", is_global=False)
                    db.add(rule)
                    
            # PIS/COFINS Exceções (ex: 1949 com CST 50 mas não toma crédito por regra interna)
            pis_cofins_exceptions = [
                ("1949", "50", False, "Exceção Interna: 1949 com CST 50 não gera crédito PIS/COFINS"),
                ("2949", "50", False, "Exceção Interna: 2949 com CST 50 não gera crédito PIS/COFINS")
            ]
            for cfop, cst, allow, desc in pis_cofins_exceptions:
                exists = db.query(PisCofinsCreditRule).filter(PisCofinsCreditRule.cfop == cfop, PisCofinsCreditRule.cst_expected == cst, PisCofinsCreditRule.tenant_id == str(tenant.id)).first()
                if not exists:
                    rule = PisCofinsCreditRule(cfop=cfop, cst_expected=cst, tenant_id=str(tenant.id), allow_credit=allow, description=desc, status="aprovado", is_global=False)
                    db.add(rule)
        else:
            print(f"ATENÇÃO: A empresa com CNPJ {target_cnpj} ainda não está cadastrada no banco de dados.")

        db.commit()
        print("Seeding concluido com sucesso!")
        
    except Exception as e:
        db.rollback()
        print(f"Erro ao popular banco de dados: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    seed_rules()
