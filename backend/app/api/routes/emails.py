"""Tela "E-mails": o que chegou na caixa do escritório e o que virou XML pronto.

É ONDE OS AVISOS DOS E-MAILS MORAM (decisão de 28/09/2026). Três públicos:

- **Todo interno (admin e operador)** vê os XMLs recebidos das empresas que
  opera, por competência, com o caminho para auditar ou gerar o SPED. O mesmo
  aviso aparece nas telas de Auditoria e Gerar SPED quando a empresa é
  escolhida (`/emails/recebidos`), que é onde ele é útil.
- **Só o admin** vê o resumo da manhã, a lista de e-mails e os anexos não
  aproveitados: a caixa é a geral do escritório, e remetente e assunto de
  e-mail de outras empresas (ou de funcionário) não são para o operador.
- **Por e-mail**, o resumo da manhã sai pela automação "Resumo dos e-mails"
  (Administração → Automações), para quem não abre o sistema cedo.

Listagem nunca abre `detalhe` de todo mundo (mesma armadilha do
`result_data`): o JSON só é lido nos e-mails que precisam de atenção, com teto.
"""
import html
import json
import threading
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ...core.authz import allowed_tenant_ids, assert_internal, assert_tenant_access, get_profile
from ...core.fuso import hora_br
from ...core.templating import build_templates
from ...domain.models.auth_models import Tenant
from ...domain.models.automacao import NfseAutomacaoExecucao
from ...domain.models.email_recebido import EmailMensagem, EmailRemetente
from ...infrastructure.database.session import SessionLocal, get_db
from ...services import configuracoes as cfg
from ...services.email_recebimento import ia, leitura, recebidos, remetentes
from ...services.email_recebimento.resumo import ROTULO_SITUACAO
from ...services.sped_builder import rotulo_mes

router = APIRouter(tags=["E-mails"])
templates = build_templates()

DIAS_ATENCAO = 7


def _estado_leitura(db: Session) -> dict:
    config = cfg.config_imap(db)
    estado = leitura.ler_estado(db)
    ultima = leitura._data(estado.get("ultima_leitura"))
    proxima = leitura.proxima(db)
    return {"configurado": cfg.imap_configurado(db), "ligado": config["ligado"],
            "pasta": config["pasta"], "status": estado.get("status"),
            "caixas": [c["rotulo"] for c in cfg.caixas_imap(db)],
            "mensagem": estado.get("mensagem") or "",
            "ultima": hora_br(ultima) if ultima else "",
            "proxima": hora_br(proxima) if (proxima and config["ligado"]) else "",
            "em_andamento": leitura.em_andamento()}


def _ultimo_resumo(db: Session) -> dict | None:
    e = (db.query(NfseAutomacaoExecucao)
         .filter(NfseAutomacaoExecucao.tipo == "resumo_emails",
                 NfseAutomacaoExecucao.status.in_(("ok", "sem_novidade")))
         .order_by(NfseAutomacaoExecucao.inicio.desc()).first())
    if e is None:
        return None
    try:
        dados = json.loads(e.detalhe or "{}")
    except ValueError:
        return None
    return {"quando": hora_br(e.inicio), "periodo": dados.get("periodo", ""),
            "linhas": ia.linhas_para_tela(dados.get("texto", "")),
            "texto_ia": bool(dados.get("texto_ia")), "motivo": dados.get("motivo_sem_ia", ""),
            "totais": dados.get("totais") or {}}


