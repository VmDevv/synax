"""Validar SPED — confere a EFD ICMS IPI do cliente antes da transmissão.

A aba sobe o arquivo, roda `services/sped_validador` (leiaute e regras
genéricas do PVA, sem a tela do PVA) e mostra as inconsistências com linha,
registro, campo e regra. Nada é gravado no arquivo nem no banco: validar não
altera o SPED, e o relatório fica só o tempo de baixar o CSV.

Três decisões desta rota:

- **A validação é de uma EMPRESA.** O operador escolhe a empresa, que tem de
  estar entre as dele (`resolve_tenant_for_write`), e o CNPJ do 0000 tem de
  ser o do cadastro. Sem essa trava, quem só opera a empresa A validaria o
  arquivo da B escolhendo a A no seletor — e, quando o resultado passar a ser
  guardado por empresa, ele cairia na pasta errada.
- **O mesmo POST responde JSON ou a tela.** Quem chama pela API recebe o
  resultado inteiro em JSON; o htmx da aba (cabeçalho HX-Request) recebe o
  parcial HTML. A regra de acesso é uma só, então não há caminho de API com
  menos trava que a tela.
- **"Nenhuma inconsistência" nunca sai como aprovado.** A v1 confere leiaute
  e regras genéricas; as regras específicas do PVA ainda não. A tela diz
  quantas aplicações de regra ficaram de fora, para ninguém transmitir
  achando que o PVA já foi.
"""
import csv
import io
import json
import logging
import os
import re
import shutil
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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
from ...services.sped_entregas import hash_do_corpo_arquivo
from ...services.sped_validador import ArquivoNaoValidavel, validar_arquivo
from ...services.sped_validador.campos import br

logger = logging.getLogger(__name__)
router = APIRouter()
templates = build_templates()

TEMP_DIR = os.path.join(os.getcwd(), "temp_uploads")
# Teto da LISTA guardada (a contagem segue inteira). Arquivo com a
# hierarquia quebrada gera uma inconsistência por linha; acima disso a lista
# não ajuda ninguém e o CSV vira dezenas de MB.
LIMITE_LISTA = 20_000
# Quantas linhas a tela mostra; o resto vai no CSV.
LIMITE_TELA = 500
COLUNAS_CSV = ("linha", "registro", "campo", "valor", "regra", "gravidade",
               "mensagem")


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


