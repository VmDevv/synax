"""Regressão dos achados do code review de 2026-08-18.

Cada bloco abaixo corresponde a uma falha que EXISTIA e foi corrigida. Todos são
silenciosos: o sistema respondia 200 em cima de comportamento errado — arquivo
gravado fora da pasta, nota de outra empresa entregue, traceback na tela.

Mesmo isolamento das demais suítes (dotenv neutralizado + trava SQLite), para
nunca tocar produção.
"""
import os, sys, uuid, types, io, zipfile
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_review_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
os.environ["SIEG_API_KEY"] = "chave-de-teste"
os.environ["SIEG_EMAIL"] = "teste@synax.local"
os.environ["TRUSTED_PROXY_HOPS"] = "0"

if os.path.exists("_review_test.db"):
    os.remove("_review_test.db")

from sqlalchemy import types as _satypes
import sqlalchemy.dialects.postgresql as pg

class _UUIDShim(_satypes.TypeDecorator):
    impl = _satypes.Uuid
    cache_ok = True
    def __init__(self, *a, **kw):
        kw.pop('as_uuid', None); super().__init__()
    def process_bind_param(self, value, dialect):
        return uuid.UUID(value) if isinstance(value, str) else value

pg.UUID = _UUIDShim

import backend.app.infrastructure.auth.supabase_client as sb
TOKENS = {}

class _AdminAuth:
    def update_user_by_id(self, uid, attrs):
        return types.SimpleNamespace(user=types.SimpleNamespace(id=uid))
    def sign_out(self, token, scope=None):
        return None

class _Auth:
    admin = _AdminAuth()
    def get_user(self, token):
        u = TOKENS.get(token)
        if not u:
            raise Exception("invalid token")
        return types.SimpleNamespace(user=u)

class _Client:
    auth = _Auth()

sb.get_supabase_client = lambda: _Client()
sb.get_supabase_admin_client = lambda: _Client()
import backend.app.api.dependencies.auth as dep_auth
dep_auth.get_supabase_client = lambda: _Client()
import backend.app.api.routes.auth as auth_mod
auth_mod.get_supabase_client = lambda: _Client()
auth_mod.get_supabase_admin_client = lambda: _Client()

from backend.app.infrastructure.database.session import engine as _eng
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit(f"ABORTADO: engine aponta para {_eng.url.get_backend_name()}, não SQLite.")

import backend.app.main as m
from backend.app.infrastructure.database.session import SessionLocal, engine
from backend.app.domain.models.base import Base
from backend.app.domain.models.auth_models import Tenant, UserProfile, UserTenant
from backend.app.domain.models.audit import AuditTask
from backend.app.domain.models.rules import CfopCreditRule
from backend.app.domain.models.integrations import MissingInvoiceLog
from fastapi.testclient import TestClient

Base.metadata.create_all(bind=engine)
db = SessionLocal()

uA, uB = uuid.uuid4(), uuid.uuid4()
tA, tB = str(uA), str(uB)
db.add_all([
    Tenant(id=uA, name="Empresa A (vitima)", cnpj="11111111111111"),
    Tenant(id=uB, name="Empresa B (atacante)", cnpj="22222222222222"),
])

ua_admin, ua_cliA, ua_cliB = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
db.add_all([
    UserProfile(id=ua_admin, tenant_id=uA, role="admin", name="Admin"),
    UserProfile(id=ua_cliA, tenant_id=uA, role="cliente", name="Cliente A"),
    UserProfile(id=ua_cliB, tenant_id=uB, role="cliente", name="Cliente B (atacante)"),
])
db.commit()

TOKENS["admin"] = types.SimpleNamespace(id=str(ua_admin), email="admin@synax.com")
TOKENS["cliA"] = types.SimpleNamespace(id=str(ua_cliA), email="clienteA@synax.com")
TOKENS["cliB"] = types.SimpleNamespace(id=str(ua_cliB), email="clienteB@synax.com")

# Chave que pertence à Empresa A e nunca passou por nada da Empresa B.
CHAVE_A = "35230111111111111111550010000000011000000001"
CHAVE_ORFA = "35230199999999999999550010000000991000000099"

db.add(MissingInvoiceLog(access_key=CHAVE_A, tenant_id=tA, invoice_type="NFE",
                         status="pendente_proximo_mes", emit_cnpj="33333333333333",
                         emit_name="Fornecedor da Empresa A", vl_total="1500.00",
                         task_id="task-a"))
