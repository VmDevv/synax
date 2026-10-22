"""Autorização centralizada.

IMPORTANTE: o RLS do Postgres NÃO protege esta aplicação. A conexão usa o papel
`postgres`, que tem `rolbypassrls = true`, e a maioria das tabelas sequer tem RLS
habilitado. Portanto o isolamento entre tenants precisa ser garantido AQUI, na
camada de aplicação, em toda query que toque dados de cliente.
"""
from typing import List, Optional
from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from ..infrastructure.database.session import get_db
from ..api.dependencies.auth import get_current_user
from ..domain.models.auth_models import UserProfile


# Papéis da equipe do escritório. Ferramentas internas (ex.: gerador de e-mail
# de apontamentos) são restritas a eles; o cliente final não as acessa.
INTERNAL_ROLES = ('admin', 'operador')


def get_profile(current_user=Depends(get_current_user), db: Session = Depends(get_db)) -> UserProfile:
    """Perfil local do usuário autenticado. 401 sem login, 403 sem provisionamento."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Autenticação necessária.")
    profile = db.query(UserProfile).filter(UserProfile.id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=403, detail="Usuário não provisionado.")
    return profile


def allowed_tenant_ids(profile: UserProfile) -> Optional[List[str]]:
    """Tenants que o perfil pode acessar. None significa 'todos' (apenas admin)."""
    if profile.role == 'admin':
        return None
    if profile.role == 'operador':
        return [str(t.id) for t in (profile.allowed_tenants or [])]
    return [str(profile.tenant_id)] if profile.tenant_id else []


def assert_tenant_access(profile: UserProfile, tenant_id) -> str:
    """Garante que o perfil pode operar no tenant informado. Devolve o tenant_id validado."""
    allowed = allowed_tenant_ids(profile)
    if allowed is None:
        return str(tenant_id)
    if str(tenant_id) not in allowed:
        raise HTTPException(status_code=403, detail="Acesso negado a esta empresa.")
    return str(tenant_id)


def resolve_tenant_for_write(profile: UserProfile, requested_tenant_id) -> str:
    """Tenant efetivo de uma escrita.

    - admin: usa o que foi pedido;
    - operador: precisa estar entre os vinculados;
    - demais papéis: travados no próprio tenant, ignorando o que veio do cliente.
    """
    if profile.role == 'admin':
        if not requested_tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id é obrigatório.")
        return str(requested_tenant_id)
    if profile.role == 'operador':
        return assert_tenant_access(profile, requested_tenant_id)
    if not profile.tenant_id:
        raise HTTPException(status_code=403, detail="Usuário sem empresa vinculada.")
    return str(profile.tenant_id)


def rule_status_for(profile: UserProfile) -> str:
    """Status com que uma regra confirmada na auditoria entra no banco.

    Maker-Checker: o admin é o revisor, então o que ele confirma já vale.
    Qualquer outro papel (operador, cliente) gera sugestão para a Curadoria.
    """
    return 'aprovado' if profile.role == 'admin' else 'pendente_revisao'


def assert_internal(profile: UserProfile) -> UserProfile:
    """Restringe a ferramenta à equipe do escritório (admin/operador)."""
    if profile.role not in INTERNAL_ROLES:
        raise HTTPException(status_code=403, detail="Recurso restrito à equipe interna.")
    return profile


def pode_sql_dominio(profile: UserProfile) -> bool:
    """Consulta livre (SQL) e explorador de tabelas do Domínio: o admin sempre; o operador só com a marca que um
    admin dá na edição do usuário. A consulta lê qualquer empresa do Domínio — por isso não vem junto com o papel."""
    if profile.role == 'admin':
        return True
    return profile.role == 'operador' and bool(getattr(profile, 'pode_sql_dominio', False))


def get_task_or_403(db: Session, profile: UserProfile, task_id: str):
    """Carrega uma AuditTask garantindo que ela pertence a um tenant do perfil.

    Responde 404 tanto para inexistente quanto para tenant alheio, para não
    revelar a existência de tarefas de outros clientes.
    """
    from ..domain.models.audit import AuditTask
    task = db.query(AuditTask).filter(AuditTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Auditoria não encontrada.")
    allowed = allowed_tenant_ids(profile)
    if allowed is not None and str(task.tenant_id) not in allowed:
        raise HTTPException(status_code=404, detail="Auditoria não encontrada.")
    return task
