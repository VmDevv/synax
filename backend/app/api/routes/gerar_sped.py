"""Gerar SPED a partir dos XMLs — a tela.

Sobe o ZIP do mês, sai o arquivo no leiaute da EFD para importar no Domínio.
É a ferramenta para o cliente que NÃO escritura: sem ela, alguém digita nota
por nota.

Duas decisões desta rota:

- **O arquivo não é devolvido direto no POST.** Primeiro vem a tela de
  resultado, com o que foi lido, o que ficou de fora do pacote e tudo que o
  gerador precisou presumir; o download é o passo seguinte. Entregar o arquivo
  antes de a pessoa ler o que foi presumido é o mesmo erro de gerar SPED
  corrigido sem prévia.
- **O CNPJ vem do cadastro da empresa, nunca do ZIP.** É ele que separa entrada
  de saída e denuncia XML de outro cliente no meio do pacote. Deixar o gerador
  "descobrir" a empresa pelo arquivo mais frequente escrituraria o pacote
  errado sem ninguém perceber.
"""
import logging
import os
import shutil
import uuid
import zipfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from ...core.authz import (assert_internal, get_profile,
                           resolve_tenant_for_write)
from ...core.templating import build_templates
from ...core.uploads_seguros import (LIMITE_ZIP_BYTES, extrair_xmls_do_pacote,
                                     gravar_com_limite)
from ...domain.models.auth_models import Tenant
from ...domain.models.rules import CfopClassification
from ...infrastructure.database.session import get_db
from ...services.sped_builder import Cadastro, gerar_de_pasta
from ...services.sped_rewriter.documento import ENCODING

logger = logging.getLogger(__name__)
router = APIRouter()
templates = build_templates()

TEMP_DIR = os.path.join(os.getcwd(), "temp_uploads")
NOME_ARQUIVO = "sped_gerado.txt"


def _empresas(db_session: Session, profile) -> list:
    if profile.role == "admin":
        return db_session.query(Tenant).order_by(Tenant.name).all()
    return sorted((t for t in (profile.allowed_tenants or []) if t.ativo),
                  key=lambda t: t.name)


def _regras_de_cfop(db_session: Session, tenant_id: str, cnpj: str) -> dict:
    """{(fornecedor, CFOP do fornecedor, produto): CFOP de entrada} aprovadas.

    Só as APROVADAS. Regra `pendente_revisao` é sugestão que ninguém revisou —
    entrar aqui seria escriturar por conta de um palpite que o maker-checker
    ainda não confirmou.
    """
    so_digitos = "".join(c for c in str(cnpj or "") if c.isdigit())
    try:
        linhas = (db_session.query(
                      CfopClassification.supplier_cnpj,
                      CfopClassification.supplier_cfop,
                      CfopClassification.product_code,
                      CfopClassification.entry_cfop)
                  .filter(CfopClassification.tenant_id == str(tenant_id),
                          CfopClassification.company_cnpj == so_digitos,
                          CfopClassification.status == "aprovado")
                  .all())
    except Exception:                                     # noqa: BLE001
        logger.warning("Não foi possível carregar as regras de CFOP", exc_info=True)
        return {}
    return {(f, c, p): e for f, c, p, e in linhas if e}


@router.get("/gerar-sped", response_class=HTMLResponse)
def tela(request: Request, db_session: Session = Depends(get_db),
         profile=Depends(get_profile)):
    assert_internal(profile)
    return templates.TemplateResponse(request, "pages/gerar_sped.html", {
        "request": request, "tenants": _empresas(db_session, profile),
        "user_profile": profile,
    })


