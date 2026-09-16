"""Valida as correções do checklist vibe-security: headers, rate limiting,
vazamento de stack trace e .gitignore."""
import os, sys, uuid, types, subprocess
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_hard_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_hard_test.db"):
    os.remove("_hard_test.db")

from sqlalchemy import types as _satypes
import sqlalchemy.dialects.postgresql as pg
class _UUIDShim(_satypes.TypeDecorator):
    impl = _satypes.Uuid; cache_ok = True
    def __init__(self, *a, **kw):
        kw.pop('as_uuid', None); super().__init__()
    def process_bind_param(self, value, dialect):
        return uuid.UUID(value) if isinstance(value, str) else value
pg.UUID = _UUIDShim

import backend.app.infrastructure.auth.supabase_client as sb
TOKENS = {}
class _Auth:
    def get_user(self, token):
        u = TOKENS.get(token.replace("Bearer ", "").strip())
        if not u: raise Exception("invalid")
        return types.SimpleNamespace(user=u)
    def sign_in_with_password(self, creds):
        raise Exception("Invalid login credentials: user nao existe no projeto xyz-123.supabase.co")
    def reset_password_email(self, email, options=None): return None
class _Client:
    auth = _Auth()
sb.get_supabase_client = lambda: _Client()
sb.get_supabase_admin_client = lambda: _Client()
import backend.app.api.dependencies.auth as dep_auth
dep_auth.get_supabase_client = lambda: _Client()
import backend.app.api.routes.auth as auth_mod
auth_mod.get_supabase_client = lambda: _Client()

from backend.app.infrastructure.database.session import engine as _eng
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit("ABORTADO: engine nao e SQLite")

import backend.app.main as m
from backend.app.infrastructure.database.session import SessionLocal, engine
from backend.app.domain.models.base import Base
from backend.app.core import rate_limit
from fastapi.testclient import TestClient

Base.metadata.create_all(bind=engine)
c = TestClient(m.app, raise_server_exceptions=False)

results = []
def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("  OK  " if cond else " FALHA") + f" | {name}" + (f"  [{detail}]" if detail and not cond else ""))

print("\n=== 1. Headers de seguranca ===")
r = c.get("/login")
esperados = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}
for h, v in esperados.items():
    check(f"{h} presente e correto", r.headers.get(h) == v, f"valor={r.headers.get(h)}")
check("Permissions-Policy presente", "Permissions-Policy" in r.headers)
check("HSTS ausente em HTTP (correto)", "Strict-Transport-Security" not in r.headers)

print("\n=== 2. Login nao vaza detalhe do provedor ===")
rate_limit._BUCKETS.clear()
r = c.post("/api/auth/login", data={"email": "a@b.com", "password": "errada"})
check("responde 401", r.status_code == 401, f"status={r.status_code}")
check("mostra mensagem generica", "E-mail ou senha inválidos" in r.text)
check("NAO vaza host do Supabase", "supabase.co" not in r.text)
check("NAO vaza texto interno do erro", "Invalid login credentials" not in r.text)

print("\n=== 2b. Sessao expirada leva ao login, nao a um JSON cru ===")
# Antes: digitar /admin sem sessao devolvia {"detail": ...} na tela do navegador,
# e o usuario ficava preso — so a raiz "/" redirecionava para o login.
NAV = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
sem_seguir = TestClient(m.app, raise_server_exceptions=False, follow_redirects=False)

for caminho in ("/admin", "/curation", "/nfse"):
    r = sem_seguir.get(caminho, headers=NAV)
    destino = r.headers.get("location", "")
    check(f"navegacao para {caminho} redireciona ao login",
          r.status_code == 302 and destino.startswith("/login"),
          f"status={r.status_code} location={destino}")
    check(f"{caminho} preservado no next", f"next={caminho}" in destino, destino)

