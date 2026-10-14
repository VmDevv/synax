"""Chegou SPED por e-mail: baixa do SIEG os XMLs das notas de entrada dele.

Pedido do Sandro em 29/09/2026. O desenho saiu de MEDIÇÃO na conta do
escritório, no mesmo dia, depois que o primeiro SPED real (BIANCARDI, 444
notas) levou meia hora:

- **O lote (`BaixarXmls`) é lento E incompleto.** Aceita no máximo 50 por
  chamada ("take inválido" acima disso) e uma chamada a cada ~40 s (429 com
  `Retry-After`) — e, pelo destinatário, devolveu "Nenhum arquivo XML
  localizado" a partir da nota 200 de um mês que tinha 411 no SPED, todas
  achadas pela chave. A primeira versão ia por lote e, quando a página voltava
  curta, dava o mês por encerrado e seguia nota a nota, devagar.
- **A busca por chave (`BaixarXml`) não mostrou limite**: 43 por minuto uma
  por vez (1,4 s cada), 196 com 5 em paralelo, 348 com 10 — nenhum 429 em 110
  chamadas. Por isso agora é SÓ por chave, `PARALELO` de cada vez: um SPED de
  444 notas leva uns 2 minutos. Se o SIEG passar a pedir espera, ela é
  respeitada (`Retry-After`), e espera longa pausa a fila inteira.

Regras que continuam:

1. **Só as notas que a auditoria usa**: C100/D100 de ENTRADA (IND_OPER 0), sem
   os cancelados/denegados/inutilizados (a mesma lista do `sped_parser`). O que
   já está guardado não é baixado de novo.
2. **Uma fila, um SPED de cada vez.** As chamadas vão em paralelo, mas o banco
   só é tocado pela thread da fila — a sessão do SQLAlchemy não é de threads.
3. **Cancelar** (tela) grava `cancelado`; a fila confere o BANCO antes de cada
   rodada e nunca grava desfecho por cima. O que veio fica; "Retomar" continua.
4. **Nota que o cofre não tem vira "não encontrada"** — nunca some.

A empresa do XML baixado é a do SPED, e não a do CNPJ de dentro do XML: nota
de entrada de emissão própria (devolução, importação) tem outro destinatário,
e mesmo assim é desta empresa — a chave está no SPED dela.
"""
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from ...domain.models.email_recebido import EmailSped, EmailXml
from .. import configuracoes as cfg
from . import anexos, armazenamento

logger = logging.getLogger(__name__)

PARALELO = 6                 # medido sem 429 até 10; 6 deixa folga para o botão de DANFE
RODADA = 30                  # chaves por rodada: o progresso e o "cancelar" andam por rodada
MAX_ESPERA_S = 300           # Retry-After acima disto: pausa a fila e o agendador retoma
RODADAS_COM_FALHA = 3        # rodadas SEGUIDAS só com erro: o SIEG está fora
ERROS_POR_CHAVE = 3          # a mesma chave falhando: conta como não encontrada
SEM_EFEITO = {"02", "03", "04", "05"}

_trava = threading.Lock()
_pausa = {"ate": None}


class _Cancelado(Exception):
    """Alguém clicou em "Cancelar download" na tela."""


def em_andamento() -> bool:
    return _trava.locked()


# ------------------------------------------------------------ o que baixar

def chaves_de_entrada(caminho: str) -> list[str]:
    """Chaves dos C100 (campo 09) e D100 (campo 10) de ENTRADA e com efeito,
    na ordem do arquivo, sem repetir. Linha a linha: o SPED pode ter 300 MB."""
    vistas, chaves = set(), []
    with open(caminho, "rb") as f:
        for bruta in f:
            if not (bruta.startswith(b"|C100|") or bruta.startswith(b"|D100|")):
                continue
            partes = bruta.decode("latin-1").split("|")
            indice = 9 if partes[1] == "C100" else 10
            if len(partes) <= indice or partes[2].strip() != "0":
                continue
            if partes[6].strip() in SEM_EFEITO:
                continue
            chave = partes[indice].strip()
            if len(chave) == 44 and chave.isdigit() and chave not in vistas:
                vistas.add(chave)
                chaves.append(chave)
    return chaves


# ------------------------------------------------------------------ a fila

def pausado(agora: datetime | None = None) -> bool:
    """O SIEG pediu uma espera longa: a fila INTEIRA para até lá — o limite
    é da conta, e o próximo SPED levaria o mesmo 429."""
    ate = _pausa["ate"]
    return bool(ate and (agora or datetime.utcnow()) < ate)


