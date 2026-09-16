"""Módulo de Notas Fiscais de Serviço (NFS-e).

Independente do motor de auditoria: rotas, tabela e templates próprios. Nenhum
endpoint ou lógica existente é alterado.
"""
import io
import os
import re
import time
import math
import uuid
import json
import zipfile
import logging
import tempfile
from datetime import datetime

from fastapi import APIRouter, Depends, Request, Form, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...core.templating import build_templates
from ...core.authz import get_profile, allowed_tenant_ids, assert_tenant_access
from ...core.rate_limit import limitar
from ...infrastructure.database.session import get_db
from ...domain.models.auth_models import Tenant
from ...domain.models.nfse import NfseDocument, NfseCursor
from ...services.nfse.abrasf_parser import parse_nfse_xml
from ...services.nfse import storage, adn_client, certificados

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/nfse", tags=["NFS-e"])
templates = build_templates()

# Teto de relógio para UMA busca em lote. A requisição precisa terminar enquanto
# o operador ainda está olhando; o que sobrar continua na próxima busca, porque
# o cursor de NSU de cada empresa fica gravado.
ORCAMENTO_LOTE_SEG = float(os.environ.get("NFSE_LOTE_ORCAMENTO_SEG", "240"))

# Tetos da importação. Uma NFS-e real tem alguns KB; estes limites são folgados
# para o uso legítimo e fecham a porta para arquivo inflado de propósito.
LIMITE_ARQUIVO_BYTES = 8 * 1024 * 1024      # 8 MB por XML
LIMITE_TOTAL_BYTES = 200 * 1024 * 1024      # 200 MB por requisição
LIMITE_ARQUIVOS = 5000                      # notas por requisição
LIMITE_ZIP_NOTAS = 20000                    # notas por ZIP gerado para download

# Colunas do relatório ABRASF, na ordem da planilha do escritório.
COLUNAS = [
    ("numero", "Número"), ("dt_emissao", "Emissão"), ("dt_competencia", "Competência"),
    ("prestador", "Prestador"), ("rz_prestador", "Razão Social Prestador"),
    ("uf_prest", "UF"), ("mun_prest", "Município"), ("insc_prest", "Insc. Municipal"),
    ("tomador", "Tomador"), ("rz_tomador", "Razão Social Tomador"),
    ("uf_toma", "UF Tomador"), ("cod_mun_toma", "Cód. Mun. Tomador"),
    ("optante_sn", "Optante SN"), ("cod_servico", "Cód. Serviço"), ("cnae", "CNAE"),
    ("valor_servico", "Valor Serviço"), ("ir", "IR"), ("iss", "ISS"),
    ("iss_retido", "ISS Retido"), ("csll", "CSLL"), ("deducoes", "Deduções"),
    ("pis", "PIS"), ("cofins", "COFINS"), ("inss", "INSS"),
    ("desconto_incondic", "Desc. Incond."), ("desconto_condic", "Desc. Cond."),
    ("out_retencoes", "Outras Retenções"), ("aliquota", "Alíquota"),
    ("base_calculo", "Base de Cálculo"), ("valor_liquido", "Valor Líquido"),
    ("dt_cancelamento", "Dt. Cancelamento"), ("motivo_cancel", "Motivo Cancel."),
    ("numero_nota_cancel", "Nota Substituta"), ("status", "Status"),
]

CAMPOS_MONETARIOS = {
    "valor_servico", "ir", "iss", "csll", "deducoes", "pis", "cofins", "inss",
    "desconto_incondic", "desconto_condic", "out_retencoes", "base_calculo",
    "valor_liquido",
}


def _empresas_do_perfil(db: Session, profile):
    """Empresas que este usuário pode consultar.

    `permitidos is None` é exclusivo do admin (ver allowed_tenant_ids) — só ele
    continua vendo empresa inativada aqui; para operador/cliente ela some.
    """
    permitidos = allowed_tenant_ids(profile)
    q = db.query(Tenant).order_by(Tenant.name)
    if permitidos is not None:
        if not permitidos:
            return []
        q = q.filter(Tenant.id.in_([uuid.UUID(t) for t in permitidos]), Tenant.ativo == True)  # noqa: E712
    return q.all()


