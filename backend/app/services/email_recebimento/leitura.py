"""Uma rodada de leitura da caixa: de hora em hora, pelo agendador.

Não é uma automação configurável na aba Automações porque não tem o que
configurar — só ligar, na aba Configurações. O resumo da manhã, esse sim, é
automação (tem horário, destinatários e período).

Quatro regras:

1. **Nada é marcado no servidor** (ver `imap.py`). O que já foi processado mora
   em `email_mensagens` e no estado gravado em `app_settings` (`imap_estado`):
   UIDVALIDITY e o último UID visto.

2. **Não roda em dobro.** A vez é tomada com UPDATE condicionado ao valor
   antigo da próxima leitura (`imap_proxima`), como `automacao.reivindicar`; e
   uma trava de processo impede a leitura agendada e o "Ler agora" juntos.

3. **Primeira leitura não varre a caixa inteira.** Sem estado, pega os últimos
   `PRIMEIRA_LEITURA_DIAS` dias — a caixa geral tem anos de e-mail, e baixar
   tudo seria horas de rede e milhares de notas antigas misturadas às do mês.
   Pasta recriada no servidor (UIDVALIDITY novo) volta a buscar pela data, e o
   Message-ID impede processar de novo.

4. **Cada e-mail é gravado quando termina**, com o último UID junto. Servidor
   caindo no meio da leitura continua de onde parou na próxima hora.
"""
import hashlib
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from ...domain.models.auth_models import Tenant
from ...domain.models.configuracao import AppSetting
from ...domain.models.email_recebido import EmailMensagem, EmailSped, EmailXml
from .. import configuracoes as cfg
from ..email_envio import CABECALHO_PROPRIO
from . import anexos, armazenamento, imap, remetentes

logger = logging.getLogger(__name__)

INTERVALO_MIN = 60
PRIMEIRA_LEITURA_DIAS = 7
# Teto por rodada: uma caixa com centenas de e-mails novos (a primeira
# leitura, ou o servidor fora por um dia) termina na hora seguinte, sem
# segurar a thread por uma tarde.
MAX_POR_RODADA = 300
# O corpo do e-mail só existe para o resumo da manhã. Passado o prazo, é
# apagado: e-mail de funcionário não fica parado no banco sem motivo.
DIAS_TRECHO = 10
DIAS_TRILHA = 180

CHAVE_ESTADO = "imap_estado"
CHAVE_PROXIMA = "imap_proxima"

_trava = threading.Lock()


# ------------------------------------------------------------------ estado

def ler_estado(db, chave: str = CHAVE_ESTADO) -> dict:
    """O "até onde li" de uma caixa. Sem `chave`, o resumo que a tela mostra
    (e o da caixa principal, que sempre morou aqui)."""
    try:
        estado = json.loads(cfg.ler(db, chave) or "{}")
    except ValueError:
        estado = {}
    return estado if isinstance(estado, dict) else {}


def _gravar_estado(db, estado: dict, chave: str = CHAVE_ESTADO) -> None:
    cfg.gravar(db, chave, json.dumps(estado, ensure_ascii=False, default=str),
               "leitura da caixa")


def _iso(quando: datetime | None) -> str | None:
    return quando.strftime("%Y-%m-%dT%H:%M:%S") if quando else None


def _data(texto) -> datetime | None:
    try:
        return datetime.strptime(str(texto)[:19], "%Y-%m-%dT%H:%M:%S")
    except (TypeError, ValueError):
        return None


def em_andamento() -> bool:
    return _trava.locked()


# ---------------------------------------------------------------- agenda

def reivindicar(db, agora: datetime) -> bool:
    """Toma a vez da leitura das próximas `INTERVALO_MIN` — só um consegue."""
    linha = db.get(AppSetting, CHAVE_PROXIMA)
    nova = _iso(agora + timedelta(minutes=INTERVALO_MIN))
    if linha is None:
        db.add(AppSetting(chave=CHAVE_PROXIMA, valor=nova, segredo=False,
                          atualizado_por="leitura da caixa"))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return False
        return True
    antiga = linha.valor
    prevista = _data(antiga)
    if prevista is not None and prevista > agora:
        return False
    condicao = (AppSetting.valor == antiga) if antiga is not None else AppSetting.valor.is_(None)
    resultado = db.execute(update(AppSetting)
                           .where(AppSetting.chave == CHAVE_PROXIMA, condicao)
                           .values(valor=nova))
    db.commit()
    return resultado.rowcount == 1


