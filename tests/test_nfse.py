"""Módulo de Notas de Serviço (NFS-e).

Cobre: parser ABRASF, isolamento por empresa, filtros, paginação, ZIP e
gravação em disco. Também confirma que o módulo NÃO alterou o motor existente.
"""
import os, sys, uuid, types, io, zipfile, tempfile, shutil
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

_TMP_STORAGE = tempfile.mkdtemp(prefix="nfse_test_")
os.environ["NFSE_STORAGE_DIR"] = _TMP_STORAGE
os.environ["DATABASE_URL"] = "sqlite:///./_nfse_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_nfse_test.db"):
    os.remove("_nfse_test.db")

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
from backend.app.domain.models.auth_models import Tenant, UserProfile, UserTenant
from backend.app.domain.models.nfse import NfseDocument
from backend.app.services.nfse.abrasf_parser import parse_nfse_xml
from fastapi.testclient import TestClient

Base.metadata.create_all(bind=engine)

results = []
def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(("  OK  " if cond else " FALHA") + f" | {name}" + (f"  [{detail}]" if detail and not cond else ""))

# ---------- XML ABRASF de exemplo (namespace + blocos aninhados) ----------
XML_NFSE = """<?xml version="1.0" encoding="UTF-8"?>
<ConsultarNfseResposta xmlns="http://www.abrasf.org.br/nfse.xsd">
 <ListaNfse><CompNfse><Nfse><InfNfse>
  <Numero>298231</Numero>
  <CodigoVerificacao>ABC123XY</CodigoVerificacao>
  <DataEmissao>2026-07-16T10:22:00</DataEmissao>
  <Competencia>2026-07-01T00:00:00</Competencia>
  <ValoresNfse><BaseCalculo>267.26</BaseCalculo><Aliquota>2.00</Aliquota>
    <ValorIss>5.35</ValorIss><ValorLiquidoNfse>267.26</ValorLiquidoNfse></ValoresNfse>
  <PrestadorServico>
    <IdentificacaoPrestador><Cnpj>03813799000123</Cnpj>
      <InscricaoMunicipal>11994101</InscricaoMunicipal></IdentificacaoPrestador>
    <RazaoSocial>COMLINK S. A.</RazaoSocial>
    <Endereco><Uf>SP</Uf><CodigoMunicipio>3543402</CodigoMunicipio></Endereco>
  </PrestadorServico>
  <DeclaracaoPrestacaoServico><InfDeclaracaoPrestacaoServico>
    <Servico>
      <Valores><ValorServicos>267.26</ValorServicos><ValorPis>1.26</ValorPis>
        <ValorCofins>5.83</ValorCofins><ValorInss>0</ValorInss><ValorIr>0</ValorIr>
        <ValorCsll>0</ValorCsll><OutrasRetencoes>0</OutrasRetencoes>
        <ValorDeducoes>0</ValorDeducoes><DescontoIncondicionado>0</DescontoIncondicionado>
        <DescontoCondicionado>0</DescontoCondicionado></Valores>
      <IssRetido>2</IssRetido>
      <ItemListaServico>0105</ItemListaServico>
      <Discriminacao>SERVICO DE LINK DEDICADO DE INTERNET</Discriminacao>
    </Servico>
    <Tomador>
      <IdentificacaoTomador><CpfCnpj><Cnpj>48135251000100</Cnpj></CpfCnpj></IdentificacaoTomador>
      <RazaoSocial>SETE INDUSTRIA E COMERCIO LTDA</RazaoSocial>
      <Endereco><Uf>ES</Uf><CodigoMunicipio>3203205</CodigoMunicipio></Endereco>
    </Tomador>
  </InfDeclaracaoPrestacaoServico></DeclaracaoPrestacaoServico>
 </InfNfse></Nfse></CompNfse></ListaNfse>
</ConsultarNfseResposta>"""