def _consultar(db: Session, profile, tenant_id, q_prestador="", q_tomador="",
               q_status="", competencia="", q_texto="", page=1, per_page=50):
    """Monta a consulta já restrita às empresas do perfil."""
    query = db.query(NfseDocument)

    if tenant_id:
        assert_tenant_access(profile, tenant_id)
        query = query.filter(NfseDocument.tenant_id == str(tenant_id))
    else:
        permitidos = allowed_tenant_ids(profile)
        if permitidos is not None:
            if not permitidos:
                return [], 0, 1, 1
            query = query.filter(NfseDocument.tenant_id.in_(permitidos))

    if q_prestador:
        alvo = f"%{q_prestador}%"
        query = query.filter(or_(NfseDocument.prestador.ilike(alvo),
                                 NfseDocument.rz_prestador.ilike(alvo)))
    if q_tomador:
        alvo = f"%{q_tomador}%"
        query = query.filter(or_(NfseDocument.tomador.ilike(alvo),
                                 NfseDocument.rz_tomador.ilike(alvo)))
    if q_status == "canceladas":
        query = query.filter(NfseDocument.dt_cancelamento.isnot(None))
    elif q_status == "normais":
        query = query.filter(NfseDocument.dt_cancelamento.is_(None))
    if competencia:
        try:
            ano, mes = competencia.split("-")
            ini = datetime(int(ano), int(mes), 1)
            fim = datetime(int(ano) + (1 if mes == "12" else 0),
                           1 if mes == "12" else int(mes) + 1, 1)
            query = query.filter(NfseDocument.dt_competencia >= ini,
                                 NfseDocument.dt_competencia < fim)
        except (ValueError, IndexError):
            pass
    if q_texto:
        alvo = f"%{q_texto}%"
        query = query.filter(or_(NfseDocument.numero.ilike(alvo),
                                 NfseDocument.discriminacao.ilike(alvo),
                                 NfseDocument.cod_servico.ilike(alvo)))

    total = query.count()
    # O teto protege o servidor; o piso é só sanidade — pedir poucas por página
    # não custa nada, já que a consulta usa LIMIT.
    per_page = min(max(per_page, 1), 500)
    paginas = math.ceil(total / per_page) if total else 1
    page = min(max(page, 1), paginas)
    linhas = (query.order_by(NfseDocument.dt_emissao.desc().nullslast())
              .offset((page - 1) * per_page).limit(per_page).all())
    return linhas, total, page, paginas


def _contexto(db, profile, request, **kw):
    linhas, total, page, paginas = _consultar(db, profile, **kw)
    return {
        "request": request,
        # O menu do topo some sem isto: `layout/base.html` esconde Auditoria de
        # PDF, Curadoria e Administração atrás de `user_profile.role == 'admin'`,
        # e variável ausente no Jinja é falsa — o admin entrava em Notas de
        # Serviço e ficava sem saída a não ser pelo logo.
        "user_profile": profile,
        "colunas": COLUNAS,
        "monetarios": CAMPOS_MONETARIOS,
        "linhas": linhas,
        "total": total,
        "page": page,
        "total_pages": paginas,
        "empresas": _empresas_do_perfil(db, profile),
        "storage_ok": storage.disponivel(),
        "storage_dir": storage.raiz(),
        **kw,
    }


@router.get("", response_class=HTMLResponse)
def pagina(request: Request, db: Session = Depends(get_db), profile=Depends(get_profile)):
    ctx = _contexto(db, profile, request, tenant_id="", q_prestador="", q_tomador="",
                    q_status="", competencia="", q_texto="", page=1, per_page=50)
    return templates.TemplateResponse(request, "pages/nfse.html", ctx)


@router.get("/tabela", response_class=HTMLResponse)
def tabela(request: Request, tenant_id: str = "", q_prestador: str = "", q_tomador: str = "",
           q_status: str = "", competencia: str = "", q_texto: str = "",
           page: int = 1, per_page: int = 50,
           db: Session = Depends(get_db), profile=Depends(get_profile)):
    ctx = _contexto(db, profile, request, tenant_id=tenant_id, q_prestador=q_prestador,
                    q_tomador=q_tomador, q_status=q_status, competencia=competencia,
                    q_texto=q_texto, page=page, per_page=per_page)
    return templates.TemplateResponse(request, "partials/nfse/tabela.html", ctx)