def _responder(request, contexto: dict, status: int = 200):
    if _quer_html(request):
        return templates.TemplateResponse(
            request, "partials/validar_sped_resultado.html",
            {"request": request, **contexto}, status_code=status)
    if "erro" in contexto:
        return JSONResponse({"erro": contexto["erro"]}, status_code=status)
    extras = {k: contexto.get(k) for k in
              ("processo", "competencia", "hash_corpo", "registrada")}
    return JSONResponse(contexto["resultado"].como_dict() | extras,
                        status_code=status)


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
        return _responder(request, {"erro": "Escolha a empresa dona do arquivo."},
                          status=200 if _quer_html(request) else 400)
    tenant_id = resolve_tenant_for_write(profile, tenant_id)
    empresa = db_session.query(Tenant).filter(Tenant.id == tenant_id).first()
    if empresa is None:
        raise HTTPException(status_code=404, detail="Empresa não encontrada.")

    processo = str(uuid.uuid4())
    pasta = os.path.join(TEMP_DIR, processo)
    os.makedirs(pasta, exist_ok=True)
    caminho = os.path.join(pasta, "sped_validar.txt")
    try:
        await gravar_com_limite(sped_file, caminho, LIMITE_SPED_BYTES,
                                "O arquivo SPED")
        resultado = await run_in_threadpool(validar_arquivo, caminho, LIMITE_LISTA)
        hash_corpo = await run_in_threadpool(hash_do_corpo_arquivo, caminho)
    except HTTPException:
        shutil.rmtree(pasta, ignore_errors=True)
        raise
    except ArquivoNaoValidavel as exc:
        shutil.rmtree(pasta, ignore_errors=True)
        return _responder(request, {"erro": exc.mensagem},
                          status=200 if _quer_html(request) else 422)
    except Exception:                                     # noqa: BLE001
        shutil.rmtree(pasta, ignore_errors=True)
        logger.exception("Falha ao validar o SPED")
        return _responder(request, {"erro": "Não foi possível ler o arquivo. "
                                            "Confira se é uma EFD ICMS IPI."},
                          status=200 if _quer_html(request) else 500)
    finally:
        # O SPED do cliente não fica no servidor: só o relatório, se houver.
        try:
            os.remove(caminho)
        except OSError:
            pass

    do_arquivo = _identificador(resultado.empresa.get("cnpj")
                                or resultado.empresa.get("cpf"))
    do_cadastro = _identificador(empresa.cnpj)
    if do_cadastro and do_arquivo != do_cadastro:
        shutil.rmtree(pasta, ignore_errors=True)
        return _responder(request, {"erro": (
            f"O arquivo é de outra empresa: o 0000 traz "
            f"{resultado.empresa.get('nome') or 'sem nome'} "
            f"({do_arquivo or 'sem CNPJ/CPF'}), e a empresa escolhida é "
            f"{empresa.name} ({do_cadastro}).")},
            status=200 if _quer_html(request) else 422)

    with open(os.path.join(pasta, "relatorio.csv"), "w", encoding="utf-8-sig",
              newline="") as f:
        escritor = csv.writer(f, delimiter=";")
        escritor.writerow(COLUNAS_CSV)
        for i in resultado.inconsistencias:
            escritor.writerow([i.linha or "", i.registro, i.campo, i.valor,
                               i.regra, i.gravidade, i.mensagem])
    with open(os.path.join(pasta, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"tenant_id": str(tenant_id),
                   "nome": resultado.empresa.get("nome", ""),
                   "dt_fin": resultado.empresa.get("dt_fin", "")}, f)

    logger.info("SPED validado processo=%s tenant=%s linhas=%d erros=%d adv=%d "
                "por=%s", processo, tenant_id, resultado.linhas,
                resultado.erros, resultado.advertencias,
                getattr(profile, "name", "") or getattr(profile, "id", ""))

    competencia = _competencia(resultado)
    anteriores = _validacoes_anteriores(db_session, tenant_id, competencia)
    registrada = _registrar(db_session, processo, tenant_id, profile,
                            nome_seguro(sped_file.filename, "sped.txt"),
                            competencia, resultado, hash_corpo)

    return _responder(request, {
        "resultado": resultado, "processo": processo, "empresa": empresa,
        "sem_cnpj_no_cadastro": not do_cadastro,
        "limite_tela": LIMITE_TELA, "br": br,
        "competencia": competencia, "hash_corpo": hash_corpo,
        "anteriores": anteriores, "registrada": registrada,
    })


def _competencia(resultado) -> str | None:
    dt_ini = resultado.empresa.get("dt_ini") or ""
    return comp.valida(f"{dt_ini[:4]}-{dt_ini[4:6]}") if len(dt_ini) == 8 else None


def _validacoes_anteriores(db_session: Session, tenant_id: str,
                           competencia: str | None) -> list:
    """As validações já feitas desta empresa nesta competência, mais recente
    primeiro: a segunda pessoa a validar fica sabendo que alguém já validou, e
    se foi o mesmo arquivo (mesmo hash do corpo)."""
    if not competencia:
        return []
    try:
        return (db_session.query(SpedValidacao.created_at, SpedValidacao.user_name,
                                 SpedValidacao.erros, SpedValidacao.hash_corpo,
                                 SpedValidacao.cod_fin)
                .filter(SpedValidacao.tenant_id == str(tenant_id),
                        SpedValidacao.competencia == competencia)
                .order_by(SpedValidacao.created_at.desc())
                .limit(10).all())
    except Exception:                                     # noqa: BLE001
        logger.warning("Não foi possível ler as validações anteriores",
                       exc_info=True)
        db_session.rollback()
        return []