XML_CANCELADA = XML_NFSE.replace(
    "<Numero>298231</Numero>",
    "<Numero>298232</Numero><DataCancelamento>2026-07-20T08:00:00</DataCancelamento>")

print("\n=== 1. Parser ABRASF ===")
fd, tmp = tempfile.mkstemp(suffix=".xml")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(XML_NFSE)
d = parse_nfse_xml(tmp)
os.remove(tmp)
check("reconhece a nota", d is not None)
check("numero", d and d["numero"] == "298231", f"={d.get('numero') if d else None}")
check("prestador (nao confunde com tomador)", d and d["prestador"] == "03813799000123",
      f"={d.get('prestador') if d else None}")
check("tomador", d and d["tomador"] == "48135251000100", f"={d.get('tomador') if d else None}")
check("razao do prestador", d and d["rz_prestador"] == "COMLINK S. A.")
check("UF do prestador", d and d["uf_prest"] == "SP", f"={d.get('uf_prest') if d else None}")
check("UF do tomador separada", d and d["uf_toma"] == "ES", f"={d.get('uf_toma') if d else None}")
check("valor do servico", d and abs(d["valor_servico"] - 267.26) < 0.01)
check("ISS", d and abs(d["iss"] - 5.35) < 0.01)
check("PIS", d and abs(d["pis"] - 1.26) < 0.01)
check("COFINS", d and abs(d["cofins"] - 5.83) < 0.01)
check("aliquota", d and abs(d["aliquota"] - 2.0) < 0.01)
check("codigo do servico", d and d["cod_servico"] == "0105")
check("discriminacao", d and "LINK DEDICADO" in d["discriminacao"])
check("ISS retido = Nao", d and d["iss_retido"] == "Não", f"={d.get('iss_retido') if d else None}")
check("status padrao autorizado", d and "Autorizado" in d["status"])

fd, tmp = tempfile.mkstemp(suffix=".xml")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(XML_CANCELADA)
dc = parse_nfse_xml(tmp)
os.remove(tmp)
check("detecta cancelamento", dc and dc["dt_cancelamento"] is not None)

fd, tmp = tempfile.mkstemp(suffix=".xml")
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write("<qualquer><coisa>1</coisa></qualquer>")
check("ignora XML que nao e NFS-e", parse_nfse_xml(tmp) is None)
os.remove(tmp)

# ---------- Cenário multi-empresa ----------
db = SessionLocal()
tA, tB = uuid.uuid4(), uuid.uuid4()
db.add_all([Tenant(id=tA, name="Empresa A", cnpj="48135251000100"),
            Tenant(id=tB, name="Empresa B", cnpj="22222222222222")])
u_adm, u_cliB = uuid.uuid4(), uuid.uuid4()
db.add_all([UserProfile(id=u_adm, tenant_id=None, role="admin", name="Admin"),
            UserProfile(id=u_cliB, tenant_id=tB, role="cliente", name="Cliente B")])
db.commit(); db.close()
TOKENS["adm"] = types.SimpleNamespace(id=str(u_adm), email="admin@synax.com")
TOKENS["cliB"] = types.SimpleNamespace(id=str(u_cliB), email="cliB@synax.com")

from backend.app.api.dependencies.db import get_rls_db as _real_rls
def _fake_rls():
    s = SessionLocal()
    try: yield s
    finally: s.close()
m.app.dependency_overrides[_real_rls] = _fake_rls

c = TestClient(m.app, raise_server_exceptions=False)
def as_(tok):
    c.cookies.clear()
    if tok: c.cookies.set("access_token", f"Bearer {tok}")
    return c

print("\n=== 2. Importacao (XML solto e ZIP) ===")
r = as_("adm").post("/nfse/importar", data={"tenant_id": str(tA)},
                    files=[("arquivos", ("nota1.xml", XML_NFSE, "text/xml"))])
check("importa XML solto", r.status_code == 200, f"status={r.status_code}")

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("pasta/nota2.xml", XML_CANCELADA)
buf.seek(0)
r = as_("adm").post("/nfse/importar", data={"tenant_id": str(tA)},
                    files=[("arquivos", ("lote.zip", buf.read(), "application/zip"))])
