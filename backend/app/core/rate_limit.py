"""Rate limiting em memória (janela deslizante).

Escopo: deploy de servidor único, como é o caso hoje. Se um dia rodar com
múltiplos workers ou réplicas, cada processo terá seu próprio contador e o
limite efetivo será multiplicado — nesse cenário, trocar por Redis.

O contador vive no processo, não em tabela do banco: se ficasse numa tabela
exposta via PostgREST, o próprio usuário poderia zerá-lo.
"""
import os
import time
import logging
from collections import deque
from fastapi import Request, HTTPException

logger = logging.getLogger(__name__)

# chave -> (janela_seg, marcas). A janela vive junto do bucket porque a poda
# precisa usar a janela DAQUELE limite, não a de quem por acaso chamou.
_BUCKETS: dict[str, tuple[float, deque]] = {}
_LAST_SWEEP = {"ts": 0.0}
_SWEEP_INTERVAL = 300.0

# Quantos proxies confiáveis existem à frente da aplicação (0 = nenhum).
# Sem isto, o cliente manda `X-Forwarded-For: <ip aleatório>` e ganha um balde
# de limite novo a cada requisição — a proteção do login virava enfeite.
PROXIES_CONFIAVEIS = int(os.environ.get("TRUSTED_PROXY_HOPS", "0"))


def client_ip(request: Request) -> str:
    """IP real do cliente, considerando apenas proxies declaradamente confiáveis."""
    direto = request.client.host if request.client else "desconhecido"
    if PROXIES_CONFIAVEIS <= 0:
        return direto

    cadeia = [p.strip() for p in (request.headers.get("x-forwarded-for") or "").split(",") if p.strip()]
    # O proxy ANEXA à direita: o IP real é o n-ésimo de trás para frente. Ler o
    # primeiro da lista é ler literalmente o que o atacante escreveu no header.
    if len(cadeia) >= PROXIES_CONFIAVEIS:
        return cadeia[-PROXIES_CONFIAVEIS]
    return direto


def _sweep(agora: float) -> None:
    """Remove buckets ociosos para a memória não crescer indefinidamente.

    Cada bucket é podado com a SUA janela: usar a janela de quem chamou apagava
    marcas ainda válidas de limites mais longos — um POST em /api/auth/login
    (300s) zerava o contador de forgot-password (900s).
    """
    if agora - _LAST_SWEEP["ts"] < _SWEEP_INTERVAL:
        return
    _LAST_SWEEP["ts"] = agora
    for chave, (janela, marcas) in list(_BUCKETS.items()):
        while marcas and agora - marcas[0] > janela:
            marcas.popleft()
        if not marcas:
            _BUCKETS.pop(chave, None)


def consumir(chave: str, maximo: int, janela_seg: float) -> bool:
    """Registra um acesso. Devolve False se o limite foi estourado."""
    agora = time.monotonic()
    _sweep(agora)

    _, marcas = _BUCKETS.setdefault(chave, (janela_seg, deque()))
    _BUCKETS[chave] = (janela_seg, marcas)

    while marcas and agora - marcas[0] > janela_seg:
        marcas.popleft()
    if len(marcas) >= maximo:
        return False
    marcas.append(agora)
    return True


def limitar(nome: str, maximo: int, janela_seg: float, por_ip: bool = True):
    """Dependência do FastAPI que aplica o limite por IP."""
    def _dep(request: Request):
        chave = f"{nome}:{client_ip(request) if por_ip else 'global'}"
        if not consumir(chave, maximo, janela_seg):
            logger.warning("Rate limit atingido em '%s' por %s", nome, client_ip(request))
            raise HTTPException(
                status_code=429,
                detail="Muitas tentativas. Aguarde alguns instantes e tente novamente.",
                headers={"Retry-After": str(int(janela_seg))},
            )
        return True
    return _dep
