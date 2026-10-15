"""O período que uma rodada de informativo cobre.

Tudo em UTC, como o banco. As fronteiras de MÊS são as de Brasília: "mês
anterior" rodado às 02h do dia 1º (23h do dia 31 em UTC-3) não pode cair no mês
errado.

"Desde a última rodada" começa onde a última rodada BEM-SUCEDIDA terminou —
rodada que falhou ou foi interrompida não conta, e por isso nada cai no vão
entre dois envios. Na primeira vez, sem rodada anterior, vale a frequência:
um dia para a diária, sete para a semanal, trinta para a mensal.
"""
from datetime import datetime, timedelta

from ...core.fuso import FUSO_LOCAL_HORAS

_DIAS = {"24h": 1, "7d": 7, "15d": 15, "30d": 30}
_PRIMEIRA_VEZ = {"semanal": 7, "mensal": 30}


def _utc(local: datetime) -> datetime:
    return local - timedelta(hours=FUSO_LOCAL_HORAS)


def _local(utc: datetime) -> datetime:
    return utc + timedelta(hours=FUSO_LOCAL_HORAS)


def janela(periodo: str, agora: datetime, ultimo_fim: datetime | None = None,
           frequencia: str = "diaria") -> tuple[datetime, datetime]:
    """(início, fim) em UTC. O fim é sempre `agora`, menos no mês fechado."""
    if periodo in _DIAS:
        return agora - timedelta(days=_DIAS[periodo]), agora

    local = _local(agora)
    inicio_do_mes = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if periodo == "mes_atual":
        return _utc(inicio_do_mes), agora
    if periodo == "mes_anterior":
        ultimo_dia_anterior = inicio_do_mes - timedelta(days=1)
        inicio_anterior = ultimo_dia_anterior.replace(day=1)
        return _utc(inicio_anterior), _utc(inicio_do_mes)

    # "desde_ultima" (e qualquer valor desconhecido, por segurança)
    if ultimo_fim and ultimo_fim < agora:
        return ultimo_fim, agora
    return agora - timedelta(days=_PRIMEIRA_VEZ.get(frequencia, 1)), agora


def _fim_visivel(fim: datetime) -> datetime:
    """O fim da janela é EXCLUSIVO. Caindo à meia-noite (o "mês anterior"
    termina às 00:00 do dia 1º), mostrar "a 01/09 00:00" faz parecer que o
    dia 1º entrou — por isso a tela mostra o último minuto que de fato entrou."""
    local = _local(fim)
    if (local.hour, local.minute, local.second) == (0, 0, 0):
        return local - timedelta(minutes=1)
    return local


def rotulo(inicio: datetime, fim: datetime) -> str:
    """ "18/09/2026 07:00 a 25/09/2026 07:00", no horário de Brasília."""
    return f"{_local(inicio):%d/%m/%Y %H:%M} a {_fim_visivel(fim):%d/%m/%Y %H:%M}"


def rotulo_curto(inicio: datetime, fim: datetime) -> str:
    """ "18/09–25/09", para o assunto do e-mail."""
    return f"{_local(inicio):%d/%m}–{_fim_visivel(fim):%d/%m}"
