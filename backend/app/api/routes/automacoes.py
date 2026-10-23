"""Aba "Automações" da Administração: coleta agendada de NFS-e e informativos.

Só admin: a automação percorre empresas da carteira inteira com os
certificados do escritório. O `main.py` monta este roteador sob `/admin` com
`require_admin`, e cada rota que grava pede o perfil de admin de novo.

A regra do serviço (quando rodar, trava contra rodar em dobro, o que registrar)
está em services/nfse/automacao.py, e o que cada TIPO aceita de configuração
em services/automacoes/tipos.py. Aqui é só a tela.
"""
import json
import uuid
from datetime import datetime
from html import escape as html_escape

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from ...core.templating import build_templates
from ...domain.models.auth_models import Tenant
from ...domain.models.automacao import NfseAutomacao, NfseAutomacaoExecucao
from ...domain.models.certificates import TenantCertificate
from ...infrastructure.database.session import SessionLocal, get_db
from ...services import configuracoes as cfg
from ...services.automacoes import resumo_auditorias
from ...services.automacoes import tipos as tipos_aut
from ...services.nfse import automacao as svc
from ...services.nfse import informativo, relatorio_coleta
from .admin import require_admin

router = APIRouter(tags=["Automações"])
templates = build_templates()

STATUS_ROTULO = svc.ROTULOS_STATUS

# O que o modal de "nova automação" já traz preenchido, por tipo. O resumo
# nasce semanal na segunda de manhã (a semana que passou, antes de a equipe
# começar); o de erros, diário e calado quando não há erro.
_NOVA = {
    tipos_aut.COLETA: {"nome": "Coleta de NFS-e", "frequencia": "diaria", "hora": "07:00",
                       "dias": [0, 1, 2, 3, 4]},
    tipos_aut.RESUMO: {"nome": "Resumo semanal das auditorias", "frequencia": "semanal",
                       "hora": "08:00", "dias": [0]},
    tipos_aut.ERROS: {"nome": "Erros do sistema", "frequencia": "diaria", "hora": "08:00",
                      "dias": [0, 1, 2, 3, 4]},
    # 07:30 e nos dias úteis: é o "resumo da manhã", lido antes de a equipe
    # começar. A leitura da caixa roda antes dele, na própria rodada.
    tipos_aut.EMAILS: {"nome": "Resumo dos e-mails", "frequencia": "diaria",
                       "hora": "07:30", "dias": [0, 1, 2, 3, 4]},
    # De madrugada, nos dias úteis: o PVA leva até meio minuto por arquivo, e a pasta do mês pode ter dezenas.
    tipos_aut.VALIDAR: {"nome": "Validar e assinar SPED", "frequencia": "diaria",
                        "hora": "06:00", "dias": [0, 1, 2, 3, 4]},
}


def _toast(texto: str, sucesso: bool = True) -> str:
    classe = "alert-success" if sucesso else "alert-error"
    return (f'<div id="toast-container" hx-swap-oob="beforeend">'
            f'<div class="alert {classe} shadow-lg font-medium text-sm">'
            f'{html_escape(texto)}</div></div>')


FECHAR_MODAL = '<div id="modal-container" hx-swap-oob="innerHTML"></div>'


def _empresas_ativas(db: Session) -> list[dict]:
    """Empresas para o seletor, com a marca de quem tem certificado."""
    com_cert = {str(r[0]) for r in db.query(TenantCertificate.tenant_id).all()}
    return [{"id": str(t.id), "nome": t.name, "cnpj": t.cnpj or "",
             "certificado": str(t.id) in com_cert}
            for t in db.query(Tenant).filter(Tenant.ativo.is_(True))
                                     .order_by(Tenant.name).all()]


