"""A fila da aba Validar SPED: um arquivo por vez no PVA, em segundo plano.

O PVA leva de 7 a 33 s por arquivo (e há um piso de ~7 s mesmo com 4 KB: ele cria um banco por escrituração), e é um
só. Validar dentro do clique deixaria a segunda pessoa esperando a primeira sem saber por quê. Então o clique só
ENFILEIRA e volta na hora; uma thread manda arquivo por arquivo ao motor (`pva_motor.uso_exclusivo`) e a tela
acompanha pela situação gravada na linha de `sped_validacoes`.

Quatro regras:

- **O resultado vai em colunas** (veredito, estado, erros, advertências, falha, tempo) — o painel do mês é uma
  consulta só. A lista linha a linha fica em `temp_uploads/<processo>/` (JSON para a tela, CSV para baixar), como já
  ficava o relatório, e sai pela limpeza de sempre (`core/limpeza_uploads.py`).
- **O SPED do cliente fica no servidor só até o PVA responder.** Apagado no `finally`, com ou sem resposta.
- **A fila vive na memória do processo.** Reiniciar o servidor perde o que estava na fila — a linha fica em
  `na_fila`/`validando`, e a tela, não achando o pedido aqui, diz "interrompida", nunca "validando" para sempre.
- **O validador antigo roda junto, só para comparar** (`py_erros`/`py_advertencias` e o CSV dele), até o Sandro
  decidir apagá-lo. Ele nunca decide o veredito: se quebrar, a coluna fica NULA e o PVA segue valendo.
"""
import csv
import json
import logging
import os
import shutil
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime

from . import pva_motor

logger = logging.getLogger(__name__)

TEMP_DIR = os.path.join(os.getcwd(), "temp_uploads")
ARQUIVO_SPED = "sped_validar.txt"
# Mais que isso na fila é engano (ou uma pasta inteira subindo pela API): a fila passaria de meia hora.
MAX_FILA = 30
# Teto da lista do validador antigo (a contagem segue inteira): hierarquia quebrada gera uma por linha.
LIMITE_LISTA_ANTIGO = 20_000
MOTOR = "pva"

NA_FILA = "na_fila"
VALIDANDO = "validando"
CONCLUIDA = "concluida"
FALHOU = "falhou"
INTERROMPIDA = "interrompida"     # só na tela: a linha ficou pendente e o pedido não está mais na memória
PENDENTES = frozenset({NA_FILA, VALIDANDO})

COLUNAS_CSV = ("tipo", "linha", "registro", "campo", "valor", "esperado", "mensagem", "codigo", "conteudo")
COLUNAS_CSV_ANTIGO = ("linha", "registro", "campo", "valor", "regra", "gravidade", "mensagem")


class FilaCheia(Exception):
    pass


@dataclass
class Pedido:
    processo: str
    tenant_id: str
    enfileirado_em: datetime = field(default_factory=datetime.utcnow)
    inicio: datetime | None = None


_guarda = threading.Lock()
_fila: deque[Pedido] = deque()
_atual: Pedido | None = None
_thread: threading.Thread | None = None
# O fim dos últimos pedidos, para a tela não depender só do UPDATE: se ele falhar, a linha fica "validando", e é
# daqui que sai "concluída, mas não registrada".
_finais: "OrderedDict[str, dict]" = OrderedDict()
MAX_FINAIS = 500


def pasta(processo: str) -> str:
    return os.path.join(TEMP_DIR, processo)


def cabe() -> bool:
    with _guarda:
        return len(_fila) < MAX_FILA


def enfileirar(pedido: Pedido) -> int:
    """Põe na fila e devolve quantos arquivos estão na frente (contando o que está no PVA agora)."""
    global _thread
    with _guarda:
        if len(_fila) >= MAX_FILA:
            raise FilaCheia(f"A fila do PVA está cheia ({MAX_FILA} arquivos esperando). Espere alguns terminarem e "
                            "suba de novo.")
        na_frente = len(_fila) + (1 if _atual is not None else 0)
        _fila.append(pedido)
        if _thread is None:
            _thread = threading.Thread(target=_rodar, name="validar-sped-pva", daemon=True)
            _thread.start()
        return na_frente


def estado(processo: str) -> dict | None:
    """O que a memória sabe do pedido: na fila (com a posição), validando (desde quando), ou como terminou."""
    with _guarda:
        if _atual is not None and _atual.processo == processo:
            return {"situacao": VALIDANDO, "desde": _atual.inicio}
        for i, p in enumerate(_fila):
            if p.processo == processo:
                return {"situacao": NA_FILA, "na_frente": i + (1 if _atual is not None else 0)}
        return dict(_finais[processo]) if processo in _finais else None


def em_andamento() -> set[str]:
    with _guarda:
        return {p.processo for p in _fila} | ({_atual.processo} if _atual is not None else set())


def esperar(segundos: float = 30.0) -> bool:
    """Para os testes: espera a fila esvaziar. True se esvaziou."""
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        with _guarda:
            if _thread is None:
                return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------ execução

def _rodar() -> None:
    global _atual, _thread
    while True:
        with _guarda:
            if not _fila:
                _atual = None
                _thread = None
                return
            _atual = _fila.popleft()
            _atual.inicio = datetime.utcnow()
            pedido = _atual
        try:
            final = _um_arquivo(pedido)
        except Exception:                                   # noqa: BLE001 — um arquivo não derruba a fila
            logger.exception("Validar SPED: o pedido %s parou por erro inesperado", pedido.processo)
            falha = "O Synax parou com um erro inesperado ao validar este arquivo; o motivo está no log do servidor."
            final = {"situacao": FALHOU, "falha": falha,
                     "registrada": _atualizar(pedido.processo, situacao=FALHOU, falha=falha,
                                              concluida_em=datetime.utcnow())}
        with _guarda:
            _finais[pedido.processo] = final
            while len(_finais) > MAX_FINAIS:
                _finais.popitem(last=False)