check("importa ZIP", r.status_code == 200, f"status={r.status_code}")

s = SessionLocal()
docs = s.query(NfseDocument).filter_by(tenant_id=str(tA)).all()
check("2 notas indexadas", len(docs) == 2, f"n={len(docs)}")
check("retencoes somadas", any(abs(x.total_retencoes - 7.09) < 0.01 for x in docs))
check("cancelada identificada", any(x.cancelada for x in docs))
s.close()

print("\n=== 3. Arquivo gravado na pasta da EMPRESA, por dia ===")
# Desde 01/10/2026 a pasta e a da empresa (razao social + CNPJ), com uma
# subpasta por dia de execucao — e nao mais a do CNPJ do TOMADOR por mes.
from backend.app.services.nfse import storage as _storage
esperado = os.path.join(_TMP_STORAGE, "Empresa A 48135251000100", _storage.dia_de_hoje())
check("pasta EMPRESA CNPJ/data criada", os.path.isdir(esperado), f"esperado={esperado}")
check("XML salvo em disco", len(os.listdir(esperado)) >= 1 if os.path.isdir(esperado) else False)
check("nota ABRASF nomeada por prestador e numero, nao pelo nome enviado",
      os.path.isdir(esperado) and "NFSe_03813799000123_298231.xml" in os.listdir(esperado),
      os.listdir(esperado) if os.path.isdir(esperado) else None)
check("nada gravado na pasta do tomador por mes",
      not os.path.isdir(os.path.join(_TMP_STORAGE, "48135251000100")))

print("\n=== 4. Reimportar nao duplica ===")
r = as_("adm").post("/nfse/importar", data={"tenant_id": str(tA)},
                    files=[("arquivos", ("nota1.xml", XML_NFSE, "text/xml"))])
s = SessionLocal()
check("continua com 2 notas", s.query(NfseDocument).filter_by(tenant_id=str(tA)).count() == 2,
      f"n={s.query(NfseDocument).filter_by(tenant_id=str(tA)).count()}")
s.close()

print("\n=== 5. Isolamento entre empresas ===")
r = as_("cliB").get("/nfse/tabela")
check("cliente de outra empresa nao ve as notas", "298231" not in r.text, f"status={r.status_code}")
r = as_("cliB").get(f"/nfse/tabela?tenant_id={tA}")
check("forcar tenant alheio e barrado", r.status_code == 403, f"status={r.status_code}")
r = as_(None).get("/nfse/tabela")
check("anonimo barrado", r.status_code in (401, 403), f"status={r.status_code}")

print("\n=== 6. Tela, colunas e filtros ===")
r = as_("adm").get("/nfse")
check("pagina abre", r.status_code == 200, f"status={r.status_code}")
for rotulo in ["Número", "Valor Serviço", "ISS Retido", "CSLL", "INSS", "Base de Cálculo", "Status"]:
    check(f"coluna '{rotulo}' presente", rotulo in r.text)
check("botao de importar", "Importar NFS-e" in r.text)
check("botao de ZIP", "Baixar ZIP" in r.text)

r = as_("adm").get("/nfse/tabela?q_status=canceladas")
check("filtro de canceladas", "298232" in r.text and "298231" not in r.text)
r = as_("adm").get("/nfse/tabela?competencia=2026-07")
check("filtro de competencia traz as notas", "298231" in r.text)
r = as_("adm").get("/nfse/tabela?competencia=2026-01")
check("competencia sem notas fica vazia", "298231" not in r.text)
r = as_("adm").get("/nfse/tabela?q_texto=LINK")
check("busca livre pela discriminacao", "298231" in r.text)
r = as_("adm").get("/nfse/tabela?q_prestador=COMLINK")
check("filtro por prestador", "298231" in r.text)