@router.post("/api/gerar-sped", response_class=HTMLResponse)
async def gerar(request: Request,
                xmls_zip: UploadFile = File(...),
                tenant_id: str = Form(...),
                perfil: str = Form("A"),
                ind_ativ: str = Form("1"),
                db_session: Session = Depends(get_db),
                profile=Depends(get_profile)):
    assert_internal(profile)
    tenant_id = resolve_tenant_for_write(profile, tenant_id)
    empresa = db_session.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not empresa or not empresa.cnpj:
        return templates.TemplateResponse(
            request, "partials/gerar_sped_resultado.html",
            {"request": request,
             "erro": "A empresa selecionada não tem CNPJ no cadastro, e é ele "
                     "que separa as notas de entrada dela das demais do pacote."})

    processo = str(uuid.uuid4())
    pasta = os.path.join(TEMP_DIR, processo)
    xml_dir = os.path.join(pasta, "xmls")
    os.makedirs(xml_dir, exist_ok=True)
    pacote = os.path.join(pasta, "xmls.pacote")
    try:
        await gravar_com_limite(xmls_zip, pacote, LIMITE_ZIP_BYTES,
                                "O pacote de XMLs")
        try:
            extrair_xmls_do_pacote(pacote, xml_dir)
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400,
                                detail="O arquivo enviado não é um ZIP válido.")
        finally:
            try:
                os.remove(pacote)
            except OSError:
                pass

        cadastro = Cadastro(cnpj=empresa.cnpj, nome=empresa.name,
                            uf=empresa.uf or "", perfil=perfil,
                            ind_ativ=ind_ativ)
        resultado = gerar_de_pasta(
            xml_dir, cadastro,
            _regras_de_cfop(db_session, tenant_id, empresa.cnpj))
    except HTTPException:
        shutil.rmtree(pasta, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(pasta, ignore_errors=True)
        logger.exception("Falha ao gerar SPED a partir dos XMLs tenant=%s",
                         tenant_id)
        return templates.TemplateResponse(
            request, "partials/gerar_sped_resultado.html",
            {"request": request,
             "erro": "Não foi possível gerar o arquivo. Confira se o ZIP tem "
                     "XMLs de NF-e e tente de novo."})
    finally:
        # Os XMLs são o volume; o que precisa sobreviver é o TXT gerado.
        shutil.rmtree(xml_dir, ignore_errors=True)

    if resultado.documento is None:
        shutil.rmtree(pasta, ignore_errors=True)
        return templates.TemplateResponse(
            request, "partials/gerar_sped_resultado.html",
            {"request": request, "resultado": resultado,
             "erro": resultado.avisos[0] if resultado.avisos
                     else "Nada a gerar com este pacote."})

    caminho = os.path.join(pasta, NOME_ARQUIVO)
    with open(caminho, "w", encoding=ENCODING, newline="") as f:
        f.write(resultado.documento.serializar())

    logger.info("SPED gerado dos XMLs tenant=%s processo=%s notas=%d itens=%d "
                "fretes=%d", tenant_id, processo, resultado.notas,
                resultado.itens, resultado.fretes)

    return templates.TemplateResponse(
        request, "partials/gerar_sped_resultado.html",
        {"request": request, "resultado": resultado, "processo": processo,
         "empresa": empresa,
         "linhas": len(resultado.documento.registros)})


@router.get("/api/gerar-sped/{processo}/arquivo")
def baixar(processo: str, profile=Depends(get_profile)):
    assert_internal(profile)
    # O identificador é um UUID gerado aqui e não vem de nome de arquivo, mas
    # ele entra num caminho: conferir o formato é o que impede `..\..\` virar
    # leitura de arquivo do servidor.
    try:
        uuid.UUID(processo)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")

    caminho = os.path.join(TEMP_DIR, processo, NOME_ARQUIVO)
    if not os.path.exists(caminho):
        raise HTTPException(
            status_code=404,
            detail="O arquivo gerado não está mais no servidor. Gere de novo.")

    with open(caminho, "rb") as f:
        conteudo = f.read()

    from .correcao_sped import _cabecalhos_download
    return StreamingResponse(
        iter([conteudo]), media_type="application/octet-stream",
        headers={**_cabecalhos_download("Sped_Gerado.txt"),
                 "Content-Length": str(len(conteudo))})
