from html import escape as html_escape
import logging
import os
from typing import List
from urllib.parse import quote

from fastapi import FastAPI, Request, Depends, Form, Response
from fastapi.exception_handlers import http_exception_handler as http_exception_handler_padrao
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import cast, String, and_, or_, select, func, literal, union_all
from sqlalchemy.orm import Session

# O mais cedo possível: qualquer ERROR logado a partir daqui (inclusive
# durante o resto desta inicialização) já passa pelo diagnóstico automático.
from .core.logging_config import configurar_logging
configurar_logging()

# Imports das rotas (as que já construímos)
from .api.routes.uploads import router as uploads_router
from .api.htmx_routes.audit_status import router as htmx_router
from .api.routes.auth import router as auth_router
from .infrastructure.database.session import engine, get_db
from .infrastructure.database import schema_upgrades
from .domain.models.base import Base

# Importar TODOS os modelos antes do create_all.
#
# `Base.metadata.create_all` só cria as tabelas dos modelos JÁ IMPORTADOS — o
# metadata é preenchido no import de cada classe. Um modelo que só é alcançado
# por um router (importado abaixo, depois desta linha) não existe ainda quando o
# create_all roda, e a tabela nunca é criada: foi o que aconteceu com
# `app_settings` em 10/09/2026, que só era importada via routes/sieg.py.
#
# A lista é explícita e tem de citar cada módulo de modelo. `tests/
# test_configuracoes.py` confere que nenhum ficou de fora.
from .domain.models import (audit, auth_models, certificates, configuracao,
                            integrations, nfse, rules)

logger = logging.getLogger(__name__)

# Preparo do esquema no import. Falha aqui NÃO derruba o processo: um soluço de
# rede (DNS do Supabase sem resolver, por exemplo) matava o worker inteiro e o
# servidor não subia — e com --reload isso acontecia a cada arquivo salvo.
#
# Deixar subir é a escolha certa: em produção as tabelas já existem e o
# create_all seria no-op de qualquer forma; se o banco estiver realmente fora,
# cada requisição falha com 500 (visível e recuperável) em vez de o processo
# morrer. O erro fica no log, e o diagnóstico automático por IA o analisa.
try:
    # Criar tabelas no banco de dados (SQLite local)
    Base.metadata.create_all(bind=engine)
    # Colunas novas em tabelas que já existiam antes do model mudar (ver
    # docstring do módulo): create_all não cobre esse caso.
    schema_upgrades.aplicar(engine)
except Exception:
    logger.exception(
        "Não foi possível preparar o esquema do banco no startup. A aplicação "
        "vai subir mesmo assim; confira a conexão se as telas derem erro."
    )

# SPEDs e XMLs de auditorias que não chegaram ao fim ficavam em temp_uploads/
# para sempre: em 01/09/2026 eram 1,04 GB em 233 pastas, com 85 delas paradas
# havia mais de uma semana. `execute_audit` limpa o caminho feliz; isto aqui
# cuida do que falhou, do que esperava confirmação e do que nunca rodou.
from .core.limpeza_uploads import limpar_no_startup  # noqa: E402
limpar_no_startup(os.path.join(os.getcwd(), "temp_uploads"))

# /docs, /redoc e /openapi.json nascem ABERTOS no FastAPI, sem login nenhum — e
# o schema é o mapa completo da API: 108 rotas com nome de cada campo de
# formulário, incluindo as de admin. Não dá acesso a nada (as rotas continuam
# exigindo sessão), mas entrega de graça o reconhecimento a quem for procurar
# brecha num sistema que guarda SPED e XML de cliente. Fechado por padrão;
# `EXPOR_DOCS=1` no .env reabre para desenvolver.
_DOCS_ABERTOS = os.getenv("EXPOR_DOCS", "").strip().lower() in ("1", "true", "sim")

