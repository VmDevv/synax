"""Configuração central de logging da aplicação.

Sem isto, o processo nunca chamava `logging.basicConfig`: o handler que sobra
é o `logging.lastResort` do próprio Python — só nível WARNING+, sem timestamp,
sem nome de módulo. Todo `logger.info(...)` espalhado pelo código (ex.:
integrações SIEG/ADN) ia para lugar nenhum, e um `logger.exception(...)`
aparecia no terminal como uma linha crua tipo `backend.app.x:ERROR:mensagem`.

`configurar_logging()` também é o ponto em que o handler de IA de diagnóstico
(ver ai_error_reporter.py) é pendurado no logger raiz — assim ele enxerga
literalmente qualquer ERROR/CRITICAL logado em qualquer módulo do sistema,
sem precisar tocar cada try/except individualmente.
"""
import logging

_FORMATO = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
_CONFIGURADO = False

# Caminhos que não viram linha no registro de acesso: a conferência do Painel
# Dev, que pergunta se o Synax está no ar a cada poucos minutos. Ela batia em
# /login e enchia o terminal com "127.0.0.1 GET /login 200" (21/10/2026).
SEM_REGISTRO_DE_ACESSO = ("/saude",)


class SemConferencia(logging.Filter):
    """Tira do log do uvicorn as linhas dos caminhos de SEM_REGISTRO_DE_ACESSO.
    O registro de acesso do uvicorn traz (cliente, método, caminho, versão,
    status) em `record.args`."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        caminho = str(args[2]).split("?", 1)[0] if len(args) >= 3 else ""
        return caminho not in SEM_REGISTRO_DE_ACESSO


def configurar_logging() -> None:
    global _CONFIGURADO
    if _CONFIGURADO:
        return
    _CONFIGURADO = True

    logging.basicConfig(level=logging.INFO, format=_FORMATO, datefmt="%Y-%m-%d %H:%M:%S")

    # Bibliotecas de rede são MUITO verbosas em INFO (uma linha por request);
    # sobem para WARNING para não afogar o terminal.
    for nome_ruidoso in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(nome_ruidoso).setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").addFilter(SemConferencia())

    from .ai_error_reporter import AIErrorHandler

    logging.getLogger().addHandler(AIErrorHandler())
