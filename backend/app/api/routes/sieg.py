import html
import logging
import re
from datetime import date

from fastapi import APIRouter, HTTPException, Response, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

# As credenciais do cofre saem da tela de Configurações (banco), com o
# `.env` como reserva — ver services/configuracoes.py.
from ...services.configuracoes import cliente_sieg
from ...core.authz import (
    get_profile, assert_tenant_access, allowed_tenant_ids, get_task_or_403,
)
from ...infrastructure.database.session import get_db
from ...domain.models.auth_models import Tenant
from ...domain.models.integrations import MissingInvoiceLog

logger = logging.getLogger(__name__)

# Estas rotas fazem proxy do cofre fiscal do SIEG usando a conta do ESCRITÓRIO,
# que enxerga o cofre inteiro. Exigir login (get_profile) só provava que a pessoa
# está cadastrada: qualquer cliente baixava a NF-e de qualquer empresa do país
# informando os 44 dígitos da chave. Cada rota precisa, além do login, amarrar a
# chave a uma auditoria/empresa que o perfil realmente enxerga.
router = APIRouter(tags=["SIEG"], dependencies=[Depends(get_profile)])


def _chave_valida(chave_acesso: str) -> str:
    chave = re.sub(r"\D", "", str(chave_acesso))
    if len(chave) != 44:
        raise HTTPException(status_code=400,
                            detail="Chave de acesso inválida. Deve possuir 44 dígitos.")
    return chave


def _autorizar_chave(db: Session, profile, chave: str, task_id: str | None) -> None:
    """A chave precisa ter aparecido em algo que ESTE perfil enxerga.

    Responde 404 e não 403: um 403 já confirmaria ao atacante que a nota existe
    no cofre — mesma postura de get_task_or_403.
    """
    permitidos = allowed_tenant_ids(profile)
    if permitidos is None:      # admin enxerga tudo
        return
    if not permitidos:
        raise HTTPException(status_code=404, detail="Nota não encontrada.")

    if task_id:
        # get_task_or_403 já garante que a auditoria é de um tenant do perfil.
        task = get_task_or_403(db, profile, task_id)
        if chave in (task.result_data or ""):
            return

    achou = db.query(MissingInvoiceLog.access_key).filter(
        MissingInvoiceLog.access_key == chave,
        MissingInvoiceLog.tenant_id.in_(permitidos),
    ).first()
    if not achou:
        raise HTTPException(status_code=404, detail="Nota não encontrada.")


def _pagina_danfe_indisponivel(chave: str) -> str:
    """Página de erro para abrir em aba nova.

    O motivo devolvido pela API do SIEG (terceiro) NÃO entra aqui: ia cru para o
    HTML e ainda expunha o comportamento interno do cofre. Ele fica no log.
    """
    return f"""
        <!DOCTYPE html>
        <html lang="pt-BR">
        <head>
            <meta charset="UTF-8">
            <title>Synax | Erro ao Gerar DANFE</title>
            <script src="/static/js/vendor/tailwind-3.4.17.js"></script>
        </head>
        <body class="bg-slate-50 flex items-center justify-center min-h-screen p-4 font-sans">
            <div class="max-w-md w-full bg-white rounded-xl shadow-lg border border-slate-200 p-6 text-center">
                <div class="w-12 h-12 rounded-full bg-red-100 text-red-600 flex items-center justify-center mx-auto mb-4 text-xl font-bold">
                    !
                </div>
                <h2 class="text-lg font-bold text-slate-800 mb-2">DANFE Não Disponível no Cofre SIEG</h2>
                <p class="text-sm text-slate-600 mb-6">
                    Não foi possível gerar a DANFE para a chave <strong>{chave}</strong>.
                    A nota pode não estar no cofre fiscal.
                </p>
                <button onclick="window.close()" class="px-4 py-2 rounded-lg bg-[#037fb5] text-white hover:bg-[#026a9e] text-sm font-semibold transition-colors">
                    Fechar Janela
                </button>
            </div>
        </body>
        </html>
        """


@router.get("/api/sieg/danfe/{chave_acesso}")
async def get_sieg_danfe_pdf(
    chave_acesso: str,
    task_id: str | None = None,
    db: Session = Depends(get_db),
    profile = Depends(get_profile),
):
    """DANFE em PDF da chave informada, desde que ela pertença ao alcance do perfil."""
    chave_limpa = _chave_valida(chave_acesso)
    _autorizar_chave(db, profile, chave_limpa, task_id)

    sucesso, pdf_or_err = cliente_sieg(db).gerar_danfe_por_chave(chave_limpa)
    if sucesso and isinstance(pdf_or_err, bytes):
        return Response(
            content=pdf_or_err,
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="DANFE_{chave_limpa}.pdf"'},
        )

    logger.warning("DANFE indisponível chave=%s motivo=%s", chave_limpa, pdf_or_err)
    return HTMLResponse(content=_pagina_danfe_indisponivel(html.escape(chave_limpa)),
                        status_code=404)


