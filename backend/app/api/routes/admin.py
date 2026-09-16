import logging
import os
import re
from fastapi import APIRouter, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from typing import List
import uuid
import math

from ...infrastructure.database.session import get_db
from ...domain.models.auth_models import Tenant, UserProfile, UserTenant
from ...domain.models.rules import CfopCreditRule, CfopClassification, CteCfopClassification, IpiCreditRule, PisCofinsCreditRule, IcmsAnticipationRule
from ...domain.models.audit import AuditTask
from ...services import ficha_empresa as ficha_svc
from ...services import erros_sistema
from ...infrastructure.auth.supabase_client import get_supabase_admin_client, get_supabase_client
from ...core.rate_limit import limitar
from ...api.dependencies.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()
from ...core.templating import build_templates
templates = build_templates()

def require_admin(request: Request, current_user = Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user:
        raise HTTPException(status_code=401, detail="Autenticação necessária.")
    profile = db.query(UserProfile).filter(UserProfile.id == current_user.id).first()
    if not profile or profile.role != 'admin':
        raise HTTPException(status_code=403, detail="Acesso negado. Apenas administradores.")
    return profile

def _require_admin(current_user, db: Session):
    """Valida que o usuário autenticado é admin. Retorna o UserProfile ou lança 401/403."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Autenticação necessária.")
    profile = db.query(UserProfile).filter(UserProfile.id == current_user.id).first()
    if not profile or profile.role != 'admin':
        raise HTTPException(status_code=403, detail="Acesso negado. Apenas administradores.")
    return profile

def _validate_tenant_access(current_user, tenant_id: str, db: Session):
    """Valida que o usuário tem acesso ao tenant_id informado."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Autenticação necessária.")
    profile = db.query(UserProfile).filter(UserProfile.id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=403, detail="Perfil não encontrado.")
    if profile.role != 'admin' and str(profile.tenant_id) != tenant_id:
        raise HTTPException(status_code=403, detail="Acesso negado a este tenant.")
    return profile

@router.get("", response_class=HTMLResponse)
def admin_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    profile: UserProfile = Depends(require_admin)
):
    tenants = db.query(Tenant).order_by(Tenant.name).all()
    return templates.TemplateResponse(request, "pages/admin.html", {
        "request": request,
        "user_email": current_user.email,
        "user_profile": profile,
        "tenants": tenants
    })

@router.get("/modals/tenant", response_class=HTMLResponse)
def get_modal_tenant(request: Request):
    return templates.TemplateResponse(request, "partials/admin/modals/tenant.html", {"request": request})

@router.get("/modals/user", response_class=HTMLResponse)
def modal_user(request: Request, tenant_id: str = None, db: Session = Depends(get_db)):
    """tenant_id é opcional: quando o modal abre a partir de uma empresa, ela já vem marcada."""
    tenants = db.query(Tenant).order_by(Tenant.name).all()
    return templates.TemplateResponse(request, "partials/admin/modals/user.html", {
        "request": request, "tenant_id": tenant_id, "tenants": tenants
    })

@router.get("/modals/rule", response_class=HTMLResponse)
def get_modal_rule(request: Request, tenant_id: str):
    return templates.TemplateResponse(request, "partials/admin/modals/rule.html", {"request": request, "tenant_id": tenant_id})

@router.get("/modals/client", response_class=HTMLResponse)
def get_modal_client(request: Request):
    return templates.TemplateResponse(request, "partials/admin/modals/client.html", {"request": request})

@router.post("/clients/create", response_class=HTMLResponse)
def provision_client(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    nome_empresa: str = Form(...),
    cnpj: str = Form(...),
    db: Session = Depends(get_db)
):
    try:
        try:
            supabase_admin = get_supabase_admin_client()
        except ValueError as ve:
            return templates.TemplateResponse(request, "partials/admin/modals/client.html", {
                "request": request, "error_msg": str(ve)
            })

        # 1. Supabase Auth Provisioning
        try:
            res = supabase_admin.auth.admin.create_user({
                "email": email,
                "password": password,
                "email_confirm": True
            })
            user_id = res.user.id
        except Exception as auth_err:
            error_msg = str(auth_err)
            if "already registered" in error_msg.lower() or "User already exists" in error_msg:
                error_msg = "Este e-mail já está cadastrado no sistema."
            return templates.TemplateResponse(request, "partials/admin/modals/client.html", {
                "request": request, "error_msg": error_msg
            })
            
        # 2. Local Database Provisioning
        tenant = db.query(Tenant).filter(Tenant.cnpj == cnpj).first()
        if not tenant:
            tenant = Tenant(name=nome_empresa, cnpj=cnpj)
            db.add(tenant)
            db.flush() # Force ID generation
            
        # 3. User Profile Creation
        # We need to make sure the user profile does not exist yet to avoid duplicate keys
        profile = db.query(UserProfile).filter(UserProfile.id == user_id).first()
        if not profile:
            profile = UserProfile(
                id=uuid.UUID(user_id),
                name=nome_empresa,
                role="cliente",
                tenant_id=tenant.id
            )
            db.add(profile)
        
        db.commit()
        
        return templates.TemplateResponse(request, "partials/admin/modals/client.html", {
            "request": request, 
            "success_msg": f"Cliente provisionado com sucesso!"
        })
        
    except Exception:
        db.rollback()
        logger.exception("Falha ao provisionar cliente")
        return templates.TemplateResponse(request, "partials/admin/modals/client.html", {
            "request": request, "error_msg": "Não foi possível provisionar o cliente."
        })

@router.get("/tenants/list", response_class=HTMLResponse)
def list_tenants(request: Request, db: Session = Depends(get_db), toast_message: str = None):
    tenants = db.query(Tenant).order_by(Tenant.name).all()
    profiles = db.query(UserProfile).all()
    
    # Agrupar perfis por tenant
    tenant_users = {str(t.id): [] for t in tenants}
    sem_empresa = []
    for p in profiles:
        t_id = str(p.tenant_id)
        if t_id in tenant_users:
            tenant_users[t_id].append(p)
        else:
            # Admins costumam ter tenant_id nulo (enxergam todas as empresas).
            # Sem esta lista eles não apareceriam em tela alguma — e não haveria
            # como editá-los ou excluí-los pela interface.
            sem_empresa.append(p)

    return templates.TemplateResponse(request, "partials/admin/tenant_list.html", {
        "request": request,
        "tenants": tenants,
        "tenant_users": tenant_users,
        "sem_empresa": sem_empresa,
        # Duas consultas agregadas para a lista inteira — a alternativa (uma
        # ficha por linha) seriam 1.360 idas ao banco com 170 empresas.
        "resumo": ficha_svc.resumo_para_lista(db),
        "toast_message": toast_message
    })


@router.get("/tenants/{tenant_id}/ficha", response_class=HTMLResponse)
def ficha_tenant(request: Request, tenant_id: str, db: Session = Depends(get_db),
                 current_user = Depends(get_current_user)):
    """Painel lateral com o estado da empresa: auditoria, curadoria, cadastro.

    Só leitura. Empresa inexistente devolve 200 com aviso na tela, e não 404:
    o htmx não troca o alvo em resposta 4xx, então o usuário clicaria e nada
    aconteceria — o mesmo motivo documentado no CLAUDE.md para o Form vazio.
    """
    _validate_tenant_access(current_user, tenant_id, db)
    return templates.TemplateResponse(request, "partials/admin/tenant_ficha.html", {
        "request": request,
        "f": ficha_svc.montar_ficha(db, tenant_id),
    })

def _uf_valida(valor) -> str:
    """Sigla de UF normalizada, ou "" se nao for 2 letras.

    A UF da empresa decide CFOP 1933 x 2933 na auditoria de PDF. Deixar em
    branco aqui faz a geracao do arquivo do Dominio recusar depois, longe
    daqui, sem o admin ligar uma coisa a outra.
    """
    uf = str(valor or "").upper().strip()
    return uf if len(uf) == 2 and uf.isalpha() else ""


@router.post("/tenants", response_class=HTMLResponse)
def create_tenant(
    request: Request,
    name: str = Form(...),
    cnpj: str = Form(...),
    uf: str = Form(""),
    db: Session = Depends(get_db)
):
    try:
        uf_limpa = _uf_valida(uf)
        if not uf_limpa:
            return list_tenants(request, db, toast_message="❌ Informe a UF da empresa (2 letras).")
        new_tenant = Tenant(name=name, cnpj=cnpj, uf=uf_limpa)
        db.add(new_tenant)
        db.commit()
        return list_tenants(request, db, toast_message="🏢 Empresa cadastrada com sucesso!")
    except Exception as e:
        db.rollback()
        return list_tenants(request, db, toast_message="❌ Erro ao cadastrar empresa.")

@router.post("/users", response_class=HTMLResponse)
def create_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    name: str = Form(...),
    role: str = Form("cliente"),
    tenant_ids: List[str] = Form(default=[]),
    db: Session = Depends(get_db)
):
    """Cria o usuário no Supabase Auth e, numa única transação local, grava o
    perfil e todos os vínculos empresa↔usuário vindos da grade de checkboxes."""
    if role not in ('cliente', 'operador', 'admin'):
        return list_tenants(request, db, toast_message="❌ Nível de acesso inválido.")

    # Normaliza e valida os tenants recebidos contra o que existe de fato no banco.
    requested = {t for t in tenant_ids if t}
    valid_tenants = []
    if requested:
        try:
            requested_uuids = {uuid.UUID(t) for t in requested}
        except ValueError:
            return list_tenants(request, db, toast_message="❌ Empresa inválida na seleção.")
        valid_tenants = db.query(Tenant).filter(Tenant.id.in_(requested_uuids)).all()
        if len(valid_tenants) != len(requested_uuids):
            return list_tenants(request, db, toast_message="❌ Uma das empresas selecionadas não existe mais.")

    # Admin enxerga todas as empresas; os demais precisam de pelo menos um vínculo.
    if role != 'admin' and not valid_tenants:
        return list_tenants(request, db, toast_message="❌ Selecione ao menos uma empresa para o usuário.")

    try:
        supabase_admin = get_supabase_admin_client()
    except ValueError as ve:
        return list_tenants(request, db, toast_message=f"❌ Erro: {ve}")

    # 1. Cria no Supabase Auth (service_role dispensa confirmação de e-mail)
    try:
        res = supabase_admin.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True
        })
        user_id = uuid.UUID(res.user.id)
    except Exception as auth_err:
        msg = str(auth_err)
        if "already registered" in msg.lower() or "already exists" in msg.lower():
            msg = "Este e-mail já está cadastrado."
        return list_tenants(request, db, toast_message=f"❌ Erro ao criar login: {msg}")

    # 2. Perfil + vínculos N:N na mesma transação
    try:
        primary_tenant = valid_tenants[0].id if valid_tenants else None
        db.add(UserProfile(id=user_id, tenant_id=primary_tenant, name=name, role=role))
        for t in valid_tenants:
            db.add(UserTenant(user_id=user_id, tenant_id=t.id))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao vincular usuário %s às empresas", user_id)
        return list_tenants(request, db, toast_message="❌ Erro ao vincular o usuário às empresas.")

    vinculo = f"{len(valid_tenants)} empresa(s)" if valid_tenants else "acesso global"
    return list_tenants(request, db, toast_message=f"✅ {name} criado como {role} — {vinculo}.")