app = FastAPI(
    title="SaaS Tax Audit - SPED",
    docs_url="/docs" if _DOCS_ABERTOS else None,
    redoc_url="/redoc" if _DOCS_ABERTOS else None,
    openapi_url="/openapi.json" if _DOCS_ABERTOS else None,
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Headers de segurança em todas as respostas.

    Sem CSP restritiva por ora: a interface carrega Tailwind, DaisyUI, HTMX,
    Alpine e FontAwesome via CDN e usa scripts/estilos inline, então uma política
    estrita quebraria a tela. Os demais headers já eliminam clickjacking,
    sniffing de MIME e vazamento de URL interna via Referer.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    # HSTS só faz sentido sob HTTPS; em localhost travaria o acesso por http.
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response

# Servindo os arquivos estáticos extraídos (CSS/JS)
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

# Ambiente Jinja2 único, com autoescape ligado (ver core/templating.py)
from .core.templating import build_templates
templates = build_templates()
_jinja_env = templates.env

# Rotas de API e HTMX
app.include_router(uploads_router)
app.include_router(htmx_router)
app.include_router(auth_router)

from .api.routes.sieg import router as sieg_router
app.include_router(sieg_router)

# Módulo de Notas de Serviço — independente do motor de auditoria.
from .api.routes.nfse import router as nfse_router
app.include_router(nfse_router)

# Gerar SPED a partir dos XMLs: para o cliente que não escritura. Ferramenta
# interna, e cada rota valida papel e empresa por dentro.
from .api.routes.gerar_sped import router as gerar_sped_router
app.include_router(gerar_sped_router)

# Correção do SPED: gera o arquivo retificado a partir do que a auditoria
# apontou. Cada rota valida papel e tenant por dentro (`assert_internal` +
# `get_task_or_403`), como as de upload.
from .api.routes.correcao_sped import router as correcao_sped_router
app.include_router(correcao_sped_router)

# Ajuste de sub-apuração (C195/C197 nos C190 de CFOP 5106/6106): transformação
# mecânica sobre um SPED já pronto, sem tenant — ferramenta interna.
from .api.routes.ajuste_sped import router as ajuste_sped_router
app.include_router(ajuste_sped_router)

from .api.routes.admin import router as admin_router, require_admin
# RBAC: /admin inteiro é exclusivo de admin. Operador e cliente nunca entram.
app.include_router(admin_router, prefix="/admin", dependencies=[Depends(require_admin)])

# Auditoria de PDF por IA: aba nova, por ora só admin. O require_admin no
# include é a trava do roteador; cada rota do módulo valida de novo.
from .api.routes.auditoria_pdf import router as auditoria_pdf_router
app.include_router(auditoria_pdf_router, dependencies=[Depends(require_admin)])

# === SISTEMA DE LOGIN / PROTEÇÃO DE ROTAS ===
from .api.dependencies.auth import get_current_user
from .api.dependencies.db import get_rls_db

# Rota Principal que carrega o Dashboard (Protegida)
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """O navegador pede este arquivo em toda navegação; sem ele, cada página
    deixava um 404 no log, atrapalhando a leitura de erros de verdade."""
    return FileResponse("frontend/static/img/logo.svg", media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, current_user = Depends(get_current_user), db_session = Depends(get_rls_db)):
    if not current_user:
        return RedirectResponse(url="/login")
        
    from .domain.models.auth_models import UserProfile, Tenant
    
    # Buscar perfil local do usuário para saber a role e o tenant_id
    user_profile = db_session.query(UserProfile).filter(UserProfile.id == current_user.id).first()
    
    # Sem perfil local não há papel definido: negar em vez de assumir privilégio.
    if not user_profile:
        return HTMLResponse(
            "<div class='p-8 text-center'><h2 class='text-lg font-bold text-red-700'>Acesso não provisionado</h2>"
            "<p class='text-slate-600 mt-2'>Seu usuário ainda não foi vinculado a uma empresa. "
            "Procure o administrador.</p>"
            "<a href='/logout' class='text-blue-600 underline mt-4 inline-block'>Sair</a></div>",
            status_code=403
        )


    tenants = []
    if user_profile.role == 'admin':
        # Admin continua vendo as inativas — o template marca a tag.
        tenants = db_session.query(Tenant).order_by(Tenant.name).all()
    elif user_profile.role == 'operador':
        if hasattr(user_profile, 'allowed_tenants'):
            # Empresa inativada some do seletor do operador: não faz sentido
            # abrir uma nova auditoria para quem já não é cliente do escritório.
            tenants = sorted((t for t in user_profile.allowed_tenants if t.ativo),
                             key=lambda t: t.name)
        
    return templates.TemplateResponse(request, "pages/dashboard.html", {
        "request": request, 
        "user_email": current_user.email,
        "user_profile": user_profile,
        "tenants": tenants
    })

# === CURADORIA GERAL (exclusiva de Admin) ===
# Fila única de aprovação Maker-Checker. Operador e cliente nunca acessam:
# as regras que eles criam entram aqui como 'pendente_revisao' e só o admin aprova.

# Campos que compõem a chave natural de cada tipo de regra, na ordem do _batch_key.
RULE_KEY_FIELDS = {
    'cfop': ('tenant_id', 'company_cnpj', 'supplier_cnpj', 'supplier_cfop', 'product_code'),
    'pis_cofins': ('tenant_id', 'cfop', 'cst_expected'),
    'ipi': ('tenant_id', 'cfop', 'cst_expected'),
    'icms': ('tenant_id', 'cfop'),
    'cte': ('tenant_id', 'company_cnpj', 'supplier_cfop', 'emitter_uf'),
    'anticipation': ('tenant_id', 'uf_origem', 'uf_destino', 'cfop'),
    # Par SPED×XML aprovado na tela da IA. Mesma chave exata de cinco campos da
    # tabela — `_rule_by_key` reconstrói a linha a partir dela.
    'item_match': ('tenant_id', 'company_cnpj', 'supplier_cnpj', 'sped_cod', 'xml_cod'),
}


def _rule_models():
    from .domain.models.rules import (
        CfopClassification, CteCfopClassification, PisCofinsCreditRule,
        IpiCreditRule, CfopCreditRule, IcmsAnticipationRule, ItemMatchRule
    )
    return {
        'cfop': CfopClassification,
        'pis_cofins': PisCofinsCreditRule,
        'ipi': IpiCreditRule,
        'icms': CfopCreditRule,
        'cte': CteCfopClassification,
        'anticipation': IcmsAnticipationRule,
        # Par SPED×XML que um operador aprovou na tela da IA. Sem esta entrada a
        # regra ficava gravada em `pendente_revisao` sem tela onde ser aprovada,
        # e o item voltava a ser perguntado todo mês.
        'item_match': ItemMatchRule,
    }


# Campos que o admin pode alterar inline no data grid. Allow-list: impede que a
# rota de edição escreva em colunas sensíveis como status, tenant_id ou is_global.
EDITABLE_FIELDS = {
    'cfop': {'entry_cfop': 'str'},
    'cte': {'entry_cfop': 'str'},
    'icms': {'allow_credit': 'bool'},
    'ipi': {'allow_credit': 'bool'},
    'pis_cofins': {'allow_credit': 'bool'},
    'anticipation': {
        'mva_percentage': 'float',
        'aliquota_interna_percentage': 'float',
        'aliquota_interestadual_percentage': 'float',
        'reducao_base_calculo_percentage': 'float',
    },
}


def _batch_key(rule, rule_type: str) -> str:
    return "|".join(str(getattr(rule, f) or "") for f in RULE_KEY_FIELDS[rule_type])


def _rule_by_key(db_session, model, rule_type: str, key: str):
    """Localiza uma regra pendente pela chave natural composta."""
    fields = RULE_KEY_FIELDS[rule_type]
    parts = key.split("|", len(fields) - 1)
    if len(parts) != len(fields):
        return None
    return db_session.query(model).filter(
        model.status == 'pendente_revisao',
        *[getattr(model, f) == v for f, v in zip(fields, parts)]
    ).first()


def _pending_age(created_at) -> str:
    """Há quanto tempo a regra está aguardando revisão."""
    if not created_at:
        return "—"
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc) if created_at.tzinfo else datetime.utcnow()
    delta = now - created_at
    days, seconds = delta.days, delta.seconds
    if days >= 1:
        return f"{days} dia{'s' if days > 1 else ''}"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}min"
    return "agora"