def _escopo_do_cartao(a: NfseAutomacao, tipo: str, ativas: set, n_todas: int) -> str:
    # Erro do sistema e e-mail da caixa não têm escopo por empresa: um é do
    # servidor, o outro é da caixa do escritório inteira.
    if tipo in (tipos_aut.ERROS, tipos_aut.EMAILS):
        return ""
    if a.escopo == "selecionadas":
        ids = svc.ids_selecionados(a)
        if tipo != tipos_aut.COLETA:
            return f"{len(ids)} empresa(s) escolhida(s)"
        validas = [i for i in ids if i in ativas]
        escopo = f"{len(validas)} empresa(s) escolhida(s)"
        if len(validas) < len(ids):
            escopo += f" · {len(ids) - len(validas)} inativa(s), fora da coleta"
        return escopo
    return f"Todas com certificado ({n_todas})" if tipo == tipos_aut.COLETA else "Todas as empresas"


def _contexto(db: Session, request: Request) -> dict:
    automacoes = db.query(NfseAutomacao).order_by(NfseAutomacao.created_at).all()
    # Coluna a coluna, sem o `detalhe`: a listagem não abre o JSON de ninguém.
    historico = (db.query(
        NfseAutomacaoExecucao.id, NfseAutomacaoExecucao.automacao_id,
        NfseAutomacaoExecucao.tipo,
        NfseAutomacaoExecucao.gatilho, NfseAutomacaoExecucao.status,
        NfseAutomacaoExecucao.inicio, NfseAutomacaoExecucao.fim,
        NfseAutomacaoExecucao.empresas_total, NfseAutomacaoExecucao.empresas_ok,
        NfseAutomacaoExecucao.empresas_falha, NfseAutomacaoExecucao.empresas_adiadas,
        NfseAutomacaoExecucao.notas_novas, NfseAutomacaoExecucao.certificados_vencendo,
        NfseAutomacaoExecucao.mensagem, NfseAutomacaoExecucao.email_status)
        .order_by(NfseAutomacaoExecucao.inicio.desc()).limit(20).all())

    ultima_por_automacao = {}
    for h in historico:
        ultima_por_automacao.setdefault(h.automacao_id, h)

    ativas = {str(t.id) for t in db.query(Tenant.id).filter(Tenant.ativo.is_(True)).all()}
    com_cert = {str(r[0]) for r in db.query(TenantCertificate.tenant_id).all()}
    n_todas = len(ativas & com_cert)

    cartoes = []
    for a in automacoes:
        tipo = a.tipo if tipos_aut.valido(a.tipo or "") else tipos_aut.COLETA
        destinatarios, _inv = informativo.ler_destinatarios(a.destinatarios or "")
        cartoes.append({
            "a": a,
            "tipo": tipo,
            "info": tipos_aut.TIPOS[tipo],
            "agenda": svc.descrever_agenda(a.frequencia, a.hora, a.dias_semana, a.dia_mes),
            "escopo": _escopo_do_cartao(a, tipo, ativas, n_todas),
            "descricao": tipos_aut.descrever(tipo, tipos_aut.ler_config(a)),
            "ultima": ultima_por_automacao.get(a.id),
            "destinatarios": destinatarios,
        })

    nomes = {a.id: a.nome for a in automacoes}
    return {
        "request": request,
        "cartoes": cartoes,
        "historico": historico,
        "nomes": nomes,
        "tipos": tipos_aut.TIPOS,
        "status_rotulo": STATUS_ROTULO,
        "rodando": any(h.status == "rodando" for h in historico),
        "agendador_ligado": bool(svc._thread is not None and svc._thread.is_alive()),
        # Automação com destinatário e servidor de e-mail em branco: o
        # informativo nunca sairia. A tela avisa antes da primeira coleta.
        "smtp_configurado": cfg.smtp_configurado(db),
    }


def _lista(db: Session, request: Request, extra: str = "",
           acompanhar: bool = False) -> HTMLResponse:
    """`acompanhar`: logo depois do "rodar agora" a execução pode ainda não
    estar no banco (a thread acabou de nascer) — a tela passa a se atualizar
    mesmo assim, senão o clique pareceria não ter feito nada."""
    ctx = _contexto(db, request)
    ctx["rodando"] = ctx["rodando"] or acompanhar
    html = templates.get_template("partials/admin/automacoes.html").render(ctx)
    return HTMLResponse(html + extra)


