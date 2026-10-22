"""Código gravado DEPOIS que o servidor subiu: o que só vale com reinício.

O servidor roda com `--reload`, mas desde 13/10/2026 o recarregamento não
dispara: o `.py` gravado fica no disco e o processo segue com a versão antiga,
sem aviso nenhum — a mudança "entregue" não está no ar, e ninguém percebe até
alguém conferir a hora do processo. Este módulo responde "há reinício
pendente?" para a página de Administração.

O retrato é tirado quando o processo importa este módulo, na subida da
aplicação (`api/routes/admin.py` o importa no topo). Depois, um arquivo conta
como pendente se o CONTEÚDO mudou — a data de gravação sozinha não basta,
porque regravar um arquivo igual (cópia, checkout) mexe na data sem mudar
nada — e também se surgiu ou sumiu desde então.

O que conta: todo `.py` do backend e os dados que o validador de SPED guarda na
memória ao subir (`sped_validador/dados/*.json`, lidos com `lru_cache`).
Template `.html` e `novidades.json` NÃO contam: são lidos a cada acesso e já
valem na hora.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import datetime

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # backend/app
DADOS_LIDOS_NA_SUBIDA = ("services/sped_validador/dados/",)


@dataclass(frozen=True)
class Alterado:
    caminho: str          # relativo a backend/app, com "/"
    situacao: str         # "alterado", "novo" ou "removido"
    quando: datetime | None


def pede_reinicio(relativo: str) -> bool:
    relativo = relativo.replace(os.sep, "/")
    if relativo.endswith(".py"):
        return True
    return relativo.endswith(".json") and relativo.startswith(DADOS_LIDOS_NA_SUBIDA)


def _resumo(caminho: str) -> str | None:
    try:
        with open(caminho, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _arquivos(raiz: str):
    for pasta, subpastas, nomes in os.walk(raiz):
        subpastas[:] = [s for s in subpastas if s != "__pycache__"]
        for nome in nomes:
            caminho = os.path.join(pasta, nome)
            relativo = os.path.relpath(caminho, raiz).replace(os.sep, "/")
            if pede_reinicio(relativo):
                yield relativo, caminho


def fotografar(raiz: str = RAIZ) -> dict[str, str]:
    """{arquivo: hash do conteúdo} do que o processo carregou."""
    return {rel: h for rel, caminho in _arquivos(raiz)
            if (h := _resumo(caminho)) is not None}


def alterados(raiz: str = RAIZ, foto: dict | None = None,
              inicio: float | None = None) -> list[Alterado]:
    """O que mudou desde o retrato, do mais recente para o mais antigo.

    Só abre o arquivo cuja data de gravação passou da subida: a varredura de
    todo minuto lê a data de ~300 arquivos e o conteúdo de quase nenhum.
    """
    foto = FOTO if foto is None else foto
    inicio = INICIO if inicio is None else inicio
    saida, vistos = [], set()
    for rel, caminho in _arquivos(raiz):
        vistos.add(rel)
        try:
            gravado = os.path.getmtime(caminho)
        except OSError:
            continue
        if gravado <= inicio:
            continue
        if rel not in foto:
            saida.append(Alterado(rel, "novo", datetime.fromtimestamp(gravado)))
        elif _resumo(caminho) != foto[rel]:
            saida.append(Alterado(rel, "alterado", datetime.fromtimestamp(gravado)))
    saida += [Alterado(rel, "removido", None) for rel in foto if rel not in vistos]
    return sorted(saida, key=lambda a: a.quando or datetime.max, reverse=True)


def subida() -> datetime:
    return datetime.fromtimestamp(INICIO)


INICIO = time.time()
FOTO = fotografar()
