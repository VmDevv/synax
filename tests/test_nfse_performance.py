"""Desempenho das telas de certificado e visibilidade das falhas do ADN.

Dois problemas reais motivaram esta suíte:
  1) as telas de Certificados e Importar NFS-e faziam uma consulta ao banco POR
     EMPRESA — com 167 empresas eram ~29 s só para desenhar a tela;
  2) qualquer recusa do ADN terminava num `return` mudo: o operador via
     "0 notas" sem nenhuma pista do motivo.
"""
import os, sys, time, uuid, types, inspect, tempfile, shutil
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

_TMP_STORAGE = tempfile.mkdtemp(prefix="nfse_perf_")
_CERT_DIR = tempfile.mkdtemp(prefix="certs_perf_")
os.environ["NFSE_STORAGE_DIR"] = _TMP_STORAGE
os.environ["CERTIFICADOS_DIR"] = _CERT_DIR
os.environ["DATABASE_URL"] = "sqlite:///./_nfse_perf_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_nfse_perf_test.db"):
    os.remove("_nfse_perf_test.db")

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
class _Client:
    auth = _Auth()
sb.get_supabase_client = lambda: _Client()
sb.get_supabase_admin_client = lambda: _Client()
import backend.app.api.dependencies.auth as dep_auth
dep_auth.get_supabase_client = lambda: _Client()

from backend.app.infrastructure.database.session import engine as _eng
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit("ABORTADO: engine nao e SQLite")

import backend.app.main as m
from backend.app.infrastructure.database.session import SessionLocal, engine
from backend.app.domain.models.base import Base
from backend.app.domain.models.auth_models import Tenant, UserProfile
from backend.app.domain.models.certificates import TenantCertificate
from backend.app.services.nfse import certificados, adn_client
from backend.app.core.security import encrypt_data
from fastapi.testclient import TestClient
from sqlalchemy import event

Base.metadata.create_all(bind=engine)

results = []
def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("  OK  " if cond else " FALHA") + f" | {name}" + (f"  [{detail}]" if detail and not cond else ""))

client = TestClient(m.app)

def as_(chave):
    c = TestClient(m.app)
    if chave:
        c.cookies.set("access_token", chave)
    return c

# ---------- massa: 60 empresas, metade com certificado ----------
N = 60
s = SessionLocal()
ids = []
for i in range(N):
    cnpj = f"{11000000000000 + i}"
    t = Tenant(id=uuid.uuid4(), name=f"Empresa {i:03d}", cnpj=cnpj)
    s.add(t); s.flush()
    ids.append(str(t.id))
    if i % 2 == 0:
        nome = f"cert_{cnpj}.pfx"
        with open(os.path.join(_CERT_DIR, nome), "wb") as f:
            f.write(b"x")
        s.add(TenantCertificate(tenant_id=str(t.id), cnpj=cnpj, arquivo=nome,
                                senha_encrypted=encrypt_data("segredo"), ativo=True))

adm_id = uuid.uuid4()
s.add(UserProfile(id=adm_id, name="Admin", role="admin", tenant_id=None))
s.commit(); s.close()
TOKENS["adm"] = types.SimpleNamespace(id=str(adm_id), email="a@a.com")

print("\n=== 1. Carga em lote (fim do N+1) ===")

s = SessionLocal()
registros = certificados.carregar_todos(s)
check("carregar_todos traz todos os certificados de uma vez", len(registros) == N // 2,
      f"{len(registros)} != {N//2}")
alguns = certificados.carregar_todos(s, ids[:6])
check("carregar_todos filtra por lista de empresas", len(alguns) == 3, f"{len(alguns)}")
check("lista vazia nao vira consulta", certificados.carregar_todos(s, []) == {})
s.close()

arquivos = certificados.listar_arquivos()
d_ok = certificados.diagnosticar(registros[ids[0]], "11000000000000", arquivos)
check("diagnosticar aprova empresa com arquivo presente", d_ok["ok"], str(d_ok))
d_sem = certificados.diagnosticar(None, "11000000000001", arquivos)
check("diagnosticar reprova empresa sem cadastro", not d_sem["ok"] and "Sem certificado" in d_sem["motivo"])

# diagnosticar nao pode tocar o banco: sua assinatura nao recebe Session
params = inspect.signature(certificados.diagnosticar).parameters
check("diagnosticar nao recebe sessao de banco", "db" not in params, str(list(params)))

print("\n=== 2. Telas nao consultam o banco por empresa ===")

consultas = {"n": 0}
@event.listens_for(engine, "before_cursor_execute")
def _contar(conn, cursor, stmt, params_, ctx, many):
    consultas["n"] += 1