# ===== Edição e exclusão de usuários (somente admin — o router inteiro exige) =====

def _validar_tenants(db: Session, tenant_ids: List[str]):
    """Converte e valida os IDs recebidos contra as empresas existentes."""
    requested = {t for t in tenant_ids if t}
    if not requested:
        return []
    try:
        requested_uuids = {uuid.UUID(t) for t in requested}
    except ValueError:
        raise ValueError("Empresa inválida na seleção.")
    encontrados = db.query(Tenant).filter(Tenant.id.in_(requested_uuids)).all()
    if len(encontrados) != len(requested_uuids):
        raise ValueError("Uma das empresas selecionadas não existe mais.")
    return encontrados


def _outros_admins(db: Session, excluindo_id) -> int:
    """Quantos admins restariam além deste usuário."""
    return db.query(UserProfile).filter(
        UserProfile.role == 'admin', UserProfile.id != excluindo_id
    ).count()


# Validação propositalmente permissiva: quem decide se o endereço existe é o
# Supabase. Aqui só barramos o que é obviamente inválido antes de gastar a chamada.
_RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def _email_do_usuario(uid) -> str | None:
    """E-mail de login, lido do Supabase Auth. None se não for possível obter."""
    try:
        resp = get_supabase_admin_client().auth.admin.get_user_by_id(str(uid))
        return getattr(resp.user, "email", None)
    except Exception:
        logger.warning("Não foi possível obter o e-mail do usuário %s", uid, exc_info=True)
        return None


@router.get("/modals/user/edit/{user_id}", response_class=HTMLResponse)
def modal_user_edit(request: Request, user_id: str, db: Session = Depends(get_db)):
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        return HTMLResponse("<div class='alert alert-error'>Usuário inválido.</div>", status_code=400)

    profile = db.query(UserProfile).filter(UserProfile.id == uid).first()
    if not profile:
        return HTMLResponse("<div class='alert alert-error'>Usuário não encontrado.</div>", status_code=404)

    tenants = db.query(Tenant).order_by(Tenant.name).all()
    vinculados = {str(t.id) for t in (profile.allowed_tenants or [])}
    if profile.tenant_id:
        vinculados.add(str(profile.tenant_id))

    # O e-mail é a identidade de login e vive no Supabase Auth, não em
    # user_profiles. Se o provedor estiver fora do ar, o modal ainda abre —
    # apenas sem permitir editar o e-mail.
    email_atual = _email_do_usuario(uid)

    return templates.TemplateResponse(request, "partials/admin/modals/user_edit.html", {
        "request": request,
        "perfil": profile,
        "tenants": tenants,
        "vinculados": vinculados,
        "email_atual": email_atual,
        "ultimo_admin": profile.role == 'admin' and _outros_admins(db, uid) == 0,
    })


@router.post("/users/{user_id}/edit", response_class=HTMLResponse)
def edit_user(
    request: Request,
    user_id: str,
    name: str = Form(...),
    role: str = Form("cliente"),
    email: str = Form(""),
    tenant_ids: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
    admin_profile: UserProfile = Depends(require_admin),
):
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        return list_tenants(request, db, toast_message="❌ Usuário inválido.")

    profile = db.query(UserProfile).filter(UserProfile.id == uid).first()
    if not profile:
        return list_tenants(request, db, toast_message="❌ Usuário não encontrado.")

    if role not in ('cliente', 'operador', 'admin'):
        return list_tenants(request, db, toast_message="❌ Nível de acesso inválido.")

    # Trava: rebaixar o último admin deixaria o sistema sem ninguém para
    # administrar — e o painel exige admin, então não haveria como reverter.
    if profile.role == 'admin' and role != 'admin' and _outros_admins(db, uid) == 0:
        return list_tenants(request, db, toast_message="❌ Este é o único administrador. Promova outro antes de alterar o nível dele.")

    try:
        valid_tenants = _validar_tenants(db, tenant_ids)
    except ValueError as err:
        return list_tenants(request, db, toast_message=f"❌ {err}")

    if role != 'admin' and not valid_tenants:
        return list_tenants(request, db, toast_message="❌ Selecione ao menos uma empresa para este usuário.")

    # E-mail é identidade de login: trocamos ANTES do commit local, porque é a
    # operação que pode falhar (formato inválido, e-mail já em uso). Se falhar,
    # nada local é alterado.
    novo_email = (email or "").strip().lower()
    email_alterado = False
    if novo_email:
        if not _RE_EMAIL.match(novo_email):
            return list_tenants(request, db, toast_message="❌ E-mail inválido.")
        atual = _email_do_usuario(uid)
        if atual is None:
            return list_tenants(request, db, toast_message="❌ Não foi possível consultar o e-mail atual. Tente novamente.")
        if novo_email != atual.strip().lower():
            try:
                get_supabase_admin_client().auth.admin.update_user_by_id(
                    str(uid), {"email": novo_email, "email_confirm": True}
                )
                email_alterado = True
            except Exception as err:
                logger.exception("Falha ao alterar e-mail do usuário %s", user_id)
                msg = str(err)
                if "already" in msg.lower() or "registered" in msg.lower() or "exists" in msg.lower():
                    return list_tenants(request, db, toast_message="❌ Este e-mail já está em uso por outra conta.")
                return list_tenants(request, db, toast_message="❌ Não foi possível alterar o e-mail.")

    try:
        profile.name = name
        profile.role = role
        profile.tenant_id = valid_tenants[0].id if valid_tenants else None

        # Revínculo completo: apaga os antigos e grava a seleção atual.
        db.query(UserTenant).filter(UserTenant.user_id == uid).delete(synchronize_session=False)
        for t in valid_tenants:
            db.add(UserTenant(user_id=uid, tenant_id=t.id))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao editar usuário %s", user_id)
        return list_tenants(request, db, toast_message="❌ Erro ao salvar as alterações.")

    sufixo = " · e-mail de acesso alterado" if email_alterado else ""
    return list_tenants(request, db, toast_message=f"✅ {name} atualizado ({role}){sufixo}.")


@router.post("/users/{user_id}/reset-password", response_class=HTMLResponse)
def send_password_reset(
    request: Request,
    user_id: str,
    db: Session = Depends(get_db),
    admin_profile: UserProfile = Depends(require_admin),
    _rl = Depends(limitar("admin_reset_pwd", maximo=20, janela_seg=600)),
):
    """Dispara o e-mail de redefinição de senha para o usuário.

    O admin nunca vê nem define a senha: o link vai para a caixa do próprio
    usuário, que escolhe a nova senha na tela de redefinição.
    """
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        return list_tenants(request, db, toast_message="❌ Usuário inválido.")

    if not db.query(UserProfile).filter(UserProfile.id == uid).first():
        return list_tenants(request, db, toast_message="❌ Usuário não encontrado.")

    destino = _email_do_usuario(uid)
    if not destino:
        return list_tenants(request, db, toast_message="❌ Não foi possível obter o e-mail deste usuário.")

    redirect_to = str(request.base_url).rstrip("/") + "/reset-password"
    try:
        get_supabase_client().auth.reset_password_email(destino, {"redirect_to": redirect_to})
    except Exception:
        logger.exception("Falha ao enviar redefinição de senha para o usuário %s", user_id)
        return list_tenants(request, db, toast_message="❌ Não foi possível enviar o e-mail de redefinição.")

    return list_tenants(request, db, toast_message=f"📧 Link de redefinição enviado para {destino}.")


@router.post("/users/{user_id}/delete", response_class=HTMLResponse)
def delete_user(
    request: Request,
    user_id: str,
    db: Session = Depends(get_db),
    admin_profile: UserProfile = Depends(require_admin),
):
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        return list_tenants(request, db, toast_message="❌ Usuário inválido.")

    profile = db.query(UserProfile).filter(UserProfile.id == uid).first()
    if not profile:
        return list_tenants(request, db, toast_message="❌ Usuário não encontrado.")

    # Trava 1: excluir a si mesmo derrubaria a própria sessão no meio da operação.
    if str(admin_profile.id) == str(uid):
        return list_tenants(request, db, toast_message="❌ Você não pode excluir a própria conta.")

    # Trava 2: sem nenhum admin, ninguém acessa o painel — e não há como recriar
    # um admin pela interface, porque a interface exige ser admin.
    if profile.role == 'admin' and _outros_admins(db, uid) == 0:
        return list_tenants(request, db, toast_message="❌ Este é o único administrador. Promova outro antes de excluí-lo.")

    nome = profile.name or "Usuário"
    try:
        supabase_admin = get_supabase_admin_client()
    except ValueError as ve:
        return list_tenants(request, db, toast_message=f"❌ Erro: {ve}")

    try:
        # Apaga o local primeiro (ainda reversível: sem commit) e só então o
        # login no Supabase, que é irreversível. Se o Supabase falhar, o
        # rollback devolve o perfil e nada fica inconsistente.
        db.query(UserTenant).filter(UserTenant.user_id == uid).delete(synchronize_session=False)
        db.delete(profile)
        db.flush()
        supabase_admin.auth.admin.delete_user(str(uid))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao excluir usuário %s", user_id)
        return list_tenants(request, db, toast_message="❌ Erro ao excluir o usuário. Nada foi alterado.")

    return list_tenants(request, db, toast_message=f"🗑️ {nome} foi excluído do sistema.")