def _decorate(rule, rule_type: str, tenant_name: str):
    """Anexa os campos que o template da linha consome."""
    from datetime import datetime, timezone
    rule._batch_key = _batch_key(rule, rule_type)
    rule._tenant_name = tenant_name
    rule._pending_age = _pending_age(rule.created_at)
    stale = False
    if rule.created_at:
        now = datetime.now(timezone.utc) if rule.created_at.tzinfo else datetime.utcnow()
        stale = (now - rule.created_at).days >= 3
    rule._pending_stale = stale
    return rule


# Cache curto do mapa de empresas: são poucas linhas e mudam raramente, mas
# buscá-las custava um round-trip de ~150ms em cada render e em cada ação inline.
_TENANT_CACHE = {"data": None, "ts": 0.0}
_TENANT_TTL = 60.0


def _tenant_map(db_session) -> dict:
    import time
    from .domain.models.auth_models import Tenant
    now = time.monotonic()
    if _TENANT_CACHE["data"] is None or (now - _TENANT_CACHE["ts"]) > _TENANT_TTL:
        _TENANT_CACHE["data"] = {
            str(t.id): t.name for t in db_session.query(Tenant.id, Tenant.name).all()
        }
        _TENANT_CACHE["ts"] = now
    return _TENANT_CACHE["data"]