consultas["n"] = 0
r = as_("adm").get("/admin/certificados")
q_cert = consultas["n"]
check("aba Certificados responde 200", r.status_code == 200, f"status={r.status_code}")
check(f"aba Certificados nao faz 1 consulta por empresa ({q_cert} para {N} empresas)",
      q_cert < N, f"{q_cert} consultas")

consultas["n"] = 0
r = as_("adm").get("/nfse/drawer")
q_drawer = consultas["n"]
check("drawer de NFS-e responde 200", r.status_code == 200, f"status={r.status_code}")
check(f"drawer nao faz 1 consulta por empresa ({q_drawer} para {N} empresas)",
      q_drawer < N, f"{q_drawer} consultas")

event.remove(engine, "before_cursor_execute", _contar)

import backend.app.api.routes.admin as rota_admin
import backend.app.api.routes.nfse as rota_nfse
src_admin = inspect.getsource(rota_admin.listar_certificados)
check("rota de certificados usa a carga em lote", "carregar_todos" in src_admin)
check("rota de certificados nao chama diagnostico(db, ...)", "svc.diagnostico(" not in src_admin)
src_drawer = inspect.getsource(rota_nfse.drawer)
check("drawer usa a carga em lote", "carregar_todos" in src_drawer)
check("drawer nao chama diagnostico(db, ...)", "certificados.diagnostico(" not in src_drawer)

print("\n=== 3. Paginacao da aba Certificados ===")

r = as_("adm").get("/admin/certificados?per_page=10")
check("pagina 1 mostra no maximo 10 empresas", r.text.count('name="arquivo"') == 10,
      str(r.text.count('name="arquivo"')))
check("mostra o total de empresas", f"{N}" in r.text)
r2 = as_("adm").get("/admin/certificados?per_page=10&page=2")
check("pagina 2 traz outras empresas", "Empresa 010" in r2.text and "Empresa 000" not in r2.text)
r3 = as_("adm").get("/admin/certificados?per_page=10&page=999")
check("pagina alem do fim volta para a ultima", r3.status_code == 200 and 'name="arquivo"' in r3.text)
r4 = as_("adm").get("/admin/certificados?q=Empresa+007")
check("busca por nome filtra", r4.text.count('name="arquivo"') == 1,
      str(r4.text.count('name="arquivo"')))
r5 = as_("adm").get("/admin/certificados?situacao=pendentes&per_page=200")
check("filtro de pendentes esconde os prontos", "Empresa 001" in r5.text and "Empresa 000" not in r5.text)

print("\n=== 3b. A pagina nao repete a lista de arquivos em cada linha ===")

# Cada linha repetia um <select> com os 172 arquivos da pasta: 25 linhas viravam
# 4.300 <option> e 1,2 MB de HTML — era isso que impedia a aba de abrir.
r = as_("adm").get("/admin/certificados?per_page=25")
opcoes = r.text.count("<option")
peso_kb = len(r.text) / 1024
check(f"poucas <option> no HTML ({opcoes} para {len(arquivos)} arquivos x 25 linhas)",
      opcoes < 60, f"{opcoes} opcoes")
check(f"pagina leve ({peso_kb:.0f} KB)", peso_kb < 250, f"{peso_kb:.0f} KB")
check("lista de arquivos entregue uma vez so, em JSON",
      r.text.count('id="certs-arquivos"') == 1)
check("dropdown se completa no primeiro clique", "synPreencherCert" in r.text)

print("\n=== 3c. Arquivo inexistente e recusado ===")

alvo = ids[0]
r = as_("adm").post(f"/admin/certificados/{alvo}",
                    data={"arquivo": "nao_existe.pfx", "senha": "x", "ativo": "1"})
check("arquivo fora da pasta e recusado", "não está na pasta" in r.text, r.text[:200])
s = SessionLocal()
reg = s.query(TenantCertificate).filter_by(tenant_id=alvo).first()
check("vinculo antigo preservado apos a recusa", reg.arquivo != "nao_existe.pfx", str(reg.arquivo))
s.close()

r = as_("adm").post(f"/admin/certificados/{alvo}",
                    data={"arquivo": "../../fora.pfx", "senha": "x", "ativo": "1"})
check("caminho relativo nao escapa da pasta", "não está na pasta" in r.text)

print("\n=== 3d. Validade dos certificados: badges e resumo ===")

from datetime import date, datetime, timedelta
hoje = date.today()