@router.get("/drawer", response_class=HTMLResponse)
def drawer(request: Request, db: Session = Depends(get_db), profile=Depends(get_profile)):
    empresas = _empresas_do_perfil(db, profile)

    # A busca em lote usa os certificados guardados na rede — é ferramenta do
    # escritório. O cliente final continua enviando o próprio certificado.
    pode_lote = profile.role in ("admin", "operador")

    # A lista vai para o template já pronta em JSON: com 167 empresas, montar um
    # nó por empresa no servidor custava caro no navegador (um efeito reativo por
    # linha). O Alpine renderiza só a fatia visível a partir daqui.
    lista = []
    if pode_lote:
        # Uma consulta e uma listagem de pasta para TODAS as empresas.
        registros = certificados.carregar_todos(db, [str(e.id) for e in empresas])
        arquivos = certificados.listar_arquivos()
        for e in empresas:
            cnpj = re.sub(r"\D", "", e.cnpj or "")
            diag = (certificados.diagnosticar(registros.get(str(e.id)), cnpj, arquivos)
                    if len(cnpj) == 14 else {"ok": False, "motivo": "Empresa sem CNPJ"})
            lista.append({
                "id": str(e.id),
                "nome": e.name or "",
                "cnpj": e.cnpj or "",
                "ok": bool(diag.get("ok")),
                "motivo": diag.get("motivo") or "",
                "busca": f"{e.name or ''} {e.cnpj or ''}".lower(),
                # Só o admin chega a ver e=inativa aqui (empresas já vem filtrada
                # para operador); o card do template marca a tag quando presente.
                "ativo": bool(e.ativo),
            })

    return templates.TemplateResponse(request, "partials/nfse/drawer_import.html", {
        "request": request,
        "empresas": empresas,
        "lista_empresas": lista,
        "prontas": sum(1 for x in lista if x["ok"]),
        "pode_lote": pode_lote,
        "storage_ok": storage.disponivel(),
        "storage_dir": storage.raiz(),
    })


@router.post("/importar", response_class=HTMLResponse)
async def importar(request: Request, tenant_id: str = Form(...),
                   arquivos: list[UploadFile] = File(...),
                   db: Session = Depends(get_db), profile=Depends(get_profile),
                   _rl=Depends(limitar("nfse_import", maximo=20, janela_seg=600))):
    """Importa XMLs de NFS-e (soltos ou dentro de ZIP), grava em disco e indexa."""
    assert_tenant_access(profile, tenant_id)

    lidos: list[tuple[str, bytes]] = []
    orcamento = LIMITE_TOTAL_BYTES
    recusados = 0

    for arq in arquivos:
        conteudo = await arq.read()
        nome = arq.filename or "sem-nome"

        if nome.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
                    for info in z.infolist():
                        if not info.filename.lower().endswith(".xml"):
                            continue
                        # `file_size` vem do índice do ZIP e é conferido contra o
                        # orçamento ANTES de descomprimir: um zip de 51 KB pode
                        # esconder 50 MB de XML (razão medida de 1026:1), e
                        # z.read() traria tudo para a memória de uma vez.
                        if info.file_size > LIMITE_ARQUIVO_BYTES or info.file_size > orcamento:
                            recusados += 1
                            continue
                        dados = z.read(info)
                        orcamento -= len(dados)
                        lidos.append((os.path.basename(info.filename), dados))
                        if len(lidos) >= LIMITE_ARQUIVOS:
                            break
            except zipfile.BadZipFile:
                continue

        elif nome.lower().endswith(".xml"):
            if len(conteudo) > LIMITE_ARQUIVO_BYTES or len(conteudo) > orcamento:
                recusados += 1
                continue
            orcamento -= len(conteudo)
            lidos.append((os.path.basename(nome), conteudo))

        if len(lidos) >= LIMITE_ARQUIVOS:
            break

    novos = atualizados = ignorados = 0
    for nome, conteudo in lidos:
        fd, tmp = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(conteudo)
            dados = parse_nfse_xml(tmp)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass

        if not dados:
            ignorados += 1
            continue

        rel = storage.gravar(dados.get("tomador") or "sem-tomador",
                             dados.get("dt_competencia"), nome, conteudo)

        existente = db.query(NfseDocument).filter_by(
            tenant_id=str(tenant_id), prestador=dados["prestador"],
            numero=dados["numero"], mun_prest=dados["mun_prest"] or "").first()

        if existente:
            for campo, valor in dados.items():
                if campo not in ("prestador", "numero", "mun_prest"):
                    setattr(existente, campo, valor)
            if rel:
                existente.arquivo_xml = rel
            atualizados += 1
        else:
            doc = NfseDocument(tenant_id=str(tenant_id), origem="upload",
                               arquivo_xml=rel, **{**dados,
                                                   "mun_prest": dados["mun_prest"] or ""})
            db.add(doc)
            novos += 1

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao gravar NFS-e importadas")
        return HTMLResponse(
            "<div class='alert alert-error'>Erro ao salvar as notas importadas.</div>",
            status_code=500)

    ctx = _contexto(db, profile, request, tenant_id=tenant_id, q_prestador="",
                    q_tomador="", q_status="", competencia="", q_texto="",
                    page=1, per_page=50)
    ctx["toast"] = (f"{novos} nova(s), {atualizados} atualizada(s)"
                    + (f", {ignorados} ignorada(s)" if ignorados else "")
                    + (f", {recusados} recusada(s) por tamanho" if recusados else ""))
    return templates.TemplateResponse(request, "partials/nfse/tabela.html", ctx)