def _tenant_name_for(db_session, tenant_id) -> str:
    return _tenant_map(db_session).get(str(tenant_id), str(tenant_id))


def _counts(db_session) -> dict:
    """Contadores das 6 abas em UMA query (UNION ALL).

    Cada ida ao Supabase custa ~140ms de rede contra ~0.1ms de execução, então
    o que pesa é o NÚMERO de round-trips, não o tamanho das tabelas. Seis COUNTs
    separados gastavam ~850ms; unificados, ~140ms.
    """
    models = _rule_models()
    parts = [
        select(literal(key).label("k"), func.count().label("n"))
        .select_from(m).where(m.status == 'pendente_revisao')
        for key, m in models.items()
    ]
    rows = db_session.execute(union_all(*parts)).all()
    counts = {r.k: r.n for r in rows}
    return {key: counts.get(key, 0) for key in models}


def _tabs_oob(db_session, rule_type: str) -> str:
    """Abas com contadores atualizados, trocadas fora de banda após cada ação."""
    return templates.env.get_template("partials/curation/_tabs.html").render(
        counts=_counts(db_session), current_rule_type=rule_type, oob=True
    )


def _row_html(request, rule, rule_type: str, tenant_name: str) -> str:
    return templates.env.get_template("partials/curation/_row.html").render(
        request=request, r=_decorate(rule, rule_type, tenant_name), current_rule_type=rule_type
    )


def _apply_curation_filters(query, model, q_tenant, q_supplier, q_cfop, q_creator):
    from .domain.models.auth_models import Tenant
    if q_tenant:
        query = query.join(Tenant, model.tenant_id == cast(Tenant.id, String)).filter(
            Tenant.cnpj.ilike(f"%{q_tenant}%")
        )
    if q_supplier and hasattr(model, 'supplier_cnpj'):
        query = query.filter(model.supplier_cnpj.ilike(f"%{q_supplier}%"))
    if q_cfop:
        if hasattr(model, 'cfop'):
            query = query.filter(model.cfop.ilike(f"%{q_cfop}%"))
        elif hasattr(model, 'supplier_cfop'):
            query = query.filter(model.supplier_cfop.ilike(f"%{q_cfop}%"))
    if q_creator and hasattr(model, 'created_by'):
        query = query.filter(model.created_by.ilike(f"%{q_creator}%"))
    return query


