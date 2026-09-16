"""Tela de auditoria de notas em PDF por IA (admin).

Todas as rotas daqui exigem admin — o router inteiro é montado com
`Depends(require_admin)` em main.py, e cada rota valida de novo, porque
depender só do include deixa a trava a um refactor de distância.

O fluxo é AUTÔNOMO: o usuário manda o PDF e pronto. Não há empresa a escolher,
porque a IA lê prestador e tomador do próprio documento e o CFOP sai da
comparação entre as duas UFs. A versão anterior exigia selecionar o tomador
para pegar `Tenant.uf` — e as 170 empresas estavam sem UF cadastrada, então a
tela não deixava fazer nada.

Não há estado de servidor entre a análise e o download: o resultado da IA volta
para a tela e é reenviado no POST do download. Guardar em memória de processo
quebraria com mais de um worker do Uvicorn, e criar tabela para um arquivo que
o usuário baixa em seguida é peso sem dono.
"""
import json
import logging
import os
import tempfile
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from ...core.templating import build_templates
from ...domain.models.auth_models import UserProfile
from ...infrastructure.database.session import get_db
from ..dependencies.auth import get_current_user
from ...services.nfse import pdf_ia_auditor as auditor
from ...services.nfse import pdf_ia_lancamento as lancamento

logger = logging.getLogger(__name__)

router = APIRouter()
templates = build_templates()

MAX_UPLOAD_BYTES = auditor.MAX_PDF_BYTES


def _admin(current_user, db: Session) -> UserProfile:
    if not current_user:
        raise HTTPException(status_code=401, detail="Autenticação necessária.")
    perfil = db.query(UserProfile).filter(UserProfile.id == current_user.id).first()
    if not perfil or perfil.role != "admin":
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores.")
    return perfil


def _aviso(mensagem: str, tom: str = "amber") -> HTMLResponse:
    """Resposta 200 com o motivo visível.

    200 e não 4xx de propósito: o htmx não troca o alvo em resposta de erro, e o
    usuário clicaria sem nada acontecer na tela.
    """
    cores = {"amber": ("amber", "triangle-exclamation"), "red": ("red", "circle-xmark")}
    cor, icone = cores.get(tom, cores["amber"])
    return HTMLResponse(
        f'<div class="rounded-lg border border-{cor}-200 bg-{cor}-50 p-4 text-{cor}-800 text-sm">'
        f'<i class="fa-solid fa-{icone} mr-2"></i>{mensagem}</div>'
    )


@router.get("/auditoria-pdf", response_class=HTMLResponse)
def pagina(request: Request, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    perfil = _admin(current_user, db)
    return templates.TemplateResponse(request, "pages/auditoria_pdf.html", {
        "request": request,
        "user_email": current_user.email,
        "current_user": current_user,
        "user_profile": perfil,
    })


@router.post("/api/auditoria-pdf/analisar", response_class=HTMLResponse)
async def analisar(
    request: Request,
    arquivo: UploadFile = File(...),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin(current_user, db)

    caminho = os.path.join(tempfile.gettempdir(), f"pdf_ia_{datetime.now():%Y%m%d%H%M%S%f}.pdf")
    try:
        from ...core.uploads_seguros import gravar_com_limite
        await gravar_com_limite(arquivo, caminho, MAX_UPLOAD_BYTES, "PDF")
        with open(caminho, "rb") as f:
            pdf_bytes = f.read()
    except HTTPException as e:
        return _aviso(str(e.detail))
    except Exception:
        logger.exception("Falha ao receber o PDF")
        return _aviso("Não foi possível ler o arquivo enviado.", tom="red")
    finally:
        if os.path.exists(caminho):
            try:
                os.remove(caminho)
            except OSError:
                pass

    try:
        resultado = auditor.auditar_pdf(pdf_bytes)
    except auditor.ErroAuditoriaPdf as e:
        return _aviso(str(e), tom="red")
    except Exception:
        logger.exception("Falha inesperada na auditoria de PDF por IA")
        return _aviso("Falha inesperada ao analisar o PDF. O erro foi registrado.", tom="red")

    preparadas = lancamento.preparar_notas(resultado["notas_validadas"])

    # O que a tela mostra por linha: os dois lados da operação e o CFOP que saiu
    # deles, para conferir antes de gerar o arquivo que vai para o Domínio.
    linhas = []
    for nota in preparadas:
        f = nota["_fornecedor"]
        linhas.append({
            "num_nota": nota.get("num_nota", ""),
            "data_emissao": nota.get("data_emissao", ""),
            "cnpj": f["cnpj"],
            "fornecedor": f["razao"],
            "uf_prestador": f["uf"],
            "cnpj_tomador": nota.get("cnpj_tomador", ""),
            "uf_tomador": nota["_uf_tomador"],
            "valor": nota.get("valor", 0.0),
            "retencoes": lancamento.total_retencoes(nota),
            "cfop": nota["_cfop"],
            "interna": nota["_cfop"] == lancamento.CFOP_INTERNO,
            "presumido": nota["_presumido"],
            "aviso": lancamento.motivo_presuncao(nota),
        })

    return templates.TemplateResponse(request, "partials/pdf_ia/resultado.html", {
        "request": request,
        "linhas": linhas,
        "divergencias": resultado["divergencias"],
        "resumo": lancamento.resumo(resultado["notas_validadas"]),
        "payload": json.dumps(resultado, ensure_ascii=False),
    })


def _resultado_do_form(payload: str) -> dict:
    try:
        dados = json.loads(payload or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Resultado da análise inválido.")
    return {
        "notas_validadas": dados.get("notas_validadas") or [],
        "divergencias": dados.get("divergencias") or [],
    }


def _nome_arquivo(extensao: str) -> str:
    return f"notas de servico {datetime.now():%Y-%m-%d}.{extensao}"


@router.post("/api/auditoria-pdf/txt")
def baixar_txt(
    payload: str = Form(""),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin(current_user, db)
    resultado = _resultado_do_form(payload)
    conteudo = lancamento.gerar_txt_importacao(resultado["notas_validadas"])
    return Response(
        content=conteudo.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{_nome_arquivo("txt")}"'},
    )


@router.post("/api/auditoria-pdf/excel")
def baixar_excel(
    payload: str = Form(""),
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _admin(current_user, db)
    resultado = _resultado_do_form(payload)
    buffer = lancamento.gerar_excel_auditoria(
        resultado["notas_validadas"], resultado["divergencias"])
    return Response(
        content=buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{_nome_arquivo("xlsx")}"'},
    )