r = sem_seguir.get("/admin", headers={**NAV, "HX-Request": "true"})
check("chamada HTMX recebe HX-Redirect (o 302 seria ignorado)",
      r.status_code == 204 and r.headers.get("HX-Redirect", "").startswith("/login"),
      f"status={r.status_code} hx={r.headers.get('HX-Redirect')}")

r = sem_seguir.get("/admin", headers={"Accept": "application/json"})
check("chamada de API continua recebendo 401 em JSON",
      r.status_code == 401 and "application/json" in r.headers.get("content-type", ""),
      f"status={r.status_code} ct={r.headers.get('content-type')}")

print("\n=== 2c. O next do login nao pode virar redirecionamento aberto ===")
from backend.app.api.routes.auth import destino_seguro
maliciosos = [
    ("//evil.com", "protocolo relativo"),
    ("https://evil.com", "URL absoluta"),
    ("http://evil.com/x", "URL absoluta http"),
    ("/\\evil.com", "barra invertida"),
    ("", "vazio"),
    ("evil.com", "sem barra inicial"),
]
for entrada, rotulo in maliciosos:
    check(f"next recusado: {rotulo}", destino_seguro(entrada) == "/",
          f"{entrada!r} -> {destino_seguro(entrada)!r}")
for bom in ("/admin", "/nfse?page=2", "/curation"):
    check(f"next interno aceito: {bom}", destino_seguro(bom) == bom)
check("next para /login nao cria laco", destino_seguro("/login?next=/login") == "/")

r = sem_seguir.get("/login?next=//evil.com", headers=NAV)
check("pagina de login neutraliza next malicioso",
      'name="next" value="/"' in r.text, "campo oculto nao neutralizado")
r = sem_seguir.get("/login?next=/admin", headers=NAV)
check("pagina de login guarda o destino legitimo", 'name="next" value="/admin"' in r.text)

rate_limit._BUCKETS.clear()
r = sem_seguir.post("/api/auth/login",
                    data={"email": "a@b.com", "password": "x", "next": "//evil.com"})
check("login com senha errada nao propaga next malicioso",
      'name="next" value="/"' in r.text)

print("\n=== 2d. Ruido no log ===")
import inspect
from backend.app.api.routes import auth as rota_auth
src_login = inspect.getsource(rota_auth.login_action)
check("senha errada nao despeja traceback no log", "exc_info=not esperado" in src_login)
check("erro real do Supabase esta na lista de rotineiros",
      "AuthApiError" in rota_auth.ERROS_DE_CREDENCIAL)
check("falha inesperada AINDA registra o traceback",
      "AuthApiError" in rota_auth.ERROS_DE_CREDENCIAL
      and "ConnectionError" not in rota_auth.ERROS_DE_CREDENCIAL)
r = c.get("/favicon.ico")
check("favicon existe (parava de poluir o log com 404)", r.status_code == 200,
      f"status={r.status_code}")

print("\n=== 3. Rate limiting ===")
rate_limit._BUCKETS.clear()
# login: 5 tentativas por e-mail em 5min
codigos = []
for i in range(8):
    rr = c.post("/api/auth/login", data={"email": "alvo@synax.com", "password": f"tent{i}"})
    codigos.append(rr.status_code)
check("bloqueia forca bruta no login (429)", 429 in codigos, f"codigos={codigos}")
check("permite as primeiras tentativas", codigos[0] == 401, f"primeiro={codigos[0]}")
check("bloqueio vem depois de 5 tentativas", codigos.index(429) == 5, f"bloqueou na tentativa {codigos.index(429)+1 if 429 in codigos else '-'}")

rate_limit._BUCKETS.clear()
cods = [c.post("/api/auth/forgot-password", data={"email": f"x{i}@y.com"}).status_code for i in range(7)]
check("limita reset de senha", 429 in cods, f"codigos={cods}")

rate_limit._BUCKETS.clear()
# IP diferente nao deve herdar o bloqueio de outro IP
for i in range(6):
    c.post("/api/auth/login", data={"email": "vitima@synax.com", "password": "x"},
           headers={"X-Forwarded-For": "1.1.1.1"})
