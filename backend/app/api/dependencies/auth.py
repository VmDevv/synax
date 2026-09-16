import hashlib
import time
from fastapi import Request
from backend.app.infrastructure.auth.supabase_client import get_supabase_client

# Validar o token chamava a API HTTP do Supabase Auth em TODA requisição — inclusive
# nos polls de 2s da auditoria — somando centenas de ms a cada tela. Guardamos o
# resultado por poucos segundos: janela curta o bastante para que revogação de
# acesso continue surtindo efeito quase imediato.
_TOKEN_TTL = 30.0
_token_cache: dict[str, tuple[float, object]] = {}


def _chave_cache(token: str) -> str:
    """O JWT inteiro era usado como chave, ficando em claro num dict do processo
    (visível em qualquer dump de memória ou depurador). O hash serve igual para
    indexar e não é reutilizável como credencial."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cached_user(token: str):
    hit = _token_cache.get(_chave_cache(token))
    if hit and (time.monotonic() - hit[0]) < _TOKEN_TTL:
        return hit[1]
    return None


def _store_user(token: str, user):
    # Poda simples para o cache não crescer sem limite em processos longos.
    if len(_token_cache) > 512:
        agora = time.monotonic()
        for k, (ts, _) in list(_token_cache.items()):
            if (agora - ts) >= _TOKEN_TTL:
                _token_cache.pop(k, None)
    _token_cache[_chave_cache(token)] = (time.monotonic(), user)


def invalidar_token(token: str) -> None:
    """Descarta a entrada do cache. Chamado no logout: sem isso, o token recém
    revogado continuava aceito por até _TOKEN_TTL segundos."""
    if token:
        _token_cache.pop(_chave_cache(token), None)


def extrair_token(request: Request) -> str | None:
    """Token da requisição: header Authorization ou cookie access_token."""
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ")[1]

    cookie_token = request.cookies.get("access_token")
    if cookie_token:
        return cookie_token.replace("Bearer ", "").strip()

    return None


def get_current_user(request: Request):
    token = extrair_token(request)
    if not token:
        return None

    cached = _cached_user(token)
    if cached is not None:
        return cached

    try:
        supabase = get_supabase_client()
        user_resp = supabase.auth.get_user(token)
        _store_user(token, user_resp.user)
        return user_resp.user
    except Exception:
        return None