@router.get("/certificados", response_class=HTMLResponse)
def listar_certificados(request: Request, db: Session = Depends(get_db),
                        toast_message: str = None, q: str = "", situacao: str = "",
                        page: int = 1, per_page: int = 25):
    """Mapeamento de certificados A1 por empresa (somente admin — router inteiro)."""
    import math
    from sqlalchemy import or_ as _or
    from ...services.nfse import certificados as svc
    from ...domain.models.certificates import TenantCertificate

    consulta = db.query(Tenant)
    if q:
        alvo = f"%{q}%"
        consulta = consulta.filter(_or(Tenant.name.ilike(alvo), Tenant.cnpj.ilike(alvo)))
    tenants_filtrados = consulta.order_by(Tenant.name).all()

    # Carga única: um SELECT para os certificados e uma listagem da pasta.
    # Antes, cada empresa disparava a própria consulta — 167 idas de ~175ms.
    cadastrados = svc.carregar_todos(db)
    arquivos = svc.listar_arquivos()

    # Nomes de arquivo que embutem a senha não podem ser exibidos na íntegra:
    # a tela vazaria a credencial que estamos protegendo no banco.
    def _seguro(nome: str) -> str:
        return re.sub(r"(senha)[\s_-]*([A-Za-z0-9]+)", r"\1 ****", nome, flags=re.I)

    todas = []
    for t in tenants_filtrados:
        cnpj = re.sub(r"\D", "", t.cnpj or "")
        cert = cadastrados.get(str(t.id))
        sugestao = svc.localizar_por_cnpj(cnpj, arquivos) if len(cnpj) == 14 else None
        diag = (svc.diagnosticar(cert, cnpj, arquivos) if len(cnpj) == 14
                else {"ok": False, "motivo": "Empresa sem CNPJ"})
        nome_arquivo = (cert.arquivo if cert else None) or sugestao
        todas.append({
            "tenant": t,
            "cnpj": cnpj,
            "cert": cert,
            "arquivo": nome_arquivo,
            "rotulo": _seguro(nome_arquivo) if nome_arquivo else "",
            "sugerido": bool(not (cert and cert.arquivo) and sugestao),
            "diag": diag,
            "validade": svc.classificar_validade(cert, diag),
        })

    # O resumo conta todas as empresas da busca, ANTES do filtro de situação:
    # um painel de vencimento que some quando você filtra não vigia nada.
    resumo = {"ok": 0, "alerta": 0, "problema": 0, "desconhecido": 0}
    for linha in todas:
        resumo[linha["validade"]["grupo"]] += 1

    filtros = {
        "pendentes": lambda l: not l["diag"]["ok"],
        "prontos": lambda l: l["diag"]["ok"],
        "alerta": lambda l: l["validade"]["grupo"] == "alerta",
        "problema": lambda l: l["validade"]["grupo"] == "problema",
        "desconhecido": lambda l: l["validade"]["grupo"] == "desconhecido",
    }
    if situacao in filtros:
        todas = [l for l in todas if filtros[situacao](l)]

    total = len(todas)
    per_page = min(max(per_page, 10), 200)
    total_pages = math.ceil(total / per_page) if total else 1
    page = min(max(page, 1), total_pages)
    linhas = todas[(page - 1) * per_page: page * per_page]
    prontos = sum(1 for l in todas if l["diag"]["ok"])

    return templates.TemplateResponse(request, "partials/admin/certificados_list.html", {
        "request": request,
        "linhas": linhas,
        "arquivos": [{"nome": a, "rotulo": _seguro(a), "sensivel": bool(re.search(r"senha", a, re.I))}
                     for a in sorted(arquivos)],
        "pasta": svc.pasta(),
        "pasta_ok": bool(arquivos),
        "toast_message": toast_message,
        "q": q, "situacao": situacao,
        "page": page, "total_pages": total_pages, "total": total,
        "per_page": per_page, "prontos": prontos,
        "resumo": resumo, "dias_alerta": svc.DIAS_ALERTA,
    })


# Precisa vir ANTES de /certificados/{tenant_id}: o FastAPI casa as rotas na
# ordem de registro, e a rota dinâmica engoliria "validades" como se fosse um id.
@router.post("/certificados/validades", response_class=HTMLResponse)
def atualizar_validades(request: Request, db: Session = Depends(get_db),
                        _rl=Depends(limitar("cert_validades", maximo=4, janela_seg=900))):
    """Relê todos os .pfx e grava a data de expiração de cada um.

    Leva ~36 s para os 164 certificados. Por isso é uma ação explícita, e não
    algo feito a cada abertura da tela: depois disso a listagem lê a data do
    banco, de graça.
    """
    from ...services.nfse import certificados as svc

    try:
        r = svc.atualizar_validades(db)
    except Exception:
        db.rollback()
        logger.exception("Falha ao atualizar validades dos certificados")
        return listar_certificados(
            request, db, toast_message="❌ Erro ao ler os certificados.")

    partes = [f"{r['lidos']} certificado(s) lido(s)"]
    if r["atualizados"]:
        partes.append(f"{r['atualizados']} com validade nova")
    if r["nao_abriram"]:
        partes.append(f"{r['nao_abriram']} não abriram (senha)")
    if r["sem_arquivo"]:
        partes.append(f"{r['sem_arquivo']} sem arquivo na pasta")
    return listar_certificados(request, db, toast_message="🔎 " + " · ".join(partes))


@router.post("/certificados/{tenant_id}", response_class=HTMLResponse)
def salvar_certificado(request: Request, tenant_id: str,
                       arquivo: str = Form(""), senha: str = Form(""),
                       ativo: str = Form(""), db: Session = Depends(get_db)):
    """Vincula arquivo e/ou define a senha. Senha vazia mantém a atual."""
    from ...core.security import encrypt_data
    from ...domain.models.certificates import TenantCertificate
    from ...services.nfse import certificados as svc

    try:
        uid = uuid.UUID(tenant_id)
    except ValueError:
        return listar_certificados(request, db, toast_message="❌ Empresa inválida.")

    empresa = db.query(Tenant).filter(Tenant.id == uid).first()
    if not empresa:
        return listar_certificados(request, db, toast_message="❌ Empresa não encontrada.")

    cnpj = re.sub(r"\D", "", empresa.cnpj or "")
    if len(cnpj) != 14:
        return listar_certificados(request, db, toast_message="❌ A empresa precisa ter CNPJ válido.")

    cert = db.query(TenantCertificate).filter_by(
        tenant_id=str(uid), cnpj=cnpj).first()

    if not cert and not senha:
        return listar_certificados(
            request, db, toast_message="❌ Informe a senha para cadastrar o certificado.")

    # Só o nome do arquivo é aceito, nunca um caminho — e ele precisa existir de
    # fato na pasta: gravar um nome inexistente produziria uma falha silenciosa
    # só na hora da consulta ao ADN. Validado ANTES de mexer em qualquer campo,
    # para que uma recusa não deixe alteração pendente na sessão.
    nome = os.path.basename(arquivo) if arquivo else ""
    if nome and nome not in svc.listar_arquivos():
        return listar_certificados(
            request, db, toast_message=f"❌ Arquivo '{nome}' não está na pasta.")

    try:
        if not cert:
            cert = TenantCertificate(tenant_id=str(uid), cnpj=cnpj,
                                     razao_social=empresa.name,
                                     senha_encrypted=encrypt_data(senha))
            db.add(cert)
        elif senha:
            cert.senha_encrypted = encrypt_data(senha)

        if nome:
            cert.arquivo = nome
        cert.ativo = bool(ativo)
        cert.ultimo_erro = None
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao salvar certificado da empresa %s", tenant_id)
        return listar_certificados(request, db, toast_message="❌ Erro ao salvar o certificado.")

    return listar_certificados(
        request, db, toast_message=f"🔐 Certificado de {empresa.name} atualizado.")


@router.post("/certificados/{tenant_id}/testar", response_class=HTMLResponse)
def testar_certificado(request: Request, tenant_id: str, db: Session = Depends(get_db)):
    """Abre o .pfx com a senha guardada para confirmar que o par está correto."""
    from ...services.nfse import certificados as svc
    from ...domain.models.certificates import TenantCertificate
    from cryptography.hazmat.primitives.serialization import pkcs12

    empresa = db.query(Tenant).filter(Tenant.id == uuid.UUID(tenant_id)).first()
    cnpj = re.sub(r"\D", "", empresa.cnpj or "") if empresa else ""
    try:
        pfx, senha = svc.carregar_material(db, tenant_id, cnpj)
    except ValueError as err:
        return listar_certificados(request, db, toast_message=f"❌ {err}")

    try:
        chave, cert_x509, _ = pkcs12.load_key_and_certificates(pfx, senha.encode("utf-8"))
    except Exception:
        reg = db.query(TenantCertificate).filter_by(
            tenant_id=str(tenant_id), cnpj=cnpj).first()
        if reg:
            reg.ultimo_erro = "Senha não abre o certificado"
            db.commit()
        return listar_certificados(
            request, db, toast_message="❌ A senha não abre este certificado.")
    finally:
        pfx, senha = b"", ""

    reg = db.query(TenantCertificate).filter_by(tenant_id=str(tenant_id), cnpj=cnpj).first()
    validade = getattr(cert_x509, "not_valid_after_utc", None) or cert_x509.not_valid_after
    if reg:
        reg.validade = validade.replace(tzinfo=None)
        reg.ultimo_erro = None
        db.commit()

    return listar_certificados(
        request, db,
        toast_message=f"✅ Certificado válido até {validade:%d/%m/%Y}.")


@router.post("/tenants/{tenant_id}/delete", response_class=HTMLResponse)
def delete_tenant(request: Request, tenant_id: str, db: Session = Depends(get_db)):
    try:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if tenant:
            db.delete(tenant)
            db.commit()
            return list_tenants(request, db, toast_message="🗑️ Empresa excluída com sucesso!")
        return list_tenants(request, db, toast_message="❌ Empresa não encontrada.")
    except Exception:
        db.rollback()
        logger.exception("Falha ao excluir empresa %s", tenant_id)
        return list_tenants(request, db, toast_message="❌ Erro ao excluir empresa. Remova usuários e regras antes.")

@router.post("/tenants/{tenant_id}/toggle-status", response_class=HTMLResponse)
def toggle_tenant_status(request: Request, tenant_id: str, db: Session = Depends(get_db)):
    """Ativa/inativa a empresa. Reversível — ao contrário de excluir, não perde
    nenhum dado nem exige recriar usuários e regras quando o cliente volta."""
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        return list_tenants(request, db, toast_message="❌ Empresa não encontrada.")

    try:
        tenant.ativo = not tenant.ativo
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Falha ao alternar status da empresa %s", tenant_id)
        return list_tenants(request, db, toast_message="❌ Erro ao atualizar o status da empresa.")

    msg = (f"✅ {tenant.name} está ativa novamente." if tenant.ativo
           else f"🚫 {tenant.name} foi inativada — deixa de aparecer para operadores e clientes.")
    return list_tenants(request, db, toast_message=msg)