db.add(AuditTask(id="task-a", tenant_id=tA, status="concluido", user_id=str(ua_cliA),
                 file_name="sped_a.txt",
                 result_data='{"resumo": {"notas_processadas": 1}, "chave": "%s"}' % CHAVE_A))
# Regra marcada is_global: só o tenant dono dela deve enxergá-la.
db.add(CfopCreditRule(tenant_id="tenant-dono-da-global", cfop="1999",
                      allow_credit=True, status="aprovado", is_global=True))
db.add(CfopCreditRule(tenant_id=tB, cfop="1888", allow_credit=False, status="aprovado",
                      is_global=False))
db.commit()
db.close()

from backend.app.api.dependencies.db import get_rls_db as _real_rls
def _fake_rls():
    s = SessionLocal()
    try: yield s
    finally: s.close()
m.app.dependency_overrides[_real_rls] = _fake_rls

c = TestClient(m.app, raise_server_exceptions=False)

def as_(tok):
    c.cookies.clear()
    if tok:
        c.cookies.set("access_token", f"Bearer {tok}")
    return c

OK = FALHAS = 0
_falhou = []

def check(nome, condicao, detalhe=""):
    global OK, FALHAS
    if condicao:
        OK += 1
        print(f"  OK   | {nome}")
    else:
        FALHAS += 1
        _falhou.append(nome)
        print(f" FALHA | {nome}  [{detalhe}]")


# ============================================================================
print("\n=== 1. Path traversal no upload (escrita arbitrária de arquivo) ===")
# `UploadFile.filename` vem do cabeçalho Content-Disposition: é dado do atacante.
# `os.path.join(task_dir, "../../app/main.py")` escapava da pasta efêmera e
# sobrescrevia código do servidor — no Windows, um caminho absoluto descartava a
# base inteira.
from backend.app.core.uploads_seguros import nome_seguro, extrair_xmls_do_zip

CASOS = [
    ("../../app/main.py", "main.py"),
    ("..\\..\\..\\evil.py", "evil.py"),
    ("C:/Windows/System32/drivers/etc/hosts", "hosts"),
    ("/etc/passwd", "passwd"),
    ("....//....//x.txt", "x.txt"),
    ("sped_normal.txt", "sped_normal.txt"),
]
for entrada, esperado in CASOS:
    obtido = nome_seguro(entrada, "padrao.txt")
    check(f"nome_seguro({entrada!r}) -> {esperado!r}", obtido == esperado, f"obtido={obtido!r}")

for vazio in ["", None, "...", "///", "..."]:
    check(f"nome_seguro({vazio!r}) cai no padrão",
          nome_seguro(vazio, "padrao.txt") == "padrao.txt",
          f"obtido={nome_seguro(vazio, 'padrao.txt')!r}")

check("nome_seguro nunca devolve separador de caminho",
      all(("/" not in nome_seguro(e, "p.txt") and "\\" not in nome_seguro(e, "p.txt"))
          for e, _ in CASOS))
check("nome_seguro nunca devolve '..'",
      all(".." not in nome_seguro(e, "p.txt") for e, _ in CASOS))


# ============================================================================
print("\n=== 2. Extração de ZIP: traversal e bomba de descompressão ===")
import tempfile

destino = tempfile.mkdtemp(prefix="synax_zip_")
zip_path = os.path.join(destino, "entrada.zip")
alvo_dir = os.path.join(destino, "xmls")
os.makedirs(alvo_dir, exist_ok=True)

with zipfile.ZipFile(zip_path, "w") as z:
    z.writestr("../../fuga.xml", "<nfe>fuga</nfe>")
    z.writestr("normal.xml", "<nfe>ok</nfe>")
    z.writestr("nao_e_xml.exe", "MZ")

extraidos = extrair_xmls_do_zip(zip_path, alvo_dir)
gravados = sorted(os.listdir(alvo_dir))

check("Membro '../../fuga.xml' não escapou da pasta de destino",
      not os.path.exists(os.path.join(destino, "..", "fuga.xml")))
check("Todos os arquivos gravados ficaram dentro do destino",
      all(os.path.abspath(os.path.join(alvo_dir, g)).startswith(os.path.abspath(alvo_dir))
          for g in gravados), f"gravados={gravados}")
check("Arquivo não-XML foi ignorado", "nao_e_xml.exe" not in gravados, f"gravados={gravados}")
check("XML legítimo foi extraído", "normal.xml" in gravados, f"gravados={gravados}")
check("Contagem de extraídos bate com os XMLs", extraidos == 2, f"extraidos={extraidos}")

