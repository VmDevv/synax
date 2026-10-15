"""Coleta automática de NFS-e pelo ADN, no horário que o admin configurou.

O agendador roda DENTRO do servidor web (decisão do Sandro em 24/09/2026: o
servidor do escritório não é desligado). É uma thread que acorda a cada minuto,
procura a automação vencida e a executa. Três cuidados que não se deduzem do
código à primeira vista:

1. **Não roda em dobro.** Quem executa precisa antes trocar a `proxima_
   execucao` no banco com um UPDATE condicionado ao valor antigo (`reivindicar`)
   — dois processos olhando a mesma automação, só um consegue. E uma trava de
   processo impede duas coletas ao mesmo tempo (a agendada e a do botão "rodar
   agora"), o que dobraria as chamadas ao ADN e o risco de "consumo indevido".

2. **Servidor desligado na hora marcada roda UMA vez quando volta.** A próxima
   execução é recalculada a partir de agora, não a partir da que perdeu: três
   dias fora não viram três coletas seguidas.

3. **Nenhuma rodada termina "verde com zero".** Cada empresa sai com o
   resultado dela — notas novas, falha com o motivo, ou ADIADA (o ADN pediu
   para esperar) —, e o status da rodada reflete o pior caso. Uma coleta que
   não consultou ninguém não pode dizer que ninguém recebeu nota.

O download em si é o MESMO da tela (`_buscar_uma_empresa` em routes/nfse.py):
cursor por NSU, nota gravada antes de o cursor andar. Só o teto de tempo por
empresa é maior, porque aqui ninguém está esperando a resposta.

Desde 25/09/2026 o mesmo agendador roda também os INFORMATIVOS (resumo das
auditorias, erros do sistema — services/automacoes/). A trava passou a ser por
TIPO, e cada rodada agendada corre na própria thread: uma coleta de NFS-e que
leva uma hora não segura o resumo marcado para o mesmo horário, e duas coletas
continuam sem rodar juntas.
"""
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta

from sqlalchemy import update

from ...core.fuso import FUSO_LOCAL_HORAS
from ...domain.models.auth_models import Tenant
from ...domain.models.automacao import NfseAutomacao, NfseAutomacaoExecucao
from ...domain.models.certificates import TenantCertificate
from . import certificados

logger = logging.getLogger(__name__)

FREQUENCIAS = ("diaria", "semanal", "mensal")
ESCOPOS = ("todas", "selecionadas")
# O 29, o 30 e o 31 não existem em todo mês: automação marcada para o 31
# simplesmente não rodaria em abril, e ninguém seria avisado.
DIA_MES_MAXIMO = 28
DIAS_CURTOS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]

# status -> (rótulo, tom). O tom escolhe a cor na tela e no e-mail.
ROTULOS_STATUS = {
    "rodando": ("Em andamento", "info"),
    "ok": ("Concluída", "ok"),
    "com_falhas": ("Concluída com avisos", "aviso"),
    "falhou": ("Falhou", "erro"),
    "vazia": ("Nada a consultar", "aviso"),
    "interrompida": ("Interrompida", "aviso"),
    # Informativo sem nada no período: nenhuma auditoria, nenhum erro.
    "sem_novidade": ("Nada a informar", "info"),
}

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Tempo máximo por empresa numa rodada. O que não couber continua na próxima,
# porque o cursor de NSU fica gravado — mas na primeira coleta de uma empresa
# grande (90 dias de notas no ADN) o teto de 60s da tela não daria conta.
TETO_EMPRESA_SEG = float(os.environ.get("NFSE_AUTOMACAO_TETO_SEG", "600"))

# Certificado que vence dentro deste prazo entra no informativo.
AVISO_CERTIFICADO_DIAS = 30

INTERVALO_SEG = 60

_executando = threading.Lock()      # a trava da COLETA (nome mantido: os testes a usam)
_travas = {"coleta_nfse": _executando}
_travas_guarda = threading.Lock()
_parar = threading.Event()
_thread = None
# Última rodada disparada pelo botão — os testes esperam por ela.
_ultima_manual = None


def _trava(tipo: str) -> threading.Lock:
    """Uma trava por tipo de automação: duas coletas nunca juntas (ADN), mas
    a coleta não segura o resumo nem o informe de erros."""
    with _travas_guarda:
        if tipo not in _travas:
            _travas[tipo] = threading.Lock()
        return _travas[tipo]