print("\n=== 7. Paginacao ===")
r = as_("adm").get("/nfse/tabela?per_page=1")
check("paginacao ativa com 1 por pagina", "1 / 2" in r.text, "esperado 2 paginas")

print("\n=== 8. Download ZIP e XML ===")
r = as_("adm").post("/nfse/zip", data={"tenant_id": str(tA)})
check("ZIP responde 200", r.status_code == 200, f"status={r.status_code}")
check("conteudo e um ZIP valido", r.content[:2] == b"PK", f"inicio={r.content[:4]}")
z = zipfile.ZipFile(io.BytesIO(r.content))
check("ZIP contem os XMLs", len(z.namelist()) == 2, f"itens={z.namelist()}")
check("ZIP organizado por CNPJ", all(n.startswith("48135251000100/") for n in z.namelist()),
      f"itens={z.namelist()}")

r = as_("cliB").post("/nfse/zip", data={"tenant_id": str(tA)})
check("ZIP de empresa alheia e barrado", r.status_code == 403, f"status={r.status_code}")

r = as_("adm").get("/nfse/documento/03813799000123/298231/3543402/xml")
check("download do XML", r.status_code == 200 and b"<Numero>298231" in r.content,
      f"status={r.status_code}")
# 404 e nao 403: o recorte por empresa entra ANTES da busca, entao a nota alheia
# simplesmente nao existe para quem pediu. Um 403 confirmaria que ela existe.
r = as_("cliB").get("/nfse/documento/03813799000123/298231/3543402/xml")
check("XML de empresa alheia barrado", r.status_code == 404, f"status={r.status_code}")

r = as_("adm").get("/nfse/documento/03813799000123/298231/3543402/pdf")
check("PDF ausente responde com aviso claro", r.status_code == 404 and "DANFSe" in r.text)

print("\n=== 8b. Nota sem codigo de municipio (100% das notas hoje) ===")
# O ABRASF nem sempre traz CodigoMunicipio. Sem rotulo na URL o link saia como
# /documento/CNPJ/NUMERO//pdf — barra dupla que nao casa com a rota.
s = SessionLocal()
sem_mun = NfseDocument(
    tenant_id=str(tA), prestador="03813799000123", numero="777001", mun_prest="",
    rz_prestador="Prestador Sem Municipio", valor_servico=1500.0,
    arquivo_xml=s.query(NfseDocument).filter_by(numero="298231").first().arquivo_xml)
s.merge(sem_mun); s.commit(); s.close()

base = "/nfse/documento/03813799000123/777001"
r = as_("adm").get(f"{base}/sem_municipio/pdf")
check("rotulo sem_municipio acha a nota", r.status_code == 404 and "DANFSe" in r.text,
      f"status={r.status_code}")
check("pagina oferece o XML", 'sem_municipio/xml' in r.text)
check("pagina mostra o numero da nota", "777001" in r.text)

r = as_("adm").get(f"{base}/sem_municipio/xml")
check("XML da nota sem municipio baixa", r.status_code == 200, f"status={r.status_code}")

r = as_("adm").get(f"{base}/geral/xml")
check("rotulo 'geral' tambem aceito", r.status_code == 200, f"status={r.status_code}")

r = as_("adm").get(f"{base}/3543402/xml")
check("link antigo com municipio errado ainda resolve", r.status_code == 200,
      f"status={r.status_code}")

r = as_("cliB").get(f"{base}/sem_municipio/xml")
check("nota sem municipio respeita o isolamento", r.status_code == 404,
      f"status={r.status_code}")

r = as_("adm").get("/nfse/documento/03813799000123/000000/sem_municipio/pdf")
check("nota inexistente continua 404", r.status_code == 404)

r = as_("adm").get("/nfse/tabela")
check("tabela nao gera mais URL com barra dupla",
      "//pdf" not in r.text and "//xml" not in r.text)
check("tabela usa o rotulo no lugar do municipio vazio", "sem_municipio/pdf" in r.text)