def _modal(request: Request, db: Session, dados: dict, erro: str = "") -> HTMLResponse:
    html = templates.get_template("partials/admin/modals/automacao.html").render({
        "request": request, "d": dados, "erro": erro,
        "empresas": _empresas_ativas(db), "dias_curtos": svc.DIAS_CURTOS,
        "smtp_configurado": cfg.smtp_configurado(db),
        "tipos": tipos_aut.TIPOS,
        "periodos_resumo": tipos_aut.PERIODOS_RESUMO,
        "periodos_erros": tipos_aut.PERIODOS_ERROS,
        "periodos_emails": tipos_aut.PERIODOS_EMAILS,
        "gravidades": tipos_aut.GRAVIDADES,
        "dia_mes_maximo": svc.DIA_MES_MAXIMO})
    return HTMLResponse(html)


# --------------------------------------------------------------------- rotas

@router.get("/automacoes", response_class=HTMLResponse)
def tela(request: Request, db: Session = Depends(get_db)):
    return _lista(db, request)


@router.get("/automacoes/nova", response_class=HTMLResponse)
def nova(request: Request, tipo: str = tipos_aut.COLETA, db: Session = Depends(get_db)):
    if not tipos_aut.valido(tipo):
        tipo = tipos_aut.COLETA
    return _modal(request, db, {"id": "", "tipo": tipo, **_NOVA[tipo], "dia_mes": 1,
                                "escopo": "todas", "empresas": [], "ativo": True,
                                "destinatarios": "", "config": tipos_aut.padrao(tipo)})


@router.get("/automacoes/{automacao_id}/editar", response_class=HTMLResponse)
def editar(automacao_id: str, request: Request, db: Session = Depends(get_db)):
    a = db.get(NfseAutomacao, automacao_id)
    if a is None:
        return _lista(db, request, _toast("Essa automação não existe mais.", False))
    tipo = a.tipo if tipos_aut.valido(a.tipo or "") else tipos_aut.COLETA
    return _modal(request, db, {
        "id": a.id, "tipo": tipo, "nome": a.nome, "frequencia": a.frequencia, "hora": a.hora,
        "dias": svc.dias_de(a.dias_semana), "dia_mes": a.dia_mes or 1, "escopo": a.escopo,
        "empresas": svc.ids_selecionados(a), "ativo": bool(a.ativo),
        "destinatarios": ", ".join(informativo.ler_destinatarios(a.destinatarios or "")[0]),
        "config": tipos_aut.ler_config(a)})


