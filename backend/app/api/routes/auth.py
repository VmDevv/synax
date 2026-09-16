import logging
from fastapi import APIRouter, Request, Form, Response, Depends, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from ...infrastructure.auth.supabase_client import get_supabase_client, get_supabase_admin_client
from ...core.rate_limit import limitar, consumir, client_ip
from ..dependencies.auth import extrair_token, invalidar_token

logger = logging.getLogger(__name__)

router = APIRouter()
from ...core.templating import build_templates
templates = build_templates()

# Erros de credencial do Supabase: rotineiros, não merecem traceback no log.
ERROS_DE_CREDENCIAL = {
    "AuthApiError", "AuthError", "AuthInvalidCredentialsError",
    "AuthWeakPasswordError",
}


def destino_seguro(caminho: str) -> str:
    """Só caminhos internos: um `next` absoluto viraria redirecionamento aberto —
    um link do nosso domínio que joga o usuário em outro site, prato feito para
    phishing. Volta para a raiz em qualquer caso duvidoso.
    """
    if (not caminho or not caminho.startswith("/") or caminho.startswith("//")
            or "\\" in caminho or caminho.startswith("/login")):
        return "/"
    return caminho


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse(
        request, "pages/login.html",
        {"request": request, "next": destino_seguro(next)})

@router.post("/api/auth/login", response_class=HTMLResponse)
def login_action(
    request: Request,
    response: Response,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    _rl = Depends(limitar("login_ip", maximo=10, janela_seg=300))
):
    # Limite por IP (acima) + por e-mail (abaixo): só por IP, um atacante troca de
    # IP; só por e-mail, ele varre vários e-mails do mesmo IP.
    if not consumir(f"login_email:{email.strip().lower()}", maximo=5, janela_seg=300):
        logger.warning("Rate limit por e-mail atingido no login")
        return templates.TemplateResponse(request, "pages/login.html",
            {"request": request, "next": destino_seguro(next),
             "error": "Muitas tentativas para esta conta. Aguarde 5 minutos."},
            status_code=429
        )
    try:
        # Tenta autenticar no Supabase
        supabase = get_supabase_client()
        auth_response = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })
        
        # Pega o token de acesso que o Supabase gerou
        token = auth_response.session.access_token
        
        # Volta para a tela que o usuário tentou abrir (ou a raiz), com o cookie.
        redirect = RedirectResponse(url=destino_seguro(next),
                                    status_code=status.HTTP_302_FOUND)
        redirect.set_cookie(
            key="access_token",
            value=f"Bearer {token}",
            httponly=True,
            # Secure impede que o cookie de sessão trafegue em HTTP puro. Fica
            # condicionado ao esquema porque, em localhost (http), o navegador
            # descartaria o cookie e o login não funcionaria em desenvolvimento.
            secure=request.url.scheme == "https",
            samesite="lax",
            path="/",
            max_age=3600 * 24 * 7 # 1 semana
        )
        return redirect
        
    except Exception as err:
        # Mensagem genérica de propósito: detalhar o erro do provedor revelaria
        # se o e-mail existe (enumeração de contas) e expõe internos do Supabase.
        #
        # Sem `exc_info`: senha errada é evento rotineiro, e o traceback de 25
        # linhas do Supabase a cada tentativa enterrava os erros de verdade no
        # log. Falha inesperada (rede, provedor fora) ainda vem com o traceback.
        # Checagem por nome, e não por import: a família de exceções do
        # supabase_auth muda de lugar entre versões, e um ImportError aqui
        # traria o traceback de volta em TODA tentativa de login.
        esperado = err.__class__.__name__ in ERROS_DE_CREDENCIAL
        logger.warning("Falha de autenticação no login (%s) para o IP %s",
                       err.__class__.__name__, client_ip(request),
                       exc_info=not esperado)
        return templates.TemplateResponse(request, "pages/login.html",
            {"request": request, "next": destino_seguro(next),
             "error": "E-mail ou senha inválidos."},
            status_code=401
        )

@router.get("/logout")
def logout(request: Request, response: Response):
    """Encerra a sessão de verdade: apagar o cookie não bastava.

    O JWT continuava válido até expirar (uma semana) e o cache de tokens ainda o
    aceitava por até 30s. Quem tivesse copiado o token seguia dentro do sistema
    depois de o usuário clicar em "Sair".
    """
    token = extrair_token(request)
    if token:
        invalidar_token(token)
        try:
            supabase = get_supabase_client()
            supabase.auth.admin.sign_out(token)
        except Exception:
            # Provedor fora do ar não pode impedir o logout local.
            logger.warning("Não foi possível revogar a sessão no Supabase", exc_info=True)

    redirect = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    redirect.delete_cookie("access_token", path="/")
    return redirect