def _cert(dias):
    """Registro falso com validade a N dias de hoje (None = não verificado)."""
    class R:
        ativo = True
        validade = None if dias is None else datetime.combine(
            hoje + timedelta(days=dias), datetime.min.time())
    return R()

ok_diag = {"ok": True}
casos = [
    (365, "emerald", "ok",           "365 dias no verde"),
    (31,  "emerald", "ok",           "31 dias ainda no verde"),
    (30,  "amber",   "alerta",       "30 dias ja e alerta"),
    (1,   "amber",   "alerta",       "vespera do vencimento"),
    (0,   "amber",   "alerta",       "vence hoje"),
    (-1,  "red",     "problema",     "vencido ontem"),
    (-45, "red",     "problema",     "vencido ha 45 dias"),
    (None,"slate",   "desconhecido", "sem validade lida"),
]
for dias, cor, grupo, rotulo in casos:
    v = certificados.classificar_validade(_cert(dias), ok_diag, hoje)
    check(f"{rotulo}: {cor}/{grupo}", v["cor"] == cor and v["grupo"] == grupo,
          f"{v['cor']}/{v['grupo']} — {v['rotulo']}")

v = certificados.classificar_validade(_cert(120), ok_diag, hoje)
check("rotulo do verde traz os dias", "120" in v["rotulo"], v["rotulo"])
check("data devolvida para a tela", v["validade"] == hoje + timedelta(days=120))

v = certificados.classificar_validade(None, {"ok": False, "motivo": "Arquivo ausente na pasta"}, hoje)
check("arquivo ausente e vermelho", v["cor"] == "red" and v["grupo"] == "problema", str(v))

v = certificados.classificar_validade(_cert(200), {"ok": False, "motivo": "Certificado inativo"}, hoje)
check("inativo nao conta como problema", v["grupo"] == "desconhecido", str(v))

# A tela nao pode voltar a abrir os .pfx: sao ~36s para os 164.
src_lista = inspect.getsource(rota_admin.listar_certificados)
check("listagem nao le arquivo .pfx", "ler_validade" not in src_lista
      and "atualizar_validades" not in src_lista)
check("listagem usa a validade ja gravada", "classificar_validade" in src_lista)

r = as_("adm").get("/admin/certificados?per_page=10")
check("coluna Validade / Status na tabela", "Validade / Status" in r.text)
check("cartao de resumo presente", "A vencer" in r.text and "Com problema" in r.text)
check("botao de verificar validades", "/admin/certificados/validades" in r.text)

print("\n=== 3e. Resumo conta TODAS as empresas, nao so a pagina ===")

s = SessionLocal()
regs = s.query(TenantCertificate).order_by(TenantCertificate.cnpj).all()
for i, reg in enumerate(regs):                 # 30 certificados na massa
    reg.validade = datetime.combine(hoje + timedelta(days=[400, 10, -5][i % 3]),
                                    datetime.min.time())
s.commit(); s.close()

import re as _re
def _num(texto, titulo):
    """Le o numero do cartao de resumo daquele titulo."""
    m = _re.search(titulo + r".*?>(\d+)</div>", texto, _re.S)
    return int(m.group(1)) if m else -1

r = as_("adm").get("/admin/certificados?per_page=10")
verdes, amarelos, vermelhos = (_num(r.text, t) for t in ("Válidos", "A vencer", "Com problema"))
check(f"resumo enxerga alem da pagina (verde={verdes}, pagina=10)", verdes == 10,
      f"verde={verdes}")
check(f"amarelos contados ({amarelos})", amarelos == 10, f"{amarelos}")
check(f"vermelhos incluem expirados e sem certificado ({vermelhos})", vermelhos == 40,
      f"{vermelhos}")
check("os quatro grupos somam o total de empresas",
      verdes + amarelos + vermelhos + _num(r.text, "Não verificados") == N,
      f"{verdes}+{amarelos}+{vermelhos}+{_num(r.text, 'Não verificados')} != {N}")

r = as_("adm").get("/admin/certificados?situacao=alerta&per_page=200")
check("filtro 'a vencer' devolve so os amarelos", r.text.count('name="arquivo"') == 10,
      str(r.text.count('name="arquivo"')))
r = as_("adm").get("/admin/certificados?situacao=problema&per_page=200")
check("filtro 'com problema' devolve so os vermelhos", r.text.count('name="arquivo"') == 40,
      str(r.text.count('name="arquivo"')))
check("resumo nao encolhe quando o filtro e aplicado", _num(r.text, "Válidos") == 10,
      str(_num(r.text, "Válidos")))

print("\n=== 3f. Atualizacao das validades ===")