def proxima(db) -> datetime | None:
    return _data(cfg.ler(db, CHAVE_PROXIMA))


def rodar_se_devido(sessao_factory, agora: datetime, em_paralelo: bool = True) -> bool:
    """Chamado pelo agendador a cada minuto. Liga a leitura se for a hora."""
    db = sessao_factory()
    try:
        config = cfg.config_imap(db)
        if not config["ligado"] or not cfg.imap_configurado(db):
            return False
        if _trava.locked() or not reivindicar(db, agora):
            return False
    finally:
        db.close()
    if em_paralelo:
        threading.Thread(target=ler, args=(sessao_factory, "agenda"),
                         name="leitura-caixa", daemon=True).start()
    else:
        ler(sessao_factory, "agenda")
    return True


# ---------------------------------------------------------------- leitura

def ler(sessao_factory, gatilho: str = "agenda", esperar: float | None = None) -> dict:
    """Uma rodada. Nunca levanta: o resultado (e o estado gravado) diz o que houve.

    `esperar`: segundos para aguardar outra leitura em andamento terminar (o
    resumo da manhã espera, para não resumir a caixa pela metade). Sem ele,
    leitura ocupada devolve `{"status": "ocupada"}` na hora.
    """
    pegou = _trava.acquire(timeout=esperar) if esperar else _trava.acquire(blocking=False)
    if not pegou:
        return {"status": "ocupada", "mensagem": "Já há uma leitura da caixa em andamento."}
    db = sessao_factory()
    inicio = datetime.utcnow()
    estado = {}
    try:
        estado = ler_estado(db)
        resultado = _ler(db, estado, inicio)
    except imap.FalhaLeitura as err:
        resultado = {"status": "falhou", "mensagem": str(err)}
    except Exception:
        logger.exception("Leitura da caixa de e-mail falhou")
        try:
            db.rollback()
        except Exception:                                 # noqa: BLE001
            pass
        resultado = {"status": "falhou",
                     "mensagem": "Erro inesperado na leitura da caixa; veja o log do servidor."}
    try:
        from . import sieg_download
        sieg_download.iniciar_se_preciso(sessao_factory)
    except Exception:
        logger.exception("Não foi possível iniciar o download dos XMLs no SIEG")
    try:
        estado = {**ler_estado(db), "ultima_leitura": _iso(inicio), "gatilho": gatilho,
                  "fim": _iso(datetime.utcnow()), **{k: v for k, v in resultado.items()
                                                     if k in ("status", "mensagem", "emails",
                                                              "xmls", "restam")}}
        _gravar_estado(db, estado)
        _limpar(db, inicio)
    except Exception:
        logger.exception("Não foi possível gravar o estado da leitura da caixa")
    finally:
        db.close()
        _trava.release()
    return resultado