@router.get("/modals/tenant/edit/{tenant_id}", response_class=HTMLResponse)
def get_modal_edit_tenant(request: Request, tenant_id: str, db: Session = Depends(get_db)):
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        return HTMLResponse("<div class='alert alert-error'>Empresa não encontrada.</div>")
    return templates.TemplateResponse(request, "partials/admin/modals/tenant_edit.html", {"request": request, "tenant": tenant})

@router.post("/tenants/{tenant_id}/edit", response_class=HTMLResponse)
def edit_tenant(
    request: Request,
    tenant_id: str,
    name: str = Form(...),
    cnpj: str = Form(...),
    uf: str = Form(""),
    db: Session = Depends(get_db)
):
    try:
        uf_limpa = _uf_valida(uf)
        if not uf_limpa:
            return list_tenants(request, db, toast_message="❌ Informe a UF da empresa (2 letras).")
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if tenant:
            tenant.name = name
            tenant.cnpj = cnpj
            tenant.uf = uf_limpa
            db.commit()
            return list_tenants(request, db, toast_message="🏢 Empresa atualizada com sucesso!")
        return list_tenants(request, db, toast_message="❌ Empresa não encontrada.")
    except Exception as e:
        db.rollback()
        return list_tenants(request, db, toast_message="❌ Erro ao atualizar empresa.")

_RE_NAO_DIGITO = re.compile(r"\D")

# O seletor de status manda o rótulo da tela; o banco guarda o valor do
# maker-checker. "Pendente" e "pendente_revisao" são a MESMA coisa, e a
# tradução precisa existir: o código fazia `status.lower()` seco, então filtrar
# por Pendente no quadro de CFOP procurava por "pendente" — que não existe em
# linha nenhuma — e devolvia lista vazia sempre, como se não houvesse regra
# aguardando aprovação. O quadro de CT-e escapava por mandar o valor cru.
_STATUS_DE_REGRA = {
    "aprovado": "aprovado",
    "pendente": "pendente_revisao",
    "pendente_revisao": "pendente_revisao",
}


def _status_alvo(status: str):
    """Status como está gravado no banco, ou None quando não há filtro."""
    bruto = (status or "").strip().lower()
    if not bruto or bruto == "todos":
        return None
    return _STATUS_DE_REGRA.get(bruto, bruto)


def _aplicar_busca(query, termo: str, colunas, colunas_de_digitos=()):
    """Filtra a listagem de regras por um texto livre, em várias colunas de uma vez.

    A busca é feita NO BANCO, não no navegador. As seis tabelas de regra são
    paginadas de 10 em 10 no servidor: um filtro em JavaScript enxergaria só as
    dez linhas da página aberta e diria "nada encontrado" sobre uma regra que
    existe na página 40 — é o mesmo defeito que a exportação do Relatório de NFs
    tinha, gerando planilha só com a página visível.

    `colunas_de_digitos` recebe os campos que guardam só números (CNPJ). Quem
    procura digita "36.318.376/0001-96"; o banco tem "36318376000196". Sem
    normalizar, a busca mais natural do usuário é justamente a que não acha nada.
    """
    termo = (termo or "").strip()
    if not termo:
        return query

    alvo = f"%{termo}%"
    condicoes = [coluna.ilike(alvo) for coluna in colunas]

    so_digitos = _RE_NAO_DIGITO.sub("", termo)
    if len(so_digitos) >= 3 and so_digitos != termo:
        alvo_digitos = f"%{so_digitos}%"
        condicoes += [coluna.ilike(alvo_digitos) for coluna in colunas_de_digitos]

    return query.filter(or_(*condicoes))


@router.get("/rules", response_class=HTMLResponse)
def list_tenant_rules(
    request: Request,
    tenant_id: str = None,
    page: int = 1,
    db: Session = Depends(get_db),
    toast_message: str = None
):
    tenants = db.query(Tenant).order_by(Tenant.name).all()
    
    if not tenant_id and tenants:
        tenant_id = str(tenants[0].id)
    
    return templates.TemplateResponse(request, "partials/admin/rules_list.html", {
        "request": request,
        "tenant_id": tenant_id,
        "tenants": tenants,
        "toast_message": toast_message
    })