r_outro = c.post("/api/auth/login", data={"email": "outro@synax.com", "password": "x"},
                 headers={"X-Forwarded-For": "2.2.2.2"})
check("outro IP/conta nao herda bloqueio alheio", r_outro.status_code == 401, f"status={r_outro.status_code}")

print("\n=== 4. Rate limiter: janela desliza ===")
rate_limit._BUCKETS.clear()
check("consome ate o limite", all(rate_limit.consumir("t", 3, 60) for _ in range(3)))
check("bloqueia no estouro", rate_limit.consumir("t", 3, 60) is False)
rate_limit._BUCKETS.clear()
check("libera apos limpar a janela", rate_limit.consumir("t", 3, 60) is True)

print("\n=== 5. Stack trace fora da interface ===")
import inspect
src_up = inspect.getsource(sys.modules['backend.app.api.routes.uploads'])
check("nao grava traceback em error_message", "tb[-500:]" not in src_up)
check("usa logger.exception no lugar", "logger.exception" in src_up)
check("mensagem ao usuario e generica", "Não foi possível concluir a auditoria" in src_up)

print("\n=== 5b. XML hostil nao derruba o servidor ===")
import tempfile, io, zipfile, time as _time
from backend.app.services.nfse.abrasf_parser import parse_nfse_xml, _tem_doctype

def _bomba(niveis):
    """XML minusculo que se expande 10^niveis vezes ao ser interpretado."""
    ents = ['<!ENTITY a "aaaaaaaaaa">']
    for i in range(1, niveis):
        ant, cur = chr(96 + i), chr(97 + i)
        ents.append(f'<!ENTITY {cur} "{("&" + ant + ";") * 10}">')
    ult = chr(96 + niveis)
    return ('<?xml version="1.0"?>\n<!DOCTYPE r [\n' + "\n".join(ents) +
            f'\n]>\n<r><Numero>1</Numero><Cnpj>07662340000127</Cnpj>'
            f'<X>&{ult};</X></r>')

def _arquivo(texto):
    fd, p = tempfile.mkstemp(suffix=".xml")
    os.write(fd, texto.encode()); os.close(fd)
    return p

# 9 niveis = ~1 GB de memoria se for expandido
p = _arquivo(_bomba(9))
t0 = _time.time()
resultado = parse_nfse_xml(p)
gasto = _time.time() - t0
os.remove(p)
check("bomba de entidades (1 GB) e recusada", resultado is None, str(resultado)[:60])
check(f"e recusada rapido, sem expandir ({gasto*1000:.0f} ms)", gasto < 0.5, f"{gasto:.2f}s")

# XXE: leitura de arquivo local do servidor
xxe = ('<?xml version="1.0"?>\n<!DOCTYPE r [<!ENTITY x SYSTEM "file:///C:/Windows/win.ini">]>\n'
       '<r><Numero>&x;</Numero><Cnpj>07662340000127</Cnpj></r>')
p = _arquivo(xxe)
r_xxe = parse_nfse_xml(p)
os.remove(p)
check("XXE (ler arquivo do servidor) e recusado", r_xxe is None)

# o XML legitimo NAO pode ser afetado pela protecao
bom = ('<?xml version="1.0" encoding="UTF-8"?>'
       '<ConsultarNfseResposta xmlns="http://www.abrasf.org.br/nfse.xsd">'
       '<ListaNfse><CompNfse><Nfse><InfNfse><Numero>12345</Numero>'
       '<PrestadorServico><IdentificacaoPrestador><Cnpj>07662340000127</Cnpj>'
       '</IdentificacaoPrestador></PrestadorServico>'
       '</InfNfse></Nfse></CompNfse></ListaNfse></ConsultarNfseResposta>')
p = _arquivo(bom)
r_bom = parse_nfse_xml(p)
os.remove(p)
check("NFS-e legitima continua sendo lida",
      r_bom is not None and r_bom.get("numero") == "12345", str(r_bom)[:80])