def _atencao(db: Session) -> list[dict]:
    limite = datetime.utcnow() - timedelta(days=DIAS_ATENCAO)
    linhas = (db.query(EmailMensagem)
              .filter(EmailMensagem.created_at >= limite,
                      (EmailMensagem.xmls_recusados > 0)
                      | EmailMensagem.situacao.in_(("grande", "erro")))
              .order_by(EmailMensagem.created_at.desc()).limit(50).all())
    saida = []
    for m in linhas:
        try:
            detalhe = json.loads(m.detalhe or "{}")
        except ValueError:
            detalhe = {}
        motivos = [r for r in (detalhe.get("recusas") or [])][:20]
        if m.situacao in ("grande", "erro") and not motivos:
            motivos = [{"arquivo": "", "motivo": ROTULO_SITUACAO[m.situacao]}]
        saida.append({"quando": hora_br(m.recebido_em or m.created_at),
                      "de": m.remetente_nome or m.remetente or "(sem remetente)",
                      "endereco": m.remetente or "", "assunto": m.assunto or "(sem assunto)",
                      "aceitos": m.xmls_aceitos, "motivos": motivos,
                      "mais": max(0, m.xmls_recusados - len(motivos))})
    return saida


def _recentes(db: Session) -> list[dict]:
    """Os últimos e-mails, cada um com o que a memória sabe do remetente —
    é daqui que se silencia com um clique."""
    linhas = (db.query(EmailMensagem.recebido_em, EmailMensagem.created_at,
                       EmailMensagem.remetente, EmailMensagem.remetente_nome,
                       EmailMensagem.assunto, EmailMensagem.situacao,
                       EmailMensagem.xmls_aceitos, EmailMensagem.xmls_repetidos,
                       EmailMensagem.xmls_recusados, EmailMensagem.automatico,
                       EmailMensagem.pasta)
              .filter(EmailMensagem.situacao != "proprio")
              .order_by(EmailMensagem.created_at.desc()).limit(40).all())
    regras = remetentes.carregar(db)
    nomes = {str(t.id): t.name for t in db.query(Tenant.id, Tenant.name).all()}
    saida = []
    for m in linhas:
        regra = remetentes.regra_para(m.remetente, regras)
        dominio = remetentes.dominio_de(m.remetente or "")
        saida.append({**m._asdict(), "regra": regra,
                      "silenciado": bool(regra is not None and regra.silenciar),
                      "empresa": (nomes.get(regra.tenant_id, "")
                                  if (regra is not None and regra.tenant_id) else ""),
                      "dominio_silenciavel": bool(dominio)
                      and dominio not in remetentes.DOMINIOS_PESSOAIS})
    return saida


def _recebidos(db: Session, profile) -> list[dict]:
    """A tabela por empresa e mês — só o que chegou por E-MAIL. O que o SIEG
    baixou aparece no SPED de cada um; contado aqui também, cada XML ficava em
    dois lugares da tela (14/10/2026)."""
    permitidos = allowed_tenant_ids(profile)
    grupos = recebidos.por_empresa(db, permitidos, so_email=True)
    nomes = {str(t.id): t.name for t in db.query(Tenant.id, Tenant.name).all()}
    for g in grupos:
        g["empresa"] = nomes.get(str(g["tenant_id"]), "(empresa removida)")
        g["mes"] = rotulo_mes(g["competencia"]) if g["competencia"] else "sem data"
        g["ultimo_br"] = hora_br(g["ultimo"]) if g["ultimo"] else ""
    return grupos


@router.get("/emails", response_class=HTMLResponse)
def tela(request: Request, db: Session = Depends(get_db), profile=Depends(get_profile)):
    assert_internal(profile)
    admin = profile.role == "admin"
    permitidos = allowed_tenant_ids(profile)
    nomes = {str(t.id): t.name for t in db.query(Tenant.id, Tenant.name).all()}
    lista = recebidos.speds_recebidos(db, permitidos)
    contagem = recebidos.xmls_dos_speds(db, lista)
    speds = [{"s": s, "empresa": nomes.get(s.tenant_id, "(empresa removida)"),
              "guardados": contagem[s.id][0], "notas": contagem[s.id][1],
              "andando": s.sieg_status in recebidos.SIEG_ANDANDO}
             for s in lista]
    contexto = {"request": request, "user_profile": profile, "admin": admin,
                "recebidos": _recebidos(db, profile), "rotulo_situacao": ROTULO_SITUACAO,
                "speds": speds, "rotulo_mes": rotulo_mes,
                "speds_sem_xml": sum(1 for x in speds
                                     if not x["andando"] and x["guardados"] == 0),
                "dias_retencao": leitura.armazenamento.DIAS_RETENCAO}
    if admin:
        contexto.update(leitura_estado=_estado_leitura(db), resumo=_ultimo_resumo(db),
                        atencao=_atencao(db), recentes=_recentes(db),
                        dias_atencao=DIAS_ATENCAO, **_contexto_remetentes(db))
    return templates.TemplateResponse(request, "pages/emails.html", contexto)


