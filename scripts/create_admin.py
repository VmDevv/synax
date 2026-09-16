import os
from sqlalchemy import text
from backend.app.infrastructure.database.session import SessionLocal

def link_user_to_tenant():
    db = SessionLocal()
    try:
        # Pega o id do tenant VmHub
        tenant_result = db.execute(text("SELECT id FROM tenants WHERE name = 'VmHub Contabilidade'"))
        tenant = tenant_result.fetchone()
        
        if not tenant:
            print("Empresa 'VmHub Contabilidade' não encontrada.")
            return

        tenant_id = tenant[0]

        # Pega o id do primeiro usuário criado no auth.users
        user_result = db.execute(text("SELECT id, email FROM auth.users LIMIT 1"))
        user = user_result.fetchone()

        if not user:
            print("Nenhum usuário encontrado no Supabase Auth. Você já criou o usuário lá?")
            return

        user_id = user[0]
        user_email = user[1]

        # Insere no user_profiles
        db.execute(
            text("""
            INSERT INTO user_profiles (id, tenant_id, role, name) 
            VALUES (:uid, :tid, 'admin', 'Admin VmHub')
            ON CONFLICT (id) DO UPDATE SET role = 'admin', tenant_id = :tid
            """),
            {"uid": user_id, "tid": tenant_id}
        )
        db.commit()
        print(f"Sucesso! O usuário {user_email} agora é Admin da VmHub Contabilidade.")
        
    except Exception as e:
        print(f"Erro: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    link_user_to_tenant()