def _curation_response(request, db_session, current_user, rule_type,
                       q_tenant="", q_supplier="", q_cfop="", q_creator="",
                       page=1, per_page=50, toast=None, toast_class="alert-success"):
    """Renderiza a lista de curadoria. Ações em lote reusam isso para atualizar a
    tabela no lugar, com um toast fora de banda, em vez de trocá-la por um alerta."""
    import math

    models = _rule_models()
    per_page = min(max(per_page, 1), 200)
    page = max(page, 1)
    if rule_type not in models:
        rule_type = "cfop"
    model = models[rule_type]

    counts = _counts(db_session)

    q = db_session.query(model).filter(model.status == 'pendente_revisao')
    tem_filtro = any([q_tenant, q_supplier, q_cfop, q_creator])
    q = _apply_curation_filters(q, model, q_tenant, q_supplier, q_cfop, q_creator)

    # Sem filtro ativo, o total já veio no UNION dos contadores: evita um round-trip.
    total_items = q.count() if tem_filtro else counts[rule_type]
    total_pages = math.ceil(total_items / per_page) if total_items > 0 else 1
    page = min(page, total_pages)

    rules = q.order_by(model.created_at.asc()).offset((page - 1) * per_page).limit(per_page).all()

    tenant_map = _tenant_map(db_session)
    for r in rules:
        _decorate(r, rule_type, tenant_map.get(str(r.tenant_id), str(r.tenant_id)))

    html = templates.env.get_template("partials/curation/curation_list.html").render(
        request=request,
        rules=rules,
        counts=counts,
        current_rule_type=rule_type,
        q_tenant=q_tenant,
        q_supplier=q_supplier,
        q_cfop=q_cfop,
        q_creator=q_creator,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        total_items=total_items,
        current_user=current_user,
    )

    if toast:
        # Este trecho é montado em f-string, fora do autoescape do Jinja (ver
        # CLAUDE.md). Hoje o texto do toast é composto no servidor a partir de
        # contadores, mas escapar é barato e evita que uma mensagem futura com
        # dado de formulário vire HTML.
        html += (
            f'<div id="toast-container" hx-swap-oob="beforeend">'
            f'<div class="alert {toast_class} shadow-lg font-medium text-sm">'
            f'{html_escape(toast)}</div>'
            f'</div>'
        )
    return HTMLResponse(html)


@app.get("/curation", response_class=HTMLResponse)
def read_curation(
    request: Request,
    current_user = Depends(get_current_user),
    profile = Depends(require_admin)
):
    return templates.TemplateResponse(request, "pages/curation.html", {
        "request": request,
        "user_email": current_user.email,
        "current_user": current_user,
        "user_profile": profile,
        "tenant": None
    })


@app.get("/api/curation/list", response_class=HTMLResponse)
def list_curation(
    request: Request,
    rule_type: str = "cfop",
    q_tenant: str = "",
    q_supplier: str = "",
    q_cfop: str = "",
    q_creator: str = "",
    page: int = 1,
    per_page: int = 50,
    db_session: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    profile = Depends(require_admin)
):
    return _curation_response(
        request, db_session, current_user, rule_type,
        q_tenant, q_supplier, q_cfop, q_creator, page, per_page
    )


# --- Ações inline, uma linha por vez (data grid) ---