# ------------------------------------------------- memória de remetentes
# Só admin: silenciar é decidir o que o escritório deixa de ler no resumo, e a
# nota vai para a IA como informação confiável da equipe.

def _contexto_remetentes(db: Session, edicao: dict | None = None, mensagem: str = "",
                         sucesso: bool = True) -> dict:
    empresas = (db.query(Tenant.id, Tenant.name).filter(Tenant.ativo.is_(True))
                .order_by(Tenant.name).all())
    nomes = {str(t.id): t.name for t in db.query(Tenant.id, Tenant.name).all()}
    regras = [{"r": r, "empresa": nomes.get(r.tenant_id or "", "")}
              for r in db.query(EmailRemetente).order_by(EmailRemetente.silenciar.desc(),
                                                          EmailRemetente.padrao).all()]
    return {"remetentes": regras,
            "empresas_ativas": [{"id": str(t.id), "nome": t.name} for t in empresas],
            "edicao": edicao or {}, "mensagem_rem": mensagem, "sucesso_rem": sucesso}


def _secao_remetentes(request: Request, db: Session, **extra) -> HTMLResponse:
    corpo = templates.get_template("partials/emails/remetentes.html").render(
        {"request": request, **_contexto_remetentes(db, **extra)})
    return HTMLResponse(corpo)


def _so_admin(profile) -> HTMLResponse | None:
    if profile.role != "admin":
        return HTMLResponse("Só o admin mexe na memória de remetentes.", status_code=403)
    return None


def _autor(profile) -> str:
    return getattr(profile, "name", "") or str(getattr(profile, "id", ""))


@router.get("/emails/remetentes/editar", response_class=HTMLResponse)
def editar_remetente(request: Request, padrao: str = "", db: Session = Depends(get_db),
                     profile=Depends(get_profile)):
    negado = _so_admin(profile)
    if negado is not None:
        return negado
    r = db.query(EmailRemetente).filter(EmailRemetente.padrao == padrao.strip().lower()).first()
    edicao = ({"padrao": r.padrao, "rotulo": r.rotulo or "", "nota": r.nota or "",
               "tenant_id": r.tenant_id or "", "silenciar": r.silenciar} if r
              else {"padrao": padrao})
    return _secao_remetentes(request, db, edicao=edicao)


