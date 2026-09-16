from fastapi import Depends
from sqlalchemy import text
from backend.app.infrastructure.database.session import SessionLocal
from backend.app.api.dependencies.auth import get_current_user
from backend.app.domain.models.auth_models import UserProfile

def get_rls_db(current_user = Depends(get_current_user)):
    """
    Injeta o contexto do Tenant e Role (RLS) na sessão do banco.

    ATENÇÃO — ESTA CAMADA HOJE NÃO PROTEGE NADA:
    a aplicação conecta como `postgres`, papel com BYPASSRLS, e a maioria das
    tabelas nem tem RLS habilitado. Ou seja, estas variáveis são definidas mas
    ignoradas pelo Postgres. O isolamento real está no filtro em Python
    (core/authz.py + BaseRepository). Para tornar esta camada efetiva, aplique
    scripts/02_hardening_rls.sql e troque o usuário da conexão.
    NUNCA trate get_rls_db como substituto do filtro explícito de tenant.
    """
    db = SessionLocal()
    try:
        if current_user:
            # 1. Pega o perfil do usuário para saber qual empresa e qual cargo ele tem
            profile = db.query(UserProfile).filter(UserProfile.id == current_user.id).first()
            if profile:
                # 2. Configura a sessão do Postgres (SET LOCAL) para este usuário.
                # O 'true' no set_config significa "apenas para a transação atual".
                #
                # As quatro chamadas vão num SELECT só de propósito: eram quatro
                # `db.execute()` separados, e cada um é uma ida à rede. Com o
                # banco em us-east-1 e o servidor no Brasil, são ~150 ms cada —
                # meio segundo por abertura de tela gasto em configuração que o
                # Postgres resolve de uma vez. Mesma transação, mesmo efeito.
                # Operador acessa várias empresas: a política de RLS lê esta lista.
                allowed = ",".join(str(t.id) for t in (profile.allowed_tenants or [])) \
                    if profile.role == 'operador' else ""
                db.execute(
                    text("SELECT set_config('app.current_tenant', :tenant_id, true),"
                         "       set_config('app.current_role', :role, true),"
                         "       set_config('app.current_user_id', :user_id, true),"
                         "       set_config('app.allowed_tenants', :allowed, true)"),
                    {"tenant_id": str(profile.tenant_id), "role": str(profile.role),
                     "user_id": str(current_user.id), "allowed": allowed})
            else:
                db.execute(text("SELECT set_config('app.current_role', 'anon', true)"))
        else:
            db.execute(text("SELECT set_config('app.current_role', 'anon', true)"))

        yield db
    finally:
        db.close()