@router.post("/automacoes/salvar", response_class=HTMLResponse)
def salvar(request: Request,
           automacao_id: str = Form(""), tipo: str = Form(""), nome: str = Form(""),
           frequencia: str = Form("diaria"), hora: str = Form(""),
           dias: list[str] = Form([]), dia_mes: str = Form(""), escopo: str = Form("todas"),
           empresas: list[str] = Form([]), ativo: str = Form(""),
           destinatarios: str = Form(""),
           periodo: str = Form(""), ultima_por_competencia: str = Form(""),
           so_com_novidade: str = Form(""), gravidade: str = Form(""),
           incluir_log: str = Form(""), usar_ia: str = Form(""),
           pasta: str = Form(""), destino: str = Form(""), subpastas: str = Form(""),
           assinar: str = Form(""),
           db: Session = Depends(get_db), profile=Depends(require_admin)):
    """Cria ou altera. Erro de preenchimento volta no PRÓPRIO modal, com 200 —
    o htmx não troca o alvo em resposta 4xx, e a pessoa clicaria sem ver nada.
    """
    a = db.get(NfseAutomacao, automacao_id) if automacao_id else None
    # O tipo de uma automação que já existe é o do banco: o formulário não o
    # troca (uma coleta não vira resumo mudando um campo escondido).
    if a is not None:
        tipo = a.tipo if tipos_aut.valido(a.tipo or "") else tipos_aut.COLETA
    elif not tipos_aut.valido(tipo):
        tipo = tipos_aut.COLETA
    coleta = tipo == tipos_aut.COLETA
    if tipo == tipos_aut.ERROS:
        escopo = "todas"          # erro do sistema não é de empresa nenhuma
    if tipo == tipos_aut.EMAILS:
        escopo = "todas"          # a caixa é uma só, do escritório

    dias_int = svc.dias_de(",".join(dias))
    dia_mes_ok = svc.validar_dia_mes(dia_mes)
    validas = {e["id"] for e in _empresas_ativas(db)}
    escolhidas = [e for e in dict.fromkeys(empresas) if e in validas]
    emails, emails_ruins = informativo.ler_destinatarios(destinatarios)
    config, erro_config = tipos_aut.config_do_formulario(tipo, {
        "periodo": periodo, "ultima_por_competencia": ultima_por_competencia,
        "so_com_novidade": so_com_novidade, "gravidade": gravidade,
        "incluir_log": incluir_log, "usar_ia": usar_ia,
        "pasta": pasta, "destino": destino, "subpastas": subpastas, "assinar": assinar})
    dados = {"id": automacao_id, "tipo": tipo, "nome": (nome or "").strip(),
             "frequencia": frequencia, "hora": hora, "dias": dias_int,
             "dia_mes": dia_mes_ok or (dia_mes or "").strip(), "escopo": escopo,
             "empresas": escolhidas, "ativo": bool(ativo),
             "destinatarios": (destinatarios or "").strip(), "config": config}

    hora_ok = svc.validar_hora(hora)
    erro = ""
    if not dados["nome"]:
        erro = "Dê um nome à automação."
    elif frequencia not in tipos_aut.frequencias(tipo):
        erro = ("Escolha se a coleta é diária ou semanal." if coleta
                else "Escolha de quanto em quanto tempo o informativo sai.")
    elif not hora_ok:
        erro = "Informe o horário no formato HH:MM (ex.: 07:00)."
    elif frequencia == "semanal" and not dias_int:
        erro = ("Na coleta semanal, marque ao menos um dia." if coleta
                else "No envio semanal, marque ao menos um dia.")
    elif frequencia == "mensal" and dia_mes_ok is None:
        erro = f"Informe o dia do mês, de 1 a {svc.DIA_MES_MAXIMO}."
    elif escopo not in svc.ESCOPOS:
        erro = ("Escolha quais empresas entram na coleta." if coleta
                else "Escolha quais empresas entram no resumo.")
    elif escopo == "selecionadas" and not escolhidas:
        erro = ("Marque ao menos uma empresa — ou escolha \"todas com certificado\"." if coleta
                else "Marque ao menos uma empresa — ou escolha \"todas as empresas\".")
    elif erro_config:
        erro = erro_config
    elif emails_ruins:
        # Endereço torto não é descartado em silêncio: quem digitou acha que
        # a pessoa vai receber.
        erro = "E-mail inválido: " + ", ".join(emails_ruins[:3])
    elif len(emails) > informativo.MAX_DESTINATARIOS:
        erro = f"No máximo {informativo.MAX_DESTINATARIOS} destinatários por automação."
    elif not emails and tipos_aut.TIPOS[tipo]["email_obrigatorio"]:
        erro = "Informe ao menos um destinatário: o informativo existe para ser enviado."
    if erro:
        resposta = _modal(request, db, dados, erro)
        resposta.headers["HX-Retarget"] = "#modal-container"
        resposta.headers["HX-Reswap"] = "innerHTML"
        return resposta

    novo = a is None
    if novo:
        a = NfseAutomacao(id=str(uuid.uuid4()), tipo=tipo,
                          criado_por=getattr(profile, "name", "") or str(getattr(profile, "id", "")))
        db.add(a)
    a.nome = dados["nome"][:120]
    a.frequencia = frequencia
    a.hora = hora_ok
    a.dias_semana = ",".join(str(d) for d in dias_int) if frequencia == "semanal" else None
    a.dia_mes = dia_mes_ok if frequencia == "mensal" else None
    a.escopo = escopo
    a.empresas = ",".join(escolhidas) if escopo == "selecionadas" else None
    a.config = None if coleta else json.dumps(config, ensure_ascii=False)
    a.destinatarios = ",".join(emails) or None
    a.ativo = bool(ativo)
    # Recalcula sempre: mudar o horário tem de valer já na próxima vez.
    a.proxima_execucao = svc.proxima_da(a, datetime.utcnow()) if a.ativo else None
    db.commit()

    texto = "Automação criada." if novo else "Automação atualizada."
    if not a.ativo:
        texto += " Ela está pausada e não vai rodar sozinha."
    elif emails and not cfg.smtp_configurado(db):
        texto += " Atenção: o servidor de e-mail ainda não está configurado."
    if tipo == tipos_aut.VALIDAR and config.get("assinar"):
        certificado = cfg.situacao_certificado(db)
        if certificado is None or certificado["vencido"]:
            texto += (" Atenção: sem certificado válido em Configurações, a rotina só valida — ela não assina "
                      "até o e-CNPJ ser cadastrado.")
    return _lista(db, request, FECHAR_MODAL + _toast(texto))