@app.post("/api/curation/rule/update", response_class=HTMLResponse)
def curation_rule_update(
    request: Request,
    rule_type: str = Form(...),
    key: str = Form(...),
    field: str = Form(...),
    value: str = Form(""),
    db_session: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    profile = Depends(require_admin)
):
    """Edita inline um campo da regra e devolve a linha já re-renderizada."""
    model = _rule_models().get(rule_type)
    if not model or field not in EDITABLE_FIELDS.get(rule_type, {}):
        return HTMLResponse("<tr><td colspan='7' class='px-2 py-2 text-red-600 text-[13px]'>Campo não editável.</td></tr>",
                            status_code=400)

    rule = _rule_by_key(db_session, model, rule_type, key)
    if not rule:
        return HTMLResponse("")  # já saiu da fila: remove a linha

    kind = EDITABLE_FIELDS[rule_type][field]
    try:
        if kind == 'bool':
            parsed = str(value).strip().lower() in ('true', '1', 'on', 'sim')
        elif kind == 'float':
            parsed = float(str(value).replace(',', '.') or 0)
        else:
            parsed = str(value).strip()
            if not parsed:
                raise ValueError("valor vazio")
    except ValueError:
        # devolve a linha intacta, sem gravar
        return HTMLResponse(_row_html(request, rule, rule_type, _tenant_name_for(db_session, rule.tenant_id)))

    try:
        setattr(rule, field, parsed)
        rule.created_by = current_user.email
        db_session.commit()
        db_session.refresh(rule)
    except Exception:
        db_session.rollback()
        return HTMLResponse("<tr><td colspan='7' class='px-2 py-2 text-red-600 text-[13px]'>Erro ao salvar.</td></tr>",
                            status_code=500)

    return HTMLResponse(_row_html(request, rule, rule_type, _tenant_name_for(db_session, rule.tenant_id)))


@app.post("/api/curation/rule/approve", response_class=HTMLResponse)
def curation_rule_approve(
    request: Request,
    rule_type: str = Form(...),
    key: str = Form(...),
    field: str = Form(None),
    value: str = Form(None),
    db_session: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    profile = Depends(require_admin)
):
    """Aprova uma regra. Se vier field/value (Enter no campo), grava a edição junto
    na mesma transação. Devolve linha vazia + abas atualizadas via OOB."""
    model = _rule_models().get(rule_type)
    if not model:
        return HTMLResponse("", status_code=400)

    rule = _rule_by_key(db_session, model, rule_type, key)
    if not rule:
        return HTMLResponse("" + _tabs_oob(db_session, rule_type))

    try:
        if field and field in EDITABLE_FIELDS.get(rule_type, {}):
            kind = EDITABLE_FIELDS[rule_type][field]
            if kind == 'bool':
                setattr(rule, field, str(value).strip().lower() in ('true', '1', 'on', 'sim'))
            elif kind == 'float':
                setattr(rule, field, float(str(value).replace(',', '.') or 0))
            elif str(value).strip():
                setattr(rule, field, str(value).strip())
        rule.status = 'aprovado'
        rule.created_by = current_user.email
        db_session.commit()
    except Exception:
        db_session.rollback()
        return HTMLResponse("<tr><td colspan='7' class='px-2 py-2 text-red-600 text-[13px]'>Erro ao aprovar.</td></tr>",
                            status_code=500)

    return HTMLResponse("" + _tabs_oob(db_session, rule_type))


@app.post("/api/curation/rule/discard", response_class=HTMLResponse)
def curation_rule_discard(
    request: Request,
    rule_type: str = Form(...),
    key: str = Form(...),
    db_session: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    profile = Depends(require_admin)
):
    """Descarta uma sugestão. Devolve linha vazia + abas atualizadas via OOB."""
    model = _rule_models().get(rule_type)
    if not model:
        return HTMLResponse("", status_code=400)

    rule = _rule_by_key(db_session, model, rule_type, key)
    if rule:
        try:
            db_session.delete(rule)
            db_session.commit()
        except Exception:
            db_session.rollback()
            return HTMLResponse("<tr><td colspan='7' class='px-2 py-2 text-red-600 text-[13px]'>Erro ao descartar.</td></tr>",
                                status_code=500)

    return HTMLResponse("" + _tabs_oob(db_session, rule_type))


