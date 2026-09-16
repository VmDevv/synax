"""Descarte dos SPEDs e XMLs que ficaram para trás em temp_uploads/.

Por que existe: `execute_audit` apaga o SPED e a pasta de XMLs no `finally`,
então o caminho feliz se limpa sozinho. Mas há três caminhos que NÃO passam por
lá e deixam tudo em disco:

  - a auditoria falhou antes de chegar ao motor (SPED ilegível, XML corrompido);
  - a tela de confirmação da IA nunca foi respondida e o servidor foi reiniciado;
  - o upload terminou e o processamento nunca começou.

O resultado, medido em 01/09/2026 na máquina do escritório: **233 pastas,
66.368 arquivos, 1,04 GB**, com 85 pastas paradas havia mais de uma semana. São
SPEDs e XMLs de clientes — documento fiscal de terceiro parado em disco sem
ninguém saber que está lá.

O CRITÉRIO É IDADE, e o prazo é folgado de propósito. Uma auditoria em
andamento teve arquivos tocados há minutos; uma esperando confirmação expira em
1 hora (`LIMITE_ESPERA_IA_S`, em routes/uploads.py). Sete dias está muito acima
dos dois, então não há como esta rotina apagar trabalho vivo.

Roda no startup do servidor e também sob demanda por scripts/limpar_uploads.py.
"""
import logging
import os
import re
import shutil
import time

logger = logging.getLogger(__name__)

DIAS_PADRAO = int(os.environ.get("TEMP_UPLOADS_DIAS", "7") or 7)

# Só pastas com cara de task_id (UUID) são candidatas. É a trava que impede um
# erro de configuração em TEMP_DIR virar `rmtree` em diretório do sistema:
# mesmo apontado para o lugar errado, não há o que casar com este padrão.
_RE_TASK_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _mais_recente(caminho: str) -> float:
    """Modificação mais recente da pasta ou do que está logo dentro dela.

    Olhar só a pasta engana: em Windows o mtime dela não muda quando um arquivo
    lá dentro é reescrito, e uma auditoria em andamento pareceria antiga.
    """
    try:
        recente = os.path.getmtime(caminho)
    except OSError:
        return time.time()  # não deu para ler: trata como recente e não apaga

    try:
        for nome in os.listdir(caminho):
            try:
                recente = max(recente, os.path.getmtime(os.path.join(caminho, nome)))
            except OSError:
                continue
    except OSError:
        pass
    return recente


def limpar_uploads_antigos(temp_dir: str, dias: int = DIAS_PADRAO,
                           simular: bool = False) -> dict:
    """Apaga as pastas de upload paradas há mais de `dias`.

    `simular=True` só lista o que seria apagado — é o padrão do script de linha
    de comando, porque apagar documento fiscal de cliente não pode ser o
    comportamento de quem rodou o comando só para olhar.

    Devolve {"apagadas", "mantidas", "bytes", "erros", "detalhe"}.
    """
    resultado = {"apagadas": 0, "mantidas": 0, "bytes": 0, "erros": 0, "detalhe": []}
    if not os.path.isdir(temp_dir):
        return resultado

    limite = time.time() - (max(int(dias), 1) * 86400)

    for nome in os.listdir(temp_dir):
        caminho = os.path.join(temp_dir, nome)
        if not os.path.isdir(caminho) or not _RE_TASK_ID.match(nome):
            continue

        if _mais_recente(caminho) > limite:
            resultado["mantidas"] += 1
            continue

        tamanho = 0
        arquivos = 0
        for raiz, _, nomes in os.walk(caminho):
            for arq in nomes:
                try:
                    tamanho += os.path.getsize(os.path.join(raiz, arq))
                    arquivos += 1
                except OSError:
                    continue

        resultado["detalhe"].append({"pasta": nome, "arquivos": arquivos,
                                     "bytes": tamanho})
        if simular:
            resultado["apagadas"] += 1
            resultado["bytes"] += tamanho
            continue

        try:
            shutil.rmtree(caminho)
            resultado["apagadas"] += 1
            resultado["bytes"] += tamanho
        except OSError:
            # Arquivo aberto por outro processo, permissão — a próxima execução
            # tenta de novo. Falhar aqui não pode derrubar o startup.
            logger.warning("Não foi possível apagar %s", caminho, exc_info=True)
            resultado["erros"] += 1

    return resultado


def limpar_no_startup(temp_dir: str, dias: int = DIAS_PADRAO) -> None:
    """Chamada do main.py. Nunca levanta: limpeza não impede o servidor de subir."""
    try:
        r = limpar_uploads_antigos(temp_dir, dias)
        if r["apagadas"] or r["erros"]:
            logger.info(
                "Limpeza de temp_uploads: %d pasta(s) apagada(s) (%.1f MB), "
                "%d mantida(s), %d erro(s). Critério: parado há mais de %d dias.",
                r["apagadas"], r["bytes"] / (1024 * 1024), r["mantidas"],
                r["erros"], dias,
            )
    except Exception:  # noqa: BLE001 — startup acima de tudo
        logger.exception("Falha na limpeza de temp_uploads (o servidor sobe assim mesmo)")