@router.post("/emails/remetentes/salvar", response_class=HTMLResponse)
def salvar_remetente(request: Request, padrao: str = Form(""), rotulo: str = Form(""),
                     nota: str = Form(""), tenant_id: str = Form(""),
                     silenciar: str = Form(""), db: Session = Depends(get_db),
                     profile=Depends(get_profile)):
    """Cria ou altera, pela chave `padrao`. O que uma pessoa grava vira
    `origem = manual` — e o aprendizado automático não passa mais por cima."""
    negado = _so_admin(profile)
    if negado is not None:
        return negado
    edicao = {"padrao": padrao, "rotulo": rotulo, "nota": nota, "tenant_id": tenant_id,
              "silenciar": bool(silenciar)}
    try:
        chave = remetentes.normalizar(padrao)
    except remetentes.PadraoInvalido as err:
        return _secao_remetentes(request, db, edicao=edicao, mensagem=str(err), sucesso=False)
    validas = {str(t.id) for t in db.query(Tenant.id).all()}
    if tenant_id and tenant_id not in validas:
        return _secao_remetentes(request, db, edicao=edicao, sucesso=False,
                                 mensagem="Essa empresa não existe no cadastro.")
    if tenant_id and chave.startswith("@"):
        return _secao_remetentes(request, db, edicao=edicao, sucesso=False, mensagem=(
            "Empresa só se liga a um ENDEREÇO: um domínio inteiro pode ter gente de "
            "várias empresas."))
    r = db.query(EmailRemetente).filter(EmailRemetente.padrao == chave).first()
    novo = r is None
    if novo:
        r = EmailRemetente(id=str(uuid.uuid4()), padrao=chave)
        db.add(r)
    r.rotulo = (rotulo or "").strip()[:120] or None
    r.nota = (nota or "").strip()[:2000] or None
    r.tenant_id = tenant_id or None
    r.varias_empresas = False
    r.silenciar = bool(silenciar)
    r.origem = "manual"
    r.atualizado_por = _autor(profile)
    db.commit()
    texto = (f"{chave} " + ("cadastrado" if novo else "atualizado")
             + (" e silenciado: não vai mais para o resumo da IA." if r.silenciar else "."))
    return _secao_remetentes(request, db, mensagem=texto)


@router.post("/emails/remetentes/remover", response_class=HTMLResponse)
def remover_remetente(request: Request, padrao: str = Form(""),
                      db: Session = Depends(get_db), profile=Depends(get_profile)):
    negado = _so_admin(profile)
    if negado is not None:
        return negado
    r = db.query(EmailRemetente).filter(EmailRemetente.padrao == padrao.strip().lower()).first()
    if r is None:
        return _secao_remetentes(request, db, sucesso=False,
                                 mensagem="Esse remetente não está mais na memória.")
    chave = r.padrao
    db.delete(r)
    db.commit()
    return _secao_remetentes(request, db, mensagem=f"{chave} removido da memória.")


@router.post("/emails/remetentes/silenciar", response_class=HTMLResponse)
def silenciar_remetente(request: Request, endereco: str = Form(""),
                        alcance: str = Form("endereco"), db: Session = Depends(get_db),
                        profile=Depends(get_profile)):
    """O clique na lista de e-mails. Mantém nota e empresa que já existirem;
    a página recarrega para a lista inteira refletir a regra nova."""
    negado = _so_admin(profile)
    if negado is not None:
        return negado
    alvo = ("@" + remetentes.dominio_de(endereco)) if alcance == "dominio" else endereco
    try:
        chave = remetentes.normalizar(alvo)
    except remetentes.PadraoInvalido as err:
        return HTMLResponse(
            '<div id="toast-container" hx-swap-oob="beforeend"><div class="alert alert-error '
            'shadow-lg font-medium text-sm">' + html.escape(str(err)) + '</div></div>')
    r = db.query(EmailRemetente).filter(EmailRemetente.padrao == chave).first()
    if r is None:
        r = EmailRemetente(id=str(uuid.uuid4()), padrao=chave)
        db.add(r)
    r.silenciar = True
    r.origem = "manual"
    r.atualizado_por = _autor(profile)
    db.commit()
    resposta = HTMLResponse("")
    resposta.headers["HX-Refresh"] = "true"
    return resposta


@router.post("/emails/ler-agora", response_class=HTMLResponse)
def ler_agora(request: Request, db: Session = Depends(get_db), profile=Depends(get_profile)):
    """Dispara a leitura em segundo plano e devolve o cartão de estado, que se
    atualiza sozinho enquanto a leitura anda."""
    if profile.role != "admin":
        return HTMLResponse("Só o admin lê a caixa do escritório.", status_code=403)
    if not cfg.imap_configurado(db):
        mensagem, ok = ("A leitura ainda não está configurada (Administração → "
                        "Configurações → Caixa de entrada)."), False
    elif leitura.em_andamento():
        mensagem, ok = "Já há uma leitura em andamento.", False
    else:
        threading.Thread(target=leitura.ler, args=(SessionLocal, "manual"),
                         name="leitura-caixa-manual", daemon=True).start()
        mensagem, ok = "Leitura iniciada. O resultado aparece aqui em instantes.", True
    return _cartao(request, db, mensagem, ok, acompanhar=ok)