def _ler(db, estado: dict, agora: datetime) -> dict:
    """Todas as caixas (`cfg.caixas_imap`), uma depois da outra. Uma que falha
    não segura as outras: o resultado de cada uma entra na mensagem, e a
    rodada sai "falhou" se QUALQUER caixa falhou — senão a falha do fiscal@
    sumiria atrás do "ok" da caixa principal."""
    caixas = cfg.caixas_imap(db)
    if not caixas:
        raise imap.FalhaLeitura("Servidor IMAP não configurado (Administração → Configurações).")
    resultados = []
    for caixa in caixas:
        principal = caixa["chave_estado"] == CHAVE_ESTADO
        est = estado if principal else ler_estado(db, caixa["chave_estado"])
        try:
            r = _ler_caixa(db, caixa, est, agora)
        except imap.FalhaLeitura as err:
            r = {"status": "falhou", "mensagem": str(err), "emails": 0, "xmls": 0, "restam": 0}
        except Exception:
            logger.exception("Leitura da caixa %s falhou", caixa["rotulo"])
            try:
                db.rollback()
            except Exception:                             # noqa: BLE001
                pass
            r = {"status": "falhou", "emails": 0, "xmls": 0, "restam": 0,
                 "mensagem": "erro inesperado na leitura; veja o log do servidor"}
        if not principal:
            est = {**ler_estado(db, caixa["chave_estado"]), "ultima_leitura": _iso(agora),
                   "status": r["status"], "mensagem": r["mensagem"]}
            _gravar_estado(db, est, caixa["chave_estado"])
        resultados.append({**r, "caixa": caixa["rotulo"]})
    if len(resultados) == 1:
        return resultados[0]
    return {"status": "falhou" if any(r["status"] == "falhou" for r in resultados) else "ok",
            "mensagem": " | ".join(f"{r['caixa']}: {r['mensagem']}" for r in resultados),
            "emails": sum(r["emails"] for r in resultados),
            "xmls": sum(r["xmls"] for r in resultados),
            "restam": sum(r["restam"] for r in resultados)}


def _ler_caixa(db, caixa: dict, estado: dict, agora: datetime) -> dict:
    """Uma caixa. `caixa["chave"]` é como ela aparece em `email_mensagens.pasta`
    e no estado — a pasta pura na principal, "usuário/pasta" nas outras."""
    config = caixa
    pasta = caixa["chave"]
    conexao, uidvalidity = imap.conectar(config)
    try:
        mesma_pasta = (estado.get("pasta") == pasta and estado.get("uidvalidity") == uidvalidity)
        depois = int(estado.get("ultimo_uid") or 0) if mesma_pasta else 0
        # "Pasta recriada" só existe se ALGUMA leitura já abriu a caixa (tem
        # UIDVALIDITY). Tentativa que falhou antes de conectar também grava
        # `ultima_leitura` — em 28/09/2026 foram horas com a porta errada, e a
        # primeira leitura boa olhou um dia em vez de sete e pulou 38 e-mails.
        ultima = _data(estado.get("ultima_leitura")) if estado.get("uidvalidity") else None
        desde = ((ultima - timedelta(days=1)) if (ultima and not mesma_pasta)
                 else agora - timedelta(days=PRIMEIRA_LEITURA_DIAS))
        uids = imap.uids_novos(conexao, depois or None, desde)
        restam = max(0, len(uids) - MAX_POR_RODADA)
        uids = uids[:MAX_POR_RODADA]

        empresas = anexos.mapa_de_empresas(db.query(Tenant).all())
        memoria = {"regras": remetentes.carregar(db),
                   "proprios": _dominios_proprios(db, config)}
        emails = xmls = 0
        estado.update({"pasta": pasta, "uidvalidity": uidvalidity, "ultimo_uid": depois})
        _gravar_estado(db, estado, caixa["chave_estado"])
        for uid in uids:
            novo, aceitos = _um_email(db, conexao, empresas, pasta, uidvalidity, uid, memoria)
            emails += novo
            xmls += aceitos
            estado["ultimo_uid"] = max(estado["ultimo_uid"], uid)
            _gravar_estado(db, estado, caixa["chave_estado"])
    finally:
        imap.fechar(conexao)

    partes = [f"{emails} e-mail(s) novo(s)", f"{xmls} XML(s) aproveitado(s)"]
    if restam:
        partes.append(f"{restam} ficam para a próxima leitura")
    return {"status": "ok", "mensagem": " · ".join(partes), "emails": emails, "xmls": xmls,
            "restam": restam}


def _dominios_proprios(db, config: dict) -> set:
    """Os domínios do escritório: quem escreve deles não "é" cliente nenhum."""
    dominios = {remetentes.dominio_de(config.get("usuario") or "")}
    dominios.add(remetentes.dominio_de(cfg.config_smtp(db).get("remetente") or ""))
    return {d for d in dominios if d}