def pendentes(db) -> list:
    return (db.query(EmailSped)
            .filter(EmailSped.sieg_status.in_(("pendente", "baixando")))
            .order_by(EmailSped.created_at).all())


def foi_cancelado(db, sped_id: str) -> bool:
    """Lido do BANCO, e não do objeto da sessão: o clique vem de outra
    requisição, e o objeto desta thread ainda acha que está "baixando"."""
    # Linha que sumiu (o SPED foi EXCLUÍDO pela tela) vale como cancelamento:
    # seguir baixando gravaria XML de um SPED que ninguém mais quer.
    status = db.query(EmailSped.sieg_status).filter(EmailSped.id == sped_id).scalar()
    return status in (None, "cancelado")


def iniciar_se_preciso(sessao_factory) -> bool:
    """Chamado depois de cada leitura da caixa e a cada minuto pelo
    agendador. Barato quando não há nada: uma consulta."""
    if _trava.locked() or pausado():
        return False
    db = sessao_factory()
    try:
        if not pendentes(db):
            return False
    finally:
        db.close()
    threading.Thread(target=processar_fila, args=(sessao_factory,),
                     name="sieg-download", daemon=True).start()
    return True


def processar_fila(sessao_factory, dormir=time.sleep, cliente=None) -> int:
    """Baixa os SPEDs da fila, um de cada vez. Devolve quantos terminaram."""
    if not _trava.acquire(blocking=False):
        return 0
    feitos = 0
    try:
        while True:
            db = sessao_factory()
            try:
                fila = pendentes(db)
                if not fila:
                    return feitos
                sped = fila[0]
                try:
                    sieg = cliente or cfg.cliente_sieg(db)
                except RuntimeError:
                    _terminar(db, sped, "falhou", "O SIEG não está configurado "
                              "(Administração → Configurações). Os XMLs não foram baixados.")
                    feitos += 1
                    continue
                try:
                    if baixar_um(db, sped, sieg, dormir) == "pausa":
                        return feitos     # o agendador retoma depois da espera
                except Exception:
                    logger.exception("Download do SIEG falhou no SPED %s", sped.id)
                    db.rollback()
                    viva = db.get(EmailSped, sped.id)
                    if viva is not None and viva.sieg_status != "cancelado":
                        _terminar(db, viva, "falhou", "Erro inesperado no download; veja o "
                                  "log do servidor. O que foi baixado até ali está guardado.")
                feitos += 1
            finally:
                db.close()
    finally:
        _trava.release()


def _terminar(db, sped: EmailSped, status: str, mensagem: str) -> None:
    sped.sieg_status = status
    sped.sieg_mensagem = mensagem[:500]
    sped.sieg_fim = datetime.utcnow()
    db.commit()


def _guardar(db, sped: EmailSped, chave: str, xml: str) -> bool:
    """Grava o XML baixado como da empresa do SPED. Falso se não é o
    documento pedido (XML de outra chave, ilegível)."""
    dados = xml.encode("utf-8")
    lido = anexos.ler_documento(dados)
    if lido is None:
        return False
    campos = anexos.campos_do_documento(*lido[:2])
    if campos["chave"] != chave:
        return False
    if (db.query(EmailXml.id).filter(EmailXml.tenant_id == sped.tenant_id,
                                     EmailXml.chave == chave).first()):
        return True
    relativo = armazenamento.relativo(sped.tenant_id, campos["competencia"], chave)
    armazenamento.gravar(relativo, dados)
    db.add(EmailXml(id=str(uuid.uuid4()), tenant_id=sped.tenant_id, arquivo=relativo,
                    mensagem_id=sped.id, recebido_em=datetime.utcnow(), origem="sieg",
                    **{k: campos[k] for k in ("chave", "modelo", "competencia", "numero",
                                              "emitente_nome", "emitente_cnpj", "valor")}))
    return True


def _pausar(db, sped: EmailSped, segundos: int, salvar_progresso) -> str:
    salvar_progresso()
    if foi_cancelado(db, sped.id):
        raise _Cancelado()
    _pausa["ate"] = datetime.utcnow() + timedelta(seconds=segundos)
    sped.sieg_mensagem = (f"O SIEG pediu para esperar {segundos // 60 or 1} min; "
                          "o download continua sozinho depois disso.")
    db.commit()
    return "pausa"