# ===== Recuperação de senha (Supabase Auth) =====
# Fluxo: /forgot-password -> e-mail do Supabase com token de recovery no fragmento
# da URL -> /reset-password lê o fragmento no navegador -> POST troca a senha.

MIN_PASSWORD_LEN = 8


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return templates.TemplateResponse(request, "pages/forgot_password.html", {"request": request})


@router.post("/api/auth/forgot-password", response_class=HTMLResponse)
def forgot_password_action(
    request: Request,
    email: str = Form(...),
    _rl = Depends(limitar("forgot_password", maximo=5, janela_seg=900))
):
    """Dispara o e-mail de redefinição.

    Responde SEMPRE a mesma mensagem, exista ou não a conta: caso contrário o
    endpoint viraria um oráculo para enumerar e-mails de clientes.
    """
    redirect_to = str(request.base_url).rstrip("/") + "/reset-password"
    try:
        supabase = get_supabase_client()
        supabase.auth.reset_password_email(email, {"redirect_to": redirect_to})
    except Exception:
        # Falha silenciosa de propósito para quem pediu (evita oráculo de
        # e-mail); registra no servidor para diagnóstico.
        # Só o domínio vai para o log: o endereço inteiro é dado pessoal do
        # cliente, e o log alimenta o diagnóstico de erros por IA.
        logger.exception("[forgot-password] falha ao enviar para *@%s",
                         email.rsplit("@", 1)[-1] if "@" in email else "?")

    return templates.TemplateResponse(request, "pages/forgot_password.html", {
        "request": request,
        "sent": True,
        "email": email
    })


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request):
    return templates.TemplateResponse(request, "pages/reset_password.html", {"request": request})


@router.post("/api/auth/reset-password", response_class=HTMLResponse)
def reset_password_action(
    request: Request,
    access_token: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    _rl = Depends(limitar("reset_password", maximo=10, janela_seg=900))
):
    """Troca a senha usando o token de recovery emitido pelo Supabase."""
    def fail(msg: str, code: int = 400):
        return templates.TemplateResponse(
            request, "pages/reset_password.html",
            {"request": request, "error": msg},
            status_code=code
        )

    if password != password_confirm:
        return fail("As senhas não conferem.")
    if len(password) < MIN_PASSWORD_LEN:
        return fail(f"A senha deve ter ao menos {MIN_PASSWORD_LEN} caracteres.")

    # O token identifica o usuário; ninguém troca a senha de terceiros com ele.
    #
    # LIMITAÇÃO CONHECIDA: get_user() aceita QUALQUER access token válido, não só
    # o de recuperação — inclusive o cookie de sessão de alguém já logado. Ou
    # seja, um token vazado troca a senha sem conhecer a atual. A primitiva certa
    # é auth.verify_otp({"token_hash": ..., "type": "recovery"}), que exige mudar
    # o fluxo do fragmento em pages/reset_password.html. Enquanto isso não
    # acontece, o sign_out global abaixo limita a janela de abuso.
    try:
        user_resp = get_supabase_client().auth.get_user(access_token)
        user_id = user_resp.user.id
    except Exception:
        return fail("Link de redefinição inválido ou expirado. Solicite um novo.", 401)

    try:
        admin = get_supabase_admin_client()
        admin.auth.admin.update_user_by_id(user_id, {"password": password})
        # Trocar a senha tem de derrubar as sessões existentes: senão o token que
        # eventualmente vazou (e que esta rota aceita como prova de identidade)
        # continua valendo depois da recuperação.
        try:
            admin.auth.admin.sign_out(access_token, scope="global")
        except Exception:
            logger.warning("Senha trocada, mas as sessões de %s não foram revogadas",
                           user_id, exc_info=True)
    except Exception:
        # Era um print() com o texto do erro do Supabase: saía cru no stdout,
        # fora do logging, podendo carregar detalhe do provedor junto do id do
        # usuário. O traceback fica no log; o usuário recebe só a mensagem.
        logger.exception("Falha ao atualizar a senha do usuário %s", user_id)
        return fail("Não foi possível atualizar a senha. Tente novamente.", 500)

    return templates.TemplateResponse(request, "pages/login.html", {
        "request": request,
        "success": "Senha redefinida com sucesso. Faça login com a nova senha."
    })