# A rota estatica precisa vencer a dinamica /certificados/{tenant_id}: o FastAPI
# casa na ordem de registro e "validades" seria lido como um id de empresa.
rotas = [r_.path for r_ in rota_admin.router.routes
         if "POST" in getattr(r_, "methods", set())]
i_estatica = rotas.index("/certificados/validades")
i_dinamica = rotas.index("/certificados/{tenant_id}")
check("rota /validades registrada antes da dinamica", i_estatica < i_dinamica,
      f"estatica={i_estatica} dinamica={i_dinamica}")

r = as_("adm").post("/admin/certificados/validades")
check("acao de verificar responde 200", r.status_code == 200, f"status={r.status_code}")
check("resultado e informado ao usuario", "certificado(s) lido(s)" in r.text)
# os .pfx da massa sao arquivos falsos: precisam ser reportados, nao ignorados
check("arquivos que nao abrem sao contados", "não abriram" in r.text, r.text[:200])

print("\n=== 4. Drawer entrega a lista pronta ao navegador ===")

r = as_("adm").get("/nfse/drawer")
check("drawer manda os dados em JSON para o Alpine", '"busca":' in r.text and 'x-for="e in visiveis"' in r.text)
check("drawer limita o que desenha de inicio", "limite: 40" in r.text)
check("selecao sobrevive ao filtro (campos ocultos)",
      'x-for="id in sel"' in r.text and 'name="tenant_ids"' in r.text)

print("\n=== 5. O ADN nunca falha em silencio ===")

class _Resp:
    def __init__(self, status, texto, json_=None, ct="application/json"):
        self.status_code = status; self.text = texto
        self._json = json_; self.headers = {"content-type": ct}
    def json(self):
        if self._json is None:
            raise ValueError("nao e json")
        return self._json

class _ClienteFake:
    def __init__(self, resposta): self.resposta = resposta; self.urls = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, **kw):
        self.urls.append(url)
        return self.resposta

import contextlib
@contextlib.contextmanager
def _material_falso(pfx, senha):
    yield ("cert.pem", "key.pem")

_mtls_real, _http_real = adn_client._material_mtls, adn_client.httpx.Client
adn_client._material_mtls = _material_falso

def rodar(resposta):
    fake = _ClienteFake(resposta)
    adn_client.httpx.Client = lambda **kw: fake
    try:
        return list(adn_client.baixar_lote(b"x", "s", "11000000000000", 0)), fake
    finally:
        adn_client.httpx.Client = _http_real

# 500 do servidor -> erro visivel
try:
    rodar(_Resp(500, "Internal Server Error"))
    check("HTTP 500 vira erro visivel", False, "nao levantou")
except adn_client.RespostaInesperada as err:
    check("HTTP 500 vira erro visivel", "500" in str(err), str(err))

# 400 (formato de chamada errado) -> erro visivel, nao silencio
try:
    rodar(_Resp(400, '{"erro":"NSU invalido"}', {"erro": "NSU invalido"}))
    check("HTTP 400 vira erro visivel", False, "nao levantou")
except adn_client.RespostaInesperada as err:
    check("HTTP 400 vira erro visivel", "400" in str(err), str(err))

# 403 -> mensagem sobre procuracao
try:
    rodar(_Resp(403, "forbidden"))
    check("HTTP 403 fala de procuracao", False, "nao levantou")
except adn_client.CertificadoInvalido as err:
    check("HTTP 403 fala de procuracao", "procuração" in str(err).lower(), str(err))

# 200 com HTML (portal de erro) -> erro visivel
try:
    rodar(_Resp(200, "<html>manutencao</html>", None, ct="text/html"))
    check("200 nao-JSON vira erro visivel", False, "nao levantou")
except adn_client.RespostaInesperada as err:
    check("200 nao-JSON vira erro visivel", "JSON" in str(err), str(err))

# 200 com corpo desconhecido -> erro visivel (antes: 0 notas em silencio)
try:
    rodar(_Resp(200, '{"algo":"inesperado"}', {"algo": "inesperado"}))
    check("200 com corpo estranho vira erro visivel", False, "nao levantou")
except adn_client.RespostaInesperada as err:
    check("200 com corpo estranho vira erro visivel", True)

# 200 com lote vazio declarado -> NAO e erro
try:
    docs, _ = rodar(_Resp(200, '{"LoteDFe":[]}', {"LoteDFe": []}))
    check("lote vazio declarado nao e erro", docs == [])
except Exception as err:
    check("lote vazio declarado nao e erro", False, f"{type(err).__name__}: {err}")