def baixar_um(db, sped: EmailSped, sieg, dormir=time.sleep) -> str:
    """"feito", "pausa" (o SIEG pediu para esperar mais do que vale segurar
    a thread) ou "cancelado" (alguém cancelou na tela; o que foi baixado fica)."""
    sped_id = sped.id
    try:
        return _baixar(db, sped, sieg, dormir, lambda: foi_cancelado(db, sped_id))
    except _Cancelado:
        # Grava o que já veio nesta rodada (XMLs e contadores). O estado
        # "cancelado" não é tocado: esta sessão nunca o alterou.
        db.commit()
        return "cancelado"


def _baixar(db, sped: EmailSped, sieg, dormir, parar) -> str:
    caminho = armazenamento.absoluto(sped.arquivo)
    try:
        chaves = chaves_de_entrada(caminho) if caminho else None
    except OSError:
        chaves = None
    if chaves is None:
        _terminar(db, sped, "falhou", "O arquivo do SPED não está mais no servidor.")
        return "feito"

    guardadas = set()
    for i in range(0, len(chaves), 500):
        guardadas |= {c for (c,) in db.query(EmailXml.chave)
                      .filter(EmailXml.tenant_id == sped.tenant_id,
                              EmailXml.chave.in_(chaves[i:i + 500])).all()}
    try:
        progresso = json.loads(sped.progresso or "{}")
    except ValueError:
        progresso = {}
    nao_achadas = set(progresso.get("nao_achadas", []))
    faltando = [c for c in chaves if c not in guardadas]

    if parar():
        raise _Cancelado()
    if sped.sieg_status == "pendente":
        sped.sieg_inicio = datetime.utcnow()
        sped.ja_tinha = len(chaves) - len(faltando)
        sped.baixados = 0
    sped.chaves_total = len(chaves)
    sped.sieg_status = "baixando"
    sped.sieg_mensagem = None
    db.commit()

    def salvar_progresso():
        sped.progresso = json.dumps({"nao_achadas": sorted(nao_achadas)})
        sped.nao_encontrados = len(nao_achadas)
        db.commit()

    fila = [c for c in faltando if c not in nao_achadas]
    rodadas_ruins, ultimo_erro, erros_da_chave = 0, "", {}
    with ThreadPoolExecutor(max_workers=PARALELO, thread_name_prefix="sieg-chave") as pool:
        while fila:
            if parar():
                raise _Cancelado()
            rodada, fila = fila[:RODADA], fila[RODADA:]
            # Só HTTP nas threads; o banco fica aqui.
            resultados = list(pool.map(sieg.buscar_por_chave, rodada))
            de_novo, esperar, erros = [], 0, 0
            for chave, pagina in zip(rodada, resultados):
                if pagina.esperar:
                    de_novo.append(chave)
                    esperar = max(esperar, pagina.esperar)
                elif pagina.erro:
                    erros += 1
                    ultimo_erro = pagina.erro
                    erros_da_chave[chave] = erros_da_chave.get(chave, 0) + 1
                    # Uma chave que sempre falha não pode prender a fila: na
                    # terceira, vale como não encontrada ("Tentar de novo"
                    # pergunta outra vez).
                    if erros_da_chave[chave] >= ERROS_POR_CHAVE:
                        nao_achadas.add(chave)
                    else:
                        de_novo.append(chave)
                elif pagina.xmls and _guardar(db, sped, chave, pagina.xmls[0]):
                    sped.baixados += 1
                else:
                    nao_achadas.add(chave)
            salvar_progresso()
            if esperar:
                if esperar > MAX_ESPERA_S:
                    return _pausar(db, sped, esperar, salvar_progresso)
                dormir(esperar + 1)
            if erros == len(rodada):
                rodadas_ruins += 1
                if rodadas_ruins >= RODADAS_COM_FALHA:
                    if parar():
                        raise _Cancelado()
                    _terminar(db, sped, "falhou", f"O SIEG falhou {rodadas_ruins} rodadas "
                              f"seguidas ({ultimo_erro}). {sped.baixados} XML(s) baixado(s) "
                              "até ali — \"Tentar de novo\" continua deles.")
                    return "feito"
                dormir(5 * rodadas_ruins)
            elif not esperar:
                rodadas_ruins = 0
            # O que levou 429 ou erro volta para o FIM da fila: o erro isolado
            # tem outra chance, e a rodada seguinte não fica presa nele.
            fila.extend(de_novo)

    if parar():
        raise _Cancelado()
    if nao_achadas:
        _terminar(db, sped, "parcial", f"{len(nao_achadas)} nota(s) do SPED não estão no "
                  "cofre do SIEG — a auditoria vai acusá-las como XML faltante.")
    else:
        _terminar(db, sped, "concluido", "Todos os XMLs de entrada do SPED estão guardados.")
    return "feito"