@app.post("/api/curation/batch-discard", response_class=HTMLResponse)
def curation_batch_discard(
    request: Request,
    rule_type: str = Form(...),
    rule_keys: List[str] = Form(default=[]),
    db_session: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    profile = Depends(require_admin)
):
    """Descarta as regras marcadas por checkbox em um único DELETE."""
    model = _rule_models().get(rule_type)
    if not model:
        return HTMLResponse("<div class='alert alert-error'>Categoria inválida.</div>", status_code=400)

    fields = RULE_KEY_FIELDS[rule_type]
    conditions = []
    for key in rule_keys:
        parts = key.split("|", len(fields) - 1)
        if len(parts) == len(fields):
            conditions.append(and_(*[getattr(model, f) == v for f, v in zip(fields, parts)]))

    if not conditions:
        return _curation_response(request, db_session, current_user, rule_type,
                                  toast="Nenhuma regra válida selecionada.", toast_class="alert-warning")

    try:
        deleted = db_session.query(model).filter(
            model.status == 'pendente_revisao', or_(*conditions)
        ).delete(synchronize_session=False)
        db_session.commit()
    except Exception:
        db_session.rollback()
        logger.exception("Falha ao descartar regras em lote tipo=%s", rule_type)
        return HTMLResponse("<div class='alert alert-error'>Não foi possível descartar as regras.</div>", status_code=500)

    return _curation_response(request, db_session, current_user, rule_type,
                              toast=f"🗑️ {deleted} regra(s) descartada(s).", toast_class="alert-info")


@app.post("/api/curation/batch-approve", response_class=HTMLResponse)
def curation_batch_approve(
    request: Request,
    rule_type: str = Form(...),
    rule_keys: List[str] = Form(default=[]),
    q_tenant: str = Form(""),
    q_supplier: str = Form(""),
    q_cfop: str = Form(""),
    q_creator: str = Form(""),
    page: int = Form(1),
    db_session: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    profile = Depends(require_admin)
):
    """Aprova as regras marcadas por checkbox em um único UPDATE."""
    model = _rule_models().get(rule_type)
    if not model:
        return HTMLResponse("<div class='alert alert-error'>Categoria inválida.</div>", status_code=400)

    fields = RULE_KEY_FIELDS[rule_type]
    conditions = []
    for key in rule_keys:
        parts = key.split("|", len(fields) - 1)
        if len(parts) == len(fields):
            conditions.append(and_(*[getattr(model, f) == v for f, v in zip(fields, parts)]))

    if not conditions:
        return _curation_response(
            request, db_session, current_user, rule_type,
            q_tenant, q_supplier, q_cfop, q_creator, page,
            toast="Nenhuma regra válida selecionada.", toast_class="alert-warning"
        )

    try:
        updated = db_session.query(model).filter(
            model.status == 'pendente_revisao', or_(*conditions)
        ).update({"status": "aprovado"}, synchronize_session=False)
        db_session.commit()
    except Exception:
        db_session.rollback()
        logger.exception("Falha ao aprovar regras em lote tipo=%s", rule_type)
        return HTMLResponse("<div class='alert alert-error'>Não foi possível aprovar as regras.</div>", status_code=500)

    return _curation_response(
        request, db_session, current_user, rule_type,
        q_tenant, q_supplier, q_cfop, q_creator, page,
        toast=f"✅ {updated} regra(s) aprovada(s)."
    )


@app.post("/api/curation/bulk-approve-filtered", response_class=HTMLResponse)
def curation_bulk_approve_filtered(
    request: Request,
    rule_type: str = Form(...),
    q_tenant: str = Form(""),
    q_supplier: str = Form(""),
    q_cfop: str = Form(""),
    q_creator: str = Form(""),
    db_session: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    profile = Depends(require_admin)
):
    """Aprova em um único UPDATE tudo que casa com o filtro atual (todas as páginas)."""
    model = _rule_models().get(rule_type)
    if not model:
        return HTMLResponse("<div class='alert alert-error'>Categoria inválida.</div>", status_code=400)

    try:
        q = db_session.query(model).filter(model.status == 'pendente_revisao')
        q = _apply_curation_filters(q, model, q_tenant, q_supplier, q_cfop, q_creator)
        updated = q.update({"status": "aprovado"}, synchronize_session=False)
        db_session.commit()
    except Exception:
        db_session.rollback()
        logger.exception("Falha ao aprovar filtrados tipo=%s", rule_type)
        return HTMLResponse("<div class='alert alert-error'>Não foi possível aprovar as regras filtradas.</div>", status_code=500)

    return _curation_response(
        request, db_session, current_user, rule_type,
        q_tenant, q_supplier, q_cfop, q_creator,
        toast=f"✅ {updated} regra(s) filtrada(s) aprovada(s) em lote."
    )


