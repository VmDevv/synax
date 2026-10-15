"""Novidades do sistema, mostradas na tela principal depois do login.

A lista mora em `novidades.json`, ao lado deste arquivo, e entra no mesmo
commit da funcionalidade que ela anuncia: não há cadastro à mão, e o que está
no ar é o que está descrito. O "já vi" é do USUÁRIO, no banco
(`user_profiles.novidades_vistas_ate`), e não do navegador — o escritório usa
mais de um computador, e o aviso repetido em cada um é o que ensina a fechar
sem ler.

Três travas que existem por esse mesmo motivo: só aparece o que ainda não foi
visto; no máximo três por vez; e cada papel vê só o que é dele (cliente não vê
ferramenta interna).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, timedelta

logger = logging.getLogger(__name__)

ARQUIVO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "novidades.json")
MAXIMO = 3
# Quem nunca viu nada (usuário novo, ou antes desta tela existir) recebe só o
# que é recente: a lista inteira de um ano seria a janela que ninguém lê.
JANELA_SEM_HISTORICO = timedelta(days=30)
_DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CAMPOS = ("data", "titulo", "texto", "papeis")


def carregar(caminho: str = ARQUIVO) -> list[dict]:
    """As novidades válidas, mais recente primeiro. Arquivo ruim vira lista
    vazia com aviso no log — a tela principal não pode cair por causa dele."""
    try:
        with open(caminho, encoding="utf-8") as f:
            bruto = json.load(f)
    except (OSError, ValueError):
        logger.warning("Não foi possível ler %s", caminho, exc_info=True)
        return []
    itens = []
    for item in bruto.get("novidades", []):
        if not all(item.get(c) for c in _CAMPOS) or not _DATA.match(item["data"]):
            logger.warning("Novidade ignorada (faltam campos ou data): %r", item)
            continue
        if item.get("link") and not str(item["link"]).startswith("/"):
            # Só link interno: o arquivo vai para a tela de todo mundo.
            logger.warning("Novidade ignorada (link externo): %r", item)
            continue
        itens.append(item)
    return sorted(itens, key=lambda i: i["data"], reverse=True)


def do_papel(papel: str, itens: list[dict] | None = None) -> list[dict]:
    itens = carregar() if itens is None else itens
    return [i for i in itens if papel in i["papeis"]]


def pendentes(papel: str, vistas_ate: str | None, hoje: date | None = None,
              itens: list[dict] | None = None) -> list[dict]:
    """As novidades que este papel ainda não viu, no máximo `MAXIMO`."""
    meus = do_papel(papel, itens)
    if vistas_ate:
        meus = [i for i in meus if i["data"] > vistas_ate]
    else:
        corte = ((hoje or date.today()) - JANELA_SEM_HISTORICO).isoformat()
        meus = [i for i in meus if i["data"] >= corte]
    return meus[:MAXIMO]


def mais_recente(papel: str, itens: list[dict] | None = None) -> str | None:
    meus = do_papel(papel, itens)
    return meus[0]["data"] if meus else None
