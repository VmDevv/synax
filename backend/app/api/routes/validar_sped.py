"""Validar SPED — confere a EFD ICMS/IPI do cliente no PVA oficial antes da transmissão.

Desde 23/10/2026 quem valida é o próprio PVA da Receita, sem tela (`services/pva_motor`), e não mais o validador em
Python, que reimplementava as regras e corria atrás delas. As mensagens são as do PVA, e "aprovado" agora pode sair
verde: é o veredito do PVA, não o de uma imitação dele.

Quatro decisões desta rota:

- **A validação é de uma EMPRESA.** O operador escolhe a empresa, que tem de estar entre as dele
  (`resolve_tenant_for_write`), e o CNPJ do 0000 tem de ser o do cadastro — conferido ANTES de o arquivo ir ao PVA.
  Sem essa trava, quem só opera a empresa A validaria o arquivo da B escolhendo a A, e o resultado cairia no
  histórico errado.
- **O clique só enfileira** (`services/validar_sped_fila`): o PVA leva de 7 a 33 s por arquivo e valida um por vez.
  A tela acompanha pelo `GET /api/validar-sped/{processo}`, que o htmx repete enquanto o arquivo está na fila.
- **Motor fora do ar e EFD-Contribuições respondem na hora, com o motivo**, e nada entra na fila. Nunca tela verde
  sem o PVA ter dito.
- **O mesmo endereço responde JSON ou a tela.** Quem chama pela API recebe JSON; o htmx da aba (HX-Request) recebe o
  parcial. A regra de acesso é uma só.
"""
import io
import json
import logging
import os
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import null
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ...core import competencia as comp
from ...core.authz import (allowed_tenant_ids, assert_internal,
                           assert_tenant_access, get_profile,
                           resolve_tenant_for_write)
from ...core.templating import build_templates
from ...core.uploads_seguros import (LIMITE_SPED_BYTES, gravar_com_limite,
                                     nome_seguro)
from ...domain.models.audit import SpedValidacao
from ...domain.models.auth_models import Tenant
from ...infrastructure.database.session import get_db
from ...services import pva_motor
from ...services import validar_sped_fila as fila
from ...services.audit_engine import sped_parser
from ...services.sped_entregas import hash_do_corpo_arquivo

logger = logging.getLogger(__name__)
router = APIRouter()
templates = build_templates()

TEMP_DIR = fila.TEMP_DIR
# Quantas linhas a tela mostra; o resto vai no CSV.
LIMITE_TELA = 500
# Situações em que a validação terminou: a antiga (NULO) e a do PVA que respondeu.
CONCLUIDAS = (None, fila.CONCLUIDA)


def _empresas(db_session: Session, profile) -> list:
    if profile.role == "admin":
        return db_session.query(Tenant).order_by(Tenant.name).all()
    return sorted((t for t in (profile.allowed_tenants or []) if t.ativo),
                  key=lambda t: t.name)


def _identificador(valor: str | None) -> str:
    """CNPJ/CPF só com letras e dígitos — o cadastro guarda com máscara."""
    return re.sub(r"[^0-9A-Za-z]", "", str(valor or "")).upper()


