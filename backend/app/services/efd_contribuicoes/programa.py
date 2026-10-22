"""A ponte para o programa SPED PIS/COFINS, que gera a EFD-Contribuições do Lucro Presumido a partir do Domínio.

Pedido do Sandro em 22/10/2026: a página do Lucro Presumido dentro do Synax, "do jeito mais leve, prático e fácil de
manter e de achar erro". Por isso o Synax NÃO tem gerador próprio: chama o programa (`Desktop\\sped-pis-cofins`) pela
linha de comando e lê o resultado em JSON — o contrato está em `spedpc/__main__.py`, `_contrato`. Um motor só: a
correção fiscal feita lá vale nas duas telas, o Synax não instala ODBC nem herda as bibliotecas de lá, e a conexão
com o Domínio continua configurada num lugar só (a página Configurações do programa). Se o motor quebrar, quebra o
processo dele — a frase chega na tela e o servidor do Synax segue de pé.

Três regras:

- **Argumentos em lista, nunca shell.** O código da empresa e a competência vêm do formulário.
- **A última linha da saída é o JSON** (antes dela, só as linhas de progresso do lote). `{"erro": ...}` vira
  `FalhaDoPrograma` com a frase do programa; saída que não é JSON vira falha, com o fim do stderr no log.
- **O contrato tem versão.** Se o programa mudar de forma incompatível, a tela diz isso em vez de mostrar número
  no campo errado.
"""
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

CONTRATO = 1
# A raiz do Synax (backend/app/services/efd_contribuicoes/programa.py -> quatro acima). O programa mora ao lado dela
# na área de trabalho; SPEDPC_PASTA aponta outro lugar se um dia mudar.
RAIZ_SYNAX = Path(__file__).resolve().parents[4]
# O Python que tem o Streamlit, o pandas e o pyodbc de lá. NÃO é o venv do Synax, e o `python` do PATH, na máquina
# do servidor, é o atalho da Microsoft Store, que não roda nada.
PYTHON_PADRAO = ["py", "-3.14"]
SEM_JANELA = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TEMPO_CONSULTA_S = 90
TEMPO_GERAR_S = 300
VALIDADE_EMPRESAS_S = 300


class ProgramaIndisponivel(Exception):
    """O programa ou o Python dele não estão nesta máquina. A mensagem vai para a tela."""


class FalhaDoPrograma(Exception):
    """O programa rodou e disse que não deu (Domínio fora do ar, empresa inexistente...). A mensagem vai para a tela."""


def pasta() -> Path:
    return Path(os.getenv("SPEDPC_PASTA") or RAIZ_SYNAX.parent / "sped-pis-cofins")


def _python() -> list[str]:
    caminho = os.getenv("SPEDPC_PYTHON", "").strip()
    return [caminho] if caminho else list(PYTHON_PADRAO)


def comando(args: list[str]) -> tuple[list[str], Path, dict]:
    """(linha de comando, pasta onde rodar, ambiente). Separado de `executar` porque o lote usa o mesmo comando
    num processo que fica rodando."""
    raiz = pasta()
    if not (raiz / "spedpc" / "__main__.py").exists():
        raise ProgramaIndisponivel(f"O programa SPED PIS/COFINS não foi encontrado em {raiz}.")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return [*_python(), "-m", "spedpc", *args], raiz, env


def ler_resposta(stdout: str, stderr: str = "", codigo=None) -> dict:
    linhas = [linha for linha in (stdout or "").splitlines() if linha.strip()]
    try:
        dados = json.loads(linhas[-1]) if linhas else None
    except ValueError:
        dados = None
    if not isinstance(dados, dict):
        logger.error("SPED PIS/COFINS respondeu fora do contrato (código %s): %s", codigo,
                     ((stderr or "") + (stdout or ""))[-2000:])
        raise FalhaDoPrograma("O programa SPED PIS/COFINS não respondeu no formato combinado — o motivo está no log "
                              "do servidor.")
    if dados.get("contrato") != CONTRATO:
        raise FalhaDoPrograma(f"O programa SPED PIS/COFINS está no contrato {dados.get('contrato')} e o Synax espera "
                              f"o {CONTRATO}: os dois precisam ser atualizados juntos.")
    if "erro" in dados:
        raise FalhaDoPrograma(str(dados["erro"]))
    return dados


def executar(args: list[str], timeout: float = TEMPO_CONSULTA_S) -> dict:
    cmd, raiz, env = comando(args)
    try:
        r = subprocess.run(cmd, cwd=raiz, env=env, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, creationflags=SEM_JANELA)
    except FileNotFoundError as exc:
        raise ProgramaIndisponivel(f"O Python do programa SPED PIS/COFINS ({' '.join(_python())}) não foi "
                                   f"encontrado nesta máquina.") from exc
    except subprocess.TimeoutExpired as exc:
        raise FalhaDoPrograma(f"O programa SPED PIS/COFINS não respondeu em {int(timeout)} s.") from exc
    return ler_resposta(r.stdout, r.stderr, r.returncode)