@router.get("/emails/estado", response_class=HTMLResponse)
def estado(request: Request, db: Session = Depends(get_db), profile=Depends(get_profile)):
    if profile.role != "admin":
        return HTMLResponse("", status_code=403)
    return _cartao(request, db)


def _cartao(request: Request, db: Session, mensagem: str = "", ok: bool = True,
            acompanhar: bool = False) -> HTMLResponse:
    estado_ = _estado_leitura(db)
    html = templates.get_template("partials/emails/estado_leitura.html").render({
        "request": request, "leitura_estado": estado_, "aviso": mensagem, "aviso_ok": ok,
        "acompanhar": acompanhar or estado_["em_andamento"]})
    return HTMLResponse(html)


@router.get("/emails/recebidos", response_class=HTMLResponse)
def recebidos_da_empresa(request: Request, tenant_id: str = "", destino: str = "auditoria",
                         db: Session = Depends(get_db), profile=Depends(get_profile)):
    """O aviso das telas de Auditoria e Gerar SPED: "chegaram XMLs desta
    empresa por e-mail". Vazio quando não chegou nada — a tela não ganha caixa
    dizendo que não há nada."""
    assert_internal(profile)
    if not tenant_id:
        return HTMLResponse("")
    tenant_id = assert_tenant_access(profile, tenant_id)
    meses = recebidos.por_competencia(db, tenant_id)
    if not meses:
        return HTMLResponse("")
    for m in meses:
        m["mes"] = rotulo_mes(m["competencia"]) if m["competencia"] else "sem data"
        m["ultimo_br"] = hora_br(m["ultimo"]) if m["ultimo"] else ""
    html = templates.get_template("partials/emails/recebidos_empresa.html").render({
        "request": request, "meses": meses, "destino": destino,
        "total": sum(m["total"] for m in meses)})
    return HTMLResponse(html)


# --------------------------------------------- caixa de novidades da tela inicial
# Pedido do Sandro em 29/09/2026: o que chegou por e-mail aparece na tela da
# Auditoria, e dali se audita com um clique. Qualquer interno vê — só as
# empresas que opera, e SEM remetente nem assunto (a caixa é do escritório).

def _item_sped(request: Request, x: dict) -> HTMLResponse:
    corpo = templates.get_template("partials/emails/_item_sped.html").render(
        {"request": request, "x": x, "rotulo_mes": rotulo_mes})
    return HTMLResponse(corpo)


def _sped_permitido(db: Session, profile, sped_id: str):
    """O SPED, se o perfil opera a empresa dele. None se não existe ou não é dele —
    a rota responde igual nos dois casos, para não confirmar que o id existe."""
    from ...domain.models.email_recebido import EmailSped
    sped = db.query(EmailSped).filter(EmailSped.id == str(sped_id)).first()
    if sped is None:
        return None
    permitidos = allowed_tenant_ids(profile)
    if permitidos is not None and sped.tenant_id not in permitidos:
        return None
    return sped


@router.get("/emails/caixa-inicio", response_class=HTMLResponse)
def caixa_inicio(request: Request, db: Session = Depends(get_db), profile=Depends(get_profile)):
    assert_internal(profile)
    dados = recebidos.novidades(db, allowed_tenant_ids(profile))
    xmls = [g for g in dados["xmls"] if not g["tem_sped"]]
    if not dados["speds"] and not xmls:
        return HTMLResponse("")        # nada chegou: a tela não ganha caixa vazia
    for g in xmls:
        g["mes"] = rotulo_mes(g["competencia"]) if g["competencia"] else "sem data"
    corpo = templates.get_template("partials/emails/caixa_inicio.html").render(
        {"request": request, "speds": dados["speds"], "xmls": xmls, "rotulo_mes": rotulo_mes})
    return HTMLResponse(corpo)


