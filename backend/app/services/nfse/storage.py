"""Guarda dos XMLs de NFS-e em disco, organizados por CNPJ.

Caminho: <RAIZ>/<CNPJ do tomador>/<AAAA-MM>/<arquivo>.xml

O diretório raiz vem de NFSE_STORAGE_DIR (.env). O padrão é o compartilhamento
do escritório; em desenvolvimento cai numa pasta local, para não depender da rede.
"""
import os
import io
import re
import logging
import zipfile
from datetime import datetime

logger = logging.getLogger(__name__)

RAIZ_PADRAO = r"\\srv-ap02\Desenvolvimento\NFS-E"


def raiz() -> str:
    return os.environ.get("NFSE_STORAGE_DIR") or RAIZ_PADRAO


def _seguro(parte: str) -> str:
    """Impede que dado vindo do XML escape do diretório (../, barras, etc.)."""
    limpo = re.sub(r"[^A-Za-z0-9._-]", "", str(parte or ""))
    return limpo or "desconhecido"


def caminho_relativo(cnpj_tomador: str, competencia: datetime | None, nome: str) -> str:
    pasta_mes = competencia.strftime("%Y-%m") if competencia else "sem-competencia"
    return os.path.join(_seguro(cnpj_tomador), _seguro(pasta_mes), _seguro(nome))


def gravar(cnpj_tomador: str, competencia: datetime | None, nome: str, conteudo: bytes) -> str | None:
    """Grava o XML e devolve o caminho relativo. None se a raiz estiver indisponível."""
    rel = caminho_relativo(cnpj_tomador, competencia, nome)
    destino = os.path.join(raiz(), rel)
    try:
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        with open(destino, "wb") as f:
            f.write(conteudo)
        return rel
    except OSError:
        # Rede fora do ar não pode derrubar a importação: o registro continua
        # válido no banco, apenas sem o arquivo guardado.
        logger.warning("Não foi possível gravar NFS-e em %s", destino, exc_info=True)
        return None


def ler(rel: str) -> bytes | None:
    if not rel:
        return None
    caminho = os.path.join(raiz(), rel)
    try:
        with open(caminho, "rb") as f:
            return f.read()
    except OSError:
        return None


def zipar(itens: list[tuple[str, str]]) -> io.BytesIO:
    """Monta um ZIP em memória. `itens` = [(caminho_relativo, nome_no_zip)]."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, nome in itens:
            dados = ler(rel)
            if dados is not None:
                z.writestr(nome, dados)
    buffer.seek(0)
    return buffer


def disponivel() -> bool:
    """A raiz está acessível para escrita?"""
    try:
        os.makedirs(raiz(), exist_ok=True)
        return os.access(raiz(), os.W_OK)
    except OSError:
        return False