@router.post("/automacoes/{automacao_id}/ativo", response_class=HTMLResponse)
def alternar(automacao_id: str, request: Request, db: Session = Depends(get_db),
             profile=Depends(require_admin)):
    a = db.get(NfseAutomacao, automacao_id)
    if a is None:
        return _lista(db, request, _toast("Essa automação não existe mais.", False))
    a.ativo = not a.ativo
    a.proxima_execucao = svc.proxima_da(a, datetime.utcnow()) if a.ativo else None
    db.commit()
    return _lista(db, request, _toast(
        f"\"{a.nome}\" " + ("reativada." if a.ativo else "pausada: não vai rodar sozinha.")))


@router.post("/automacoes/{automacao_id}/excluir", response_class=HTMLResponse)
def excluir(automacao_id: str, request: Request, db: Session = Depends(get_db),
            profile=Depends(require_admin)):
    a = db.get(NfseAutomacao, automacao_id)
    if a is None:
        return _lista(db, request, _toast("Essa automação não existe mais.", False))
    nome = a.nome
    # O histórico fica: é a trilha do que foi coletado, e some da tela só o
    # nome (a linha passa a dizer "automação excluída").
    db.delete(a)
    db.commit()
    return _lista(db, request, _toast(f"\"{nome}\" excluída. O histórico foi mantido."))


@router.post("/automacoes/{automacao_id}/rodar", response_class=HTMLResponse)
def rodar(automacao_id: str, request: Request, db: Session = Depends(get_db),
          profile=Depends(require_admin)):
    if db.get(NfseAutomacao, automacao_id) is None:
        return _lista(db, request, _toast("Essa automação não existe mais.", False))
    ok, texto = svc.rodar_agora(SessionLocal, automacao_id)
    return _lista(db, request, _toast(texto, ok), acompanhar=ok)