def _um_email(db, conexao, empresas: dict, pasta: str, uidvalidity: str, uid: int,
              memoria: dict | None = None) -> tuple[int, int]:
    """(1 se gravou um e-mail novo, XMLs aproveitados)."""
    ja = (db.query(EmailMensagem.id)
          .filter(EmailMensagem.pasta == pasta, EmailMensagem.uidvalidity == uidvalidity,
                  EmailMensagem.uid == uid).first())
    if ja:
        return 0, 0
    registro = EmailMensagem(id=str(uuid.uuid4()), pasta=pasta, uidvalidity=uidvalidity,
                             uid=uid, situacao="erro")
    tamanho = imap.tamanho(conexao, uid)
    if tamanho is not None and tamanho > imap.TAMANHO_MAXIMO:
        registro.situacao = "grande"
        registro.detalhe = json.dumps({"tamanho": tamanho}, ensure_ascii=False)
        db.add(registro)
        db.commit()
        return 1, 0
    bruto = imap.baixar(conexao, uid)
    if bruto is None:
        registro.detalhe = json.dumps({"erro": "o servidor não entregou o conteúdo"})
        db.add(registro)
        db.commit()
        return 1, 0
    try:
        return _processar(db, registro, bruto, empresas, memoria)
    except Exception:
        logger.exception("E-mail UID %s não pôde ser processado", uid)
        db.rollback()
        registro = EmailMensagem(id=str(uuid.uuid4()), pasta=pasta, uidvalidity=uidvalidity,
                                 uid=uid, situacao="erro",
                                 detalhe=json.dumps({"erro": "erro inesperado ao abrir o e-mail"}))
        db.add(registro)
        db.commit()
        return 1, 0


def _processar(db, registro: EmailMensagem, bruto: bytes, empresas: dict,
               memoria: dict | None = None) -> tuple[int, int]:
    memoria = memoria or {"regras": {}, "proprios": set()}
    msg = anexos.abrir(bruto)
    message_id = str(msg.get("Message-ID") or "").strip()[:500] or None
    if message_id:
        repetido = (db.query(EmailMensagem.id)
                    .filter(EmailMensagem.message_id == message_id).first())
        if repetido:
            # Mesmo e-mail com UID novo (a pasta foi recriada no servidor).
            return 0, 0
    nome, endereco = anexos.remetente(msg)
    registro.message_id = message_id
    registro.remetente, registro.remetente_nome = endereco, nome
    registro.assunto = str(msg.get("Subject") or "").strip()[:500]
    registro.recebido_em = imap.data_do_cabecalho(msg.get("Date"))

    if msg.get(CABECALHO_PROPRIO):
        registro.situacao = "proprio"
        db.add(registro)
        db.commit()
        return 1, 0

    registro.automatico = remetentes.parece_automatico(msg)
    # Silenciado não vai para a IA — então o corpo nem é guardado.
    if not remetentes.silenciado(endereco, memoria["regras"]):
        registro.trecho = anexos.texto_do_corpo(msg) or None
    xmls, speds, recusas, total = anexos.arquivos_do_email(msg)
    registro.anexos = total
    speds_aceitos = _guardar_speds(db, registro, speds, empresas, recusas)
    aceitos, repetidos, vistos = [], 0, set()
    for nome_xml, dados in xmls:
        r = anexos.classificar(nome_xml, dados, empresas)
        if not r["ok"]:
            recusas.append({k: r[k] for k in ("arquivo", "documento", "motivo") if k in r})
            continue
        chave_unica = (r["tenant_id"], r["chave"])
        existe = chave_unica in vistos or (
            db.query(EmailXml.id).filter(EmailXml.tenant_id == r["tenant_id"],
                                         EmailXml.chave == r["chave"]).first())
        if existe:
            repetidos += 1
            continue
        vistos.add(chave_unica)
        relativo = armazenamento.relativo(r["tenant_id"], r["competencia"], r["chave"])
        armazenamento.gravar(relativo, dados)
        db.add(EmailXml(id=str(uuid.uuid4()), tenant_id=r["tenant_id"], chave=r["chave"],
                        modelo=r["modelo"], competencia=r["competencia"], numero=r["numero"],
                        emitente_nome=r["emitente_nome"], emitente_cnpj=r["emitente_cnpj"],
                        valor=r["valor"], arquivo=relativo, mensagem_id=registro.id,
                        recebido_em=registro.recebido_em, origem="email"))
        aceitos.append({"empresa": r["empresa"], "tenant_id": r["tenant_id"],
                        "modelo": r["modelo"], "numero": r["numero"],
                        "competencia": r["competencia"], "emitente": r["emitente_nome"]})

    registro.xmls_aceitos, registro.xmls_repetidos = len(aceitos), repetidos
    registro.xmls_recusados = len(recusas)
    if speds_aceitos:
        registro.situacao = "com_sped"
    elif aceitos or repetidos:
        registro.situacao = "com_xml"
    elif xmls or recusas or speds:
        registro.situacao = "xml_recusado"
    elif total:
        registro.situacao = "outros_anexos"
    else:
        registro.situacao = "sem_anexo"
    registro.detalhe = json.dumps({"aceitos": aceitos[:300], "recusas": recusas[:300],
                                   "repetidos": repetidos, "speds": speds_aceitos},
                                  ensure_ascii=False)
    remetentes.aprender_empresa(db, endereco, {a["tenant_id"] for a in aceitos},
                                memoria["proprios"], memoria["regras"])
    db.add(registro)
    db.commit()
    return 1, len(aceitos)