try:
    docs, _ = rodar(_Resp(200, '{"StatusProcessamento":"NENHUM_DOCUMENTO_LOCALIZADO"}',
                          {"StatusProcessamento": "NENHUM_DOCUMENTO_LOCALIZADO"}))
    check("status 'nenhum documento' nao e erro", docs == [])
except Exception as err:
    check("status 'nenhum documento' nao e erro", False, f"{type(err).__name__}: {err}")

# 404 -> fim da fila, sem erro
docs, _ = rodar(_Resp(404, ""))
check("404 encerra sem erro", docs == [])

print("\n=== 5b. A consulta em lote nao pode rodar para sempre ===")

# 167 empresas x 40 paginas x 90s de timeout deixava a tela "consultando" por
# horas. Agora existe teto por empresa e teto para o lote inteiro.
check("timeout de conexao curto", adn_client.TEMPO_LIMITE.connect <= 15,
      str(adn_client.TEMPO_LIMITE.connect))
check("timeout de leitura curto", adn_client.TEMPO_LIMITE.read <= 45,
      str(adn_client.TEMPO_LIMITE.read))
check("existe teto por empresa", adn_client.LIMITE_EMPRESA_SEG <= 120,
      str(adn_client.LIMITE_EMPRESA_SEG))
check("existe orcamento para o lote inteiro", rota_nfse.ORCAMENTO_LOTE_SEG <= 600,
      str(rota_nfse.ORCAMENTO_LOTE_SEG))
check("o lote respeita o orcamento", "prazo" in inspect.getsource(rota_nfse.consultar_lote))
check("empresas nao consultadas sao informadas",
      "nao_consultadas" in inspect.getsource(rota_nfse.consultar_lote))

# o teto de tempo interrompe de verdade a paginacao
class _RespLote:
    status_code = 200
    text = "{}"
    headers = {"content-type": "application/json"}
    def json(self):
        # sempre um lote cheio com NSU crescente: sem o teto, lacaria ate 40 paginas
        _RespLote.n += 1
        return {"LoteDFe": [{"NSU": _RespLote.n * 100 + i,
                             "ArquivoXml": _b64gz(f"<xml>{i}</xml>")} for i in range(50)]}
_RespLote.n = 0

import base64 as _b64, gzip as _gz
def _b64gz(txt):
    return _b64.b64encode(_gz.compress(txt.encode())).decode()

class _ClienteLento:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, **kw):
        time.sleep(0.05)
        return _RespLote()

adn_client._material_mtls = _material_falso
adn_client.httpx.Client = lambda **kw: _ClienteLento()
try:
    t0 = time.time()
    docs = list(adn_client.baixar_lote(b"x", "s", "11000000000000", 0, limite_seg=0.2))
    gasto = time.time() - t0
    check(f"o teto interrompe a paginacao ({gasto:.2f}s, {len(docs)} docs)",
          gasto < 2.0 and len(docs) < 40 * 50, f"{gasto:.2f}s / {len(docs)} docs")
finally:
    adn_client.httpx.Client = _http_real

print("\n=== 6. NSU vai no caminho da URL ===")

check("URL do NSU 0", adn_client._url_do_nsu(0).endswith("/DFe/0"), adn_client._url_do_nsu(0))
check("URL do NSU 4321", adn_client._url_do_nsu(4321).endswith("/DFe/4321"))
_, fake = rodar(_Resp(404, ""))
check("chamada real usa o NSU no caminho", fake.urls and fake.urls[0].endswith("/0"),
      str(fake.urls))
src_lote = inspect.getsource(adn_client.baixar_lote)
check("nao passa mais NSU em query string", 'params={"NSU"' not in src_lote)

adn_client._material_mtls = _mtls_real

print("\n=== 7. Resumo por empresa chega na tela ===")

tpl = open("frontend/templates/partials/nfse/tabela.html", encoding="utf-8").read()
check("tabela renderiza os resumos do lote", "{% if resumos %}" in tpl)
check("tabela mostra o erro de cada empresa", "r.erro" in tpl)
src_lote_rota = inspect.getsource(rota_nfse.consultar_lote)
check("rota do lote envia os resumos ao template", '"resumos"' in src_lote_rota)
src_uma = inspect.getsource(rota_nfse._buscar_uma_empresa)
check("resposta inesperada do ADN vira mensagem apresentavel",
      "RespostaInesperada" in src_uma)

engine.dispose()
if os.path.exists("_nfse_perf_test.db"):
    try: os.remove("_nfse_perf_test.db")
    except Exception: pass
shutil.rmtree(_TMP_STORAGE, ignore_errors=True)
shutil.rmtree(_CERT_DIR, ignore_errors=True)

fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
