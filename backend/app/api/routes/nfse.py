"""Módulo de Notas Fiscais de Serviço (NFS-e).

Independente do motor de auditoria: rotas, tabela e templates próprios. Nenhum
endpoint ou lógica existente é alterado.
"""
import html
import io
import os
import re
import math
import uuid
import json
import zipfile
import logging
import tempfile
from datetime import datetime

from fastapi import APIRouter, Depends, Request, Form, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from sqlalchemy import and_, case, false, func, or_
from sqlalchemy.orm import Session

from ...core.templating import build_templates
from ...core.authz import get_profile, allowed_tenant_ids, assert_tenant_access
from ...core.rate_limit import limitar
from ...infrastructure.database.session import get_db
from ...domain.models.auth_models import Tenant
from ...domain.models.nfse import NfseDocument, NfseCursor
from ...services.nfse.abrasf_parser import parse_nfse_xml
from ...services.nfse import storage, adn_client, certificados
from ...services.nfse import lote as lote_nfse
from ...services.nfse import classificacao

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/nfse", tags=["NFS-e"])
templates = build_templates()

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


def _filtrar(db: Session, profile, tenant_id, empresas, q_prestador="", q_tomador="",
             q_status="", competencia="", q_texto="", q_tipo="", lote_id=None):
    """A consulta já restrita às empresas do perfil e aos filtros, SEM ordem
    nem página — serve à tabela e ao resumo. None = nada visível."""
    query = db.query(NfseDocument)
    if lote_id:
        query = query.filter(NfseDocument.lote_id == lote_id)

    if tenant_id:
        assert_tenant_access(profile, tenant_id)
        query = query.filter(NfseDocument.tenant_id == str(tenant_id))
    else:
        permitidos = allowed_tenant_ids(profile)
        if permitidos is not None:
            if not permitidos:
                return None
            query = query.filter(NfseDocument.tenant_id.in_(permitidos))

    if q_tipo in ("recebidas", "emitidas"):
        # "Recebida" é a nota em que a EMPRESA DONA é a tomadora — o CNPJ muda
        # de empresa para empresa, então o filtro é um par (empresa, CNPJ).
        campo = NfseDocument.tomador if q_tipo == "recebidas" else NfseDocument.prestador
        pares = [(str(e.id), classificacao.digitos(e.cnpj)) for e in empresas
                 if classificacao.digitos(e.cnpj)
                 and (not tenant_id or str(e.id) == str(tenant_id))]
        query = query.filter(or_(*[and_(NfseDocument.tenant_id == t, campo == c)
                                   for t, c in pares]) if pares else false())

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
    return query


def _consultar(db: Session, profile, tenant_id, empresas=(), q_prestador="", q_tomador="",
               q_status="", competencia="", q_texto="", q_tipo="", page=1, per_page=50,
               lote_id=None, **_ignorar):
    """Página da tabela: (linhas, total, página, páginas, consulta filtrada)."""
    query = _filtrar(db, profile, tenant_id, empresas, q_prestador, q_tomador, q_status,
                     competencia, q_texto, q_tipo, lote_id)
    if query is None:
        return [], 0, 1, 1, None
    total = query.count()
    # O teto protege o servidor; o piso é só sanidade — pedir poucas por página
    # não custa nada, já que a consulta usa LIMIT.
    per_page = min(max(per_page, 1), 500)
    paginas = math.ceil(total / per_page) if total else 1
    page = min(max(page, 1), paginas)
    linhas = (query.order_by(NfseDocument.dt_emissao.desc().nullslast())
              .offset((page - 1) * per_page).limit(per_page).all())
    return linhas, total, page, paginas, query