print("\n=== 9. Consulta ao ADN (certificado + senha) ===")
from backend.app.domain.models.nfse import NfseCursor
from backend.app.services.nfse import adn_client

XML_ADN = XML_NFSE.replace("<Numero>298231</Numero>", "<Numero>900001</Numero>")
XML_ADN2 = XML_NFSE.replace("<Numero>298231</Numero>", "<Numero>900002</Numero>")

CHAMADAS = {"n": 0}
FALHAR_NO_MEIO = {"on": False}

def _fake_baixar(pfx, senha, cnpj, ultimo_nsu=0, max_paginas=40, limite_seg=None,
                 estado=None):
    """Dublê do ADN: entrega 2 documentos e opcionalmente falha após o 1º."""
    CHAMADAS["n"] += 1
    if ultimo_nsu < 10:
        yield (10, XML_ADN)
        if FALHAR_NO_MEIO["on"]:
            raise adn_client.CertificadoInvalido("queda simulada de rede")
        yield (11, XML_ADN2)

_real = adn_client.baixar_lote
adn_client.baixar_lote = _fake_baixar
import backend.app.api.routes.nfse as rota_nfse
rota_nfse.adn_client.baixar_lote = _fake_baixar

r = as_("adm").post("/nfse/consultar-adn",
                    data={"tenant_id": str(tA), "cnpj": "48135251000100", "senha": "x"},
                    files=[("certificado", ("cert.pfx", b"conteudo-falso", "application/x-pkcs12"))])
check("consulta ao ADN responde 200", r.status_code == 200, f"status={r.status_code}")

s = SessionLocal()
check("notas do ADN indexadas",
      s.query(NfseDocument).filter_by(tenant_id=str(tA), origem="adn").count() == 2,
      f"n={s.query(NfseDocument).filter_by(tenant_id=str(tA), origem='adn').count()}")
cur = s.query(NfseCursor).filter_by(tenant_id=str(tA), cnpj="48135251000100").first()
check("cursor de NSU gravado", cur is not None and cur.nsu == 11, f"nsu={cur.nsu if cur else None}")
s.close()

r = as_("adm").post("/nfse/consultar-adn",
                    data={"tenant_id": str(tA), "cnpj": "48135251000100", "senha": "x"},
                    files=[("certificado", ("cert.pfx", b"conteudo-falso", "application/x-pkcs12"))])
s = SessionLocal()
check("2a consulta nao duplica (cursor respeitado)",
      s.query(NfseDocument).filter_by(tenant_id=str(tA), origem="adn").count() == 2)
s.close()

print("\n=== 10. Queda no meio: nao perde nota (cursor conservador) ===")
s = SessionLocal()
cur = s.query(NfseCursor).filter_by(tenant_id=str(tA), cnpj="48135251000100").first()
cur.ultimo_nsu = "0"
s.commit()
s.query(NfseDocument).filter_by(tenant_id=str(tA), origem="adn").delete()
s.commit(); s.close()

FALHAR_NO_MEIO["on"] = True
r = as_("adm").post("/nfse/consultar-adn",
                    data={"tenant_id": str(tA), "cnpj": "48135251000100", "senha": "x"},
                    files=[("certificado", ("cert.pfx", b"conteudo-falso", "application/x-pkcs12"))])
FALHAR_NO_MEIO["on"] = False
check("falha do ADN devolve erro tratado", r.status_code == 400, f"status={r.status_code}")
s = SessionLocal()
salvas = s.query(NfseDocument).filter_by(tenant_id=str(tA), origem="adn").count()
cur = s.query(NfseCursor).filter_by(tenant_id=str(tA), cnpj="48135251000100").first()
check("a nota ja baixada foi PRESERVADA", salvas == 1, f"salvas={salvas}")
check("cursor parou no ultimo gravado (nao pulou)", cur.nsu == 10, f"nsu={cur.nsu}")
check("erro registrado no cursor", bool(cur.ultimo_erro))
s.close()