p = _arquivo("<?xml version='1.0'?>\n\n   <!DOCTYPE x>\n<r/>")
check("DOCTYPE e detectado onde quer que apareca no cabecalho", _tem_doctype(p))
os.remove(p)

print("\n=== 5c. Tetos de tamanho na importacao ===")
from backend.app.api.routes import nfse as rota_nfse
check("existe teto por arquivo", 0 < rota_nfse.LIMITE_ARQUIVO_BYTES <= 32 * 1024 * 1024,
      str(rota_nfse.LIMITE_ARQUIVO_BYTES))
check("existe teto por requisicao", 0 < rota_nfse.LIMITE_TOTAL_BYTES <= 500 * 1024 * 1024)
check("existe teto de quantidade de arquivos", 0 < rota_nfse.LIMITE_ARQUIVOS <= 20000)
check("existe teto para o ZIP de download", 0 < rota_nfse.LIMITE_ZIP_NOTAS <= 50000)

# o zip bomb precisa ser barrado pelo indice, ANTES de descomprimir
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("bomba.xml", b"<r>" + b"A" * (50 * 1024 * 1024) + b"</r>")
with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
    declarado = z.infolist()[0].file_size
check(f"zip bomb ({len(buf.getvalue())/1024:.0f} KB -> {declarado/1024/1024:.0f} MB) "
      "excede o teto por arquivo", declarado > rota_nfse.LIMITE_ARQUIVO_BYTES)

src_import = inspect.getsource(rota_nfse.importar)
check("o tamanho e conferido antes de z.read()",
      src_import.index("info.file_size > LIMITE_ARQUIVO_BYTES") < src_import.index("z.read(info)"))
check("importacao informa quantos arquivos recusou", "recusada(s) por tamanho" in src_import)

src_zip = inspect.getsource(rota_nfse.baixar_zip)
check("download em ZIP tem rate limit", "limitar(" in src_zip)
check("download em ZIP tem teto de notas", "LIMITE_ZIP_NOTAS" in src_zip)

from backend.app.api.routes import admin as rota_admin
check("leitura em massa de certificados tem rate limit",
      "limitar(" in inspect.getsource(rota_admin.atualizar_validades))

print("\n=== 5d. O mapa da API nao fica publico ===")
# O FastAPI publica /docs, /redoc e /openapi.json ABERTOS por padrao. Nao dao
# acesso (as rotas seguem exigindo sessao), mas o schema lista as 108 rotas com
# o nome de cada campo de formulario, inclusive as de admin — reconhecimento de
# graca para quem procurar brecha num sistema que guarda SPED e XML de cliente.
c.cookies.clear()
for url in ("/docs", "/redoc", "/openapi.json"):
    r = c.get(url)
    check(f"{url} nao responde sem login", r.status_code == 404,
          f"HTTP {r.status_code} — docs_url/redoc_url/openapi_url foram reabertos?")

check("e o schema nao vaza nem por dentro (app.openapi_url)",
      m.app.openapi_url is None, m.app.openapi_url)
check("mas da para reabrir com EXPOR_DOCS para desenvolver",
      'EXPOR_DOCS' in open(os.path.join("backend", "app", "main.py"),
                           encoding="utf-8").read())


print("\n=== 6. .gitignore protege segredos e dados de cliente ===")
def ignorado(p):
    return subprocess.run(["git", "check-ignore", "-q", p]).returncode == 0
for p in [".env", "temp_uploads/", ".sieg_cache_pdf/", "multi_tenant_audit.db", "audit_rules.db"]:
    check(f"{p} ignorado pelo git", ignorado(p))
for p in ["Portaria 16.xlsx", "backend/app/main.py"]:
    check(f"{p} permanece versionavel", not ignorado(p))

engine.dispose()
if os.path.exists("_hard_test.db"):
    try: os.remove("_hard_test.db")
    except Exception: pass

fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
for f in fails:
    print("  -", f)
sys.exit(1 if fails else 0)