def _resumo_da_empresa(query, cnpj: str) -> dict:
    """Recebidas e emitidas da empresa, com o que foi retido em cada lado.

    As retenções não se somam entre os lados: na nota RECEBIDA é a empresa
    quem reteve e tem de recolher; na EMITIDA foi o cliente quem reteve dela.
    Uma consulta só, agrupada, sobre o MESMO filtro da tabela.
    """
    c = func.coalesce
    n = NfseDocument
    tipo_sql = case((n.prestador == cnpj, classificacao.EMITIDA),
                    (n.tomador == cnpj, classificacao.RECEBIDA),
                    else_=classificacao.OUTRA)
    retido_iss = case((n.iss_retido.ilike("s%"), c(n.iss, 0.0)), else_=0.0)
    federais = c(n.ir, 0.0) + c(n.pis, 0.0) + c(n.cofins, 0.0) + c(n.csll, 0.0) + c(n.inss, 0.0)
    cancelada = case((or_(n.dt_cancelamento.isnot(None), n.status.ilike("%cancel%")), 1), else_=0)
    linhas = (query.with_entities(tipo_sql, func.count(), func.sum(c(n.valor_servico, 0.0)),
                                  func.sum(retido_iss), func.sum(federais), func.sum(cancelada))
              .group_by(tipo_sql).all())
    vazio = {"qtd": 0, "valor": 0.0, "iss_retido": 0.0, "retencoes": 0.0, "canceladas": 0}
    resumo = {t: dict(vazio) for t in (classificacao.RECEBIDA, classificacao.EMITIDA,
                                       classificacao.OUTRA)}
    for t, qtd, valor, iss, ret, canc in linhas:
        resumo[t] = {"qtd": int(qtd or 0), "valor": float(valor or 0), "iss_retido": float(iss or 0),
                     "retencoes": float(ret or 0), "canceladas": int(canc or 0)}
    return resumo


def _ultimo_lote(db: Session, profile) -> dict | None:
    """O lote que a tela mostra por padrão: o da memória (em andamento ou que
    acabou de terminar), se a pessoa pode vê-lo; senão o mais recente que
    gravou nota numa empresa dela — é o que sobra depois de um reinício."""
    atual = lote_nfse.atual()
    if atual is not None and lote_nfse.pode_ver(atual, profile):
        return {"id": atual.id, "quando": atual.inicio, "rodando": atual.rodando}
    q = (db.query(NfseDocument.lote_id, func.max(NfseDocument.updated_at))
         .filter(NfseDocument.lote_id.isnot(None)))
    permitidos = allowed_tenant_ids(profile)
    if permitidos is not None:
        if not permitidos:
            return None
        q = q.filter(NfseDocument.tenant_id.in_(permitidos))
    linha = (q.group_by(NfseDocument.lote_id)
             .order_by(func.max(NfseDocument.updated_at).desc()).first())
    return {"id": linha[0], "quando": linha[1], "rodando": False} if linha else None


