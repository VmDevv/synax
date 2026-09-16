"""Ajuste de sub-apuração (C195/C197) — a tela.

Sobe um SPED Fiscal já pronto, o sistema insere o par C195+C197 depois de cada
C190 com CFOP 5106 ou 6106 (ver `services/sped_debito_subapuracao.py` para a
regra completa e por que os campos são os que são) e devolve o arquivo ajustado
para importar de volta.

Ferramenta interna (`assert_internal`) e **sem tenant nenhum** — de propósito.
Diferente de `gerar_sped.py` e `correcao_sped.py`, esta não olha para nenhuma
regra cadastrada nem precisa saber de qual empresa é o arquivo: é uma
transformação puramente mecânica sobre o texto do SPED, a mesma para qualquer
cliente. Pedir tenant aqui seria complexidade que a ferramenta não usa.
"""
import logging
import os
import shutil
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse

from ...core.authz import assert_internal, get_profile
from ...core.templating import build_templates
from ...core.uploads_seguros import LIMITE_SPED_BYTES, gravar_com_limite
from ...services.sped_debito_subapuracao import aplicar
from ...services.sped_rewriter.documento import ENCODING

logger = logging.getLogger(__name__)
router = APIRouter()
templates = build_templates()

TEMP_DIR = os.path.join(os.getcwd(), "temp_uploads")
NOME_ARQUIVO = "sped_ajustado.txt"


@router.get("/ajuste-sped", response_class=HTMLResponse)
def tela(request: Request, profile=Depends(get_profile)):
    assert_internal(profile)
    return templates.TemplateResponse(
        request, "pages/ajuste_sped.html",
        {"request": request, "user_profile": profile})


@router.post("/api/ajuste-sped", response_class=HTMLResponse)
async def processar(request: Request, sped_file: UploadFile = File(...),
                    profile=Depends(get_profile)):
    assert_internal(profile)

    processo = str(uuid.uuid4())
    pasta = os.path.join(TEMP_DIR, processo)
    os.makedirs(pasta, exist_ok=True)
    caminho_original = os.path.join(pasta, "sped_original.txt")

    try:
        await gravar_com_limite(sped_file, caminho_original, LIMITE_SPED_BYTES,
                                "O arquivo SPED")
        with open(caminho_original, "r", encoding=ENCODING, newline="") as f:
            texto = f.read()
        resultado = aplicar(texto)
    except HTTPException:
        shutil.rmtree(pasta, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(pasta, ignore_errors=True)
        logger.exception("Falha ao aplicar o ajuste de sub-apuração")
        return templates.TemplateResponse(
            request, "partials/ajuste_sped_resultado.html",
            {"request": request,
             "erro": "Não foi possível processar o arquivo. Confira se é um "
                     "SPED Fiscal (EFD ICMS/IPI) válido e tente de novo."})
    finally:
        # O upload cru não precisa sobreviver; só o arquivo AJUSTADO, se houver.
        try:
            os.remove(caminho_original)
        except OSError:
            pass

    if resultado.documento is None:
        shutil.rmtree(pasta, ignore_errors=True)
        motivo = (resultado.avisos[0] if resultado.avisos else
                  "Nenhum registro C190 com CFOP 5106 ou 6106 foi encontrado "
                  "neste arquivo (ou o ajuste já estava aplicado). Nada foi "
                  "gerado — baixar aqui devolveria uma cópia idêntica ao "
                  "original.")
        return templates.TemplateResponse(
            request, "partials/ajuste_sped_resultado.html",
            {"request": request, "resultado": resultado, "erro": motivo})

    caminho_novo = os.path.join(pasta, NOME_ARQUIVO)
    with open(caminho_novo, "w", encoding=ENCODING, newline="") as f:
        f.write(resultado.documento.serializar())

    logger.info(
        "Ajuste de sub-apuração aplicado processo=%s ajustes=%d ja_existiam=%d "
        "por=%s", processo, len(resultado.aplicados), len(resultado.ja_existiam),
        getattr(profile, "name", "") or getattr(profile, "id", ""))

    return templates.TemplateResponse(
        request, "partials/ajuste_sped_resultado.html",
        {"request": request, "resultado": resultado, "processo": processo,
         "linhas": len(resultado.documento.registros)})


@router.get("/api/ajuste-sped/{processo}/arquivo")
def baixar(processo: str, profile=Depends(get_profile)):
    assert_internal(profile)
    # O identificador é um UUID gerado aqui, não vem de nome de arquivo, mas
    # entra num caminho: conferir o formato é o que impede `..\..\` virar
    # leitura de arquivo do servidor.
    try:
        uuid.UUID(processo)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")

    caminho = os.path.join(TEMP_DIR, processo, NOME_ARQUIVO)
    if not os.path.exists(caminho):
        raise HTTPException(
            status_code=404,
            detail="O arquivo não está mais no servidor. Envie o SPED de novo.")

    with open(caminho, "rb") as f:
        conteudo = f.read()

    from .correcao_sped import _cabecalhos_download
    return StreamingResponse(
        iter([conteudo]), media_type="application/octet-stream",
        headers={**_cabecalhos_download("Sped_Ajustado.txt"),
                 "Content-Length": str(len(conteudo))})