def _guardar_speds(db, registro: EmailMensagem, speds: list, empresas: dict,
                   recusas: list) -> list[dict]:
    """Grava o SPED e deixa o download dos XMLs no SIEG na fila. O mesmo
    arquivo mandado de novo (mesmo conteúdo, mesma empresa) não vira outra
    fila — senão cada reenvio baixaria o mês inteiro do SIEG de novo.
    """
    aceitos = []
    for nome, dados in speds:
        r = anexos.classificar_sped(nome, dados, empresas)
        if not r["ok"]:
            recusas.append({"arquivo": r["arquivo"], "documento": "SPED", "motivo": r["motivo"]})
            continue
        sha = hashlib.sha256(dados).hexdigest()
        ja = (db.query(EmailSped.id)
              .filter(EmailSped.tenant_id == r["tenant_id"], EmailSped.sha256 == sha).first())
        if ja is None:
            relativo = armazenamento.relativo_sped(r["tenant_id"], r["competencia"], sha)
            armazenamento.gravar(relativo, dados)
            db.add(EmailSped(id=str(uuid.uuid4()), tenant_id=r["tenant_id"],
                             competencia=r["competencia"], leiaute=r["leiaute"],
                             arquivo=relativo, nome_original=nome.split(" › ")[-1][:255],
                             sha256=sha, mensagem_id=registro.id,
                             recebido_em=registro.recebido_em, sieg_status="pendente"))
        aceitos.append({"empresa": r["empresa"], "tenant_id": r["tenant_id"],
                        "competencia": r["competencia"], "leiaute": r["leiaute"],
                        "repetido": ja is not None})
    return aceitos


# ---------------------------------------------------------------- limpeza

def _limpar(db, agora: datetime) -> None:
    """XML vencido sai do disco e do banco; corpo de e-mail vencido é apagado;
    a trilha dos e-mails vai embora depois de `DIAS_TRILHA`."""
    limite_xml = agora - timedelta(days=armazenamento.DIAS_RETENCAO)
    vencidos = db.query(EmailXml).filter(EmailXml.created_at < limite_xml).limit(2000).all()
    for x in vencidos:
        armazenamento.apagar(x.arquivo)
        db.delete(x)
    (db.query(EmailMensagem)
     .filter(EmailMensagem.created_at < agora - timedelta(days=DIAS_TRECHO),
             EmailMensagem.trecho.isnot(None))
     .update({EmailMensagem.trecho: None}, synchronize_session=False))
    (db.query(EmailMensagem)
     .filter(EmailMensagem.created_at < agora - timedelta(days=DIAS_TRILHA))
     .delete(synchronize_session=False))
    db.commit()