@router.get("/emails/sped/{sped_id}", response_class=HTMLResponse)
def item_sped(sped_id: str, request: Request, db: Session = Depends(get_db),
              profile=Depends(get_profile)):
    """Um item da caixa — é o que ele busca de 10 em 10 s enquanto o SIEG baixa."""
    assert_internal(profile)
    sped = _sped_permitido(db, profile, sped_id)
    if sped is None:
        return HTMLResponse("")
    empresa = db.query(Tenant.name).filter(Tenant.id == _uuid_ou(sped.tenant_id)).first()
    return _item_sped(request, {"s": sped, "empresa": empresa[0] if empresa else ""})


@router.post("/emails/sped/{sped_id}/tentar-de-novo", response_class=HTMLResponse)
def tentar_de_novo(sped_id: str, request: Request, db: Session = Depends(get_db),
                   profile=Depends(get_profile)):
    """Volta o SPED para a fila. As notas "não encontradas" são perguntadas de
    novo: o cofre pode ter recebido depois."""
    from ...services.email_recebimento import sieg_download
    assert_internal(profile)
    sped = _sped_permitido(db, profile, sped_id)
    if sped is None:
        return HTMLResponse("")
    if sped.sieg_status in ("parcial", "falhou"):
        sped.sieg_status, sped.progresso, sped.sieg_mensagem = "pendente", None, None
        sped.nao_encontrados = 0
        db.commit()
        sieg_download.iniciar_se_preciso(SessionLocal)
    elif sped.sieg_status == "cancelado":
        # Retomar: o progresso (meses já baixados por lote, notas que o cofre
        # não tem) fica, e o que já está guardado não é baixado de novo.
        sped.sieg_status, sped.sieg_mensagem = "pendente", None
        db.commit()
        sieg_download.iniciar_se_preciso(SessionLocal)
    empresa = db.query(Tenant.name).filter(Tenant.id == _uuid_ou(sped.tenant_id)).first()
    return _item_sped(request, {"s": sped, "empresa": empresa[0] if empresa else ""})


@router.post("/emails/sped/{sped_id}/cancelar", response_class=HTMLResponse)
def cancelar_download(sped_id: str, request: Request, db: Session = Depends(get_db),
                      profile=Depends(get_profile)):
    """Para o download no SIEG. A thread confere o banco antes de cada chamada
    ao cofre, então para na próxima — o que já foi baixado fica guardado e
    serve para auditar."""
    assert_internal(profile)
    sped = _sped_permitido(db, profile, sped_id)
    if sped is None:
        return HTMLResponse("")
    if sped.sieg_status in ("pendente", "baixando"):
        from ...core.fuso import hora_local
        prontos = (sped.ja_tinha or 0) + (sped.baixados or 0)
        quem = getattr(profile, "name", "") or "alguém"
        sped.sieg_status = "cancelado"
        sped.sieg_mensagem = (f"Cancelado por {quem} às {hora_local(datetime.utcnow()):%H:%M}. "
                              f"{prontos} XML(s) já baixados ficam guardados.")[:500]
        sped.sieg_fim = datetime.utcnow()
        db.commit()
    empresa = db.query(Tenant.name).filter(Tenant.id == _uuid_ou(sped.tenant_id)).first()
    return _item_sped(request, {"s": sped, "empresa": empresa[0] if empresa else ""})


def _recarregar() -> HTMLResponse:
    resposta = HTMLResponse("")
    resposta.headers["HX-Refresh"] = "true"
    return resposta


@router.post("/emails/sped/{sped_id}/excluir", response_class=HTMLResponse)
def excluir_sped(sped_id: str, db: Session = Depends(get_db), profile=Depends(get_profile)):
    """Tira o SPED recebido (e os XMLs que o SIEG baixou para ele). Se o
    download estiver andando, ele para na próxima rodada: a fila confere o
    banco, e a linha que sumiu vale como cancelamento."""
    assert_internal(profile)
    sped = _sped_permitido(db, profile, sped_id)
    if sped is not None:
        recebidos.excluir_sped(db, sped)
    return _recarregar()