def _quer_html(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _recusar(request: Request, mensagem: str, status: int):
    """Recusa com o motivo. Na tela é 200: o htmx não troca o alvo em 4xx, e o clique ficaria sem resposta."""
    if _quer_html(request):
        return templates.TemplateResponse(
            request, "partials/validar_sped_resultado.html",
            {"request": request, "erro": mensagem})
    return JSONResponse({"erro": mensagem}, status_code=status)


def _uuid_ou_404(processo: str) -> None:
    # Identificador entra num caminho: só UUID, ou `..\..\` vira leitura de arquivo do servidor.
    try:
        uuid.UUID(processo)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Validação não encontrada.")


def _cabecalho(caminho: str) -> dict | None:
    """Os campos do 0000 que a rota usa, na posição do EFD ICMS/IPI. None se a primeira linha não é um 0000."""
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


def _contar_linhas(caminho: str) -> int:
    n = 0
    ultimo = b""
    with open(caminho, "rb") as f:
        while bloco := f.read(1 << 20):
            n += bloco.count(b"\n")
            ultimo = bloco[-1:]
    return n + (1 if ultimo and ultimo != b"\n" else 0)


def br(ddmmaaaa: str) -> str:
    """"31012026" (data do SPED) -> "31/01/2026"."""
    v = ddmmaaaa or ""
    return f"{v[0:2]}/{v[2:4]}/{v[4:8]}" if len(v) == 8 and v.isdigit() else v


def _competencia(dt_ini: str) -> str | None:
    return comp.valida(f"{dt_ini[4:8]}-{dt_ini[2:4]}") if len(dt_ini) == 8 and dt_ini.isdigit() else None


def _dica_admin(profile) -> str:
    if getattr(profile, "role", "") != "admin":
        return ""
    return (f" Para ligar, na pasta do pva-motor: {pva_motor.COMANDO_LIGAR}. Para desligar, sempre POST /encerrar "
            "(matar o processo deixa o MySQL do PVA preso na porta).")


@router.get("/validar-sped", response_class=HTMLResponse)
def tela(request: Request, db_session: Session = Depends(get_db),
         profile=Depends(get_profile)):
    assert_internal(profile)
    return templates.TemplateResponse(request, "pages/validar_sped.html", {
        "request": request, "tenants": _empresas(db_session, profile),
        "user_profile": profile, "competencia": comp.fechada(),
    })


@router.post("/api/validar-sped")
async def validar(request: Request,
                  sped_file: UploadFile = File(...),
                  # Form(""): campo vazio vira 422 antes de entrar na função
                  # com Form(...), e o htmx não troca o alvo em 4xx — o
                  # clique ficaria sem resposta nenhuma na tela.
                  tenant_id: str = Form(""),
                  db_session: Session = Depends(get_db),
                  profile=Depends(get_profile)):
    assert_internal(profile)
    if not tenant_id.strip():
        return _recusar(request, "Escolha a empresa dona do arquivo.", 400)
    tenant_id = resolve_tenant_for_write(profile, tenant_id)
    empresa = db_session.query(Tenant).filter(Tenant.id == tenant_id).first()
    if empresa is None:
        raise HTTPException(status_code=404, detail="Empresa não encontrada.")

    processo = str(uuid.uuid4())
    os.makedirs(fila.pasta(processo), exist_ok=True)
    caminho = os.path.join(fila.pasta(processo), fila.ARQUIVO_SPED)
    try:
        await gravar_com_limite(sped_file, caminho, LIMITE_SPED_BYTES, "O arquivo SPED")
        cab = await run_in_threadpool(_cabecalho, caminho)
        leiaute = await run_in_threadpool(sped_parser.detectar_leiaute, caminho)
    except HTTPException:
        fila.descartar(processo)
        raise
    except OSError:
        fila.descartar(processo)
        logger.exception("Validar SPED: não foi possível ler o arquivo enviado")
        return _recusar(request, "Não foi possível ler o arquivo enviado. Suba de novo.", 500)

    recusa = _recusa_do_arquivo(cab, leiaute, empresa)
    if recusa:
        fila.descartar(processo)
        return _recusar(request, recusa, 422)

    try:
        await run_in_threadpool(pva_motor.saude)
    except pva_motor.MotorIndisponivel as exc:
        fila.descartar(processo)
        return _recusar(request, exc.mensagem + _dica_admin(profile), 503)
    if not fila.cabe():
        fila.descartar(processo)
        return _recusar(request, f"A fila do PVA está cheia ({fila.MAX_FILA} arquivos esperando). Espere alguns "
                                 "terminarem e suba de novo.", 503)

    hash_corpo = await run_in_threadpool(hash_do_corpo_arquivo, caminho)
    linhas = await run_in_threadpool(_contar_linhas, caminho)
    competencia = _competencia(cab["dt_ini"])
    with open(os.path.join(fila.pasta(processo), "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"tenant_id": str(tenant_id), "linhas": linhas, **cab}, f)

    do_arquivo = _identificador(cab["cnpj"] or cab["cpf"])
    try:
        db_session.add(SpedValidacao(
            id=processo, tenant_id=str(tenant_id),
            user_id=str(getattr(profile, "id", "") or ""),
            user_name=getattr(profile, "name", None),
            file_name=nome_seguro(sped_file.filename, "sped.txt"), competencia=competencia,
            cnpj=do_arquivo[:14] or None, cod_fin=(cab["cod_fin"][:1] or None),
            leiaute=(cab["cod_ver"][:3] or None), linhas=linhas,
            # NULOS até o PVA responder: zero aqui seria lido como "sem erro". `null()` e não None: com None o ORM
            # aplica o default (0) da coluna.
            erros=null(), advertencias=null(), nao_conferidas=null(),
            hash_corpo=hash_corpo, motor=fila.MOTOR, situacao=fila.NA_FILA))
        db_session.commit()
    except Exception:                                     # noqa: BLE001
        # Sem a linha não há como a tela acompanhar nem o histórico guardar: melhor não validar do que validar
        # sem trilha (foi um INSERT falhando em silêncio que fez a trilha da correção do SPED se perder em 10/09).
        logger.exception("Não foi possível registrar a validação do SPED")
        db_session.rollback()
        fila.descartar(processo)
        return _recusar(request, "Não foi possível registrar a validação no histórico da empresa, e o arquivo não "
                                 "foi validado. Tente de novo ou avise o administrador.", 500)

    try:
        na_frente = fila.enfileirar(fila.Pedido(processo=processo, tenant_id=str(tenant_id)))
    except fila.FilaCheia as exc:
        db_session.query(SpedValidacao).filter(SpedValidacao.id == processo).delete()
        db_session.commit()
        fila.descartar(processo)
        return _recusar(request, str(exc), 503)

    logger.info("SPED na fila do PVA processo=%s tenant=%s linhas=%d na_frente=%d por=%s", processo, tenant_id,
                linhas, na_frente, getattr(profile, "name", "") or getattr(profile, "id", ""))
    if _quer_html(request):
        return templates.TemplateResponse(request, "partials/validar_sped_resultado.html", {
            "request": request, "fase": fila.NA_FILA, "processo": processo, "na_frente": na_frente,
            "segundos": 0, "cab": cab, "empresa": empresa, "br": br})
    return JSONResponse({"processo": processo, "situacao": fila.NA_FILA, "na_frente": na_frente,
                         "competencia": competencia, "hash_corpo": hash_corpo}, status_code=202)


def _recusa_do_arquivo(cab: dict | None, leiaute: str, empresa) -> str | None:
    if cab is None:
        return ("O arquivo não começa com o registro 0000: não é uma EFD ICMS/IPI. Confira se é o TXT do SPED Fiscal "
                "e se não veio vazio.")
    if leiaute == sped_parser.CONTRIBUICOES:
        return ("Este arquivo é EFD-Contribuições (PIS/COFINS), e o Synax valida no PVA da EFD ICMS/IPI. O PVA da "
                "EFD-Contribuições ainda não está instalado no servidor: valide este arquivo no PVA dela.")
    # Data do 0000 fora do formato (leiaute "") não é recusada aqui: o PVA diz o motivo com a mensagem oficial.
    do_arquivo = _identificador(cab["cnpj"] or cab["cpf"])
    do_cadastro = _identificador(empresa.cnpj)
    if do_cadastro and do_arquivo != do_cadastro:
        return (f"O arquivo é de outra empresa: o 0000 traz {cab['nome'] or 'sem nome'} "
                f"({do_arquivo or 'sem CNPJ/CPF'}), e a empresa escolhida é {empresa.name} ({do_cadastro}).")
    return None


@router.get("/api/validar-sped/motor", response_class=HTMLResponse)
def motor(request: Request, profile=Depends(get_profile)):
    """O motor do PVA está de pé? A aba mostra antes de alguém subir arquivo."""
    assert_internal(profile)
    try:
        saude = pva_motor.saude()
        contexto = {"ligado": True, "versao": saude.get("versaoPva") or ""}
    except pva_motor.MotorIndisponivel as exc:
        contexto = {"ligado": False, "motivo": exc.mensagem + _dica_admin(profile)}
    if not _quer_html(request):
        return JSONResponse(contexto)
    return templates.TemplateResponse(request, "partials/validar_sped_motor.html",
                                      {"request": request, **contexto})


@router.get("/api/validar-sped/painel", response_class=HTMLResponse)
def painel(request: Request, competencia: str = "",
           db_session: Session = Depends(get_db),
           profile=Depends(get_profile)):
    """As empresas do usuário numa competência: validada ou não, e como saiu.

    Coluna a coluna, sem carregar objeto inteiro: é tela de listagem.
    """
    assert_internal(profile)
    competencia = comp.valida(competencia) or comp.fechada()
    empresas = _empresas(db_session, profile)
    permitidas = allowed_tenant_ids(profile)
    ids = [str(t.id) for t in empresas]
    if permitidas is not None:
        ids = [i for i in ids if i in set(permitidas)]
    linhas = []
    if ids:
        linhas = (db_session.query(
                      SpedValidacao.id, SpedValidacao.tenant_id, SpedValidacao.created_at,
                      SpedValidacao.user_name, SpedValidacao.erros,
                      SpedValidacao.advertencias, SpedValidacao.cod_fin,
                      SpedValidacao.motor, SpedValidacao.situacao, SpedValidacao.aprovado)
                  .filter(SpedValidacao.tenant_id.in_(ids),
                          SpedValidacao.competencia == competencia)
                  .order_by(SpedValidacao.created_at.desc())
                  .all())
    andando = fila.em_andamento()
    ultima: dict = {}
    recente: dict = {}
    quantas: dict = {}
    for linha in linhas:
        recente.setdefault(linha.tenant_id, linha)
        if linha.situacao in CONCLUIDAS:
            ultima.setdefault(linha.tenant_id, linha)
            quantas[linha.tenant_id] = quantas.get(linha.tenant_id, 0) + 1
    quadro = []
    for t in empresas:
        if str(t.id) not in ids:
            continue
        r = recente.get(str(t.id))
        # A tentativa mais nova que ainda não terminou (ou não terminou nunca) aparece ao lado da última concluída.
        if r is None or r.situacao in CONCLUIDAS:
            andamento = None
        elif r.situacao in fila.PENDENTES and r.id in andando:
            andamento = r.situacao
        else:
            andamento = "nao_concluida"
        quadro.append({"empresa": t, "ultima": ultima.get(str(t.id)), "andamento": andamento,
                       "quantas": quantas.get(str(t.id), 0)})
    return templates.TemplateResponse(
        request, "partials/validar_sped_painel.html",
        {"request": request, "quadro": quadro, "competencia": competencia,
         "rotulo": comp.rotulo(competencia),
         "validadas": sum(1 for q in quadro if q["ultima"])})


def _fase(linha) -> tuple[str, dict | None, bool]:
    """(fase, o que a fila sabe, registrada). A linha diz a situação; a memória da fila corrige dois casos: o
    pedido sumiu (servidor reiniciado = interrompida) e o fim que não chegou ao banco."""
    if linha.motor != fila.MOTOR:
        return "antiga", None, True
    mem = fila.estado(linha.id)
    if linha.situacao not in fila.PENDENTES:
        return linha.situacao or fila.CONCLUIDA, mem, True
    if mem is None:
        return fila.INTERROMPIDA, None, True
    if mem["situacao"] in fila.PENDENTES:
        return mem["situacao"], mem, True
    return mem["situacao"], mem, bool(mem.get("registrada"))


def _anteriores(db_session: Session, linha) -> list:
    """As validações concluídas antes desta, da mesma empresa e competência, mais recente primeiro: quem valida
    fica sabendo que alguém já validou, e se foi o mesmo arquivo (mesmo hash do corpo)."""
    if not linha.competencia:
        return []
    try:
        anteriores = (db_session.query(SpedValidacao.created_at, SpedValidacao.user_name,
                                       SpedValidacao.erros, SpedValidacao.hash_corpo,
                                       SpedValidacao.cod_fin, SpedValidacao.situacao,
                                       SpedValidacao.motor, SpedValidacao.aprovado)
                      .filter(SpedValidacao.tenant_id == linha.tenant_id,
                              SpedValidacao.competencia == linha.competencia,
                              SpedValidacao.id != linha.id,
                              SpedValidacao.created_at <= linha.created_at)
                      .order_by(SpedValidacao.created_at.desc())
                      .limit(20).all())
    except Exception:                                     # noqa: BLE001
        logger.warning("Não foi possível ler as validações anteriores", exc_info=True)
        db_session.rollback()
        return []
    return [a for a in anteriores if a.situacao in CONCLUIDAS][:10]


def _meta(processo: str) -> dict:
    try:
        with open(os.path.join(fila.pasta(processo), "meta.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


@router.get("/api/validar-sped/{processo}", response_class=HTMLResponse)
def andamento(request: Request, processo: str,
              db_session: Session = Depends(get_db),
              profile=Depends(get_profile)):
    """Onde está a validação: na fila, no PVA, ou o resultado. O htmx da aba repete enquanto não terminou."""
    assert_internal(profile)
    _uuid_ou_404(processo)
    linha = db_session.get(SpedValidacao, processo)
    if linha is None:
        raise HTTPException(status_code=404, detail="Validação não encontrada.")
    # A validação é da empresa: quem não opera ela não vê, mesmo com o link.
    assert_tenant_access(profile, linha.tenant_id)

    fase, mem, registrada = _fase(linha)
    empresa = db_session.query(Tenant).filter(Tenant.id == linha.tenant_id).first()
    resultado = fila.ler_resultado(processo) if fase == fila.CONCLUIDA else None
    segundos = 0
    if fase == fila.VALIDANDO and mem and mem.get("desde"):
        segundos = int((datetime.utcnow() - mem["desde"]).total_seconds())
    falha = linha.falha or (mem or {}).get("falha")
    final = fase not in fila.PENDENTES

    if not _quer_html(request):
        corpo = {"processo": processo, "situacao": fase, "registrada": registrada,
                 "na_frente": (mem or {}).get("na_frente"), "falha": falha,
                 "resultado": resultado.como_dict() if resultado else None}
        if fase == fila.CONCLUIDA and resultado is None:
            corpo.update(aprovado=linha.aprovado, erros=linha.erros, advertencias=linha.advertencias)
        return JSONResponse(corpo)

    contexto = {
        "request": request, "fase": fase, "processo": processo, "linha": linha, "r": resultado,
        "registrada": registrada, "falha": falha, "na_frente": (mem or {}).get("na_frente", 0),
        "segundos": segundos, "cab": _meta(processo), "empresa": empresa, "br": br,
        "limite_tela": LIMITE_TELA, "admin": getattr(profile, "role", "") == "admin",
        "sem_cnpj_no_cadastro": empresa is not None and not _identificador(empresa.cnpj),
        "anteriores": _anteriores(db_session, linha) if final else [],
    }
    resposta = templates.TemplateResponse(request, "partials/validar_sped_resultado.html", contexto)
    if final:
        # O painel do mês escuta este evento: a empresa muda de situação sem recarregar a página.
        resposta.headers["HX-Trigger"] = "validarSpedConcluida"
    return resposta


@router.get("/api/validar-sped/{processo}/csv")
def baixar_csv(processo: str, antigo: int = 0, profile=Depends(get_profile)):
    assert_internal(profile)
    _uuid_ou_404(processo)
    # O relatório do validador antigo só existe para a comparação do período em paralelo: é do admin.
    if antigo and getattr(profile, "role", "") != "admin":
        raise HTTPException(status_code=403, detail="Só o administrador baixa o relatório do validador antigo.")
    meta = _meta(processo)
    try:
        with open(os.path.join(fila.pasta(processo), "relatorio_antigo.csv" if antigo else "relatorio.csv"),
                  "rb") as f:
            conteudo = f.read()
    except OSError:
        conteudo = None
    if not meta or conteudo is None:
        raise HTTPException(status_code=404,
                            detail="O relatório não está mais no servidor. "
                                   "Valide o arquivo de novo.")
    # O relatório é da empresa: quem não opera ela não baixa, mesmo com o link.
    assert_tenant_access(profile, meta.get("tenant_id"))

    from .correcao_sped import _cabecalhos_download
    dt_ini = meta.get("dt_ini", "")
    sufixo = f"_{dt_ini[4:8]}-{dt_ini[2:4]}" if len(dt_ini) == 8 else ""
    nome = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", meta.get("nome") or "Empresa")
    nome = re.sub(r"\s+", " ", nome).strip(" .")[:80] or "Empresa"
    prefixo = "Validador_antigo" if antigo else "Validacao_PVA"
    return StreamingResponse(
        io.BytesIO(conteudo), media_type="application/octet-stream",
        headers={**_cabecalhos_download(f"{prefixo}_{nome}{sufixo}.csv"),
                 "Content-Length": str(len(conteudo))})
