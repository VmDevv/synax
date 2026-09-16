"""Conversão do horário do banco para o horário de quem lê a tela.

Todo `created_at` do sistema é gravado com `datetime.utcnow()` (ver
`TimestampMixin`). O escritório trabalha em horário de Brasília, três horas
atrás — então o Radar de Atividade mostrava uma auditoria das 15h como se fosse
das 18h, e tudo que era feito depois das 21h entrava no dia seguinte.

O deslocamento é FIXO. O Brasil não tem horário de verão desde 2019, e todas as
empresas da carteira estão em UTC-3. Se algum dia entrar cliente no Acre
(UTC-5) ou no Amazonas (UTC-4), isto aqui vira o único ponto a mudar — e a
mudança certa passa a ser guardar o fuso no `Tenant`, não trocar a constante.
"""
from datetime import timedelta

FUSO_LOCAL_HORAS = -3


def hora_local(dt):
    """UTC do banco -> horário de Brasília. `None` continua `None`."""
    if not dt:
        return None
    return dt + timedelta(hours=FUSO_LOCAL_HORAS)


def hora_br(dt, formato: str = "%d/%m/%Y %H:%M") -> str:
    """Filtro de template: já converte e já formata. Vazio vira travessão."""
    local = hora_local(dt)
    if not local:
        return "—"
    try:
        return local.strftime(formato)
    except (AttributeError, ValueError):
        return "—"