# Bomba de descompressão: o índice do ZIP declara o tamanho real, conferido
# ANTES de descomprimir. `extractall` sozinho escrevia tudo em disco.
from fastapi import HTTPException
import backend.app.core.uploads_seguros as us

bomba_path = os.path.join(destino, "bomba.zip")
with zipfile.ZipFile(bomba_path, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("gigante.xml", "A" * (5 * 1024 * 1024))

alvo_bomba = os.path.join(destino, "bomba_saida")
os.makedirs(alvo_bomba, exist_ok=True)
limite_original = us.LIMITE_DESCOMPRIMIDO_BYTES
us.LIMITE_DESCOMPRIMIDO_BYTES = 1024          # teto artificialmente baixo
try:
    us.extrair_xmls_do_zip(bomba_path, alvo_bomba)
    recusou = False
    codigo = None
except HTTPException as err:
    recusou, codigo = True, err.status_code
finally:
    us.LIMITE_DESCOMPRIMIDO_BYTES = limite_original

check("ZIP acima do teto descomprimido é recusado", recusou, "extraiu sem reclamar")
check("Recusa da bomba usa 413 (Payload Too Large)", codigo == 413, f"codigo={codigo}")
check("Nada foi gravado em disco na recusa", os.listdir(alvo_bomba) == [],
      f"sobrou={os.listdir(alvo_bomba)}")


# ============================================================================
print("\n=== 3. IDOR: proxy do cofre fiscal SIEG ===")
# O proxy consulta com a conta do ESCRITÓRIO, que enxerga o cofre inteiro. Exigir
# login só provava cadastro: um cliente baixava a NF-e de qualquer empresa do
# país informando os 44 dígitos.
import backend.app.api.routes.sieg as sieg_mod

CHAMADAS_SIEG = []

class _SiegFalso:
    def gerar_danfe_por_chave(self, chave):
        CHAMADAS_SIEG.append(("danfe", chave))
        return True, b"%PDF-1.4 conteudo"
    def baixar_xml_por_chave(self, chave):
        CHAMADAS_SIEG.append(("xml", chave))
        return True, "<nfe>conteudo sigiloso</nfe>"

# As rotas pedem o cliente ao `configuracoes.cliente_sieg(db)` desde 10/09/2026
# (as credenciais passaram a sair da tela do admin, com o .env de reserva).
# Substituir o SiegClient no modulo do cliente nao alcancaria mais as rotas: o
# que elas chamam e o resolvedor.
sieg_mod.SiegClient = lambda *a, **k: _SiegFalso()
import backend.app.api.routes.sieg as rota_sieg  # noqa: E402
rota_sieg.cliente_sieg = lambda db: _SiegFalso()

for rota in ("danfe", "xml"):
    CHAMADAS_SIEG.clear()
    r = as_("cliB").get(f"/api/sieg/{rota}/{CHAVE_A}")
    check(f"Cliente B NÃO baixa {rota} de nota da Empresa A",
          r.status_code == 404, f"status={r.status_code}")
    check(f"Cofre SIEG nem chegou a ser consultado ({rota})",
          CHAMADAS_SIEG == [], f"chamadas={CHAMADAS_SIEG}")

check("Negativa usa 404, não 403 (403 confirmaria que a nota existe)",
      as_("cliB").get(f"/api/sieg/xml/{CHAVE_A}").status_code == 404)

r = as_("cliA").get(f"/api/sieg/xml/{CHAVE_A}")
check("Cliente A baixa a própria nota (vinculada por MissingInvoiceLog)",
      r.status_code == 200, f"status={r.status_code}")

r = as_("cliA").get(f"/api/sieg/danfe/{CHAVE_A}?task_id=task-a")
check("Cliente A abre a DANFE pela auditoria dele (task_id)",
      r.status_code == 200, f"status={r.status_code}")

r = as_("cliB").get(f"/api/sieg/danfe/{CHAVE_A}?task_id=task-a")
check("Cliente B não usa o task_id da Empresa A como atalho",
      r.status_code == 404, f"status={r.status_code}")

r = as_("cliA").get(f"/api/sieg/xml/{CHAVE_ORFA}")
check("Chave que nunca passou por auditoria alguma é negada",
      r.status_code == 404, f"status={r.status_code}")

r = as_("admin").get(f"/api/sieg/xml/{CHAVE_ORFA}")
check("Admin segue com acesso amplo ao cofre", r.status_code == 200, f"status={r.status_code}")

r = as_(None).get(f"/api/sieg/xml/{CHAVE_A}", follow_redirects=False)
check("Anônimo continua barrado", r.status_code in (401, 403, 302),
      f"status={r.status_code}")

r = as_("cliA").get("/api/sieg/xml/123")
check("Chave com tamanho inválido -> 400", r.status_code == 400, f"status={r.status_code}")


# ============================================================================
print("\n=== 4. Credencial do SIEG fora do código-fonte ===")
import backend.app.services.integrations.sieg_client as sc

fonte = io.open(sc.__file__, encoding="utf-8").read()
check("Chave de API não está literal no arquivo",
      "z6wPFMhVt" not in fonte, "credencial ainda embutida")
check("E-mail da conta SIEG não está literal no arquivo",
      "@massucatti.cnt.br" not in fonte, "e-mail ainda embutido")
check("Credenciais vêm de variável de ambiente",
      'os.environ.get("SIEG_API_KEY"' in fonte and 'os.environ.get("SIEG_EMAIL"' in fonte)

chave_antes, email_antes = sc.SIEG_API_KEY, sc.SIEG_EMAIL
sc.SIEG_API_KEY = sc.SIEG_EMAIL = ""
try:
    sc.SiegClient()
    falhou_fechado = False
except RuntimeError:
    falhou_fechado = True
finally:
    sc.SIEG_API_KEY, sc.SIEG_EMAIL = chave_antes, email_antes

check("Sem credencial configurada, o cliente falha fechado (não sobe mudo)",
      falhou_fechado, "instanciou sem credencial")


# ============================================================================
print("\n=== 5. Traceback nunca chega à tela do cliente ===")
# execute_audit gravava `type(e).__name__ + traceback` em error_message, que é
# renderizado num f-string HTML. Como a auditoria processa XML de fornecedor,
# um texto de exceção com dado de terceiro virava XSS armazenado.
import backend.app.services.audit_engine.cross_checker as cc

fonte_cc = io.open(cc.__file__, encoding="utf-8").read()
check("cross_checker não grava traceback em error_message",
      "traceback.format_exc()" not in fonte_cc, "ainda formata traceback para o banco")
check("cross_checker registra a falha via logger.exception",
      "logger.exception(" in fonte_cc)

db2 = SessionLocal()
db2.add(AuditTask(id="task-erro", tenant_id=tA, status="erro", user_id=str(ua_cliA),
                  file_name="x.txt",
                  error_message="<img src=x onerror=alert('xss')>Falha KeyError em /app/secreto.py"))
db2.commit()
db2.close()

r = as_("cliA").get("/api/status/task-erro")
corpo = r.text
check("Página de erro responde 200", r.status_code == 200, f"status={r.status_code}")
check("HTML perigoso do error_message sai escapado",
      "<img src=x onerror" not in corpo, "tag crua na resposta")
check("Escape produziu entidade HTML", "&lt;img" in corpo, "não escapou")

r = as_("cliA").get("/api/status/<script>alert(1)</script>")
check("task_id malicioso não vira script na resposta",
      "<script>alert(1)</script>" not in r.text, "script refletido")


# ============================================================================
print("\n=== 6. Rate limit: X-Forwarded-For e poda por janela ===")
import backend.app.core.rate_limit as rl

rl._BUCKETS.clear()
rl._LAST_SWEEP["ts"] = 0.0

class _ReqFalso:
    def __init__(self, ip, xff=None):
        self.client = types.SimpleNamespace(host=ip)
        self.headers = {"x-forwarded-for": xff} if xff else {}

rl.PROXIES_CONFIAVEIS = 0
check("Sem proxy confiável, X-Forwarded-For é ignorado",
      rl.client_ip(_ReqFalso("10.0.0.1", "1.2.3.4")) == "10.0.0.1",
      f"obtido={rl.client_ip(_ReqFalso('10.0.0.1', '1.2.3.4'))}")

rl.PROXIES_CONFIAVEIS = 1
check("Com 1 proxy, lê o ÚLTIMO da cadeia (o que o proxy anexou)",
      rl.client_ip(_ReqFalso("10.0.0.1", "1.2.3.4, 200.200.200.200")) == "200.200.200.200",
      f"obtido={rl.client_ip(_ReqFalso('10.0.0.1', '1.2.3.4, 200.200.200.200'))}")
check("IP forjado no início da cadeia não é aceito",
      rl.client_ip(_ReqFalso("10.0.0.1", "1.2.3.4, 200.200.200.200")) != "1.2.3.4")
check("Cadeia mais curta que o esperado cai no IP direto",
      rl.client_ip(_ReqFalso("10.0.0.1", "")) == "10.0.0.1")
rl.PROXIES_CONFIAVEIS = 0

# Poda: usar a janela de quem chamou apagava marcas válidas de limites maiores —
# um POST em /api/auth/login (300s) zerava o contador de forgot-password (900s).
#
# O bug só aparece com tempo DECORRIDO: as marcas precisam ser mais velhas que a
# janela curta e mais novas que a longa. Em vez de esperar, envelhecemos as
# marcas na mão — é o mesmo estado que o processo teria minutos depois.
rl._BUCKETS.clear()
rl._LAST_SWEEP["ts"] = 0.0
for _ in range(5):
    rl.consumir("limite_longo", maximo=5, janela_seg=900)
check("Limite longo estourou após 5 tentativas",
      rl.consumir("limite_longo", maximo=5, janela_seg=900) is False)

import time as _time
_janela, _marcas = rl._BUCKETS["limite_longo"]
for _i in range(len(_marcas)):
    _marcas[_i] = _marcas[_i] - 60.0        # 1 minuto atrás: velho para 1s, novo para 900s
check("Janela fica guardada junto do bucket", _janela == 900, f"janela={_janela}")

rl._LAST_SWEEP["ts"] = _time.monotonic() - rl._SWEEP_INTERVAL - 1   # força a varredura
rl.consumir("limite_curto", maximo=10, janela_seg=1)   # janela curta dispara o sweep

check("Varredura com janela curta NÃO zerou o limite longo",
      rl.consumir("limite_longo", maximo=5, janela_seg=900) is False,
      "o contador de 900s foi apagado pela poda de 1s")
check("Bucket longo preservou suas 5 marcas",
      len(rl._BUCKETS["limite_longo"][1]) == 5,
      f"marcas={len(rl._BUCKETS.get('limite_longo', (0, []))[1])}")


# ============================================================================
print("\n=== 7. Auditoria não roda sobre dados fictícios ===")
import backend.app.services.integrations.sieg_integration as si

fonte_si = io.open(si.__file__, encoding="utf-8").read()
check("Notas fictícias saíram do serviço de integração",
      "Malha Fina LTDA" not in fonte_si, "mock ainda presente")
check("fetch_xmls falha explicitamente em vez de devolver mock",
      "NotImplementedError" in fonte_si)

fonte_up = io.open(os.path.join("backend", "app", "api", "routes", "uploads.py"),
                   encoding="utf-8").read()
check("Rota de upload não invoca mais o SIEG em background",
      "SiegIntegrationService" not in fonte_up, "ramo do mock ainda no background")
check("CNPJ e período fixos saíram do código",
      '"00000000000000"' not in fonte_up and '"2023-01-01"' not in fonte_up)


# ============================================================================
print("\n=== 8. NÃO existe regra global: cada tenant só vê as suas ===")
# O escopo era `tenant_id == meu OU is_global`, então uma regra gravada sob um
# tenant fantasma decidia crédito de ICMS/IPI em TODAS as empresas do sistema.
# Decisão do escritório (25/08/2026): ICMS e IPI seguem a linha do CFOP — a
# regra é do tenant, ponto.
from backend.app.infrastructure.repositories.rules_repository import RulesRepository

db3 = SessionLocal()
repo_b = RulesRepository(db3, tB)
cfops = {r.cfop for r in repo_b.credit_rules.get_all()}
check("Regra is_global de outro tenant NÃO aparece (get_all)", "1999" not in cfops, f"cfops={cfops}")
check("Regra própria do tenant continua aparecendo", "1888" in cfops, f"cfops={cfops}")
check("Regra is_global NÃO aparece na consulta unitária (get_credit_rule)",
      repo_b.get_credit_rule("1999") is None)
check("Consulta unitária acha a regra do próprio tenant",
      repo_b.get_credit_rule("1888") is not None)

repo_a = RulesRepository(db3, tA)
cfops_a = {r.cfop for r in repo_a.credit_rules.get_all()}
check("Regra privada da Empresa B não vaza para a Empresa A",
      "1888" not in cfops_a, f"cfops_a={cfops_a}")
check("Empresa A também NÃO enxerga a regra is_global", "1999" not in cfops_a, f"cfops_a={cfops_a}")

repo_dono = RulesRepository(db3, "tenant-dono-da-global")
check("O tenant dono da regra continua enxergando a dele",
      "1999" in {r.cfop for r in repo_dono.credit_rules.get_all()})
db3.close()


# ============================================================================
print("\n=== 9. Logout encerra a sessão de verdade ===")
# Apagar o cookie não bastava: o JWT valia por uma semana e o cache de tokens
# ainda o aceitava por até 30s.
import backend.app.api.dependencies.auth as da

da._token_cache.clear()
r = as_("cliA").get("/api/status/task-a")
check("Sessão ativa antes do logout", r.status_code == 200, f"status={r.status_code}")
check("Token entrou no cache", len(da._token_cache) >= 1)

check("Cache é indexado por hash, não pelo JWT em claro",
      all(len(k) == 64 and all(ch in "0123456789abcdef" for ch in k)
          for k in da._token_cache),
      f"chaves={list(da._token_cache)[:1]}")

as_("cliA").get("/logout", follow_redirects=False)
check("Logout removeu o token do cache", len(da._token_cache) == 0,
      f"restaram={len(da._token_cache)}")

fonte_auth = io.open(os.path.join("backend", "app", "api", "routes", "auth.py"),
                     encoding="utf-8").read()
check("Logout revoga a sessão no provedor", "sign_out" in fonte_auth)
check("Reset de senha derruba as sessões existentes",
      fonte_auth.count("sign_out") >= 2, "só o logout revoga")


# ============================================================================
print("\n=== 10. Erros internos não vazam detalhe do banco ===")
for caminho in [os.path.join("backend", "app", "api", "routes", "admin.py"),
                os.path.join("backend", "app", "main.py")]:
    conteudo = io.open(caminho, encoding="utf-8").read()
    check(f"{os.path.basename(caminho)} não devolve str(e) ao browser",
          "{str(e)}" not in conteudo, "ainda interpola a exceção na resposta")
    check(f"{os.path.basename(caminho)} registra a falha no log",
          "logger.exception(" in conteudo)

conteudo_admin = io.open(os.path.join("backend", "app", "api", "routes", "admin.py"),
                         encoding="utf-8").read()
check("Nenhuma exceção é interpolada dentro de Swal.fire (quebrava a string JS)",
      "Swal.fire('Erro', 'Ocorreu um erro ao salvar a regra: {" not in conteudo_admin)


# ============================================================================
print("\n=== 11. Contrato frontend <-> backend do proxy SIEG ===")
# A rota passou a exigir que a chave esteja amarrada ao perfil, e o task_id da
# auditoria é essa prova. Se os links do template perderem o parâmetro, a tela
# volta a funcionar só para admin — falha silenciosa para todo cliente.
tpl = io.open(os.path.join("frontend", "templates", "partials", "curation",
                           "curation_results.html"), encoding="utf-8").read()

import re as _re
links = _re.findall(r'href="/api/sieg/danfe/[^"]*"', tpl)
sem_task = [l for l in links if "task_id=" not in l]
check("Template tem links de DANFE", len(links) > 0, "nenhum link encontrado")
check("Todo link de DANFE carrega task_id", sem_task == [], f"sem task_id: {sem_task[:2]}")
check("O link montado em JS também leva o task_id", "taskIdSieg" in tpl)

# Ponta a ponta: a chave sai do result_data e volta autorizada pelo task_id.
db4 = SessionLocal()
db4.add(AuditTask(id="task-render", tenant_id=tA, status="concluido",
                  user_id=str(ua_cliA), file_name="sped.txt",
                  result_data='{"resumo": {"notas_processadas": 1}, '
                              '"unrecorded_invoices": [{"key": "%s"}]}' % CHAVE_ORFA))
db4.commit()
db4.close()

r = as_("cliA").get(f"/api/sieg/danfe/{CHAVE_ORFA}?task_id=task-render")
check("Chave citada no resultado da auditoria é liberada pelo task_id",
      r.status_code == 200, f"status={r.status_code}")

r = as_("cliB").get(f"/api/sieg/danfe/{CHAVE_ORFA}?task_id=task-render")
check("Mesma chave segue negada para outra empresa",
      r.status_code == 404, f"status={r.status_code}")

r = as_("cliA").get(f"/api/sieg/danfe/{CHAVE_ORFA}?task_id=task-inexistente")
check("task_id inexistente não vira passe livre",
      r.status_code == 404, f"status={r.status_code}")


# ============================================================================
print("\n" + "=" * 60)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("FALHAS:")
    for nome in _falhou:
        print(f"  - {nome}")
sys.exit(1 if FALHAS else 0)