# A lista de empresas é uma consulta ao Domínio a cada pedido; a tela a pede ao abrir e a cada geração (para conferir
# o acesso). Cinco minutos de cache poupam o Domínio, e "Atualizar a lista" força.
_cache = {"ts": 0.0, "lista": None}
_trava_cache = threading.Lock()


def empresas(atualizar: bool = False) -> list[dict]:
    """[{"codi_emp", "nome", "cnpj", "presumido"}] — as matrizes ativas do Domínio."""
    with _trava_cache:
        if not atualizar and _cache["lista"] is not None and time.time() - _cache["ts"] < VALIDADE_EMPRESAS_S:
            return _cache["lista"]
    lista = executar(["empresas"])["empresas"]
    with _trava_cache:
        _cache.update(ts=time.time(), lista=lista)
    return lista


def esquecer_empresas() -> None:
    with _trava_cache:
        _cache.update(ts=0.0, lista=None)


def info() -> dict:
    """{"pasta_padrao", "conexao"} — a conexão vem sem a senha."""
    return executar(["info"])


def gerar(codi_emp: int, competencia: str, destino: Path) -> dict:
    """A EFD de uma empresa num mês (`competencia` em AAAA-MM), gravada em `destino`, já validada e conferida contra a
    apuração do Domínio. O dicionário é o do contrato (`spedpc/__main__.py`, `_responder`)."""
    ano, mes = competencia.split("-")
    return executar(["gerar", "--empresa", str(int(codi_emp)), "--competencia", f"{mes}/{ano}",
                     "--saida", str(destino), "--json"], timeout=TEMPO_GERAR_S)


def args_do_lote(competencia: str, codigos: list[int], origem: str) -> list[str]:
    ano, mes = competencia.split("-")
    return ["lote", "--competencia", f"{mes}/{ano}", "--empresas", *[str(int(c)) for c in codigos],
            "--origem", origem, "--json"]


def historico(limite: int = 20) -> list[dict]:
    return executar(["historico", "--limite", str(int(limite))])["execucoes"]


def execucao(ident: str) -> dict | None:
    lista = executar(["historico", "--id", str(ident)])["execucoes"]
    return lista[0] if lista else None


# ------------------------------------------------------------------------------- a página Domínio (só leitura)
# As tabelas vêm como {"colunas", "linhas", "total"}: `linhas` pode vir cortada no limite, `total` é o de verdade.
def _mes(competencia: str) -> str:
    ano, mes = competencia.split("-")
    return f"{mes}/{ano}"


def testar() -> str:
    """A frase do driver ("Conectado: SQL Anywhere"). Falha vira FalhaDoPrograma com o motivo."""
    return executar(["testar"])["mensagem"]


def cadastro(codi_emp: int, competencia: str) -> dict | None:
    """Regime de PIS/COFINS no cadastro e a apuração gravada no mês; None se o CNPJ não está no Domínio."""
    return executar(["cadastro", "--empresa", str(int(codi_emp)), "--competencia", _mes(competencia)])["cadastro"]


def notas(codi_emp: int, competencia: str, excel: Path | None = None) -> dict:
    """{"saidas", "entradas", "por_cfop", "notas", "excel"} — o Excel leva as notas inteiras."""
    args = ["notas", "--empresa", str(int(codi_emp)), "--competencia", _mes(competencia)]
    return executar(args + (["--excel", str(excel)] if excel else []), timeout=TEMPO_GERAR_S)


def consultas_prontas() -> list[dict]:
    """[{"nome", "sql"}] — os arquivos de `sql/` do programa."""
    return executar(["consultas"])["consultas"]


def tabelas(esquema: str = "bethadba", filtro: str = "") -> dict:
    return executar(["tabelas", "--esquema", esquema, "--filtro", filtro])["tabelas"]


def colunas(tabela: str, esquema: str = "bethadba") -> dict:
    return executar(["colunas", "--tabela", tabela, "--esquema", esquema])["colunas"]


def consulta(arquivo_sql: Path, codi_emp: int | None = None, competencia: str | None = None,
             excel: Path | None = None) -> dict:
    """Consulta livre, só leitura — a trava (`odbc._somente_leitura`) é a do programa. O SQL vai num ARQUIVO: texto
    livre na linha de comando do Windows tem limite de tamanho e regra de aspas própria."""
    args = ["consulta", "--arquivo-sql", str(arquivo_sql)]
    if codi_emp is not None:
        args += ["--empresa", str(int(codi_emp))]
    if competencia:
        args += ["--competencia", _mes(competencia)]
    if excel:
        args += ["--excel", str(excel)]
    return executar(args, timeout=TEMPO_GERAR_S)