print("\n=== 11. Validacoes e isolamento da consulta ===")
r = as_("adm").post("/nfse/consultar-adn",
                    data={"tenant_id": str(tA), "cnpj": "123", "senha": "x"},
                    files=[("certificado", ("c.pfx", b"x", "application/x-pkcs12"))])
check("CNPJ invalido rejeitado", r.status_code == 400, f"status={r.status_code}")

r = as_("cliB").post("/nfse/consultar-adn",
                     data={"tenant_id": str(tA), "cnpj": "48135251000100", "senha": "x"},
                     files=[("certificado", ("c.pfx", b"x", "application/x-pkcs12"))])
check("consultar empresa alheia e barrado", r.status_code == 403, f"status={r.status_code}")

r = as_(None).post("/nfse/consultar-adn",
                   data={"tenant_id": str(tA), "cnpj": "48135251000100", "senha": "x"},
                   files=[("certificado", ("c.pfx", b"x", "application/x-pkcs12"))])
check("anonimo barrado", r.status_code in (401, 403), f"status={r.status_code}")

adn_client.baixar_lote = _real

print("\n=== 12. Certificado nunca é persistido ===")
src_rota = inspect.getsource(rota_nfse) if 'inspect' in dir() else open(
    'backend/app/api/routes/nfse.py', encoding='utf-8').read()
check("nenhuma coluna guarda certificado ou senha",
      "certificado" not in [c.name for c in NfseDocument.__table__.columns]
      and "senha" not in [c.name for c in NfseCursor.__table__.columns])
src_adn = open('backend/app/services/nfse/adn_client.py', encoding='utf-8').read()
check("PEM temporario e removido no finally", "finally:" in src_adn and "os.remove(p)" in src_adn)
check("arquivo PEM criado com permissao restrita", "0o600" in src_adn)

print("\n=== 13. Certificados guardados: senha cifrada e isolamento ===")
from backend.app.domain.models.certificates import TenantCertificate
from backend.app.core.security import encrypt_data
from backend.app.services.nfse import certificados as svc_cert

SENHA = "SenhaSecreta123"
s = SessionLocal()
s.add(TenantCertificate(tenant_id=str(tA), cnpj="48135251000100",
                        arquivo="empresaA.pfx", senha_encrypted=encrypt_data(SENHA),
                        apelido="Empresa A", razao_social="EMPRESA A", ativo=True))
s.commit()
guardado = s.query(TenantCertificate).filter_by(tenant_id=str(tA)).first()
check("senha NAO fica em texto puro no banco", SENHA not in (guardado.senha_encrypted or ""),
      "a coluna deve conter apenas o texto cifrado")
check("senha e recuperavel por quem tem a chave", guardado.senha() == SENHA)
s.close()

# pasta de certificados simulada
_CERT_DIR = tempfile.mkdtemp(prefix="certs_test_")
os.environ["CERTIFICADOS_DIR"] = _CERT_DIR
with open(os.path.join(_CERT_DIR, "empresaA.pfx"), "wb") as f:
    f.write(b"conteudo-pfx-falso")

s = SessionLocal()
pfx, senha = svc_cert.carregar_material(s, str(tA), "48135251000100")
check("carrega o .pfx da pasta", pfx == b"conteudo-pfx-falso")
check("devolve a senha decifrada", senha == SENHA)

erro = None
try:
    svc_cert.carregar_material(s, str(tB), "48135251000100")
except ValueError as e:
    erro = str(e)
check("empresa B NAO acessa o certificado da empresa A", erro is not None, f"erro={erro}")
s.close()

check("localiza .pfx pelo CNPJ no nome",
      svc_cert.localizar_por_cnpj("48135251000100", ["X 48135251000100 Y.pfx"]) is not None)
check("nao casa CNPJ diferente",
      svc_cert.localizar_por_cnpj("11111111111111", ["X 48135251000100 Y.pfx"]) is None)