def _persistir(db: Session, tenant_id: str, nome: str, conteudo: bytes, origem: str) -> str:
    """Grava um XML de NFS-e. Devolve 'novo' | 'atualizado' | 'ignorado'."""
    fd, tmp = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(conteudo)
        dados = parse_nfse_xml(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    if not dados:
        return "ignorado"

    rel = storage.gravar(dados.get("tomador") or "sem-tomador",
                         dados.get("dt_competencia"), nome, conteudo)

    existente = db.query(NfseDocument).filter_by(
        tenant_id=str(tenant_id), prestador=dados["prestador"],
        numero=dados["numero"], mun_prest=dados["mun_prest"] or "").first()

    if existente:
        for campo, valor in dados.items():
            if campo not in ("prestador", "numero", "mun_prest"):
                setattr(existente, campo, valor)
        if rel:
            existente.arquivo_xml = rel
        return "atualizado"

    db.add(NfseDocument(tenant_id=str(tenant_id), origem=origem, arquivo_xml=rel,
                        **{**dados, "mun_prest": dados["mun_prest"] or ""}))
    return "novo"


@router.post("/consultar-adn", response_class=HTMLResponse)
async def consultar_adn(request: Request, tenant_id: str = Form(...), cnpj: str = Form(...),
                        certificado: UploadFile = File(...), senha: str = Form(...),
                        reiniciar: str = Form(""),
                        db: Session = Depends(get_db), profile=Depends(get_profile),
                        _rl=Depends(limitar("nfse_adn", maximo=10, janela_seg=600))):
    """Busca NFS-e no ADN para UMA empresa, usando o certificado enviado agora.

    O certificado e a senha são usados apenas nesta requisição — nada é gravado.
    """
    assert_tenant_access(profile, tenant_id)

    import re as _re
    cnpj_limpo = _re.sub(r"\D", "", cnpj or "")
    if len(cnpj_limpo) != 14:
        return HTMLResponse(
            "<div class='alert alert-error'>CNPJ inválido — informe os 14 dígitos.</div>",
            status_code=400)

    pfx = await certificado.read()
    if not pfx:
        return HTMLResponse("<div class='alert alert-error'>Certificado vazio.</div>",
                            status_code=400)

    cursor = db.query(NfseCursor).filter_by(
        tenant_id=str(tenant_id), cnpj=cnpj_limpo).first()
    if not cursor:
        cursor = NfseCursor(tenant_id=str(tenant_id), cnpj=cnpj_limpo, ultimo_nsu="0")
        db.add(cursor)
        db.flush()

    nsu_inicial = 0 if reiniciar else cursor.nsu
    novos = atualizados = ignorados = 0
    maior_nsu = nsu_inicial

    try:
        for nsu, xml in adn_client.baixar_lote(pfx, senha, cnpj_limpo, nsu_inicial):
            nome = f"nfse_{cnpj_limpo}_{nsu}.xml"
            resultado = _persistir(db, tenant_id, nome, xml.encode("utf-8"), origem="adn")
            novos += resultado == "novo"
            atualizados += resultado == "atualizado"
            ignorados += resultado == "ignorado"

            # Grava o documento ANTES de mover o cursor. Se cair aqui, o lote é
            # reprocessado na próxima vez — o que é inofensivo. O inverso
            # (avançar sem gravar) perderia a nota para sempre.
            db.commit()
            maior_nsu = max(maior_nsu, nsu)
            cursor.ultimo_nsu = str(maior_nsu)
            cursor.ultima_consulta = datetime.utcnow()
            cursor.ultimo_erro = None
            db.commit()

    except (adn_client.CertificadoInvalido, adn_client.RespostaInesperada) as err:
        db.rollback()
        cursor = db.query(NfseCursor).filter_by(
            tenant_id=str(tenant_id), cnpj=cnpj_limpo).first()
        if cursor:
            cursor.ultimo_erro = str(err)[:500]
            cursor.ultima_consulta = datetime.utcnow()
            db.commit()
        return HTMLResponse(
            f"<div class='alert alert-error'>{str(err)}</div>", status_code=400)
    except Exception:
        db.rollback()
        logger.exception("Falha inesperada na consulta ao ADN (CNPJ %s)", cnpj_limpo)
        return HTMLResponse(
            "<div class='alert alert-error'>Erro inesperado ao consultar o ADN. "
            "As notas já baixadas foram preservadas.</div>", status_code=500)
    finally:
        # Não deixa o material do certificado vivo na memória além do necessário.
        pfx = b""
        senha = ""

    ctx = _contexto(db, profile, request, tenant_id=tenant_id, q_prestador="",
                    q_tomador="", q_status="", competencia="", q_texto="",
                    page=1, per_page=50)
    ctx["toast"] = (f"ADN: {novos} nova(s), {atualizados} atualizada(s)"
                    + (f", {ignorados} ignorada(s)" if ignorados else "")
                    + f" · NSU agora em {maior_nsu}")
    return templates.TemplateResponse(request, "partials/nfse/tabela.html", ctx)


def _buscar_uma_empresa(db: Session, tenant_id: str, cnpj: str,
                        pfx: bytes, senha: str, reiniciar: bool,
                        limite_seg: float = adn_client.LIMITE_EMPRESA_SEG) -> dict:
    """Roda o ADN para um CNPJ. Devolve o resumo, sem propagar exceção."""
    cursor = db.query(NfseCursor).filter_by(tenant_id=str(tenant_id), cnpj=cnpj).first()
    if not cursor:
        cursor = NfseCursor(tenant_id=str(tenant_id), cnpj=cnpj, ultimo_nsu="0")
        db.add(cursor)
        db.flush()

    nsu = 0 if reiniciar else cursor.nsu
    resumo = {"cnpj": cnpj, "novos": 0, "atualizados": 0, "erro": None, "nsu": nsu}

    try:
        for nsu_doc, xml in adn_client.baixar_lote(
                pfx, senha, cnpj, nsu,
                limite_seg=min(limite_seg, adn_client.LIMITE_EMPRESA_SEG)):
            nome = f"nfse_{cnpj}_{nsu_doc}.xml"
            r = _persistir(db, tenant_id, nome, xml.encode("utf-8"), origem="adn")
            resumo["novos"] += r == "novo"
            resumo["atualizados"] += r == "atualizado"
            # Grava a nota ANTES de mover o cursor: repetir lote é inofensivo,
            # pular NSU perde o documento em definitivo (janela de 90 dias).
            db.commit()
            resumo["nsu"] = max(resumo["nsu"], nsu_doc)
            cursor.ultimo_nsu = str(resumo["nsu"])
            cursor.ultima_consulta = datetime.utcnow()
            cursor.ultimo_erro = None
            db.commit()
    except Exception as err:
        db.rollback()
        # Mensagem do ADN é apresentável; o resto vai só para o log.
        if isinstance(err, (adn_client.CertificadoInvalido, adn_client.RespostaInesperada)):
            resumo["erro"] = str(err)
        else:
            logger.exception("Falha no ADN para o CNPJ %s", cnpj)
            resumo["erro"] = "Erro inesperado na consulta."
        cursor = db.query(NfseCursor).filter_by(
            tenant_id=str(tenant_id), cnpj=cnpj).first()
        if cursor:
            cursor.ultimo_erro = resumo["erro"][:500]
            cursor.ultima_consulta = datetime.utcnow()
            db.commit()
    return resumo


@router.post("/consultar-lote", response_class=HTMLResponse)
def consultar_lote(request: Request, tenant_ids: list[str] = Form(default=[]),
                   reiniciar: str = Form(""),
                   db: Session = Depends(get_db), profile=Depends(get_profile),
                   _rl=Depends(limitar("nfse_adn_lote", maximo=6, janela_seg=900))):
    """Consulta o ADN para VÁRIAS empresas usando os certificados da rede.

    O operador escolhe apenas entre as empresas vinculadas a ele; o admin vê
    todas. A senha vem cifrada do banco e é decifrada só no momento da conexão.
    """
    if not tenant_ids:
        return HTMLResponse(
            "<div class='alert alert-error'>Selecione ao menos uma empresa.</div>",
            status_code=400)

    # Sem um teto, marcar 167 empresas deixava a tela "consultando" por horas:
    # cada empresa podia gastar minutos e nada voltava até a última terminar.
    # Com orçamento, a resposta chega sempre — e o que ficou de fora é dito com
    # todas as letras. Como o cursor de cada empresa fica gravado, basta repetir
    # a busca para continuar de onde parou.
    prazo = time.monotonic() + ORCAMENTO_LOTE_SEG

    resumos = []
    nao_consultadas = 0
    for tid in tenant_ids:
        # Autorização por empresa: impede que um id seja injetado no formulário.
        assert_tenant_access(profile, tid)
        empresa = db.query(Tenant).filter(Tenant.id == uuid.UUID(tid)).first()
        if not empresa:
            continue

        if time.monotonic() > prazo:
            nao_consultadas += 1
            continue

        cnpj = re.sub(r"\D", "", empresa.cnpj or "")
        if len(cnpj) != 14:
            resumos.append({"cnpj": empresa.name, "novos": 0, "atualizados": 0,
                            "erro": "Empresa sem CNPJ válido cadastrado.", "nsu": 0,
                            "empresa": empresa.name})
            continue

        try:
            pfx, senha = certificados.carregar_material(db, tid, cnpj)
        except ValueError as err:
            resumos.append({"cnpj": cnpj, "novos": 0, "atualizados": 0,
                            "erro": str(err), "nsu": 0, "empresa": empresa.name})
            continue

        try:
            r = _buscar_uma_empresa(db, tid, cnpj, pfx, senha, bool(reiniciar),
                                    limite_seg=max(5.0, prazo - time.monotonic()))
        finally:
            pfx, senha = b"", ""
        r["empresa"] = empresa.name
        resumos.append(r)

    total_novos = sum(r["novos"] for r in resumos)
    com_erro = [r for r in resumos if r["erro"]]

    ctx = _contexto(db, profile, request, tenant_id=tenant_ids[0] if len(tenant_ids) == 1 else "",
                    q_prestador="", q_tomador="", q_status="", competencia="",
                    q_texto="", page=1, per_page=50)
    ctx["toast"] = (f"{total_novos} nota(s) nova(s) em {len(resumos)} empresa(s)"
                    + (f" · {len(com_erro)} com falha" if com_erro else "")
                    + (f" · {nao_consultadas} não consultada(s): tempo esgotado, "
                       "busque de novo para continuar" if nao_consultadas else ""))
    ctx["resumos"] = resumos
    ctx["nao_consultadas"] = nao_consultadas
    return templates.TemplateResponse(request, "partials/nfse/tabela.html", ctx)


@router.post("/zip")
def baixar_zip(chaves: list[str] = Form(default=[]), tenant_id: str = Form(""),
               db: Session = Depends(get_db), profile=Depends(get_profile),
               _rl=Depends(limitar("nfse_zip", maximo=10, janela_seg=300))):
    """ZIP das notas selecionadas (ou de toda a empresa, se nada for marcado)."""
    query = db.query(NfseDocument)
    permitidos = allowed_tenant_ids(profile)
    if tenant_id:
        assert_tenant_access(profile, tenant_id)
        query = query.filter(NfseDocument.tenant_id == str(tenant_id))
    elif permitidos is not None:
        if not permitidos:
            raise HTTPException(status_code=404, detail="Nenhuma empresa disponível.")
        query = query.filter(NfseDocument.tenant_id.in_(permitidos))

    if chaves:
        alvos = []
        for c in chaves:
            partes = c.split("|")
            if len(partes) == 3:
                alvos.append(partes)
        if not alvos:
            raise HTTPException(status_code=400, detail="Seleção inválida.")
        from sqlalchemy import and_
        query = query.filter(or_(*[
            and_(NfseDocument.prestador == p, NfseDocument.numero == n,
                 NfseDocument.mun_prest == m) for p, n, m in alvos]))

    # O ZIP é montado inteiro em memória. Sem teto, pedir "tudo" de uma empresa
    # grande derrubaria o processo — o limite obriga a filtrar antes.
    total = query.count()
    if total > LIMITE_ZIP_NOTAS:
        raise HTTPException(
            status_code=400,
            detail=(f"Seleção grande demais ({total} notas). Use os filtros ou "
                    f"selecione até {LIMITE_ZIP_NOTAS} notas por download."))

    docs = [d for d in query.all() if d.arquivo_xml]
    if not docs:
        raise HTTPException(status_code=404, detail="Nenhum arquivo disponível para download.")

    itens = [(d.arquivo_xml, f"{d.tomador or 'sem-tomador'}/{os.path.basename(d.arquivo_xml)}")
             for d in docs]
    buffer = storage.zipar(itens)
    nome = f"NFSe_{datetime.now():%Y%m%d_%H%M}.zip"
    return StreamingResponse(buffer, media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{nome}"'})


# O código do município é parte da chave da nota, mas o ABRASF nem sempre traz o
# campo — hoje ele está vazio nas 153 notas do banco. Sem um rótulo no lugar, a
# URL sai com barra dupla (/documento/CNPJ/NUMERO//pdf) e nem chega à rota.
SEM_MUNICIPIO = {"", "sem_municipio", "sem-municipio", "geral", "none", "null"}


def _localizar_documento(db: Session, profile, prestador: str, numero: str,
                         mun: str | None) -> NfseDocument | None:
    """Acha a nota dentro do alcance do usuário. None quando não existe para ele.

    O recorte por empresa vem ANTES da busca. Assim, se um dia o mesmo número
    aparecer em duas empresas, o operador recebe a nota dele — e não um 403 a
    respeito de uma nota alheia, que também revelaria que ela existe.
    """
    consulta = db.query(NfseDocument).filter(
        NfseDocument.prestador == prestador, NfseDocument.numero == numero)

    permitidos = allowed_tenant_ids(profile)
    if permitidos is not None:
        if not permitidos:
            return None
        consulta = consulta.filter(
            NfseDocument.tenant_id.in_([str(t) for t in permitidos]))

    if (mun or "").strip().lower() not in SEM_MUNICIPIO:
        doc = consulta.filter(NfseDocument.mun_prest == mun).first()
        if doc:
            return doc
        # Link antigo, ou município gravado vazio: vale tentar sem ele.
    return consulta.first()


@router.get("/documento/{prestador}/{numero}/{mun}/pdf")
def ver_pdf(request: Request, prestador: str, numero: str, mun: str,
            db: Session = Depends(get_db), profile=Depends(get_profile)):
    """Abre o PDF (DANFSe) da nota, quando o arquivo estiver guardado."""
    doc = _localizar_documento(db, profile, prestador, numero, mun)
    if not doc:
        raise HTTPException(status_code=404, detail="Nota não encontrada.")

    dados = storage.ler(doc.arquivo_pdf) if doc.arquivo_pdf else None
    if not dados:
        # Nenhuma das notas importadas por XML tem DANFSe: em vez de um 404 seco
        # numa aba nova, a página explica o porquê e oferece o XML.
        return templates.TemplateResponse(
            request, "pages/nfse_sem_pdf.html",
            {"doc": doc, "mun_url": mun or "sem_municipio",
             "user_profile": profile},
            status_code=404)
    return Response(content=dados, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="NFSe_{numero}.pdf"'})


@router.get("/documento/{prestador}/{numero}/{mun}/xml")
def ver_xml(prestador: str, numero: str, mun: str,
            db: Session = Depends(get_db), profile=Depends(get_profile)):
    doc = _localizar_documento(db, profile, prestador, numero, mun)
    if not doc:
        raise HTTPException(status_code=404, detail="Nota não encontrada.")
    dados = storage.ler(doc.arquivo_xml) if doc.arquivo_xml else None
    if not dados:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado no repositório.")
    return Response(content=dados, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="NFSe_{numero}.xml"'})