def _registrar(db_session: Session, processo: str, tenant_id: str, profile,
               nome_arquivo: str, competencia: str | None, resultado,
               hash_corpo: str) -> bool:
    """Grava a validação. Se falhar, a tela avisa que não ficou registrada:
    foi um INSERT falhando em silêncio que fez a trilha da correção do SPED
    se perder em 10/09/2026."""
    do_arquivo = _identificador(resultado.empresa.get("cnpj")
                                or resultado.empresa.get("cpf"))
    try:
        db_session.add(SpedValidacao(
            id=processo, tenant_id=str(tenant_id),
            user_id=str(getattr(profile, "id", "") or ""),
            user_name=getattr(profile, "name", None),
            file_name=nome_arquivo, competencia=competencia,
            cnpj=do_arquivo[:14] or None,
            cod_fin=(resultado.empresa.get("cod_fin") or None),
            leiaute=resultado.leiaute, linhas=resultado.linhas,
            erros=resultado.erros, advertencias=resultado.advertencias,
            nao_conferidas=sum(resultado.nao_conferidas.values()),
            hash_corpo=hash_corpo,
            versao_pva=(resultado.pva or {}).get("versao") or None))
        db_session.commit()
        return True
    except Exception:                                     # noqa: BLE001
        logger.exception("Não foi possível registrar a validação do SPED")
        db_session.rollback()
        return False


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
                      SpedValidacao.tenant_id, SpedValidacao.created_at,
                      SpedValidacao.user_name, SpedValidacao.erros,
                      SpedValidacao.advertencias, SpedValidacao.cod_fin)
                  .filter(SpedValidacao.tenant_id.in_(ids),
                          SpedValidacao.competencia == competencia)
                  .order_by(SpedValidacao.created_at.desc())
                  .all())
    ultima: dict = {}
    quantas: dict = {}
    for linha in linhas:
        ultima.setdefault(linha.tenant_id, linha)
        quantas[linha.tenant_id] = quantas.get(linha.tenant_id, 0) + 1
    quadro = [{"empresa": t, "ultima": ultima.get(str(t.id)),
               "quantas": quantas.get(str(t.id), 0)}
              for t in empresas if str(t.id) in ids]
    return templates.TemplateResponse(
        request, "partials/validar_sped_painel.html",
        {"request": request, "quadro": quadro, "competencia": competencia,
         "rotulo": comp.rotulo(competencia),
         "validadas": sum(1 for q in quadro if q["ultima"])})


@router.get("/api/validar-sped/{processo}/csv")
def baixar_csv(processo: str, profile=Depends(get_profile)):
    assert_internal(profile)
    # Identificador entra num caminho: só UUID, ou `..\..\` vira leitura de
    # arquivo do servidor.
    try:
        uuid.UUID(processo)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Relatório não encontrado.")
    pasta = os.path.join(TEMP_DIR, processo)
    try:
        with open(os.path.join(pasta, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        with open(os.path.join(pasta, "relatorio.csv"), "rb") as f:
            conteudo = f.read()
    except (OSError, ValueError):
        raise HTTPException(status_code=404,
                            detail="O relatório não está mais no servidor. "
                                   "Valide o arquivo de novo.")
    # O relatório é da empresa: quem não opera ela não baixa, mesmo com o link.
    assert_tenant_access(profile, meta.get("tenant_id"))

    from .correcao_sped import _cabecalhos_download
    dt_fin = meta.get("dt_fin", "")
    sufixo = f"_{dt_fin[0:4]}-{dt_fin[4:6]}" if len(dt_fin) == 8 else ""
    nome = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", meta.get("nome") or "Empresa")
    nome = re.sub(r"\s+", " ", nome).strip(" .")[:80] or "Empresa"
    return StreamingResponse(
        io.BytesIO(conteudo), media_type="application/octet-stream",
        headers={**_cabecalhos_download(f"Validacao_SPED_{nome}{sufixo}.csv"),
                 "Content-Length": str(len(conteudo))})