check("caminho fica preso a pasta (sem ../)",
      svc_cert.caminho_do_arquivo("../../fora.pfx").startswith(_CERT_DIR))

print("\n=== 14. Busca em lote: RBAC e uso do certificado guardado ===")
rota_nfse.adn_client.baixar_lote = _fake_baixar
s = SessionLocal()
s.query(NfseDocument).filter_by(origem="adn").delete()
cur = s.query(NfseCursor).filter_by(tenant_id=str(tA)).first()
if cur: cur.ultimo_nsu = "0"
s.commit(); s.close()

from backend.app.services.nfse import lote as lote_nfse
r = as_("adm").post("/nfse/consultar-lote", data={"tenant_ids": [str(tA)]})
check("admin dispara o lote", r.status_code == 200, f"status={r.status_code}")
check("o clique devolve o painel do lote na hora", 'id="nfse-lote"' in r.text)
lote_nfse.esperar()
s = SessionLocal()
check("notas baixadas com o certificado guardado",
      s.query(NfseDocument).filter_by(origem="adn").count() >= 1)
s.close()

r = as_("cliB").post("/nfse/consultar-lote", data={"tenant_ids": [str(tA)]})
check("empresa alheia barrada no lote", r.status_code == 403, f"status={r.status_code}")
r = as_(None).post("/nfse/consultar-lote", data={"tenant_ids": [str(tA)]})
check("anonimo barrado no lote", r.status_code in (401, 403), f"status={r.status_code}")
# Sem empresa: 200 com o motivo NA TELA. Um 400 o htmx descartaria calado.
r = as_("adm").post("/nfse/consultar-lote", data={})
check("sem empresa selecionada e recusado com aviso visivel",
      r.status_code == 200 and "Selecione ao menos uma empresa" in r.text,
      f"status={r.status_code}")

r = as_("adm").get("/nfse/drawer")
check("drawer mostra aba de empresas para admin", 'id="form-empresas"' in r.text)
r = as_("cliB").get("/nfse/drawer")
check("cliente NAO ve a aba de empresas", 'id="form-empresas"' not in r.text)

adn_client.baixar_lote = _real
shutil.rmtree(_CERT_DIR, ignore_errors=True)

print("\n=== 15b. O menu do topo sobrevive a Notas de Servico ===")
# A pagina montava o contexto sem `user_profile`, e `layout/base.html` esconde
# Auditoria de PDF, Curadoria e Administracao atras de
# `user_profile.role == 'admin'`. Variavel ausente no Jinja e FALSA, entao o
# admin entrava aqui e perdia o menu inteiro — sem erro nenhum na tela.
r = as_("adm").get("/nfse")
check("a pagina abre para o admin", r.status_code == 200, r.status_code)
for alvo, rotulo in (("/curation", "Curadoria"), ("/admin", "Administracao"),
                     ("/auditoria-pdf", "Auditoria de PDF")):
    check(f"o menu ainda tem {rotulo}", f'href="{alvo}"' in r.text,
          "user_profile sumiu do contexto?")

r_cli = as_("cliB").get("/nfse")
check("e o cliente continua SEM as abas internas",
      '/curation' not in r_cli.text and 'href="/admin"' not in r_cli.text,
      "cliente passou a ver menu de admin")


print("\n=== 15. Nao alterou o motor existente ===")
import inspect
from backend.app.services.audit_engine import cross_checker as cc
src = inspect.getsource(cc)
check("motor nao referencia NFS-e", "nfse" not in src.lower())
from backend.app.services.audit_engine import sped_parser as sp
check("parser de SPED intacto", "nfse" not in inspect.getsource(sp).lower())

engine.dispose()
for p in ("_nfse_test.db",):
    if os.path.exists(p):
        try: os.remove(p)
        except Exception: pass
shutil.rmtree(_TMP_STORAGE, ignore_errors=True)

fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
for f in fails: print("  -", f)
sys.exit(1 if fails else 0)