# ------------------------------------------------------------------ agenda

def validar_hora(texto: str) -> str | None:
    """ "7:5" -> None; "07:05" -> "07:05". Só HH:MM válido passa."""
    partes = (texto or "").strip().split(":")
    if len(partes) != 2 or not all(p.isdigit() for p in partes):
        return None
    h, m = int(partes[0]), int(partes[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


def dias_de(texto: str | None) -> list[int]:
    dias = set()
    for parte in (texto or "").split(","):
        parte = parte.strip()
        if parte.isdigit() and 0 <= int(parte) <= 6:
            dias.add(int(parte))
    return sorted(dias)


def validar_dia_mes(valor) -> int | None:
    try:
        dia = int(str(valor).strip())
    except (TypeError, ValueError):
        return None
    return dia if 1 <= dia <= DIA_MES_MAXIMO else None


def proxima_execucao(frequencia: str, hora: str, dias: list[int],
                     depois_de: datetime, dia_mes: int | None = None) -> datetime | None:
    """Próximo instante (UTC) em que a automação deve rodar, estritamente
    depois de `depois_de` (UTC). A hora configurada é de Brasília."""
    hora = validar_hora(hora)
    if not hora:
        return None
    h, m = (int(x) for x in hora.split(":"))
    local = depois_de + timedelta(hours=FUSO_LOCAL_HORAS)
    if frequencia == "mensal":
        dia = validar_dia_mes(dia_mes)
        if dia is None:
            return None
        candidato = local.replace(day=dia, hour=h, minute=m, second=0, microsecond=0)
        if candidato <= local:
            ano, mes = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
            candidato = candidato.replace(year=ano, month=mes)
        return candidato - timedelta(hours=FUSO_LOCAL_HORAS)
    permitidos = set(range(7)) if frequencia == "diaria" else set(dias or [])
    if not permitidos:
        return None
    candidato = local.replace(hour=h, minute=m, second=0, microsecond=0)
    for _ in range(8):
        if candidato > local and candidato.weekday() in permitidos:
            return candidato - timedelta(hours=FUSO_LOCAL_HORAS)
        candidato += timedelta(days=1)
    return None


def proxima_da(automacao: NfseAutomacao, depois_de: datetime) -> datetime | None:
    """A próxima execução de uma automação gravada — a mesma conta em todo
    lugar que a recalcula (gravação, reativação e cada rodada)."""
    return proxima_execucao(automacao.frequencia, automacao.hora,
                            dias_de(automacao.dias_semana), depois_de,
                            getattr(automacao, "dia_mes", None))


def descrever_agenda(frequencia: str, hora: str, dias_texto: str | None,
                     dia_mes: int | None = None) -> str:
    """ "Todo dia às 07:00" / "Seg, Qua e Sex às 07:00" / "Todo dia 1º do mês às 08:00". """
    if frequencia == "diaria":
        return f"Todo dia às {hora}"
    if frequencia == "mensal":
        dia = validar_dia_mes(dia_mes)
        if dia is None:
            return f"Mensal às {hora} (sem dia escolhido)"
        return f"Todo dia {'1º' if dia == 1 else dia} do mês às {hora}"
    nomes = [DIAS_CURTOS[d] for d in dias_de(dias_texto)]
    if not nomes:
        return f"Semanal às {hora} (sem dia escolhido)"
    if len(nomes) == 7:
        return f"Todo dia às {hora}"
    lista = nomes[0] if len(nomes) == 1 else ", ".join(nomes[:-1]) + " e " + nomes[-1]
    return f"{lista} às {hora}"


# ----------------------------------------------------------------- escopo

def ids_selecionados(automacao: NfseAutomacao) -> list[str]:
    return [x.strip() for x in (automacao.empresas or "").split(",") if x.strip()]


def empresas_do_escopo(db, automacao: NfseAutomacao) -> list:
    """Empresas ATIVAS que esta automação consulta.

    "todas" = as que têm certificado cadastrado (ativo ou não: certificado
    inativo aparece como falha no informativo, em vez de a empresa sumir da
    coleta sem ninguém notar). "selecionadas" = a lista gravada.
    """
    if automacao.escopo == "selecionadas":
        ids = []
        for bruto in ids_selecionados(automacao):
            try:
                ids.append(uuid.UUID(bruto))
            except ValueError:
                continue
        if not ids:
            return []
        filtro = Tenant.id.in_(ids)
    else:
        com_certificado = {r[0] for r in db.query(TenantCertificate.tenant_id).all()}
        ids = []
        for bruto in com_certificado:
            try:
                ids.append(uuid.UUID(str(bruto)))
            except ValueError:
                continue
        if not ids:
            return []
        filtro = Tenant.id.in_(ids)
    return (db.query(Tenant).filter(filtro, Tenant.ativo.is_(True))
            .order_by(Tenant.name).all())


def certificados_vencendo(db, empresas: list, agora: datetime) -> list[dict]:
    """Certificados das empresas do escopo que vencem em até 30 dias — ou já
    venceram. É o jeito mais comum de a coleta parar em silêncio."""
    nomes = {str(t.id): t.name for t in empresas}
    if not nomes:
        return []
    limite = agora + timedelta(days=AVISO_CERTIFICADO_DIAS)
    certs = (db.query(TenantCertificate)
             .filter(TenantCertificate.tenant_id.in_(list(nomes)),
                     TenantCertificate.validade.isnot(None),
                     TenantCertificate.validade <= limite)
             .order_by(TenantCertificate.validade).all())
    saida = []
    for c in certs:
        dias = (c.validade.date() - agora.date()).days
        saida.append({"empresa": nomes.get(str(c.tenant_id), ""), "cnpj": c.cnpj,
                      "validade": c.validade.strftime("%Y-%m-%d"), "dias": dias,
                      "vencido": dias < 0})
    return saida


# --------------------------------------------------------------- execução

def reivindicar(db, automacao: NfseAutomacao, agora: datetime) -> bool:
    """Troca a próxima execução no banco — só quem conseguir roda."""
    nova = proxima_da(automacao, agora)
    resultado = db.execute(
        update(NfseAutomacao)
        .where(NfseAutomacao.id == automacao.id,
               NfseAutomacao.ativo.is_(True),
               NfseAutomacao.proxima_execucao == automacao.proxima_execucao)
        .values(proxima_execucao=nova, ultima_execucao=agora))
    db.commit()
    return resultado.rowcount == 1


def em_andamento(db) -> NfseAutomacaoExecucao | None:
    return (db.query(NfseAutomacaoExecucao)
            .filter(NfseAutomacaoExecucao.status == "rodando")
            .order_by(NfseAutomacaoExecucao.inicio.desc()).first())


def _tipo_da_automacao(sessao_factory, automacao_id: str) -> str | None:
    """O tipo, lido numa sessão curta — quem vai esperar a trava não pode
    ficar com transação aberta no banco enquanto espera."""
    db = sessao_factory()
    try:
        automacao = db.get(NfseAutomacao, automacao_id)
        return (automacao.tipo or "coleta_nfse") if automacao is not None else None
    finally:
        db.close()


def executar(sessao_factory, automacao_id: str, gatilho: str,
             esperar: bool = True) -> str | None:
    """Roda uma automação. Devolve o id da execução, ou None se outra do mesmo
    tipo já está em andamento (e `esperar` é falso)."""
    tipo = _tipo_da_automacao(sessao_factory, automacao_id)
    if tipo is None:
        return None
    trava = _trava(tipo)
    if not trava.acquire(blocking=esperar):
        return None
    db = sessao_factory()
    execucao = None
    try:
        automacao = db.get(NfseAutomacao, automacao_id)
        if automacao is None:
            return None

        execucao = NfseAutomacaoExecucao(
            id=str(uuid.uuid4()), automacao_id=automacao.id, tipo=tipo, gatilho=gatilho,
            status="rodando", inicio=datetime.utcnow())
        db.add(execucao)
        db.commit()

        if tipo == "coleta_nfse":
            _rodar_coleta(db, automacao, execucao)
        else:
            _rodar_informativo(db, automacao, execucao, tipo, sessao_factory)
        return execucao.id
    except Exception:
        logger.exception("Automação %s (%s) falhou", automacao_id, tipo)
        try:
            db.rollback()
            if execucao is not None:
                viva = db.get(NfseAutomacaoExecucao, execucao.id)
                if viva is not None:
                    viva.status = "falhou"
                    viva.fim = datetime.utcnow()
                    viva.mensagem = (
                        "A coleta parou por um erro inesperado. O que foi baixado até "
                        "ali está gravado; veja o log do servidor." if tipo == "coleta_nfse"
                        else "O informativo não pôde ser montado por um erro inesperado e "
                             "não foi enviado; veja o log do servidor.")
                    db.commit()
        except Exception:
            logger.exception("Não foi possível registrar a falha da automação")
        return execucao.id if execucao is not None else None
    finally:
        db.close()
        trava.release()


def _rodar_coleta(db, automacao: NfseAutomacao, execucao: NfseAutomacaoExecucao) -> None:
    empresas = empresas_do_escopo(db, automacao)
    execucao.empresas_total = len(empresas)
    db.commit()

    # Import tardio: a função de download mora na rota da tela, que é quem
    # a testa há mais tempo. Um caminho só para os dois — a tela e a
    # automação não podem baixar de jeitos diferentes.
    from ...api.routes import nfse as rota_nfse

    resultados = []
    for empresa in empresas:
        resultados.append(_coletar_empresa(db, rota_nfse, empresa))
        r = resultados[-1]
        if r["tipo_erro"] == "adiada":
            execucao.empresas_adiadas += 1
        elif r["erro"]:
            execucao.empresas_falha += 1
        else:
            execucao.empresas_ok += 1
        execucao.notas_novas += r["novos"]
        execucao.notas_atualizadas += r["atualizados"]
        # Progresso visível na tela enquanto a coleta anda.
        db.commit()

    vencendo = certificados_vencendo(db, empresas, datetime.utcnow())
    execucao.certificados_vencendo = len(vencendo)
    execucao.detalhe = json.dumps(
        {"empresas": resultados, "certificados_vencendo": vencendo},
        ensure_ascii=False)
    execucao.status, execucao.mensagem = _desfecho(execucao)
    execucao.fim = datetime.utcnow()
    db.commit()
    try:
        _pos_coleta(db, execucao, automacao)
    except Exception:
        # A coleta terminou e está gravada com o status certo; um tropeço
        # no pós (banco oscilando no commit, por exemplo) não a reescreve
        # como "falhou".
        logger.exception("Pós-coleta (planilha/e-mail) falhou na execução %s", execucao.id)
        try:
            db.rollback()
        except Exception:
            pass


def _ultimo_fim(db, automacao_id: str, sem_id: str):
    """Onde a última rodada BEM-SUCEDIDA desta automação terminou. Rodada que
    falhou ou foi interrompida não conta: o período dela volta na próxima."""
    anterior = (db.query(NfseAutomacaoExecucao.periodo_fim)
                .filter(NfseAutomacaoExecucao.automacao_id == automacao_id,
                        NfseAutomacaoExecucao.id != sem_id,
                        NfseAutomacaoExecucao.periodo_fim.isnot(None),
                        NfseAutomacaoExecucao.status.in_(("ok", "sem_novidade")))
                .order_by(NfseAutomacaoExecucao.periodo_fim.desc()).first())
    return anterior[0] if anterior else None


def _rodar_informativo(db, automacao: NfseAutomacao, execucao: NfseAutomacaoExecucao,
                       tipo: str, sessao_factory=None) -> None:
    """Resumo das auditorias ou erros do sistema: levanta, registra e envia.

    Sem novidade no período (nenhuma auditoria, nenhum erro), a rodada é
    registrada como "Nada a informar" e o e-mail só sai se a automação foi
    configurada para enviar mesmo assim — a escolha fica gravada no histórico
    (`email_status = "pulado"`), para ninguém achar que o envio falhou. O
    "Rodar agora" envia sempre: quem clica quer ver o e-mail chegar, e um
    clique que não manda nada pareceria defeito.
    """
    from ..automacoes import informe_erros, janela, resumo_auditorias, tipos

    config = tipos.ler_config(automacao)
    agora = datetime.utcnow()
    inicio, fim = janela.janela(config.get("periodo", "desde_ultima"), agora,
                                _ultimo_fim(db, automacao.id, execucao.id),
                                automacao.frequencia)
    execucao.periodo_inicio, execucao.periodo_fim = inicio, fim

    if tipo == "resumo_auditorias":
        dados = resumo_auditorias.coletar(db, automacao, config, inicio, fim)
        status, mensagem, novidade = resumo_auditorias.desfecho(dados)
        execucao.empresas_total = dados["totais"]["empresas"]
    elif tipo == "erros_sistema":
        dados = informe_erros.coletar(config, inicio, fim)
        status, mensagem, novidade = informe_erros.desfecho(dados)
    elif tipo == "resumo_emails":
        from ..email_recebimento import leitura, resumo as resumo_emails
        # Lê a caixa ANTES de resumir (esperando a leitura de hora em hora, se
        # estiver no meio): o resumo das 08h tem de incluir o e-mail das 07h59.
        # O fim da janela passa a ser o fim da leitura, senão o que ela acabou
        # de gravar cairia fora do período e dentro do próximo.
        if sessao_factory is not None and cfg_imap_ligado(db):
            leitura.ler(sessao_factory, "resumo", esperar=600)
            fim = datetime.utcnow()
            execucao.periodo_fim = fim
        dados = resumo_emails.coletar(db, config, inicio, fim)
        status, mensagem, novidade = resumo_emails.desfecho(dados)
    else:
        raise ValueError(f"tipo de automação desconhecido: {tipo}")

    execucao.detalhe = json.dumps(dados, ensure_ascii=False, default=str)
    execucao.status, execucao.mensagem = status, mensagem[:500]
    execucao.fim = datetime.utcnow()
    db.commit()

    if not novidade and config.get("so_com_novidade") and execucao.gatilho != "manual":
        execucao.email_status = "pulado"
        execucao.email_detalhe = ("Nada a informar no período — esta automação envia só "
                                  "quando há novidade.")
        db.commit()
        return

    def montar():
        if tipo == "resumo_auditorias":
            planilha = None
            try:
                planilha = resumo_auditorias.gerar_planilha(dados, automacao.nome)
            except Exception:
                logger.exception("Planilha do resumo %s não montou", execucao.id)
            assunto, html, texto = resumo_auditorias.montar_email(
                dados, automacao.nome, com_anexo=bool(planilha))
            anexos = ([(resumo_auditorias.nome_arquivo(execucao), planilha, XLSX)]
                      if planilha else [])
            return assunto, html, texto, anexos
        if tipo == "resumo_emails":
            from ..email_recebimento import resumo as resumo_emails
            assunto, html, texto = resumo_emails.montar_email(dados, automacao.nome)
            return assunto, html, texto, []
        assunto, html, texto = informe_erros.montar_email(dados, automacao.nome)
        return assunto, html, texto, []

    _enviar_email(db, execucao, automacao, montar)


def cfg_imap_ligado(db) -> bool:
    from .. import configuracoes as cfg
    return cfg.config_imap(db)["ligado"] and cfg.imap_configurado(db)


def _enviar_email(db, execucao: NfseAutomacaoExecucao, automacao: NfseAutomacao, montar) -> None:
    """Envia o que `montar()` devolve (assunto, html, texto, anexos) para os
    destinatários da automação, e REGISTRA o resultado na execução: e-mail que
    não sai fica em `email_status` com o motivo — informativo que ninguém
    recebe é exatamente o silêncio que a automação existe para evitar."""
    from .. import configuracoes as cfg
    from .. import email_envio
    from . import informativo

    destinatarios, _invalidos = informativo.ler_destinatarios(automacao.destinatarios or "")
    if not destinatarios:
        execucao.email_status = "sem_destinatario"
        execucao.email_detalhe = None
        db.commit()
        return
    try:
        assunto, html, texto, anexos = montar()
        email_envio.enviar(cfg.config_smtp(db), destinatarios, assunto, html, texto, anexos)
        execucao.email_status = "enviado"
        execucao.email_detalhe = ("Enviado para " + ", ".join(destinatarios))[:500]
    except email_envio.FalhaEnvio as err:
        execucao.email_status = "falhou"
        execucao.email_detalhe = str(err)[:500]
    except Exception:
        logger.exception("Informativo da execução %s não saiu", execucao.id)
        execucao.email_status = "falhou"
        execucao.email_detalhe = ("Erro inesperado ao montar ou enviar o informativo; "
                                  "veja o log do servidor.")
    db.commit()


def _coletar_empresa(db, rota_nfse, empresa) -> dict:
    """Uma empresa. Nunca levanta: o resultado leva o motivo da falha."""
    base = {"tenant_id": str(empresa.id), "empresa": empresa.name, "cnpj": "",
            "novos": 0, "atualizados": 0, "erro": None, "tipo_erro": None, "nsu": 0}
    cnpj = "".join(ch for ch in (empresa.cnpj or "") if ch.isdigit())
    base["cnpj"] = cnpj
    if len(cnpj) != 14:
        return {**base, "erro": "Empresa sem CNPJ válido cadastrado.", "tipo_erro": "cadastro"}
    try:
        pfx, senha = certificados.carregar_material(db, str(empresa.id), cnpj)
    except ValueError as err:
        return {**base, "erro": str(err), "tipo_erro": "certificado"}
    try:
        r = rota_nfse._buscar_uma_empresa(
            db, str(empresa.id), cnpj, pfx, senha, False,
            limite_seg=TETO_EMPRESA_SEG, teto_seg=TETO_EMPRESA_SEG)
    except Exception:
        logger.exception("Coleta automática: falha na empresa %s", cnpj)
        try:
            db.rollback()
        except Exception:
            pass
        return {**base, "erro": "Erro inesperado na consulta.", "tipo_erro": "inesperado"}
    finally:
        pfx, senha = b"", ""
    return {**base, "novos": r.get("novos", 0), "atualizados": r.get("atualizados", 0),
            "erro": r.get("erro"), "tipo_erro": r.get("tipo_erro"), "nsu": r.get("nsu", 0)}


def _pos_coleta(db, execucao: NfseAutomacaoExecucao, automacao: NfseAutomacao) -> None:
    """Planilha na pasta da rede (fase 2) e informativo por e-mail (fase 3).

    Nada aqui desfaz a coleta: as notas já estão gravadas. O que falhar fica
    REGISTRADO na execução — planilha não salva vai para a mensagem, e-mail
    que não saiu vai para `email_status` com o motivo —, porque informativo
    que ninguém recebe é exatamente o silêncio que a automação existe para
    evitar.
    """
    from . import informativo, relatorio_coleta

    rotulo = ROTULOS_STATUS.get(execucao.status, (execucao.status, ""))[0]
    planilha = None
    try:
        planilha = relatorio_coleta.gerar_planilha(db, execucao, automacao.nome, rotulo)
    except Exception:
        logger.exception("Não foi possível montar a planilha da coleta %s", execucao.id)
        execucao.mensagem = ((execucao.mensagem or "") + " · planilha não gerada (erro no log)")[:500]
    if planilha:
        execucao.relatorio = relatorio_coleta.salvar(execucao, planilha)
        if execucao.relatorio is None:
            execucao.mensagem = ((execucao.mensagem or "")
                                 + " · planilha não salva: pasta da rede indisponível")[:500]
    db.commit()

    def montar():
        assunto, html, texto = informativo.montar(db, execucao, automacao.nome, rotulo,
                                                  com_anexo=bool(planilha))
        anexos = ([(relatorio_coleta.nome_arquivo(execucao), planilha, XLSX)]
                  if planilha else [])
        return assunto, html, texto, anexos

    _enviar_email(db, execucao, automacao, montar)


def _desfecho(execucao: NfseAutomacaoExecucao) -> tuple[str, str]:
    total = execucao.empresas_total
    if total == 0:
        return "vazia", "Nenhuma empresa no escopo desta automação — nada foi consultado."
    if execucao.empresas_falha == total:
        return "falhou", f"Nenhuma das {total} empresas foi consultada com sucesso."
    partes = [f"{execucao.notas_novas} nota(s) nova(s) em {execucao.empresas_ok} de {total} empresa(s)"]
    if execucao.empresas_falha:
        partes.append(f"{execucao.empresas_falha} com falha")
    if execucao.empresas_adiadas:
        partes.append(f"{execucao.empresas_adiadas} adiada(s) pelo ADN")
    status = "com_falhas" if (execucao.empresas_falha or execucao.empresas_adiadas) else "ok"
    return status, " · ".join(partes)


def rodar_devidas(sessao_factory, agora: datetime, em_paralelo: bool = False) -> int:
    """Executa as automações vencidas. Devolve quantas foram disparadas.

    `em_paralelo` (o que o agendador usa): cada rodada na própria thread, para
    uma coleta longa não segurar o informativo marcado para o mesmo horário.
    Sem ele (testes), uma depois da outra, e a função só volta no fim.
    """
    db = sessao_factory()
    try:
        devidas = (db.query(NfseAutomacao)
                   .filter(NfseAutomacao.ativo.is_(True),
                           NfseAutomacao.proxima_execucao.isnot(None),
                           NfseAutomacao.proxima_execucao <= agora)
                   .order_by(NfseAutomacao.proxima_execucao).all())
        minhas = [a.id for a in devidas if reivindicar(db, a, agora)]
    finally:
        db.close()
    # `esperar=True`: se o botão "rodar agora" estiver com a vez, a agendada
    # espera terminar — ela já foi reivindicada e não pode ser pulada.
    if em_paralelo:
        for aid in minhas:
            threading.Thread(target=executar, args=(sessao_factory, aid, "agenda"),
                             kwargs={"esperar": True}, name=f"automacao-{aid[:8]}",
                             daemon=True).start()
        return len(minhas)
    return sum(1 for aid in minhas if executar(sessao_factory, aid, "agenda", esperar=True))


def rodar_agora(sessao_factory, automacao_id: str) -> tuple[bool, str]:
    """Botão "rodar agora": dispara em segundo plano e responde na hora."""
    global _ultima_manual
    tipo = _tipo_da_automacao(sessao_factory, automacao_id) or "coleta_nfse"
    coleta = tipo == "coleta_nfse"
    if _trava(tipo).locked():
        return False, ("Já existe uma coleta em andamento. Aguarde ela terminar." if coleta
                       else "Já há um informativo deste tipo em andamento. Aguarde ele terminar.")
    _ultima_manual = threading.Thread(
        target=executar, args=(sessao_factory, automacao_id, "manual"),
        kwargs={"esperar": False}, name="automacao-manual", daemon=True)
    _ultima_manual.start()
    return True, ("Coleta iniciada. O andamento aparece no histórico abaixo." if coleta
                  else "Informativo em preparação. O resultado aparece no histórico abaixo.")


def marcar_interrompidas(db) -> int:
    """Rodada que estava "rodando" quando o servidor caiu não vai terminar."""
    presas = db.query(NfseAutomacaoExecucao).filter(
        NfseAutomacaoExecucao.status == "rodando").all()
    for e in presas:
        e.status = "interrompida"
        e.fim = datetime.utcnow()
        e.mensagem = (
            "O servidor reiniciou durante a coleta. O que foi baixado até ali está "
            "gravado, e a próxima execução continua do mesmo ponto."
            if (e.tipo or "coleta_nfse") == "coleta_nfse" else
            "O servidor reiniciou enquanto o informativo era montado; ele não foi "
            "enviado, e a próxima rodada cobre o período.")
    if presas:
        db.commit()
    return len(presas)


# -------------------------------------------------------------- agendador

def iniciar_agendador(sessao_factory) -> bool:
    """Liga a thread do agendador. `SYNAX_AGENDADOR=0` desliga — para rodar
    uma segunda cópia do sistema (teste, desenvolvimento) contra o mesmo banco
    sem ela também coletar."""
    global _thread
    if os.environ.get("SYNAX_AGENDADOR", "1") == "0":
        logger.info("Agendador de NFS-e desligado por SYNAX_AGENDADOR=0")
        return False
    if _thread is not None and _thread.is_alive():
        return False
    try:
        db = sessao_factory()
        try:
            marcar_interrompidas(db)
        finally:
            db.close()
    except Exception:
        logger.exception("Agendador de NFS-e: não foi possível revisar coletas interrompidas")
    _parar.clear()
    _thread = threading.Thread(target=_laco, args=(sessao_factory,),
                               name="agendador-nfse", daemon=True)
    _thread.start()
    logger.info("Agendador de NFS-e ligado (confere a cada %ss)", INTERVALO_SEG)
    return True


def parar_agendador() -> None:
    _parar.set()


def _laco(sessao_factory) -> None:
    while not _parar.wait(INTERVALO_SEG):
        try:
            rodar_devidas(sessao_factory, datetime.utcnow(), em_paralelo=True)
        except Exception:
            logger.exception("Agendador de automações: falha no ciclo")
        # Leitura da caixa de e-mail, de hora em hora, quando ligada na aba
        # Configurações. Na mesma thread do agendador para não existir um
        # segundo relógio no servidor; a leitura em si corre na própria thread.
        try:
            from ..email_recebimento import leitura
            leitura.rodar_se_devido(sessao_factory, datetime.utcnow())
        except Exception:
            logger.exception("Agendador: falha ao disparar a leitura da caixa de e-mail")
        # Fila de download no SIEG dos SPEDs que chegaram por e-mail: retoma o
        # que o reinício interrompeu e o que o SIEG mandou esperar.
        try:
            from ..email_recebimento import sieg_download
            sieg_download.iniciar_se_preciso(sessao_factory)
        except Exception:
            logger.exception("Agendador: falha ao retomar o download no SIEG")