def _sessao():
    from ..infrastructure.database.session import SessionLocal
    return SessionLocal()


def _atualizar(processo: str, **campos) -> bool:
    """Grava na linha do pedido. False se não deu — a tela avisa que não ficou registrado."""
    from ..domain.models.audit import SpedValidacao

    db = _sessao()
    try:
        linha = db.get(SpedValidacao, processo)
        if linha is None:
            logger.error("Validar SPED: a linha %s sumiu antes do fim da validação", processo)
            return False
        for nome, valor in campos.items():
            setattr(linha, nome, valor)
        db.commit()
        return True
    except Exception:                                       # noqa: BLE001
        logger.exception("Validar SPED: não foi possível gravar o andamento de %s", processo)
        db.rollback()
        return False
    finally:
        db.close()


def _um_arquivo(pedido: Pedido) -> dict:
    caminho = os.path.join(pasta(pedido.processo), ARQUIVO_SPED)
    _atualizar(pedido.processo, situacao=VALIDANDO)
    t0 = time.monotonic()
    try:
        try:
            with pva_motor.uso_exclusivo():
                resultado = pva_motor.validar_arquivo(caminho)
        except pva_motor.MotorIndisponivel as exc:
            logger.warning("Validar SPED: %s sem resposta do PVA: %s", pedido.processo, exc.mensagem)
            registrada = _atualizar(pedido.processo, situacao=FALHOU, falha=exc.mensagem[:500],
                                    duracao_ms=int((time.monotonic() - t0) * 1000),
                                    concluida_em=datetime.utcnow())
            return {"situacao": FALHOU, "registrada": registrada, "falha": exc.mensagem}
        py_erros, py_adv = _validador_antigo(caminho, pedido.processo)
    finally:
        try:
            os.remove(caminho)
        except OSError:
            pass

    try:
        _gravar_relatorio(pedido.processo, resultado)
    except OSError:
        # As colunas ainda saem; a tela diz que a lista linha a linha não ficou no servidor.
        logger.exception("Validar SPED: não gravou o relatório do PVA de %s", pedido.processo)
    registrada = _atualizar(
        pedido.processo, situacao=CONCLUIDA, estado_pva=resultado.estado, aprovado=resultado.aprovado,
        integrado=resultado.integrado, erros=resultado.erros, advertencias=resultado.advertencias,
        falha=(resultado.falha or None) and resultado.falha[:500], nao_conferidas=0,
        duracao_ms=resultado.ms if resultado.ms is not None else int((time.monotonic() - t0) * 1000),
        versao_pva=(resultado.versao_pva or None) and resultado.versao_pva[:20], concluida_em=datetime.utcnow(),
        py_erros=py_erros, py_advertencias=py_adv)
    logger.info("SPED validado no PVA processo=%s tenant=%s veredito=%s erros=%d adv=%d antigo=%s/%s ms=%s",
                pedido.processo, pedido.tenant_id, resultado.veredito, resultado.erros, resultado.advertencias,
                py_erros, py_adv, resultado.ms)
    return {"situacao": CONCLUIDA, "registrada": registrada}


def _validador_antigo(caminho: str, processo: str) -> tuple[int | None, int | None]:
    from .sped_validador import validar_arquivo

    try:
        antigo = validar_arquivo(caminho, LIMITE_LISTA_ANTIGO)
    except Exception:                                       # noqa: BLE001 — comparação, nunca o veredito
        logger.warning("Validar SPED: o validador antigo não leu %s (comparação fica sem ele)", processo,
                       exc_info=True)
        return None, None
    try:
        with open(os.path.join(pasta(processo), "relatorio_antigo.csv"), "w", encoding="utf-8-sig",
                  newline="") as f:
            escritor = csv.writer(f, delimiter=";")
            escritor.writerow(COLUNAS_CSV_ANTIGO)
            for i in antigo.inconsistencias:
                escritor.writerow([i.linha or "", i.registro, i.campo, i.valor, i.regra, i.gravidade, i.mensagem])
    except OSError:
        logger.warning("Validar SPED: não gravou o relatório do validador antigo de %s", processo, exc_info=True)
    return antigo.erros, antigo.advertencias


def _gravar_relatorio(processo: str, resultado: pva_motor.Resultado) -> None:
    destino = pasta(processo)
    os.makedirs(destino, exist_ok=True)
    with open(os.path.join(destino, "resultado.json"), "w", encoding="utf-8") as f:
        json.dump(resultado.como_dict(), f, ensure_ascii=False)
    with open(os.path.join(destino, "relatorio.csv"), "w", encoding="utf-8-sig", newline="") as f:
        escritor = csv.writer(f, delimiter=";")
        escritor.writerow(COLUNAS_CSV)
        for i in resultado.itens:
            escritor.writerow(["advertência" if i.advertencia else "erro", i.linha or "", i.registro, i.campo,
                               i.valor, i.esperado, i.descricao, i.codigo, i.conteudo])


def ler_resultado(processo: str) -> pva_motor.Resultado | None:
    try:
        with open(os.path.join(pasta(processo), "resultado.json"), encoding="utf-8") as f:
            return pva_motor.Resultado.de_dict(json.load(f))
    except (OSError, ValueError, TypeError):
        return None


def descartar(processo: str) -> None:
    shutil.rmtree(pasta(processo), ignore_errors=True)