@router.post("/emails/speds/excluir-sem-xml", response_class=HTMLResponse)
def excluir_speds_sem_xml(db: Session = Depends(get_db), profile=Depends(get_profile)):
    """Tira de uma vez os SPEDs que não têm nenhum XML guardado (pedido do
    Sandro em 14/10/2026: depois da limpeza dos XMLs, 70 SPEDs sobraram vazios
    e excluir um por um não fazia sentido). A lista é refeita aqui, no clique —
    não vem da tela —, e SPED com download do SIEG andando não entra."""
    assert_internal(profile)
    for sped in recebidos.speds_sem_xml(db, allowed_tenant_ids(profile)):
        recebidos.excluir_sped(db, sped, commit=False)
    db.commit()
    return _recarregar()


@router.post("/emails/xmls/excluir", response_class=HTMLResponse)
def excluir_xmls(tenant_id: str = Form(""), competencia: str = Form(""),
                 db: Session = Depends(get_db), profile=Depends(get_profile)):
    """Tira os XMLs que chegaram por e-mail de uma empresa num mês — os que a
    linha da tela conta. Os baixados do SIEG saem com o SPED deles."""
    assert_internal(profile)
    if not tenant_id:
        return _recarregar()
    tenant_id = assert_tenant_access(profile, tenant_id)
    recebidos.excluir_xmls(db, tenant_id, competencia.strip() or None, so_email=True)
    return _recarregar()


@router.get("/emails/zip")
def baixar_zip(sped: str = "", tenant_id: str = "", competencia: str = "",
               db: Session = Depends(get_db), profile=Depends(get_profile)):
    """O pacote para levar a outro lugar (o Domínio, um colega): XMLs e, se for
    de um SPED, o próprio SPED. Download de verdade — ver CLAUDE.md: anexo,
    octet-stream, nosniff e o nome duas vezes."""
    import os
    import tempfile
    import zipfile
    from urllib.parse import quote

    from fastapi import HTTPException
    from fastapi.responses import StreamingResponse

    assert_internal(profile)
    registro = None
    if sped:
        registro = _sped_permitido(db, profile, sped)
        if registro is None:
            raise HTTPException(status_code=404, detail="Pacote não encontrado.")
        tenant_id, competencia = registro.tenant_id, registro.competencia or ""
    else:
        tenant_id = assert_tenant_access(profile, tenant_id)
    arquivos = recebidos.arquivos_para_zip(db, tenant_id, registro, competencia or None,
                                           so_email=registro is None)
    if not arquivos:
        raise HTTPException(status_code=404, detail="Nenhum arquivo guardado para este pacote.")
    empresa = db.query(Tenant.name).filter(Tenant.id == _uuid_ou(tenant_id)).first()
    base = _nome_de_arquivo(empresa[0] if empresa else "empresa")
    nome = f"{base}_{competencia or 'sem-data'}_xmls.zip"

    temporario = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    with zipfile.ZipFile(temporario, "w", zipfile.ZIP_DEFLATED) as pacote:
        for origem, destino in arquivos:
            pacote.write(origem, destino)
    temporario.seek(0)

    def _blocos():
        try:
            while bloco := temporario.read(1024 * 1024):
                yield bloco
        finally:
            temporario.close()

    ascii_ = nome.encode("ascii", "ignore").decode() or "xmls.zip"
    return StreamingResponse(_blocos(), media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_}\"; filename*=UTF-8''{quote(nome)}",
        "X-Content-Type-Options": "nosniff"})


def _nome_de_arquivo(texto: str) -> str:
    import re
    import unicodedata
    limpo = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", limpo).strip("_")[:40] or "empresa"


def _uuid_ou(valor):
    try:
        return uuid.UUID(str(valor))
    except ValueError:
        return valor