def _contexto(db, profile, request, **kw):
    empresas = _empresas_do_perfil(db, profile)
    # A tela abre no que ACABOU DE CHEGAR (pedido do Sandro em 02/10/2026): com
    # milhares de notas guardadas, a busca de hoje se perdia no histórico.
    # "Todas as notas guardadas" continua a um clique.
    lote_vista = _ultimo_lote(db, profile)
    q_lote = kw.get("q_lote") or ("ultimo" if lote_vista else "todas")
    if not lote_vista:
        q_lote = "todas"
    kw["q_lote"] = q_lote
    lote_id = lote_vista["id"] if q_lote == "ultimo" else None
    linhas, total, page, paginas, query = _consultar(db, profile, empresas=empresas,
                                                     lote_id=lote_id, **kw)
    mapa = {str(e.id): (e.name or "", classificacao.digitos(e.cnpj)) for e in empresas}

    def info(nota) -> dict:
        nome, cnpj = mapa.get(str(nota.tenant_id), ("", ""))
        d = classificacao.descrever(nota, cnpj, nome)
        d["retencoes"] = classificacao.retencoes_federais(nota)
        d["iss_retido"] = classificacao.iss_retido(nota)
        return d

    tenant_id = kw.get("tenant_id")
    empresa = mapa.get(str(tenant_id)) if tenant_id else None
    resumo = (_resumo_da_empresa(query, empresa[1])
              if query is not None and empresa and empresa[1] else None)
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
        "info": info,
        "resumo": resumo,
        "lote_vista": lote_vista,
        "lote_id": lote_id,
        "empresa_atual": empresa,
        "total": total,
        "page": page,
        "total_pages": paginas,
        "empresas": empresas,
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
           q_status: str = "", competencia: str = "", q_texto: str = "", q_tipo: str = "",
           q_lote: str = "", page: int = 1, per_page: int = 50,
           db: Session = Depends(get_db), profile=Depends(get_profile)):
    ctx = _contexto(db, profile, request, tenant_id=tenant_id, q_prestador=q_prestador,
                    q_tomador=q_tomador, q_status=q_status, competencia=competencia,
                    q_texto=q_texto, q_tipo=q_tipo, q_lote=q_lote, page=page, per_page=per_page)
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

    lote_atual = lote_nfse.atual()
    lote_rodando = lote_atual is not None and lote_atual.rodando
    return templates.TemplateResponse(request, "partials/nfse/drawer_import.html", {
        "request": request,
        "empresas": empresas,
        "lista_empresas": lista,
        "prontas": sum(1 for x in lista if x["ok"]),
        "pode_lote": pode_lote,
        "lote_rodando": lote_rodando,
        "lote_dono": lote_atual.dono_nome if lote_rodando else "",
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
    pasta = _pasta_do_dia(db, tenant_id) if lidos else ""
    for nome, conteudo in lidos:
        # Arquivo que não é NFS-e não vai para a pasta da empresa: quem sobe um
        # ZIP misturado não espera achar o resto dele lá.
        r = _persistir(db, tenant_id, nome, conteudo, origem="upload",
                       pasta_rel=pasta, gravar_sempre=False)
        novos += r == "novo"
        atualizados += r == "atualizado"
        ignorados += r in ("ignorado", "evento")

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao gravar NFS-e importadas")
        return HTMLResponse(
            "<div class='alert alert-error'>Erro ao salvar as notas importadas.</div>",
            status_code=500)

    # O que acabou de entrar por aqui não é de lote: a tabela mostra todas.
    ctx = _contexto(db, profile, request, tenant_id=tenant_id, q_prestador="",
                    q_tomador="", q_status="", competencia="", q_texto="",
                    q_lote="todas", page=1, per_page=50)
    ctx["toast"] = (f"{novos} nova(s), {atualizados} atualizada(s)"
                    + (f", {ignorados} ignorada(s)" if ignorados else "")
                    + (f", {recusados} recusada(s) por tamanho" if recusados else ""))
    return templates.TemplateResponse(request, "partials/nfse/tabela.html", ctx)


def _ler_nfse(conteudo: bytes) -> dict | None:
    fd, tmp = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(conteudo)
        return parse_nfse_xml(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _nome_para_pasta(db: Session, tenant_id: str, cnpj: str, nome_empresa: str,
                     arquivos_cert: list[str] | None = None) -> str:
    """Nome usado só quando a pasta da empresa ainda NÃO existe na rede: o do
    arquivo do certificado (até o CPF/CNPJ), ou a razão social do cadastro."""
    from ...domain.models.certificates import TenantCertificate
    registro = db.query(TenantCertificate).filter_by(
        tenant_id=str(tenant_id), cnpj=cnpj).first()
    arquivo = None
    if registro is not None:
        arquivo = registro.arquivo or certificados.localizar_por_cnpj(cnpj, arquivos_cert)
    return storage.nome_do_certificado(arquivo) or nome_empresa or cnpj


def _pasta_do_dia(db: Session, tenant_id: str, cnpj: str | None = None,
                  existentes: list[str] | None = None,
                  arquivos_cert: list[str] | None = None) -> str:
    """<pasta da empresa>/<AAAA-MM-DD> — relativa à raiz das NFS-e.

    `cnpj` é o estabelecimento consultado (a busca avulsa no ADN pode ser de
    uma filial); sem ele vale o CNPJ do cadastro da empresa.
    """
    empresa = db.query(Tenant).filter(Tenant.id == uuid.UUID(str(tenant_id))).first()
    nome = (empresa.name if empresa else "") or ""
    cnpj = re.sub(r"\D", "", cnpj or (empresa.cnpj if empresa else "") or "")
    pasta = storage.pasta_da_empresa(
        cnpj, _nome_para_pasta(db, tenant_id, cnpj, nome, arquivos_cert), existentes)
    return os.path.join(pasta, storage.dia_de_hoje())


def _persistir(db: Session, tenant_id: str, nome: str, conteudo: bytes, origem: str,
               pasta_rel: str, gravar_sempre: bool = True,
               chaves: list | None = None, lote_id: str | None = None) -> str:
    """Grava um XML de NFS-e. Devolve 'novo' | 'atualizado' | 'evento' | 'ignorado'.

    `gravar_sempre`: o que vem do ADN vai para a pasta MESMO quando não é uma
    nota que o parser entende (evento de cancelamento, leiaute novo). Até
    01/10/2026 esse documento era descartado e o cursor andava por cima dele —
    perdido de vez, porque o ADN não entrega o mesmo NSU duas vezes.
    """
    dados = _ler_nfse(conteudo)
    rel = None
    if dados or gravar_sempre:
        arquivo = storage.nome_do_arquivo(conteudo, nome,
                                          (dados or {}).get("prestador", ""),
                                          (dados or {}).get("numero", ""))
        rel = storage.gravar_em(pasta_rel, arquivo, conteudo)

    if not dados:
        return "evento" if storage.identificar(conteudo)["tipo"] == "evento" else "ignorado"
    if chaves is not None:
        chaves.append((dados["prestador"], dados["numero"], dados["mun_prest"] or ""))

    existente = db.query(NfseDocument).filter_by(
        tenant_id=str(tenant_id), prestador=dados["prestador"],
        numero=dados["numero"], mun_prest=dados["mun_prest"] or "").first()

    if existente:
        for campo, valor in dados.items():
            if campo not in ("prestador", "numero", "mun_prest"):
                setattr(existente, campo, valor)
        if rel:
            existente.arquivo_xml = rel
        if lote_id:
            existente.lote_id = lote_id
        return "atualizado"

    db.add(NfseDocument(tenant_id=str(tenant_id), origem=origem, arquivo_xml=rel,
                        lote_id=lote_id, **{**dados, "mun_prest": dados["mun_prest"] or ""}))
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
    outra = adn_client.certificado_de_outra_empresa(pfx, senha, cnpj_limpo)
    if outra:
        return HTMLResponse(f"<div class='alert alert-error'>{html.escape(outra)}</div>",
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
    pasta = _pasta_do_dia(db, tenant_id, cnpj_limpo)

    try:
        for nsu, xml in adn_client.baixar_lote(pfx, senha, cnpj_limpo, nsu_inicial):
            nome = f"NSU_{cnpj_limpo}_{nsu}.xml"
            resultado = _persistir(db, tenant_id, nome, adn_client.bytes_do_xml(xml),
                                   origem="adn", pasta_rel=pasta)
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

    # O que acabou de entrar por aqui não é de lote: a tabela mostra todas.
    ctx = _contexto(db, profile, request, tenant_id=tenant_id, q_prestador="",
                    q_tomador="", q_status="", competencia="", q_texto="",
                    q_lote="todas", page=1, per_page=50)
    ctx["toast"] = (f"ADN: {novos} nova(s), {atualizados} atualizada(s)"
                    + (f", {ignorados} ignorada(s)" if ignorados else "")
                    + f" · NSU agora em {maior_nsu}")
    return templates.TemplateResponse(request, "partials/nfse/tabela.html", ctx)


class BuscaInterrompida(Exception):
    """Levantada por `ao_gravar` quando o lote pede para parar."""


def _buscar_uma_empresa(db: Session, tenant_id: str, cnpj: str,
                        pfx: bytes, senha: str, reiniciar: bool,
                        limite_seg: float = adn_client.LIMITE_EMPRESA_SEG,
                        teto_seg: float = adn_client.LIMITE_EMPRESA_SEG,
                        pasta_rel: str | None = None,
                        ao_gravar=None, lote_id: str | None = None) -> dict:
    """Roda o ADN para um CNPJ. Devolve o resumo, sem propagar exceção.

    `teto_seg` é o tempo máximo por empresa. A busca avulsa usa o padrão; o
    lote e a coleta automática rodam em segundo plano e dão mais tempo.

    `tipo_erro` separa o que o informativo precisa dizer de jeitos diferentes:
    "adiada" é o ADN pedindo para esperar (nada se perdeu), "certificado" é
    certificado ou rede, "resposta" é o ADN devolvendo o inesperado.

    `parada` é "fim" quando o ADN não tem mais nada; "tempo"/"paginas" quando
    ainda há documentos para a próxima busca. `ao_gravar(resumo)` é chamado a
    cada documento gravado — é o que deixa a tela do lote contar ao vivo.
    """
    cursor = db.query(NfseCursor).filter_by(tenant_id=str(tenant_id), cnpj=cnpj).first()
    if not cursor:
        cursor = NfseCursor(tenant_id=str(tenant_id), cnpj=cnpj, ultimo_nsu="0")
        db.add(cursor)
        db.flush()

    nsu = 0 if reiniciar else cursor.nsu
    resumo = {"cnpj": cnpj, "novos": 0, "atualizados": 0, "eventos": 0, "arquivos": 0,
              "erro": None, "tipo_erro": None, "nsu": nsu, "pasta": pasta_rel,
              "parada": None, "chaves": []}
    estado: dict = {}

    outra = adn_client.certificado_de_outra_empresa(pfx, senha, cnpj)
    if outra:
        resumo["erro"], resumo["tipo_erro"] = outra, "certificado"
        cursor.ultimo_erro = outra[:500]
        cursor.ultima_consulta = datetime.utcnow()
        db.commit()
        return resumo

    try:
        for nsu_doc, xml in adn_client.baixar_lote(
                pfx, senha, cnpj, nsu,
                limite_seg=min(limite_seg, teto_seg), estado=estado):
            # A pasta só é resolvida quando chega o primeiro documento: na
            # coleta diária quase toda empresa vem vazia, e resolver custa
            # duas listagens de pasta na rede.
            if not resumo["pasta"]:
                resumo["pasta"] = _pasta_do_dia(db, tenant_id, cnpj)
            nome = f"NSU_{cnpj}_{nsu_doc}.xml"
            r = _persistir(db, tenant_id, nome, adn_client.bytes_do_xml(xml),
                           origem="adn", pasta_rel=resumo["pasta"], chaves=resumo["chaves"],
                           lote_id=lote_id)
            resumo["novos"] += r == "novo"
            resumo["atualizados"] += r == "atualizado"
            resumo["eventos"] += r == "evento"
            resumo["arquivos"] += 1
            # Grava a nota ANTES de mover o cursor: repetir lote é inofensivo,
            # pular NSU perde o documento em definitivo (janela de 90 dias).
            db.commit()
            resumo["nsu"] = max(resumo["nsu"], nsu_doc)
            cursor.ultimo_nsu = str(resumo["nsu"])
            cursor.ultima_consulta = datetime.utcnow()
            cursor.ultimo_erro = None
            db.commit()
            if ao_gravar:
                ao_gravar(resumo)
        resumo["parada"] = estado.get("parada") or "fim"
    except BuscaInterrompida:
        # Pedido de parada do lote: tudo o que veio já está gravado e o cursor
        # está no último documento salvo. Não é erro.
        resumo["parada"] = "interrompida"
    except Exception as err:
        db.rollback()
        # Mensagem do ADN é apresentável; o resto vai só para o log.
        if isinstance(err, (adn_client.CertificadoInvalido, adn_client.RespostaInesperada)):
            resumo["erro"] = str(err)
            resumo["tipo_erro"] = (
                "adiada" if isinstance(err, adn_client.ConsumoIndevido)
                else "certificado" if isinstance(err, adn_client.CertificadoInvalido)
                else "resposta")
        else:
            resumo["tipo_erro"] = "inesperado"
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
                   _rl=Depends(limitar("nfse_adn_lote", maximo=20, janela_seg=900))):
    """COMEÇA a busca no ADN para várias empresas e volta na hora.

    O trabalho roda em segundo plano (services/nfse/lote.py) e a tela acompanha
    por `/nfse/lote`. Até 01/10/2026 o lote rodava dentro desta requisição, com
    240 s para todas as empresas: parava "do nada" depois de 5 ou 6.

    O operador escolhe apenas entre as empresas vinculadas a ele; o admin vê
    todas. A senha vem cifrada do banco e é decifrada só no momento da conexão.
    """
    # A busca em lote usa os certificados guardados na rede: é ferramenta do
    # escritório, a mesma regra que esconde a aba "Empresas" do cliente.
    if profile.role not in ("admin", "operador"):
        raise HTTPException(status_code=403, detail="Busca em lote é do escritório.")

    ids = []
    for tid in tenant_ids:
        # Autorização por empresa: impede que um id seja injetado no formulário.
        assert_tenant_access(profile, tid)
        try:
            ids.append(uuid.UUID(str(tid)))
        except ValueError:
            continue
    if not ids:
        return _painel_lote(request, profile, aviso="Selecione ao menos uma empresa.")

    empresas = db.query(Tenant).filter(Tenant.id.in_(ids)).order_by(Tenant.name).all()
    lista = [(str(e.id), e.name or "", re.sub(r"\D", "", e.cnpj or "")) for e in empresas]
    if not lista:
        return _painel_lote(request, profile, aviso="Nenhuma das empresas marcadas foi encontrada.")

    from ...infrastructure.database.session import SessionLocal
    _, motivo = lote_nfse.iniciar(SessionLocal, lista, bool(reiniciar),
                                  str(profile.id), profile.name or "")
    return _painel_lote(request, profile, aviso=motivo, acompanhando=True)


def _painel_lote(request: Request, profile, aviso: str | None = None,
                 acompanhando: bool = False) -> HTMLResponse:
    atual = lote_nfse.atual()
    ver = lote_nfse.pode_ver(atual, profile)
    return templates.TemplateResponse(request, "partials/nfse/lote_painel.html", {
        "request": request,
        "lote": atual if ver else None,
        # Quem não pode ver o detalhe ainda precisa saber que há um lote
        # rodando: é por isso que o botão dele está desligado.
        "lote_alheio": atual if (atual is not None and atual.rodando and not ver) else None,
        "contagem": atual.contagem() if (atual is not None and ver) else None,
        "relatorio_caminho": (storage.caminho_completo(atual.relatorio_rel)
                              if (atual is not None and ver and atual.relatorio_rel) else ""),
        "aviso": aviso,
        "acompanhando": acompanhando,
    })


@router.get("/lote", response_class=HTMLResponse)
def painel_lote(request: Request, acompanhando: str = "", profile=Depends(get_profile)):
    """Andamento do lote. Enquanto ele roda, o próprio painel pede de novo a
    cada 2 s; ao terminar, recarrega a tabela de notas uma vez."""
    return _painel_lote(request, profile, acompanhando=bool(acompanhando))


@router.get("/lote/relatorio")
def relatorio_lote(profile=Depends(get_profile)):
    """A planilha do último lote. Mesma regra do painel: quem o começou e o admin."""
    from ...services.nfse import relatorio_lote as rel
    atual = lote_nfse.atual()
    if atual is None or not lote_nfse.pode_ver(atual, profile) or not atual.relatorio:
        raise HTTPException(status_code=404, detail="Não há relatório de lote para baixar.")
    return Response(
        content=atual.relatorio,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{rel.nome_arquivo(atual)}"',
                 "X-Content-Type-Options": "nosniff"})


@router.post("/lote/parar", response_class=HTMLResponse)
def parar_lote(request: Request, profile=Depends(get_profile)):
    atual = lote_nfse.atual()
    if atual is not None and lote_nfse.pode_ver(atual, profile):
        lote_nfse.parar()
    return _painel_lote(request, profile, acompanhando=True)


@router.post("/zip")
def baixar_zip(chaves: list[str] = Form(default=[]), tenant_id: str = Form(""),
               lote_id: str = Form(""),
               db: Session = Depends(get_db), profile=Depends(get_profile),
               _rl=Depends(limitar("nfse_zip", maximo=10, janela_seg=300))):
    """ZIP das notas selecionadas — ou, sem nada marcado, do que a tela mostra:
    as notas do lote (`lote_id`), ou toda a empresa."""
    query = db.query(NfseDocument)
    permitidos = allowed_tenant_ids(profile)
    if tenant_id:
        assert_tenant_access(profile, tenant_id)
        query = query.filter(NfseDocument.tenant_id == str(tenant_id))
    elif permitidos is not None:
        if not permitidos:
            raise HTTPException(status_code=404, detail="Nenhuma empresa disponível.")
        query = query.filter(NfseDocument.tenant_id.in_(permitidos))
    if lote_id:
        query = query.filter(NfseDocument.lote_id == lote_id)

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