@router.get("/automacoes/execucao/{execucao_id}", response_class=HTMLResponse)
def execucao(execucao_id: str, request: Request, db: Session = Depends(get_db)):
    e = db.get(NfseAutomacaoExecucao, execucao_id)
    if e is None:
        return HTMLResponse(_toast("Execução não encontrada.", False))
    try:
        detalhe = json.loads(e.detalhe or "{}")
    except ValueError:
        detalhe = {}
    automacao = db.get(NfseAutomacao, e.automacao_id)
    nome = automacao.nome if automacao else "(automação excluída)"

    tipo = e.tipo or tipos_aut.COLETA
    if tipo == tipos_aut.VALIDAR:
        from ...services.automacoes import validar_sped as rotina_sped
        html = templates.get_template("partials/admin/modals/automacao_execucao_sped.html").render({
            "request": request, "e": e, "nome": nome, "dados": detalhe, "status_rotulo": STATUS_ROTULO,
            "situacoes": rotina_sped.SITUACOES})
        return HTMLResponse(html)
    if tipo in (tipos_aut.RESUMO, tipos_aut.ERROS, tipos_aut.EMAILS):
        modelo = {tipos_aut.RESUMO: "automacao_execucao_resumo.html",
                  tipos_aut.ERROS: "automacao_execucao_erros.html",
                  tipos_aut.EMAILS: "automacao_execucao_emails.html"}[tipo]
        contexto = {"request": request, "e": e, "nome": nome, "dados": detalhe,
                    "status_rotulo": STATUS_ROTULO}
        if tipo == tipos_aut.EMAILS:
            # O texto do modelo é separado AQUI, não no template: quem decide o
            # que é título e o que é item é a mesma função da tela E-mails, e o
            # texto do modelo nunca vira HTML.
            from ...services.email_recebimento import ia as ia_emails
            contexto["linhas"] = ia_emails.linhas_para_tela(detalhe.get("texto", ""))
        html = templates.get_template(f"partials/admin/modals/{modelo}").render(contexto)
        return HTMLResponse(html)

    empresas = detalhe.get("empresas", [])
    # Valor das notas que ENTRARAM nesta rodada — mesma conta da planilha e do
    # e-mail (relatorio_coleta), para os três nunca dizerem números diferentes.
    valores = relatorio_coleta.valores_por_empresa(db, e) if e.fim else {}
    html = templates.get_template("partials/admin/modals/automacao_execucao.html").render({
        "request": request, "e": e,
        "nome": nome,
        "com_notas": [r for r in empresas if r.get("novos") or r.get("atualizados")],
        "falhas": [r for r in empresas if r.get("erro")],
        "sem_novidade": [r for r in empresas if not r.get("erro")
                         and not r.get("novos") and not r.get("atualizados")],
        "certificados": detalhe.get("certificados_vencendo", []),
        "valores": valores,
        "status_rotulo": STATUS_ROTULO,
    })
    return HTMLResponse(html)


def _baixar(conteudo: bytes, nome: str) -> Response:
    return Response(content=conteudo, media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename=\"{nome}\"; filename*=UTF-8''{quote(nome)}",
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/automacoes/execucao/{execucao_id}/planilha")
def planilha(execucao_id: str, db: Session = Depends(get_db)):
    """A planilha da rodada, para baixar.

    Montada de novo a partir do banco, e não lida da pasta da rede: o download
    funciona mesmo com o compartilhamento fora, e sai igual à cópia salva (as
    notas e o detalhe da rodada não mudam depois dela). No resumo das
    auditorias, a fonte é o `detalhe` da rodada — os números que foram no
    e-mail, e não os de hoje.

    Download de verdade: `attachment` + `octet-stream` + `nosniff`, e o nome
    duas vezes (ASCII e UTF-8), como a correção do SPED — ver CLAUDE.md.
    """
    e = db.get(NfseAutomacaoExecucao, execucao_id)
    indisponivel = HTMLResponse("Planilha indisponível para esta execução.", status_code=404)
    if e is None or e.status == "rodando":
        return indisponivel
    automacao = db.get(NfseAutomacao, e.automacao_id)
    nome_automacao = automacao.nome if automacao else "(automação excluída)"

    tipo = e.tipo or tipos_aut.COLETA
    if tipo == tipos_aut.RESUMO:
        try:
            dados = json.loads(e.detalhe or "")
        except ValueError:
            return indisponivel
        if not isinstance(dados, dict) or "auditorias" not in dados:
            return indisponivel
        return _baixar(resumo_auditorias.gerar_planilha(dados, nome_automacao),
                       resumo_auditorias.nome_arquivo(e))
    if tipo != tipos_aut.COLETA:
        return HTMLResponse("Este informativo não tem planilha.", status_code=404)

    rotulo = STATUS_ROTULO.get(e.status, (e.status, ""))[0]
    conteudo = relatorio_coleta.gerar_planilha(db, e, nome_automacao, rotulo)
    return _baixar(conteudo, relatorio_coleta.nome_arquivo(e))