@router.get("/rules/cfop", response_class=HTMLResponse)
def list_cfop_rules(request: Request, tenant_id: str, status: str = 'Todos', page: int = 1, per_page: int = 10, busca: str = "", db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    _validate_tenant_access(current_user, tenant_id, db)
    per_page = max(1, min(per_page, 100))
    query = db.query(CfopClassification).filter(CfopClassification.tenant_id == tenant_id)
    alvo_status = _status_alvo(status)
    if alvo_status:
        query = query.filter(CfopClassification.status == alvo_status)
    query = _aplicar_busca(
        query, busca,
        colunas=(CfopClassification.product_description, CfopClassification.product_code,
                 CfopClassification.supplier_cfop, CfopClassification.entry_cfop,
                 CfopClassification.supplier_cnpj, CfopClassification.company_cnpj),
        colunas_de_digitos=(CfopClassification.supplier_cnpj,
                            CfopClassification.company_cnpj))
    total = query.count()
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    rules = query.offset((page - 1) * per_page).limit(per_page).all()
    return templates.TemplateResponse(request, "partials/admin/tables/cfop.html", {
        "request": request, "tenant_id": tenant_id, "cfop_rules": rules,
        "page": page, "per_page": per_page, "total_pages": total_pages, "total": total,
        "status": status, "busca": busca
    })

@router.get("/rules/cte", response_class=HTMLResponse)
def list_cte_rules(request: Request, tenant_id: str, status: str = 'Todos', page: int = 1, per_page: int = 10, busca: str = "", db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    _validate_tenant_access(current_user, tenant_id, db)
    per_page = max(1, min(per_page, 100))
    # Comparação direta, igual às outras cinco listagens: a coluna é
    # String(36) no model e guarda o UUID em texto. O `cast(..., UUID)`
    # que havia aqui era o único do arquivo e devolvia zero linha.
    query = db.query(CteCfopClassification).filter(CteCfopClassification.tenant_id == tenant_id)
    alvo_status = _status_alvo(status)
    if alvo_status:
        query = query.filter(CteCfopClassification.status == alvo_status)
    query = _aplicar_busca(
        query, busca,
        colunas=(CteCfopClassification.supplier_cfop, CteCfopClassification.entry_cfop,
                 CteCfopClassification.emitter_uf, CteCfopClassification.company_cnpj),
        colunas_de_digitos=(CteCfopClassification.company_cnpj,))
    total = query.count()
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    rules = query.order_by(CteCfopClassification.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    return templates.TemplateResponse(request, "partials/admin/tables/cte.html", {
        "request": request, "tenant_id": tenant_id, "cte_rules": rules,
        "page": page, "per_page": per_page, "total_pages": total_pages, "total": total,
        "status": status, "busca": busca
    })

@router.get("/rules/icms", response_class=HTMLResponse)
def list_icms_rules(request: Request, tenant_id: str, status: str = 'Todos', page: int = 1, per_page: int = 10, busca: str = "", db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    _validate_tenant_access(current_user, tenant_id, db)
    per_page = max(1, min(per_page, 100))
    query = db.query(CfopCreditRule).filter(CfopCreditRule.tenant_id == tenant_id)
    alvo_status = _status_alvo(status)
    if alvo_status:
        query = query.filter(CfopCreditRule.status == alvo_status)
    query = _aplicar_busca(query, busca,
                           colunas=(CfopCreditRule.cfop, CfopCreditRule.description))
    total = query.count()
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    rules = query.offset((page - 1) * per_page).limit(per_page).all()
    return templates.TemplateResponse(request, "partials/admin/tables/icms.html", {
        "request": request, "tenant_id": tenant_id, "credit_rules": rules,
        "page": page, "per_page": per_page, "total_pages": total_pages, "total": total,
        "status": status, "busca": busca
    })

@router.get("/rules/ipi", response_class=HTMLResponse)
def list_ipi_rules(request: Request, tenant_id: str, status: str = 'Todos', page: int = 1, per_page: int = 10, busca: str = "", db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    _validate_tenant_access(current_user, tenant_id, db)
    per_page = max(1, min(per_page, 100))
    query = db.query(IpiCreditRule).filter(IpiCreditRule.tenant_id == tenant_id)
    alvo_status = _status_alvo(status)
    if alvo_status:
        query = query.filter(IpiCreditRule.status == alvo_status)
    query = _aplicar_busca(query, busca,
                           colunas=(IpiCreditRule.cfop, IpiCreditRule.cst_expected,
                                    IpiCreditRule.description))
    total = query.count()
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    rules = query.offset((page - 1) * per_page).limit(per_page).all()
    return templates.TemplateResponse(request, "partials/admin/tables/ipi.html", {
        "request": request, "tenant_id": tenant_id, "ipi_rules": rules,
        "page": page, "per_page": per_page, "total_pages": total_pages, "total": total,
        "status": status, "busca": busca
    })

@router.get("/rules/pis_cofins", response_class=HTMLResponse)
def list_pis_cofins_rules(request: Request, tenant_id: str, status: str = 'Todos', page: int = 1, per_page: int = 10, busca: str = "", db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    _validate_tenant_access(current_user, tenant_id, db)
    per_page = max(1, min(per_page, 100))
    query = db.query(PisCofinsCreditRule).filter(PisCofinsCreditRule.tenant_id == tenant_id)
    alvo_status = _status_alvo(status)
    if alvo_status:
        query = query.filter(PisCofinsCreditRule.status == alvo_status)
    query = _aplicar_busca(query, busca,
                           colunas=(PisCofinsCreditRule.cfop, PisCofinsCreditRule.cst_expected,
                                    PisCofinsCreditRule.description))
    total = query.count()
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    rules = query.offset((page - 1) * per_page).limit(per_page).all()
    return templates.TemplateResponse(request, "partials/admin/tables/pis_cofins.html", {
        "request": request, "tenant_id": tenant_id, "pis_cofins_rules": rules,
        "page": page, "per_page": per_page, "total_pages": total_pages, "total": total,
        "status": status, "busca": busca
    })

@router.get("/rules/anticipation", response_class=HTMLResponse)
def list_anticipation_rules(request: Request, tenant_id: str, status: str = 'Todos', page: int = 1, per_page: int = 10, busca: str = "", db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    _validate_tenant_access(current_user, tenant_id, db)
    per_page = max(1, min(per_page, 100))
    query = db.query(IcmsAnticipationRule).filter(IcmsAnticipationRule.tenant_id == tenant_id)
    alvo_status = _status_alvo(status)
    if alvo_status:
        query = query.filter(IcmsAnticipationRule.status == alvo_status)
    query = _aplicar_busca(query, busca,
                           colunas=(IcmsAnticipationRule.cfop, IcmsAnticipationRule.uf_origem,
                                    IcmsAnticipationRule.uf_destino,
                                    IcmsAnticipationRule.description))
    total = query.count()
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    rules = query.offset((page - 1) * per_page).limit(per_page).all()
    return templates.TemplateResponse(request, "partials/admin/tables/anticipation.html", {
        "request": request, "tenant_id": tenant_id, "anticipation_rules": rules,
        "page": page, "per_page": per_page, "total_pages": total_pages, "total": total,
        "status": status, "busca": busca
    })

@router.patch("/rules/{rule_type}/toggle", response_class=HTMLResponse)
def toggle_rule(
    request: Request,
    rule_type: str,
    cfop: str = Form(None),
    cst_expected: str = Form(None),
    tenant_id: str = Form(...),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    _validate_tenant_access(current_user, tenant_id, db)
    try:
        rule = None
        if rule_type == 'icms':
            rule = db.query(CfopCreditRule).filter_by(cfop=cfop, tenant_id=tenant_id).first()
        elif rule_type == 'ipi':
            rule = db.query(IpiCreditRule).filter_by(cfop=cfop, cst_expected=cst_expected, tenant_id=tenant_id).first()
        elif rule_type == 'pis_cofins':
            rule = db.query(PisCofinsCreditRule).filter_by(cfop=cfop, cst_expected=cst_expected, tenant_id=tenant_id).first()
            
        if rule:
            rule.allow_credit = not rule.allow_credit
            db.commit()
            
        return HTMLResponse(status_code=200)
    except Exception:
        db.rollback()
        logger.exception("Falha ao alternar regra tipo=%s tenant=%s", rule_type, tenant_id)
        return HTMLResponse(status_code=500, content="Não foi possível atualizar a regra.")

@router.post("/rules/{rule_type}/approve", response_class=HTMLResponse)
def approve_rule(
    request: Request,
    rule_type: str,
    cfop: str = Form(None),
    tenant_id: str = Form(...),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    _validate_tenant_access(current_user, tenant_id, db)
    try:
        rule = None
        if rule_type == 'icms':
            rule = db.query(CfopCreditRule).filter_by(cfop=cfop, tenant_id=tenant_id).first()
        elif rule_type == 'cfop':
            rule = db.query(CfopClassification).filter_by(id=cfop, tenant_id=tenant_id).first()
        if rule:
            rule.status = 'aprovado'
            db.commit()
        return Response(headers={"HX-Trigger": f"ruleAdded-{rule_type}"})
    except Exception:
        db.rollback()
        logger.exception("Falha ao aprovar regra tipo=%s tenant=%s", rule_type, tenant_id)
        return HTMLResponse(status_code=500, content="Não foi possível aprovar a regra.")


@router.post("/rules/{rule_type}/batch-approve", response_class=HTMLResponse)
def batch_approve_rule(
    request: Request,
    rule_type: str,
    rule_keys: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user = Depends(require_admin)
):
    try:
        if not rule_keys:
            return Response(headers={"HX-Trigger": "rulesBatchApproved"})
            
        for key_str in rule_keys:
            parts = key_str.split("|")
            if not parts:
                continue
            
            tenant_id = parts[0]
            rule = None
            if rule_type == 'icms':
                rule = db.query(CfopCreditRule).filter_by(cfop=parts[1], tenant_id=tenant_id).first()
            elif rule_type == 'cfop':
                # No schema, CfopClassification tem company_cnpj, supplier_cnpj, supplier_cfop, product_code, tenant_id
                rule = db.query(CfopClassification).filter_by(company_cnpj=parts[1], supplier_cnpj=parts[2], supplier_cfop=parts[3], product_code=parts[4], tenant_id=tenant_id).first()
            elif rule_type == 'pis_cofins':
                rule = db.query(PisCofinsCreditRule).filter_by(cfop=parts[1], cst_expected=parts[2], tenant_id=tenant_id).first()
            elif rule_type == 'ipi':
                rule = db.query(IpiCreditRule).filter_by(cfop=parts[1], cst_expected=parts[2], tenant_id=tenant_id).first()
            elif rule_type == 'cte':
                rule = db.query(CteCfopClassification).filter_by(company_cnpj=parts[1], supplier_cfop=parts[2], emitter_uf=parts[3], tenant_id=tenant_id).first()
            elif rule_type == 'anticipation':
                rule = db.query(IcmsAnticipationRule).filter_by(uf_origem=parts[1], uf_destino=parts[2], cfop=parts[3], tenant_id=tenant_id).first()
                
            if rule:
                rule.status = 'aprovado'
        
        db.commit()
        return Response(headers={"HX-Trigger": "rulesBatchApproved"})
    except Exception:
        db.rollback()
        logger.exception("Falha ao aprovar em lote tipo=%s", rule_type)
        return HTMLResponse(status_code=500, content="Não foi possível aprovar as regras selecionadas.")

@router.post("/rules/{rule_type}/add", response_class=HTMLResponse)
async def add_specific_rule(
    request: Request,
    rule_type: str,
    db: Session = Depends(get_db)
):
    try:
        form_data = await request.form()
        tenant_id = form_data.get("tenant_id")
        if not tenant_id:
            return HTMLResponse(status_code=400, content="<script>Swal.fire('Erro', 'Tenant ID é obrigatório', 'error')</script>")
            
        if rule_type == "cfop":
            # Chave exata de quatro campos. Aqui se gravava "TODOS" em três deles:
            # o motor consulta a chave completa e nunca acharia essa regra — ela
            # só enchia a lista dando impressão de trabalho feito.
            company_cnpj = (form_data.get("company_cnpj") or "").strip()
            supplier_cnpj = (form_data.get("supplier_cnpj") or "").strip()
            product_code = (form_data.get("product_code") or "").strip()
            campos = (company_cnpj, supplier_cnpj, product_code)
            if not all(campos) or any(c in ("*", "TODOS") for c in campos):
                return HTMLResponse(status_code=400, content=(
                    "<script>Swal.fire('Regra não gravada', "
                    "'Informe CNPJ da empresa, CNPJ do fornecedor e código do produto. "
                    "A regra de CFOP casa por chave exata — regra genérica nunca casaria com uma nota.', "
                    "'warning')</script>"
                ))
            new_rule = CfopClassification(
                tenant_id=tenant_id,
                company_cnpj=company_cnpj,
                supplier_cnpj=supplier_cnpj,
                supplier_cfop=form_data.get("cfop_origem", ""),
                product_code=product_code,
                entry_cfop=form_data.get("cfop_entrada", ""),
                product_description="Regra Manual",
                status="aprovado",
                is_global=False
            )
            db.add(new_rule)
        elif rule_type == "icms":
            new_rule = CfopCreditRule(
                tenant_id=tenant_id,
                cfop=form_data.get("cfop", ""),
                allow_credit=form_data.get("allow_credit") == "on",
                description=form_data.get("description", "Regra Manual ICMS"),
                status="aprovado",
                is_global=False
            )
            db.add(new_rule)
        elif rule_type == "ipi":
            # O motor consulta (CFOP, CST). CST vazio ou "*" é curinga: casaria
            # com nada ou, pior, valeria para todo CST daquele CFOP.
            cst_ipi = (form_data.get("cst_expected") or "").strip()
            if not cst_ipi or cst_ipi == "*":
                return HTMLResponse(status_code=400, content=(
                    "<script>Swal.fire('Regra não gravada', "
                    "'Informe o CST de IPI. A regra vale para um CFOP e um CST específicos.', "
                    "'warning')</script>"
                ))
            new_rule = IpiCreditRule(
                tenant_id=tenant_id,
                cfop=form_data.get("cfop", ""),
                cst_expected=cst_ipi,
                allow_credit=form_data.get("allow_credit") == "on",
                description="Regra Manual IPI",
                status="aprovado",
                is_global=False
            )
            db.add(new_rule)
        elif rule_type == "pis_cofins":
            new_rule = PisCofinsCreditRule(
                tenant_id=tenant_id,
                cfop=form_data.get("cfop", ""),
                cst_expected=form_data.get("cst_expected", ""),
                allow_credit=form_data.get("allow_credit") == "on",
                description="Regra Manual PIS/COFINS",
                status="aprovado",
                is_global=False
            )
            db.add(new_rule)
        elif rule_type == "anticipation":
            new_rule = IcmsAnticipationRule(
                tenant_id=tenant_id,
                uf_origem=form_data.get("uf_origem", ""),
                uf_destino=form_data.get("uf_destino", ""),
                cfop=form_data.get("cfop", ""),
                reducao_base_calculo_percentage=float(form_data.get("red_bc") or 0.0),
                mva_percentage=float(form_data.get("mva") or 0.0),
                aliquota_interna_percentage=float(form_data.get("aliq_int") or 0.0),
                aliquota_interestadual_percentage=float(form_data.get("aliq_inter") or 0.0),
                description="Regra Manual Antecipação",
                status="aprovado",
                is_global=False
            )
            db.add(new_rule)
        else:
            return HTMLResponse(status_code=400, content=f"<script>Swal.fire('Erro', 'Tipo de regra inválido: {rule_type}', 'error')</script>")
            
        db.commit()
        return Response(headers={"HX-Trigger": f"ruleAdded-{rule_type}"})
        
    except IntegrityError:
        db.rollback()
        return HTMLResponse(
            status_code=400, 
            content="<script>Swal.fire('Atenção', 'Já existe uma regra cadastrada com essas chaves.', 'warning')</script>"
        )
    except Exception:
        db.rollback()
        logger.exception("Falha ao salvar regra tipo=%s", rule_type)
        return HTMLResponse(
            status_code=500,
            content="<script>Swal.fire('Erro', 'Não foi possível salvar a regra.', 'error')</script>"
        )

@router.get("/modals/rule/edit", response_class=HTMLResponse)
def get_modal_edit_rule(
    request: Request, 
    tenant_id: str,
    rule_type: str,
    company_cnpj: str = "",
    supplier_cnpj: str = "",
    supplier_cfop: str = "",
    product_code: str = "",
    cfop: str = "",
    db: Session = Depends(get_db)
):
    rule = None
    if rule_type == "classification":
        rule = db.query(CfopClassification).filter_by(
            tenant_id=tenant_id, company_cnpj=company_cnpj,
            supplier_cnpj=supplier_cnpj, supplier_cfop=supplier_cfop,
            product_code=product_code
        ).first()
    elif rule_type == "credit":
        rule = db.query(CfopCreditRule).filter_by(
            tenant_id=tenant_id, cfop=cfop
        ).first()
        
    if not rule:
        return HTMLResponse("<div class='alert alert-error'>Regra não encontrada.</div>")
        
    return templates.TemplateResponse(request, "partials/admin/modals/rule_edit.html", {
        "request": request, 
        "tenant_id": tenant_id,
        "rule_type": rule_type,
        "rule": rule
    })

@router.post("/rules/edit", response_class=HTMLResponse)
def edit_rule(
    request: Request,
    tenant_id: str = Form(...),
    rule_type: str = Form(...),
    company_cnpj: str = Form(""),
    supplier_cnpj: str = Form(""),
    supplier_cfop: str = Form(""),
    product_code: str = Form(""),
    entry_cfop: str = Form(""),
    product_description: str = Form(""),
    cfop: str = Form(""),
    allow_credit: bool = Form(False),
    description: str = Form(""),
    db: Session = Depends(get_db)
):
    try:
        if rule_type == "classification":
            rule = db.query(CfopClassification).filter_by(
                tenant_id=tenant_id, company_cnpj=company_cnpj,
                supplier_cnpj=supplier_cnpj, supplier_cfop=supplier_cfop,
                product_code=product_code
            ).first()
            if rule:
                rule.entry_cfop = entry_cfop
                rule.product_description = product_description
        elif rule_type == "credit":
            rule = db.query(CfopCreditRule).filter_by(
                tenant_id=tenant_id, cfop=cfop
            ).first()
            if rule:
                rule.allow_credit = allow_credit
                rule.description = description
                
        db.commit()
        return list_tenant_rules(request, tenant_id, 1, db, toast_message="✅ Regra atualizada com sucesso!")
    except Exception:
        db.rollback()
        logger.exception("Falha ao atualizar regra tenant=%s", tenant_id)
        return list_tenant_rules(request, tenant_id, 1, db,
                                 toast_message="❌ Não foi possível atualizar a regra.")

@router.delete("/rules/{rule_type}/delete", response_class=HTMLResponse)
async def delete_specific_rule(
    rule_type: str,
    request: Request,
    tenant_id: str,
    cfop: str = None,
    cst_expected: str = None,
    company_cnpj: str = None,
    supplier_cnpj: str = None,
    supplier_cfop: str = None,
    product_code: str = None,
    emitter_uf: str = None,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    try:
        key = request.query_params.get('key')
        if key:
            parts = key.split("|")
            tenant_id = parts[0]
            if rule_type == 'icms' or rule_type == 'anticipation': cfop = parts[1]
            elif rule_type == 'cfop': company_cnpj, supplier_cnpj, supplier_cfop, product_code = parts[1:5]
            elif rule_type == 'pis_cofins' or rule_type == 'ipi': cfop, cst_expected = parts[1:3]
            elif rule_type == 'cte': company_cnpj, supplier_cfop, emitter_uf = parts[1:4]
            
        _validate_tenant_access(current_user, tenant_id, db)
        rule = None
        
        if rule_type == "classification" or rule_type == "cfop":
            rule = db.query(CfopClassification).filter_by(
                tenant_id=tenant_id, company_cnpj=company_cnpj, supplier_cnpj=supplier_cnpj, supplier_cfop=supplier_cfop, product_code=product_code
            ).first()
        elif rule_type == "cte":
            rule = db.query(CteCfopClassification).filter_by(
                tenant_id=tenant_id, company_cnpj=company_cnpj, supplier_cfop=supplier_cfop, emitter_uf=emitter_uf
            ).first()
        elif rule_type == "icms":
            rule = db.query(CfopCreditRule).filter_by(tenant_id=tenant_id, cfop=cfop).first()
        elif rule_type == "ipi":
            rule = db.query(IpiCreditRule).filter_by(tenant_id=tenant_id, cfop=cfop, cst_expected=cst_expected).first()
        elif rule_type == "pis_cofins":
            rule = db.query(PisCofinsCreditRule).filter_by(tenant_id=tenant_id, cfop=cfop, cst_expected=cst_expected).first()
        elif rule_type == "anticipation":
            rule = db.query(IcmsAnticipationRule).filter_by(tenant_id=tenant_id, cfop=cfop).first()
            
        if rule:
            db.delete(rule)
            db.commit()
            return HTMLResponse(status_code=200, content="")
        else:
            return HTMLResponse(status_code=404, content="Regra não encontrada.")
            
    except Exception:
        db.rollback()
        logger.exception("Falha ao excluir regra tipo=%s", rule_type)
        return HTMLResponse(status_code=500, content="<script>Swal.fire('Erro', 'Não foi possível excluir a regra.', 'error')</script>")

@router.get("/modals/cfop-siblings", response_class=HTMLResponse)
def get_cfop_siblings_modal(
    request: Request,
    tenant_id: str,
    supplier_cnpj: str,
    supplier_cfop: str,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Abre modal listando todas as regras CFOP 'irmãs' (mesmo fornecedor + CFOP de origem)
    para que o admin selecione quais deseja excluir."""
    _require_admin(current_user, db)
    siblings = db.query(CfopClassification).filter_by(
        tenant_id=tenant_id,
        supplier_cnpj=supplier_cnpj,
        supplier_cfop=supplier_cfop
    ).order_by(CfopClassification.product_code).all()

    return templates.TemplateResponse(request, "partials/admin/modals/cfop_siblings.html", {
        "request": request,
        "siblings": siblings,
        "tenant_id": tenant_id,
        "supplier_cnpj": supplier_cnpj,
        "supplier_cfop": supplier_cfop,
        "total": len(siblings)
    })

@router.post("/rules/cfop/batch-delete", response_class=HTMLResponse)
def batch_delete_cfop_rules(
    request: Request,
    tenant_id: str = Form(...),
    rule_keys: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Exclui as regras CFOP selecionadas pelo admin via checkboxes do modal de irmãs.
    Cada rule_key tem formato: company_cnpj|supplier_cnpj|supplier_cfop|product_code"""
    _require_admin(current_user, db)
    deleted = 0
    try:
        for key in rule_keys:
            parts = key.split("|")
            if len(parts) != 4:
                continue
            company_cnpj, supplier_cnpj, supplier_cfop, product_code = parts
            rule = db.query(CfopClassification).filter_by(
                tenant_id=tenant_id,
                company_cnpj=company_cnpj,
                supplier_cnpj=supplier_cnpj,
                supplier_cfop=supplier_cfop,
                product_code=product_code
            ).first()
            if rule:
                db.delete(rule)
                deleted += 1
        db.commit()
        return list_tenant_rules(request, tenant_id, 1, db,
                                 toast_message=f"🗑️ {deleted} regra(s) de CFOP excluída(s) com sucesso!")
    except Exception:
        db.rollback()
        logger.exception("Falha ao excluir regras de CFOP tenant=%s", tenant_id)
        return list_tenant_rules(request, tenant_id, 1, db,
                                 toast_message="❌ Não foi possível excluir as regras.")

from sqlalchemy import cast, String

from ...services import painel_atividade


# =====================================================================
# Configurações do escritório
# =====================================================================
# RBAC: o `require_admin` do include_router em main.py cobre o /admin inteiro,
# e é o que basta — o que se edita aqui é a credencial do cofre fiscal, que dá
# acesso às notas de TODAS as empresas da carteira. Operador não entra.

@router.get("/configuracoes", response_class=HTMLResponse)
def tela_configuracoes(request: Request, db: Session = Depends(get_db)):
    from ...services import configuracoes as cfg

    email, chave = cfg.credenciais_sieg(db)
    return templates.TemplateResponse(
        request, "partials/admin/configuracoes.html", {
            "request": request,
            "sieg_email": email,
            # A chave NUNCA volta inteira para a tela: só a máscara, que serve
            # para conferir qual está gravada sem expô-la no HTML.
            "sieg_chave_mascara": cfg.mascarar(chave),
            "sieg_do_ambiente": bool(
                not cfg.ler(db, cfg.SIEG_API_KEY) and chave),
        })


@router.post("/configuracoes/sieg", response_class=HTMLResponse)
def salvar_sieg(request: Request, sieg_email: str = Form(""),
                sieg_api_key: str = Form(""),
                db: Session = Depends(get_db),
                profile=Depends(require_admin)):
    """Grava as credenciais do cofre fiscal.

    Campo de chave em BRANCO significa "não mexa na que está lá" — e não
    "apague". Sem isso, salvar só o e-mail zeraria a credencial, e o erro
    apareceria na próxima consulta ao SIEG, longe daqui.
    """
    from ...services import configuracoes as cfg

    autor = getattr(profile, "name", "") or str(getattr(profile, "id", ""))
    mudou = []

    email = (sieg_email or "").strip()
    if email != cfg.ler(db, cfg.SIEG_EMAIL):
        cfg.gravar(db, cfg.SIEG_EMAIL, email, autor)
        mudou.append("e-mail")

    chave = (sieg_api_key or "").strip()
    if chave:
        cfg.gravar(db, cfg.SIEG_API_KEY, chave, autor)
        mudou.append("chave de API")

    email_final, chave_final = cfg.credenciais_sieg(db)
    return templates.TemplateResponse(
        request, "partials/admin/configuracoes.html", {
            "request": request,
            "sieg_email": email_final,
            "sieg_chave_mascara": cfg.mascarar(chave_final),
            "sieg_do_ambiente": bool(
                not cfg.ler(db, cfg.SIEG_API_KEY) and chave_final),
            # Nada gravado não vira caixa verde: a resposta diz o que houve.
            "mensagem": ("Salvo: " + " e ".join(mudou) + "." if mudou
                         else "Nada foi alterado — os campos estão iguais ao "
                              "que já está gravado."),
            "sucesso": bool(mudou),
        })


@router.get("/erros", response_class=HTMLResponse)
def listar_erros(request: Request, db: Session = Depends(get_db)):
    """Os erros do sistema, dentro do Synax.

    Até aqui só existiam como arquivos em `logs/erros_ia/`, na máquina onde o
    servidor roda — o que não serve de outro computador e não existe num VPS.

    RBAC: o `require_admin` do include_router em main.py cobre o /admin inteiro.
    Erro de sistema traz nome de módulo, trecho de código e texto de exceção;
    nada disso é para operador nem para cliente.
    """
    return templates.TemplateResponse(request, "partials/admin/erros_list.html", {
        "request": request,
        "resumo": erros_sistema.resumo(),
        "erros": erros_sistema.listar(),
        "do_log": erros_sistema.erros_do_log(),
    })


@router.get("/erros/{arquivo}", response_class=HTMLResponse)
def ver_erro(request: Request, arquivo: str, db: Session = Depends(get_db)):
    """Um relatório inteiro, em modal.

    `arquivo` vem da URL. `erros_sistema.ler` devolve None para qualquer nome
    fora do padrão do relator — é o que impede um `..%5C..%5C.env` daqui virar
    leitura do arquivo de segredos.
    """
    dados = erros_sistema.ler(arquivo)
    if not dados:
        return HTMLResponse(
            "<div class='alert alert-error'>Relatório não encontrado.</div>",
            status_code=404)
    return templates.TemplateResponse(request, "partials/admin/erro_detalhe.html", {
        "request": request, "erro": dados,
    })


@router.get("/activity/painel", response_class=HTMLResponse)
def get_activity_panel(request: Request, dias: int = painel_atividade.DIAS_PADRAO,
                       db: Session = Depends(get_db)):
    """Painel do Radar: gráficos, uso por pessoa e o topo das empresas.

    O RBAC é o `require_admin` do include_router em main.py — o /admin inteiro
    é exclusivo de admin, e este painel cruza dados de TODAS as empresas, o que
    nenhum operador pode ver.
    """
    return templates.TemplateResponse(request, "partials/admin/activity_panel.html", {
        "request": request,
        "p": painel_atividade.montar_painel(db, dias),
    })


@router.get("/activity", response_class=HTMLResponse)
def get_activity_log(
    request: Request,
    status: str = None,
    search: str = None,
    page: int = 1,
    per_page: int = 10,
    db: Session = Depends(get_db)
):
    per_page = min(max(per_page, 1), 100)
    page = max(page, 1)

    query = db.query(AuditTask, Tenant, UserProfile).outerjoin(
        Tenant, cast(Tenant.id, String) == AuditTask.tenant_id
    ).outerjoin(
        UserProfile, cast(UserProfile.id, String) == AuditTask.user_id
    )
    
    if status and status != 'all':
        query = query.filter(AuditTask.status == status)
        
    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            (Tenant.name.ilike(search_filter)) | 
            (AuditTask.file_name.ilike(search_filter)) |
            (UserProfile.name.ilike(search_filter))
        )
        
    total_items = query.count()
    total_pages = math.ceil(total_items / per_page) if total_items > 0 else 1
    
    results = query.order_by(AuditTask.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    
    import json
    logs = []
    for task, tenant, user in results:
        parsed_result = {}
        if task.result_data:
            try:
                parsed_result = json.loads(task.result_data)
            except:
                parsed_result = {"raw": task.result_data}
                
        logs.append({
            "task": task,
            "parsed_result": parsed_result,
            "tenant_name": tenant.name if tenant else "Empresa Excluída",
            "user_name": user.name if user else "Sistema/API"
        })
        
    return templates.TemplateResponse(request, "partials/admin/activity_list.html", {
        "request": request,
        "logs": logs,
        "current_status": status or 'all',
        "current_search": search or '',
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total_items": total_items
    })


@router.get("/modals/tenant/import/{tenant_id}", response_class=HTMLResponse)
def get_modal_import_rules(request: Request, tenant_id: str, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    _validate_tenant_access(current_user, tenant_id, db)
    
    # Busca o tenant destino
    target_tenant = db.query(Tenant).filter_by(id=tenant_id).first()
    if not target_tenant:
        return HTMLResponse(status_code=404, content="Tenant não encontrado.")
        
    # Busca outros tenants para a lista
    other_tenants = db.query(Tenant).filter(Tenant.id != tenant_id).order_by(Tenant.name).all()
    
    return templates.TemplateResponse(request, "partials/admin/modals/modal_import_rules.html", {
        "request": request, 
        "target_tenant": target_tenant,
        "other_tenants": other_tenants
    })

@router.post("/tenants/{target_tenant_id}/import-rules", response_class=HTMLResponse)
async def import_tenant_rules(
    request: Request,
    target_tenant_id: str,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    try:
        _validate_tenant_access(current_user, target_tenant_id, db)
        
        form_data = await request.form()
        source_tenant_id = form_data.get("source_tenant_id")
        
        if not source_tenant_id:
            return HTMLResponse(status_code=400, content="<script>Swal.fire('Erro', 'Empresa de origem não selecionada', 'error')</script>")
            
        import_cfop = form_data.get("import_cfop") == "on"
        import_icms = form_data.get("import_icms") == "on"
        import_ipi = form_data.get("import_ipi") == "on"
        import_pis_cofins = form_data.get("import_pis_cofins") == "on"
        import_anticipation = form_data.get("import_anticipation") == "on"
        
        imported_count = 0
        
        if import_cfop:
            target_keys = { (r.company_cnpj, r.supplier_cnpj, r.supplier_cfop, r.product_code) for r in db.query(CfopClassification).filter_by(tenant_id=target_tenant_id).all() }
            source_rules = db.query(CfopClassification).filter_by(tenant_id=source_tenant_id).all()
            for r in source_rules:
                if (r.company_cnpj, r.supplier_cnpj, r.supplier_cfop, r.product_code) not in target_keys:
                    new_rule = CfopClassification(
                        tenant_id=target_tenant_id,
                        company_cnpj=r.company_cnpj, supplier_cnpj=r.supplier_cnpj, supplier_cfop=r.supplier_cfop, product_code=r.product_code,
                        product_description=r.product_description, entry_cfop=r.entry_cfop, status=r.status, is_global=r.is_global, created_by=r.created_by
                    )
                    db.add(new_rule)
                    imported_count += 1

        if import_icms:
            target_keys = { r.cfop for r in db.query(CfopCreditRule).filter_by(tenant_id=target_tenant_id).all() }
            source_rules = db.query(CfopCreditRule).filter_by(tenant_id=source_tenant_id).all()
            for r in source_rules:
                if r.cfop not in target_keys:
                    new_rule = CfopCreditRule(
                        tenant_id=target_tenant_id,
                        cfop=r.cfop, allow_credit=r.allow_credit, description=r.description, status=r.status, is_global=r.is_global, created_by=r.created_by
                    )
                    db.add(new_rule)
                    imported_count += 1
                    
        if import_ipi:
            target_keys = { (r.cfop, r.cst_expected) for r in db.query(IpiCreditRule).filter_by(tenant_id=target_tenant_id).all() }
            source_rules = db.query(IpiCreditRule).filter_by(tenant_id=source_tenant_id).all()
            for r in source_rules:
                if (r.cfop, r.cst_expected) not in target_keys:
                    new_rule = IpiCreditRule(
                        tenant_id=target_tenant_id,
                        cfop=r.cfop, cst_expected=r.cst_expected, allow_credit=r.allow_credit, description=r.description, status=r.status, is_global=r.is_global, created_by=r.created_by
                    )
                    db.add(new_rule)
                    imported_count += 1
                    
        if import_pis_cofins:
            target_keys = { (r.cfop, r.cst_expected) for r in db.query(PisCofinsCreditRule).filter_by(tenant_id=target_tenant_id).all() }
            source_rules = db.query(PisCofinsCreditRule).filter_by(tenant_id=source_tenant_id).all()
            for r in source_rules:
                if (r.cfop, r.cst_expected) not in target_keys:
                    new_rule = PisCofinsCreditRule(
                        tenant_id=target_tenant_id,
                        cfop=r.cfop, cst_expected=r.cst_expected, allow_credit=r.allow_credit, description=r.description, status=r.status, is_global=r.is_global, created_by=r.created_by
                    )
                    db.add(new_rule)
                    imported_count += 1
                    
        if import_anticipation:
            target_keys = { (r.uf_origem, r.uf_destino, r.cfop) for r in db.query(IcmsAnticipationRule).filter_by(tenant_id=target_tenant_id).all() }
            source_rules = db.query(IcmsAnticipationRule).filter_by(tenant_id=source_tenant_id).all()
            for r in source_rules:
                if (r.uf_origem, r.uf_destino, r.cfop) not in target_keys:
                    new_rule = IcmsAnticipationRule(
                        tenant_id=target_tenant_id,
                        uf_origem=r.uf_origem, uf_destino=r.uf_destino, cfop=r.cfop,
                        mva_percentage=r.mva_percentage, aliquota_interna_percentage=r.aliquota_interna_percentage,
                        aliquota_interestadual_percentage=r.aliquota_interestadual_percentage, reducao_base_calculo_percentage=r.reducao_base_calculo_percentage,
                        description=r.description, status=r.status, is_global=r.is_global, created_by=r.created_by
                    )
                    db.add(new_rule)
                    imported_count += 1
                    
        db.commit()
        return HTMLResponse(status_code=200, content=f"<script>Swal.fire('Sucesso!', '{imported_count} regra(s) importada(s) com sucesso.', 'success').then(() => {{ document.getElementById('modal-import-rules').close(); window.location.reload(); }});</script>")
        
    except Exception:
        db.rollback()
        logger.exception("Falha ao importar regras")
        return HTMLResponse(
            status_code=500,
            content="<script>Swal.fire('Erro', 'Não foi possível importar as regras.', 'error')</script>"
        )


@router.get("/email-generator", response_class=HTMLResponse)
def get_email_generator(request: Request, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    _require_admin(current_user, db)
    tenants = db.query(Tenant).order_by(Tenant.name).all()
    return templates.TemplateResponse(request, "partials/admin/email_generator.html", {
        "request": request,
        "tenants": tenants
    })


@router.post("/email-generator/generate", response_class=HTMLResponse)
def generate_client_email(
    request: Request,
    tenant_id: str = Form(""),
    recipient_name: str = Form("Responsável Fiscal / Contabilidade"),
    period: str = Form("Mês de Apuração"),
    include_icms: bool = Form(False),
    include_ipi: bool = Form(False),
    include_pis_cofins: bool = Form(False),
    include_missing_xmls: bool = Form(False),
    include_summary: bool = Form(True),
    tone: str = Form("formal"),
    custom_notes: str = Form(""),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    _require_admin(current_user, db)
    
    tenant_name = "Cliente"
    tenant_cnpj = ""
    if tenant_id:
        tenant = db.query(Tenant).filter_by(id=tenant_id).first()
        if tenant:
            tenant_name = tenant.name
            tenant_cnpj = tenant.cnpj or ""
            
    subject_parts = []
    if include_icms: subject_parts.append("ICMS")
    if include_ipi: subject_parts.append("IPI")
    if include_pis_cofins: subject_parts.append("PIS/COFINS")
    if include_missing_xmls: subject_parts.append("XMLs Faltantes")
    
    topics_str = " - ".join(subject_parts) if subject_parts else "Parecer de Auditoria"
    subject = f"[{tenant_name}] Synax - Auditoria Fiscal ({period}) - {topics_str}"
    
    # A prévia renderiza html_body com `| safe`: sem escape, um "<script>" no
    # nome do destinatário, no período ou nas observações executa no navegador
    # do admin. O texto plano (WhatsApp) é montado à parte, com os valores crus.
    from html import escape as _e, unescape as _unescape
    import re as _re
    nome_h, empresa_h, cnpj_h, periodo_h = _e(recipient_name), _e(tenant_name), _e(tenant_cnpj), _e(period)
    greeting = f"Prezado(a) {nome_h}," if tone == "formal" else f"Olá, equipe {nome_h}!"
    intro_formal = f"Esperamos que esteja bem. Realizamos a verificação eletrônica e o cruzamento fiscal do SPED C100/D100/C170 e arquivos XML de NF-e/CT-e da empresa <b>{empresa_h}</b> {f'({cnpj_h})' if cnpj_h else ''} referente ao período de <b>{periodo_h}</b>."
    intro_operacional = f"Seguem os apontamentos e divergências identificados na auditoria fiscal de <b>{empresa_h}</b> no período <b>{periodo_h}</b> para providências de correção."
    intro_orientativo = f"Como parte da nossa rotina de compliance e orientação tributária, cruzamos os dados escriturados na empresa <b>{empresa_h}</b> no período <b>{periodo_h}</b>. Abaixo compartilhamos os itens que demandam atenção na legislação."
    
    intro = intro_formal if tone == "formal" else (intro_operacional if tone == "operacional" else intro_orientativo)
    # Mesmas frases sem as tags e sem entidades, para a versão em texto plano.
    greeting_txt = _unescape(greeting)
    intro_txt = _unescape(_re.sub(r"<[^>]+>", "", intro))
    
    sections_html = []
    sections_text = []
    
    if include_summary:
        sections_html.append("""
        <div style="background-color: #f8fafc; border-left: 4px solid #3b82f6; padding: 12px 16px; margin: 16px 0; border-radius: 4px;">
            <h4 style="margin: 0 0 8px 0; color: #1e293b; font-size: 15px;">📊 Resumo da Verificação</h4>
            <p style="margin: 0; color: #475569; font-size: 14px;">O cruzamento eletrônico contemplou os itens de mercadorias, alíquotas e chaves de acesso. Pedimos especial atenção aos tópicos abaixo antes do fechamento definitivo da apuração.</p>
        </div>
        """)
        sections_text.append("[RESUMO DA VERIFICAÇÃO]\\nO cruzamento eletrônico contemplou os itens de mercadorias, alíquotas e chaves de acesso. Pedimos especial atenção aos tópicos abaixo antes do fechamento definitivo da apuração.\\n")
        
    if include_icms:
        sections_html.append("""
        <div style="margin: 20px 0;">
            <h4 style="color: #dc2626; margin: 0 0 8px 0; font-size: 15px;">🔴 Divergências em Regras e Créditos de ICMS</h4>
            <ul style="margin: 0; padding-left: 20px; color: #334155; font-size: 14px; line-height: 1.6;">
                <li><b>Créditos Glosados:</b> Foram identificados itens com tomada de crédito de ICMS cujo CFOP ou regra não permite aproveitamento conforme parametrização fiscal.</li>
                <li><b>Divergência de Valor SPED x XML:</b> Há itens com divergência de cálculo entre o valor escriturado na nota do SPED e o valor de ICMS destacado no XML de origem.</li>
                <li><i>Ação recomendada:</i> Revisar na aba de 'Inconformidades ICMS' no relatório de auditoria para estornar o crédito ou ajustar a escrituração.</li>
            </ul>
        </div>
        """)
        sections_text.append("[🔴 DIVERGÊNCIAS EM REGRAS E CRÉDITOS DE ICMS]\\n- Créditos Glosados: Itens com tomada de crédito cujo CFOP ou regra não permite aproveitamento.\\n- Divergência de Valor SPED x XML: Itens com divergência de cálculo entre SPED e XML de origem.\\n- Ação Recomendada: Revisar na aba 'Inconformidades ICMS' no relatório para estornar o crédito ou ajustar a escrituração.\\n")
        
    if include_ipi:
        sections_html.append("""
        <div style="margin: 20px 0;">
            <h4 style="color: #9333ea; margin: 0 0 8px 0; font-size: 15px;">🟣 Divergências na Apuração e CST de IPI</h4>
            <ul style="margin: 0; padding-left: 20px; color: #334155; font-size: 14px; line-height: 1.6;">
                <li><b>CST IPI Incompatível com Crédito:</b> Apontados itens escriturados com aproveitamento de IPI em CST que pela legislação do TIPI não autoriza crédito.</li>
                <li><b>Valores Divergentes:</b> Variação entre o imposto IPI declarado no arquivo fiscal e o XML emitido pelo fornecedor industrial.</li>
                <li><i>Ação recomendada:</i> Confirmar na aba de 'IPI' as regras de permissão ou estornar o lançamento na apuração do imposto.</li>
            </ul>
        </div>
        """)
        sections_text.append("[🟣 DIVERGÊNCIAS NA APURAÇÃO E CST DE IPI]\\n- CST IPI Incompatível: Apontados itens escriturados com aproveitamento de IPI em CST que não autoriza crédito.\\n- Valores Divergentes: Variação entre o SPED e o XML emitido pelo fornecedor.\\n- Ação Recomendada: Confirmar as regras na aba 'IPI' ou estornar o lançamento.\\n")
        
    if include_pis_cofins:
        sections_html.append("""
        <div style="margin: 20px 0;">
            <h4 style="color: #2563eb; margin: 0 0 8px 0; font-size: 15px;">🔵 Inconsistências de PIS e COFINS</h4>
            <ul style="margin: 0; padding-left: 20px; color: #334155; font-size: 14px; line-height: 1.6;">
                <li><b>Base de Cálculo / CST:</b> Foram encontradas diferenças na tributação e expectativa de crédito nas contribuições PIS/COFINS em comparação com os XMLs do período.</li>
                <li><i>Ação recomendada:</i> Verificar os CSTs de entrada aplicados na escrituração para evitar questionamento federal na EFD-Contribuições.</li>
            </ul>
        </div>
        """)
        sections_text.append("[🔵 INCONSISTÊNCIAS DE PIS E COFINS]\\n- Base de Cálculo / CST: Encontradas diferenças na tributação e expectativa de crédito nas contribuições em comparação com os XMLs.\\n- Ação Recomendada: Verificar os CSTs de entrada para evitar questionamento na EFD-Contribuições.\\n")
        
    if include_missing_xmls:
        sections_html.append("""
        <div style="margin: 20px 0;">
            <h4 style="color: #ea580c; margin: 0 0 8px 0; font-size: 15px;">🟠 Cobrança de XMLs Faltantes</h4>
            <ul style="margin: 0; padding-left: 20px; color: #334155; font-size: 14px; line-height: 1.6;">
                <li><b>Arquivos de Origem Ausentes:</b> Constatamos notas fiscais declaradas nos blocos C100/D100 do SPED cujos arquivos XML não constam em nossa base de conciliação.</li>
                <li><i>Ação recomendada:</i> Solicitar aos respectivos fornecedores o envio dos XMLs ou realizar o download na Secretaria da Fazenda para validação antes da entrega do SPED.</li>
            </ul>
        </div>
        """)
        sections_text.append("[🟠 COBRANÇA DE XMLS FALTANTES]\\n- Arquivos de Origem Ausentes: Notas fiscais declaradas nos blocos C100/D100 cujos arquivos XML não constam em nossa base.\\n- Ação Recomendada: Solicitar aos fornecedores o envio dos XMLs ou download na Sefaz.\\n")

    notes_html = ""
    notes_text = ""
    if custom_notes and custom_notes.strip():
        notes_html = f"""
        <div style="background-color: #fffbeb; border: 1px solid #fcd34d; padding: 12px 16px; margin: 16px 0; border-radius: 4px;">
            <h4 style="margin: 0 0 6px 0; color: #92400e; font-size: 14px;">📝 Observações Específicas da Equipe Fiscal:</h4>
            <p style="margin: 0; color: #78350f; font-size: 14px; white-space: pre-line;">{_e(custom_notes.strip())}</p>
        </div>
        """
        notes_text = f"\\n[📝 OBSERVAÇÕES ESPECÍFICAS]\\n{custom_notes.strip()}\\n"

    html_body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 680px; margin: 0 auto; color: #1e293b; line-height: 1.6;">
        <p style="font-size: 15px; margin-bottom: 16px;">{greeting}</p>
        <p style="font-size: 14px; margin-bottom: 20px;">{intro}</p>
        {''.join(sections_html)}
        {notes_html}
        <p style="font-size: 14px; margin-top: 24px;">Qualquer dúvida, estamos à disposição para auxiliar no saneamento dos itens indicados.</p>
        <p style="font-size: 14px; margin-top: 12px;">Atenciosamente,<br><b>Equipe Synax | Auditoria e Compliance Tributário</b></p>
    </div>
    """

    text_body = f"""{greeting_txt}

{intro_txt}

{''.join(sections_text)}{notes_text}
Qualquer dúvida, estamos à disposição para auxiliar no saneamento dos itens indicados.

Atenciosamente,
Equipe Synax | Auditoria e Compliance Tributário
"""

    return templates.TemplateResponse(request, "partials/admin/email_preview.html", {
        "request": request,
        "subject": subject,
        "html_body": html_body,
        "text_body": text_body.strip(),
        "tenant_name": tenant_name,
        "period": period
    })