@router.get("/api/sieg/xml/{chave_acesso}")
async def get_sieg_xml(
    chave_acesso: str,
    task_id: str | None = None,
    db: Session = Depends(get_db),
    profile = Depends(get_profile),
):
    """Baixa o XML da nota no SIEG em formato texto."""
    chave_limpa = _chave_valida(chave_acesso)
    _autorizar_chave(db, profile, chave_limpa, task_id)

    sucesso, xml_content = cliente_sieg(db).baixar_xml_por_chave(chave_limpa)
    if not sucesso:
        # O motivo do SIEG fica no log: devolvê-lo ao cliente expunha o
        # comportamento do cofre e servia de oráculo sobre notas alheias.
        logger.warning("XML indisponível no SIEG chave=%s motivo=%s", chave_limpa, xml_content)
        raise HTTPException(status_code=404, detail="XML não disponível no cofre fiscal.")

    return Response(
        content=xml_content,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{chave_limpa}.xml"'},
    )


class VerificarNaoLancadasPayload(BaseModel):
    """Schema explícito: a rota recebia `dict` cru, sem tipo nem teto de tamanho."""
    cnpj: str
    data_inicio: date
    data_fim: date
    chaves_sped: list[str] = Field(default_factory=list, max_length=200_000)

    @field_validator("cnpj")
    @classmethod
    def _so_digitos(cls, v: str) -> str:
        limpo = re.sub(r"\D", "", str(v or ""))
        if len(limpo) != 14:
            raise ValueError("CNPJ da empresa inválido. Deve ter 14 dígitos.")
        return limpo


@router.post("/api/sieg/verificar-nao-lancadas")
async def verificar_nao_lancadas_sieg(
    payload: VerificarNaoLancadasPayload,
    db: Session = Depends(get_db),
    profile = Depends(get_profile)
):
    """
    Consulta o lote de XMLs emitidos contra o CNPJ (CnpjDest) no período especificado via API SIEG (Cofre Fiscal),
    compara com a lista de chaves escrituradas no SPED e retorna as notas NÃO LANÇADAS detectadas no cofre.
    """
    cnpj = payload.cnpj
    if payload.data_fim < payload.data_inicio:
        raise HTTPException(status_code=400, detail="Período inválido: data final anterior à inicial.")
    chaves_sped = {re.sub(r"\D", "", str(c)) for c in payload.chaves_sped}

    # O CNPJ precisa pertencer a uma empresa que este usuário pode acessar; caso
    # contrário seria possível varrer o cofre fiscal de qualquer empresa.
    tenant = db.query(Tenant).filter(Tenant.cnpj == cnpj).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa não encontrada.")
    assert_tenant_access(profile, tenant.id)

    from ...services.integrations.sieg_client import SiegIndisponivel

    client = cliente_sieg(db)
    try:
        xmls = client.baixar_lote_xmls(
            cnpj=cnpj,
            xml_type=1,
            data_inicio=payload.data_inicio.isoformat(),
            data_fim=payload.data_fim.isoformat(),
            tipo_cnpj="CnpjDest"
        )
    except SiegIndisponivel as err:
        # Antes de 29/09/2026 a falha virava lista vazia e a resposta era
        # "nenhuma nota não lançada" — o pior resultado possível, dito com
        # confiança. Agora a tela recebe o motivo.
        raise HTTPException(status_code=502, detail=f"O cofre do SIEG não respondeu: {err}")

    nao_lancadas = []
    import defusedxml.ElementTree as ET  # XML do cofre; motivo em audit_engine/xml_parser.py
    for xml_str in xmls:
        try:
            root = ET.fromstring(xml_str)
            inf_nfe = root.find(".//{*}infNFe")
            if inf_nfe is None:
                continue
            inf_id = inf_nfe.attrib.get("Id", "")
            key = re.sub(r"\D", "", inf_id)
            if len(key) != 44:
                continue

            if key not in chaves_sped:
                emit_xnome = ""
                emit_el = root.find(".//{*}emit/{*}xNome")
                if emit_el is not None and emit_el.text:
                    emit_xnome = emit_el.text

                emit_cnpj = ""
                cnpj_el = root.find(".//{*}emit/{*}CNPJ")
                if cnpj_el is not None and cnpj_el.text:
                    emit_cnpj = cnpj_el.text

                vl_total = 0.0
                v_nf_el = root.find(".//{*}total/{*}ICMSTot/{*}vNF")
                if v_nf_el is not None and v_nf_el.text:
                    vl_total = float(v_nf_el.text)

                dh_emi = ""
                dh_el = root.find(".//{*}ide/{*}dhEmi")
                if dh_el is None:
                    dh_el = root.find(".//{*}ide/{*}dEmi")
                if dh_el is not None and dh_el.text:
                    dh_emi = dh_el.text[:10]

                nao_lancadas.append({
                    "key": key,
                    "tipo": "NFE",
                    "emit_name": emit_xnome or "Fornecedor (SIEG)",
                    "emit_cnpj": emit_cnpj,
                    "vl_total": vl_total,
                    "data_emissao": dh_emi,
                    "origem": "SIEG Cofre Fiscal"
                })
        except Exception:
            continue

    return {
        "status": "sucesso",
        "total_sieg_encontradas": len(xmls),
        "nao_lancadas": nao_lancadas
    }