@app.post("/api/curation/bulk-discard-filtered", response_class=HTMLResponse)
def curation_bulk_discard_filtered(
    request: Request,
    rule_type: str = Form(...),
    q_tenant: str = Form(""),
    q_supplier: str = Form(""),
    q_cfop: str = Form(""),
    q_creator: str = Form(""),
    db_session: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    profile = Depends(require_admin)
):
    """Descarta em lote tudo que casa com o filtro atual (todas as páginas)."""
    model = _rule_models().get(rule_type)
    if not model:
        return HTMLResponse("<div class='alert alert-error'>Categoria inválida.</div>", status_code=400)

    try:
        q = db_session.query(model).filter(model.status == 'pendente_revisao')
        q = _apply_curation_filters(q, model, q_tenant, q_supplier, q_cfop, q_creator)
        deleted = q.delete(synchronize_session=False)
        db_session.commit()
    except Exception:
        db_session.rollback()
        logger.exception("Falha ao descartar filtrados tipo=%s", rule_type)
        return HTMLResponse("<div class='alert alert-error'>Não foi possível descartar as regras filtradas.</div>", status_code=500)

    return _curation_response(
        request, db_session, current_user, rule_type,
        q_tenant, q_supplier, q_cfop, q_creator,
        toast=f"🗑️ {deleted} regra(s) descartada(s).", toast_class="alert-info"
    )


def _quer_html(request: Request) -> bool:
    """Requisição de navegador, e não chamada de API.

    `Accept: */*` (curl, fetch, cliente de teste) NÃO conta: só entra aqui quem
    pede text/html explicitamente, como faz a barra de endereços.
    """
    return "text/html" in (request.headers.get("accept") or "")


def _destino_seguro(caminho: str) -> str:
    """Impede que o `next` do login vire redirecionamento para fora do sistema."""
    if not caminho.startswith("/") or caminho.startswith("//") or "\\" in caminho:
        return "/"
    return caminho


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Sessão expirada no navegador leva ao login, não a um JSON cru.

    Antes, digitar /admin sem sessão devolvia {"detail": "Autenticação
    necessária."} na tela — e o usuário ficava preso, porque só a raiz "/"
    redirecionava para o login. Chamadas de API seguem recebendo JSON.
    """
    if exc.status_code == 401 and _quer_html(request):
        alvo = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        destino = f"/login?next={quote(_destino_seguro(alvo), safe='/?=&')}"
        if request.headers.get("HX-Request"):
            # O HTMX troca o pedaço da página e ignoraria um 302; este cabeçalho
            # manda o navegador navegar de verdade.
            return Response(status_code=204, headers={"HX-Redirect": destino})
        return RedirectResponse(destino, status_code=302)

    if exc.status_code == 403 and _quer_html(request):
        return templates.TemplateResponse(
            request, "pages/sem_permissao.html",
            {"detalhe": exc.detail}, status_code=403)

    return await http_exception_handler_padrao(request, exc)


# Global Exception Handler (Segurança)
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return HTMLResponse(
        content=f"""
        <div class="bg-red-50 text-red-600 p-4 border border-red-200 rounded-lg">
            <h3 class="font-bold">Ocorreu um erro interno.</h3>
            <p>Nossa equipe foi notificada. Por favor, tente novamente.</p>
        </div>
        """,
        status_code=500
    )
