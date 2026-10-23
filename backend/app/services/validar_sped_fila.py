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

Assinar (23/10/2026, à tarde): o pedido pode vir com `assinar`. Aí o motor valida e, sem erro, assina com o e-CNPJ das
Configurações numa chamada só (`pva_motor.assinar_arquivo`). Sem certificado, ou com o certificado recusado, o arquivo
é só validado e a linha diz por que não foi assinado — nunca some. O SPED assinado fica em `ARQUIVO_ASSINADO`, na
pasta do processo, para baixar (a limpeza de `temp_uploads` o leva em 7 dias). A rotina automática passa pelo MESMO
caminho (`processar_agora`), sem entrar na fila da tela, mas com a mesma trava do motor.
"""
import csv
import json
import logging
import os
import re
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
ARQUIVO_ASSINADO = "sped_assinado.txt"
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
    assinar: bool = False
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
# Os arquivos que a rotina automática está passando agora (fora da fila da tela): processo -> início.
_por_fora: dict[str, datetime] = {}


def pasta(processo: str) -> str:
    return os.path.join(TEMP_DIR, processo)


def ler_0000(caminho: str) -> dict | None:
    """Os campos do 0000 que a aba e a rotina usam, na posição do EFD ICMS/IPI. None se a primeira linha não é
    um 0000."""
    with open(caminho, "rb") as f:
        bruto = f.read(4096)
    if bruto.startswith(b"\xef\xbb\xbf"):
        bruto = bruto[3:]
    primeira = re.split(rb"\r\n|\n|\r", bruto, maxsplit=1)[0]
    try:
        linha = primeira.decode("utf-8")
    except UnicodeDecodeError:
        linha = primeira.decode("latin-1")
    campos = linha.split("|")
    if len(campos) < 3 or campos[1] != "0000":
        return None

    def c(n):
        return campos[n].strip() if len(campos) > n else ""

    return {"cod_ver": c(2), "cod_fin": c(3), "dt_ini": c(4), "dt_fin": c(5), "nome": c(6),
            "cnpj": c(7), "cpf": c(8), "uf": c(9)}


def competencia_do_0000(dt_ini: str) -> str | None:
    """AAAA-MM do DT_INI (DDMMAAAA). Sem data legível é None, nunca um mês chutado."""
    from ..core import competencia as comp

    return comp.valida(f"{dt_ini[4:8]}-{dt_ini[2:4]}") if len(dt_ini) == 8 and dt_ini.isdigit() else None


def contar_linhas(caminho: str) -> int:
    n = 0
    ultimo = b""
    with open(caminho, "rb") as f:
        while bloco := f.read(1 << 20):
            n += bloco.count(b"\n")
            ultimo = bloco[-1:]
    return n + (1 if ultimo and ultimo != b"\n" else 0)


def registrar_destino(processo: str, caminho: str) -> bool:
    """Onde a rotina gravou a cópia assinada (a da pasta do processo continua lá para baixar pela tela)."""
    return _atualizar(processo, arquivo_assinado=caminho[:500])


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
        if processo in _por_fora:
            return {"situacao": VALIDANDO, "desde": _por_fora[processo]}
        for i, p in enumerate(_fila):
            if p.processo == processo:
                return {"situacao": NA_FILA, "na_frente": i + (1 if _atual is not None else 0)}
        return dict(_finais[processo]) if processo in _finais else None


def em_andamento() -> set[str]:
    with _guarda:
        return ({p.processo for p in _fila} | ({_atual.processo} if _atual is not None else set())
                | set(_por_fora))


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
        final = _protegido(pedido)
        with _guarda:
            _guardar_final(pedido.processo, final)


def _guardar_final(processo: str, final: dict) -> None:
    """Chame com `_guarda` na mão."""
    _finais[processo] = final
    while len(_finais) > MAX_FINAIS:
        _finais.popitem(last=False)


def _protegido(pedido: Pedido) -> dict:
    try:
        return _um_arquivo(pedido)
    except Exception:                                       # noqa: BLE001 — um arquivo não derruba a fila
        logger.exception("Validar SPED: o pedido %s parou por erro inesperado", pedido.processo)
        falha = "O Synax parou com um erro inesperado ao validar este arquivo; o motivo está no log do servidor."
        return {"situacao": FALHOU, "falha": falha,
                "registrada": _atualizar(pedido.processo, situacao=FALHOU, falha=falha,
                                         concluida_em=datetime.utcnow())}


def processar_agora(pedido: Pedido) -> dict:
    """Valida (e assina) já, nesta thread — a rotina automática. Espera a trava do motor como qualquer arquivo da
    fila, e aparece como "validando" para quem abrir a validação enquanto isso."""
    with _guarda:
        _por_fora[pedido.processo] = datetime.utcnow()
    try:
        final = _protegido(pedido)
    finally:
        with _guarda:
            _por_fora.pop(pedido.processo, None)
    with _guarda:
        _guardar_final(pedido.processo, final)
    return final


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


def _certificado() -> tuple[bytes, str] | None:
    from . import configuracoes

    db = _sessao()
    try:
        return configuracoes.certificado_assinatura(db)
    finally:
        db.close()


def _validar_ou_assinar(caminho: str, pedido: Pedido):
    """(resultado, assinatura ou None). Sem certificado ou com ele recusado, valida só e diz por que não assinou."""
    if not pedido.assinar:
        return pva_motor.validar_arquivo(caminho), None
    cert = _certificado()
    if cert is None:
        return pva_motor.validar_arquivo(caminho), pva_motor.Assinatura(
            assinado=False, motivo="nenhum certificado de assinatura cadastrado em Configurações")
    try:
        return pva_motor.assinar_arquivo(caminho, *cert)
    except pva_motor.AssinaturaRecusada as exc:
        logger.warning("Validar SPED: certificado recusado no pedido %s: %s", pedido.processo, exc.mensagem)
        return pva_motor.validar_arquivo(caminho), pva_motor.Assinatura(assinado=False, motivo=exc.mensagem)


def _um_arquivo(pedido: Pedido) -> dict:
    caminho = os.path.join(pasta(pedido.processo), ARQUIVO_SPED)
    _atualizar(pedido.processo, situacao=VALIDANDO)
    t0 = time.monotonic()
    try:
        try:
            with pva_motor.uso_exclusivo():
                resultado, assinatura = _validar_ou_assinar(caminho, pedido)
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
        _gravar_relatorio(pedido.processo, resultado, assinatura)
    except OSError:
        # As colunas ainda saem; a tela diz que a lista linha a linha não ficou no servidor.
        logger.exception("Validar SPED: não gravou o relatório do PVA de %s", pedido.processo)
    assinado = False
    if assinatura is not None and assinatura.assinado:
        try:
            with open(os.path.join(pasta(pedido.processo), ARQUIVO_ASSINADO), "wb") as f:
                f.write(assinatura.arquivo)
            assinado = True
        except OSError:
            logger.exception("Validar SPED: não gravou o SPED assinado de %s", pedido.processo)
            assinatura.motivo = "o PVA assinou, mas o Synax não conseguiu gravar o arquivo assinado"
    campos_assinatura = {}
    if assinatura is not None:
        campos_assinatura = {
            "assinar": True, "assinado": assinado,
            "assinatura_motivo": None if assinado else (assinatura.motivo or None) and assinatura.motivo[:500],
            "assinatura_titular": (assinatura.titular or None) and assinatura.titular[:255],
            "assinado_em": datetime.utcnow() if assinado else None,
            "arquivo_assinado": ARQUIVO_ASSINADO if assinado else None}
    registrada = _atualizar(
        pedido.processo, situacao=CONCLUIDA, estado_pva=resultado.estado, aprovado=resultado.aprovado,
        integrado=resultado.integrado, erros=resultado.erros, advertencias=resultado.advertencias,
        falha=(resultado.falha or None) and resultado.falha[:500], nao_conferidas=0,
        duracao_ms=resultado.ms if resultado.ms is not None else int((time.monotonic() - t0) * 1000),
        versao_pva=(resultado.versao_pva or None) and resultado.versao_pva[:20], concluida_em=datetime.utcnow(),
        py_erros=py_erros, py_advertencias=py_adv, **campos_assinatura)
    logger.info("SPED validado no PVA processo=%s tenant=%s veredito=%s erros=%d adv=%d assinado=%s antigo=%s/%s "
                "ms=%s", pedido.processo, pedido.tenant_id, resultado.veredito, resultado.erros,
                resultado.advertencias, assinado if assinatura is not None else "-", py_erros, py_adv, resultado.ms)
    return {"situacao": CONCLUIDA, "registrada": registrada, "aprovado": resultado.aprovado,
            "erros": resultado.erros, "advertencias": resultado.advertencias, "veredito": resultado.veredito,
            "assinado": assinado,
            "assinatura_motivo": (assinatura.motivo if assinatura is not None and not assinado else "")}


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


def _gravar_relatorio(processo: str, resultado: pva_motor.Resultado,
                      assinatura: "pva_motor.Assinatura | None" = None) -> None:
    destino = pasta(processo)
    os.makedirs(destino, exist_ok=True)
    with open(os.path.join(destino, "resultado.json"), "w", encoding="utf-8") as f:
        json.dump({**resultado.como_dict(), "assinatura": assinatura.como_dict() if assinatura else None}, f,
                  ensure_ascii=False)
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
