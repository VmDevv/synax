# -*- coding: utf-8 -*-
"""Gerador de SPED: CFOP de entrada por regra PRÓPRIA, aprovada antes de gerar.

Pedido do Sandro em 09/10/2026, sobre a tela de mapeamento que entrou em
08/10: mapear também o CFOP do CT-e, aplicar o maker-checker da casa ANTES de
deixar gerar (o botão fica travado até tudo estar mapeado e aprovado) e guardar
essas regras SEPARADAS das da auditoria. O que esta suíte trava:

1. **O levantamento cobre item e frete numa leitura só**, com a mesma chave
   que o gerador consulta: item de NF-e por (fornecedor × CFOP × cProd), frete
   de CT-e por (CFOP do CT-e × UF do transportador).

2. **Sem regra aprovada, o arquivo não sai** — nem pelo botão, nem por POST
   montado com CFOP no formulário, nem pela rota antiga de gerar direto.

3. **Maker-checker.** O operador grava pendente e não mexe em regra aprovada;
   o admin grava aprovado; a Curadoria tem as abas do gerador e aprova lá.

4. **Separação.** Nada do gerador vai para `cfop_classifications` nem para
   `cte_cfop_classifications`, e regra aprovada da auditoria não vale no
   gerador. Até 09/10 a tela gravava na tabela da auditoria.

5. **CFOP que não serve para entrada não vira regra** — nem na tela, nem na
   Curadoria (o "5102" aprovado iria direto para o C170).

6. **Tabela nova nasce com RLS** no Postgres, como as 25 da migração.

7. **O que a tela de 08/10 gravou na tabela da auditoria sai de lá** pelo
   `scripts/separar_regras_do_gerador.py` — que simula por padrão.
"""
import io
import os
import re
import shutil
import sys
import tempfile
import types
import uuid
import zipfile

sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_gerar_mapa_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
if os.path.exists("_gerar_mapa_test.db"):
    os.remove("_gerar_mapa_test.db")

from sqlalchemy import types as _satypes          # noqa: E402
import sqlalchemy.dialects.postgresql as pg        # noqa: E402


class _UUIDShim(_satypes.TypeDecorator):
    impl = _satypes.Uuid
    cache_ok = True

    def __init__(self, *a, **kw):
        kw.pop('as_uuid', None)
        super().__init__()

    def process_bind_param(self, value, dialect):
        return uuid.UUID(value) if isinstance(value, str) else value


pg.UUID = _UUIDShim

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
# XMLs sintéticos com a estrutura real (NF-e 4.00, CT-e 4.00).
# ============================================================================
EMPRESA = "27865757000102"
FORN = "12345678000190"
TRANSP_ES_1 = "34992472000270"
TRANSP_ES_2 = "11222333000144"
TRANSP_SP = "55666777000188"
OUTRA = "99999999000199"


def nfe(chave, num, itens, dest=EMPRESA, emit=FORN):
    dets = ""
    for i, it in enumerate(itens, start=1):
        dets += (
            f'<det nItem="{i}"><prod><cProd>{it["cod"]}</cProd>'
            f'<xProd>{it["descr"]}</xProd><NCM>84713012</NCM>'
            f'<CFOP>{it["cfop"]}</CFOP><uCom>UN</uCom><qCom>{it["qtd"]}</qCom>'
            f'<vUnCom>{it["unit"]}</vUnCom><vProd>{it["total"]}</vProd>'
            f'<vDesc>0.00</vDesc></prod><imposto><ICMS><ICMS00>'
            f'<orig>0</orig><CST>00</CST><vBC>{it["total"]}</vBC>'
            f'<pICMS>12.00</pICMS><vICMS>{it["icms"]}</vICMS>'
            f'</ICMS00></ICMS></imposto></det>')
    total = sum(float(it["total"]) for it in itens)
    icms = sum(float(it["icms"]) for it in itens)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">'
        f'<NFe><infNFe Id="NFe{chave}" versao="4.00">'
        f'<ide><cUF>32</cUF><nNF>{num}</nNF><serie>1</serie><mod>55</mod>'
        '<dhEmi>2026-04-12T10:30:00-03:00</dhEmi><tpNF>1</tpNF></ide>'
        f'<emit><CNPJ>{emit}</CNPJ><xNome>DIST LAURENCINI LTDA</xNome><IE>1</IE>'
        '<enderEmit><UF>ES</UF><cMun>3205309</cMun><xMun>VITORIA</xMun></enderEmit></emit>'
        f'<dest><CNPJ>{dest}</CNPJ><xNome>CLIENTE</xNome><IE>1</IE>'
        '<enderDest><UF>ES</UF><cMun>3203205</cMun><xMun>LINHARES</xMun></enderDest></dest>'
        f'{dets}<total><ICMSTot><vProd>{total:.2f}</vProd><vNF>{total:.2f}</vNF>'
        f'<vBC>{total:.2f}</vBC><vICMS>{icms:.2f}</vICMS><vST>0.00</vST>'
        '<vFrete>0.00</vFrete><vSeg>0.00</vSeg><vOutro>0.00</vOutro>'
        '<vDesc>0.00</vDesc><vIPI>0.00</vIPI><vPIS>0.00</vPIS>'
        '<vCOFINS>0.00</vCOFINS></ICMSTot></total></infNFe></NFe></nfeProc>')


def cte(chave, num, cfop, emit, nome, uf, valor="150.00", tomador=EMPRESA):
    # toma3 = 3: o tomador é o destinatário — a empresa, quando é frete dela.
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<cteProc xmlns="http://www.portalfiscal.inf.br/cte" versao="4.00">
 <CTe><infCte Id="CTe{chave}" versao="4.00">
  <ide><cUF>32</cUF><nCT>{num}</nCT><serie>1</serie><mod>57</mod><tpCTe>0</tpCTe>
    <dhEmi>2026-04-15T08:00:00-03:00</dhEmi><CFOP>{cfop}</CFOP>
    <cMunIni>3205309</cMunIni><cMunFim>3203205</cMunFim>
    <toma3><toma>3</toma></toma3></ide>
  <emit><CNPJ>{emit}</CNPJ><IE>1</IE><xNome>{nome}</xNome>
    <enderEmit><cMun>3205309</cMun><UF>{uf}</UF></enderEmit></emit>
  <dest><CNPJ>{tomador}</CNPJ><xNome>CLIENTE</xNome></dest>
  <vPrest><vTPrest>{valor}</vTPrest></vPrest>
  <imp><ICMS><ICMS00><CST>00</CST><vBC>{valor}</vBC><pICMS>12.00</pICMS>
    <vICMS>18.00</vICMS></ICMS00></ICMS></imp>
 </infCte></CTe>
 <protCTe versao="4.00"><infProt><cStat>100</cStat></infProt></protCTe>
</cteProc>'''


CAFE = {"cod": "CAFE500", "descr": "CAFE TORRADO 500G", "cfop": "5102",
        "qtd": "10.0000", "unit": "100.00", "total": "1000.00", "icms": "120.00"}
OLEO = {"cod": "OLEO900", "descr": "OLEO DE SOJA 900ML", "cfop": "6102",
        "qtd": "3.0000", "unit": "100.00", "total": "300.00", "icms": "36.00"}
MILHO = {"cod": "MILHO1", "descr": "MILHO VERDE LATA", "cfop": "5102",
         "qtd": "2.0000", "unit": "10.00", "total": "20.00", "icms": "2.40"}

ARQUIVOS = {
    "a.xml": nfe("35260412345678000190550010000027771000002777", "2777", [CAFE, OLEO]),
    "b.xml": nfe("35260412345678000190550010000027781000002778", "2778", [CAFE]),
    # Dois CT-e do ES com o mesmo CFOP, de transportadoras diferentes: UMA regra.
    "f1.xml": cte("32260434992472000270570010000000011000000011", "11", "5353",
                  TRANSP_ES_1, "IGUACU TRANSPORTES", "ES"),
    "f2.xml": cte("32260411222333000144570010000000021000000021", "21", "5353",
                  TRANSP_ES_2, "RAPIDO CAPIXABA", "ES"),
    "f3.xml": cte("35260455666777000188570010000000031000000031", "31", "6353",
                  TRANSP_SP, "PAULISTA CARGAS", "SP", valor="300.00"),
    # Frete em que a empresa NÃO é a tomadora: não é entrada dela.
    "f4.xml": cte("32260434992472000270570010000000041000000041", "41", "5353",
                  TRANSP_ES_1, "IGUACU TRANSPORTES", "ES", tomador=OUTRA),
}


def zip_de(arquivos: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for nome, conteudo in arquivos.items():
            z.writestr(nome, conteudo)
    return buf.getvalue()


# ============================================================================
print("\n=== 1. O levantamento: item de NF-e E frete de CT-e, numa leitura so ===")

from backend.app.services.sped_builder import (  # noqa: E402
    Cadastro, levantar_combinacoes_da_pasta)

_P = tempfile.mkdtemp(prefix="synax_mapa_")
for _nome, _conteudo in ARQUIVOS.items():
    io.open(os.path.join(_P, _nome), "w", encoding="utf-8").write(_conteudo)

cad = Cadastro(cnpj=EMPRESA, nome="CLIENTE", uf="ES")
comb = levantar_combinacoes_da_pasta(_P, cad)
itens = {l["produto_cod"]: l for l in comb["nfe"]}
fretes = {(l["cfop_origem"], l["uf"]): l for l in comb["cte"]}

check("uma linha por item distinto", len(comb["nfe"]) == 2, len(comb["nfe"]))
check("o CAFE contou os dois itens", itens["CAFE500"]["qtd_itens"] == 2)
check("sugestao 5102 -> 1102 e 6102 -> 2102",
      (itens["CAFE500"]["cfop_sugerido"], itens["OLEO900"]["cfop_sugerido"]) == ("1102", "2102"))
check("a chave do item e a do gerador (fornecedor|CFOP|cProd)",
      itens["CAFE500"]["chave"] == f"{FORN}|5102|CAFE500", itens["CAFE500"]["chave"])

check("frete: uma linha por CFOP do CT-e e UF do transportador",
      set(fretes) == {("5353", "ES"), ("6353", "SP")}, set(fretes))
check("os dois CT-e do ES, de transportadoras diferentes, numa linha so",
      fretes[("5353", "ES")]["qtd_ctes"] == 2
      and sorted(fretes[("5353", "ES")]["transportadoras"])
      == ["IGUACU TRANSPORTES", "RAPIDO CAPIXABA"], fretes[("5353", "ES")])
check("com o valor somado", fretes[("5353", "ES")]["valor"] == 300.0,
      fretes[("5353", "ES")]["valor"])
check("sugestao do frete: 5353 -> 1353 e 6353 -> 2353",
      (fretes[("5353", "ES")]["cfop_sugerido"], fretes[("6353", "SP")]["cfop_sugerido"])
      == ("1353", "2353"))
check("CT-e em que a empresa nao e a tomadora fica de fora, listado",
      any(n == "f4.xml" for n, _ in comb["ignorados"]), comb["ignorados"])
shutil.rmtree(_P, ignore_errors=True)


# ============================================================================
# Harness de rota: SQLite, auth falso, TestClient.
# ============================================================================
import backend.app.infrastructure.auth.supabase_client as sb  # noqa: E402
TOKENS = {}


class _Auth:
    def get_user(self, token):
        u = TOKENS.get(token.replace("Bearer ", "").strip())
        if not u:
            raise Exception("invalid")
        return types.SimpleNamespace(user=u)


class _Client:
    auth = _Auth()


sb.get_supabase_client = lambda: _Client()
sb.get_supabase_admin_client = lambda: _Client()
import backend.app.api.dependencies.auth as dep_auth  # noqa: E402
dep_auth.get_supabase_client = lambda: _Client()

from backend.app.infrastructure.database.session import engine as _eng  # noqa: E402
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit("ABORTADO: engine nao e SQLite")

import backend.app.main as m  # noqa: E402
from backend.app.infrastructure.database.session import SessionLocal  # noqa: E402
from backend.app.domain.models.base import Base  # noqa: E402
from backend.app.domain.models.auth_models import (  # noqa: E402
    Tenant, UserProfile, UserTenant)
from backend.app.domain.models.rules import (  # noqa: E402
    CfopClassification, CteCfopClassification, GeradorCfopRegra, GeradorCteCfopRegra)
from fastapi.testclient import TestClient  # noqa: E402

Base.metadata.create_all(bind=_eng)
db = SessionLocal()
tA, tB = uuid.uuid4(), uuid.uuid4()
db.add_all([Tenant(id=tA, name="CLIENTE A", cnpj=EMPRESA, uf="ES"),
            Tenant(id=tB, name="CLIENTE B", cnpj="22222222222222", uf="ES")])
u_adm, u_ope, u_cli, u_out = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
db.add_all([
    UserProfile(id=u_adm, tenant_id=tA, role="admin", name="Admin"),
    UserProfile(id=u_ope, tenant_id=tA, role="operador", name="Operador"),
    UserProfile(id=u_cli, tenant_id=tA, role="cliente", name="Cliente"),
    UserProfile(id=u_out, tenant_id=tB, role="operador", name="Operador de outra"),
])
db.add(UserTenant(user_id=u_ope, tenant_id=tA))
db.add(UserTenant(user_id=u_out, tenant_id=tB))
db.commit()
db.close()

TOKENS["adm"] = types.SimpleNamespace(id=str(u_adm), email="adm@synax.com")
TOKENS["ope"] = types.SimpleNamespace(id=str(u_ope), email="ope@synax.com")
TOKENS["cli"] = types.SimpleNamespace(id=str(u_cli), email="cli@synax.com")
TOKENS["out"] = types.SimpleNamespace(id=str(u_out), email="out@synax.com")

c = TestClient(m.app, raise_server_exceptions=False)


def as_(token):
    c.cookies.clear()
    if token:
        c.cookies.set("access_token", f"Bearer {token}")
    return c


def analisar(token, zbytes, tenant=None):
    return as_(token).post(
        "/api/gerar-sped/analisar",
        data={"tenant_id": str(tenant or tA), "perfil": "A", "ind_ativ": "1",
              "competencia": ""},
        files={"xmls_zip": ("pacote.zip", zbytes, "application/zip")})


def pacote_de(html):
    achado = re.search(r"/api/gerar-sped/([0-9a-f-]{36})/regras", html)
    return achado.group(1) if achado else None


def campos(html, tipo):
    """(chave, CFOP) das linhas EDITÁVEIS, na ordem — o que o navegador manda.
    Campo disabled (regra aprovada travada) não entra, como no navegador."""
    chaves = re.findall(rf'name="chave_{tipo}" value="([^"]*)" ?(disabled)?>', html)
    cfops = re.findall(rf'name="cfop_{tipo}" list="[^"]*" value="([^"]*)"\s+'
                       rf'inputmode="numeric" maxlength="4" autocomplete="off" ?(disabled)?',
                       html)
    return [(ch, cf) for (ch, d1), (cf, d2) in zip(chaves, cfops) if not d1 and not d2]


def gerar_travado(html) -> bool:
    return re.search(r'id="mapa-btn-gerar"\s+disabled', html) is not None


def contador(html, qual):
    """Os chips do topo: 'aprovadas', 'pendentes' ou 'a-definir'."""
    achado = re.search(rf'id="mapa-n-{qual}">(\d+)<', html)
    return int(achado.group(1)) if achado else None


def gravar(token, pid, nfe=(), cte=()):
    return as_(token).post(f"/api/gerar-sped/{pid}/regras", data={
        "chave_nfe": [k for k, _ in nfe], "cfop_nfe": [v for _, v in nfe],
        "chave_cte": [k for k, _ in cte], "cfop_cte": [v for _, v in cte]})


ZIP = zip_de(ARQUIVOS)
K_CAFE, K_OLEO = f"{FORN}|5102|CAFE500", f"{FORN}|6102|OLEO900"
K_ES, K_SP = "5353|ES", "6353|SP"


# ============================================================================
print("\n=== 2. /analisar: o mapeamento, com o botao de gerar TRAVADO ===")

r = analisar("ope", ZIP)
html = r.text
check("operador recebe 200", r.status_code == 200, r.status_code)
check("tela de mapeamento, nao de resultado",
      "Mapeamento de CFOP de entrada" in html and "/arquivo" not in html, html[:200])
check("com as duas secoes: itens e fretes",
      "Itens de NF-e" in html and "Fretes (CT-e)" in html)
check("tudo a definir: 2 itens + 2 fretes", contador(html, "a-definir") == 4,
      contador(html, "a-definir"))
check("o botao de gerar vem travado", gerar_travado(html))
check("e a tela diz por que", "Geração bloqueada" in html and "Enviar para aprovação" in html)
check("os campos vem com a sugestao do 1o digito",
      dict(campos(html, "nfe")) == {K_CAFE: "1102", K_OLEO: "2102"}
      and dict(campos(html, "cte")) == {K_ES: "1353", K_SP: "2353"},
      (campos(html, "nfe"), campos(html, "cte")))
check("as transportadoras aparecem para julgar a regra do frete",
      "IGUACU TRANSPORTES" in html and "PAULISTA CARGAS" in html)
pid = pacote_de(html)
check("devolve o pacote guardado", bool(pid), html[:300])
pasta = os.path.join(os.getcwd(), "temp_uploads", pid or "x")
check("o pacote guarda os XMLs, o META e as combinacoes",
      os.path.isdir(os.path.join(pasta, "xmls"))
      and os.path.exists(os.path.join(pasta, "pacote.json"))
      and os.path.exists(os.path.join(pasta, "combinacoes.json")))

check("cliente toma 403", analisar("cli", ZIP).status_code == 403)
check("operador de outra empresa nao analisa por esta",
      analisar("out", ZIP).status_code == 403)
check("nem abre o mapeamento do pacote dela",
      as_("out").get(f"/api/gerar-sped/{pid}/mapa").status_code == 403)
check("nem grava regra nele",
      gravar("out", pid, nfe=[(K_CAFE, "1102")]).status_code == 403)
check("pacote com id que nao e UUID: 404",
      as_("ope").get("/api/gerar-sped/..%2F..%2Fx/mapa").status_code in (400, 404))


# ============================================================================
print("\n=== 3. Operador envia para aprovacao: pendente, e ainda nao gera ===")

r = gravar("ope", pid, nfe=[(K_CAFE, "1102"), (K_OLEO, "2102")],
           cte=[(K_ES, "1353"), (K_SP, "2353")])
html = r.text
check("200 e a tela volta", r.status_code == 200 and "Mapeamento de CFOP" in html)
check("diz que foram gravadas como pendentes",
      "4 regras gravadas como pendentes" in html, re.findall(r"regras? gravadas?[^<]*", html))
check("as quatro aguardando o admin", contador(html, "pendentes") == 4,
      contador(html, "pendentes"))
check("e o botao continua travado", gerar_travado(html))

ss = SessionLocal()
itens_db = ss.query(GeradorCfopRegra).filter_by(tenant_id=str(tA)).all()
fretes_db = ss.query(GeradorCteCfopRegra).filter_by(tenant_id=str(tA)).all()
check("duas regras de item no gerador, pendentes, do operador",
      len(itens_db) == 2 and all(x.status == "pendente_revisao"
                                 and x.created_by == "ope@synax.com" for x in itens_db),
      [(x.product_code, x.status, x.created_by) for x in itens_db])
check("com a descricao e o fornecedor para a Curadoria julgar",
      {x.product_code: x.product_description for x in itens_db}["CAFE500"]
      == "CAFE TORRADO 500G" and all(x.supplier_name for x in itens_db))
check("duas regras de frete, pela chave CFOP + UF",
      {(x.supplier_cfop, x.emitter_uf, x.entry_cfop) for x in fretes_db}
      == {("5353", "ES", "1353"), ("6353", "SP", "2353")},
      [(x.supplier_cfop, x.emitter_uf, x.entry_cfop) for x in fretes_db])
check("a regra do frete guarda as transportadoras de exemplo",
      "IGUACU" in [x for x in fretes_db if x.emitter_uf == "ES"][0].carrier_names)
check("NADA foi gravado nas tabelas da AUDITORIA",
      ss.query(CfopClassification).count() == 0
      and ss.query(CteCfopClassification).count() == 0)
ss.close()

r = as_("ope").post("/api/gerar-sped/confirmar", data={"pacote_id": pid})
check("gerar com regra pendente e recusado no servidor",
      r.status_code == 200 and "/arquivo" not in r.text
      and "só é gerado com tudo aprovado" in r.text, r.text[:300])
r = as_("ope").post("/api/gerar-sped/confirmar", data={
    "pacote_id": pid, "chave": [f"{FORN}|5102|CAFE500"], "cfop_entrada": ["1102"],
    "salvar_regras": "1"})
check("nem mandando CFOP no POST, como a tela de 08/10 fazia",
      "/arquivo" not in r.text and "só é gerado com tudo aprovado" in r.text)


# ============================================================================
print("\n=== 4. O que nao vira regra ===")

r = gravar("ope", pid, nfe=[(K_CAFE, "5102"), (K_OLEO, "999")])
check("CFOP de saida e CFOP de 3 digitos: nada gravado, com o motivo",
      "Nada foi gravado" in r.text and "não servem" in r.text
      and "5102" in r.text and "999" in r.text, re.findall(r"Nada foi gravado[^<]*", r.text))
ss = SessionLocal()
check("e a regra pendente do CAFE ficou como estava (1102)",
      ss.query(GeradorCfopRegra).filter_by(product_code="CAFE500").one().entry_cfop == "1102")
ss.close()

r = gravar("ope", pid, nfe=[(f"{OUTRA}|5102|XYZ", "1102")])
check("chave que nao e deste pacote e ignorada (o POST nao cadastra fornecedor qualquer)",
      "não é deste pacote" in r.text, re.findall(r"Nada foi gravado[^<]*", r.text))
ss = SessionLocal()
check("e nada nasceu para ela", ss.query(GeradorCfopRegra).filter_by(
    product_code="XYZ").count() == 0)
ss.close()

r = gravar("ope", pid, nfe=[(K_CAFE, "")])
check("linha sem CFOP fica de fora, avisada", "sem CFOP ficou de fora" in r.text)
r = as_("ope").post(f"/api/gerar-sped/{pid}/regras",
                    data={"chave_nfe": [K_CAFE, K_OLEO], "cfop_nfe": ["1102"]})
check("formulario com chave sem CFOP: recusa inteiro, sem gravar pela metade",
      "chegou incompleto" in r.text)
r = gravar("ope", pid, nfe=[(K_CAFE, "1102")])
check("mandar o mesmo CFOP de novo nao e 'sucesso': diz que nada mudou",
      "Nada foi gravado" in r.text and "já eram os gravados" in r.text)


# ============================================================================
print("\n=== 5. Curadoria: as abas do gerador, e a aprovacao do admin ===")

ss = SessionLocal()
contagem = m._counts(ss)
ss.close()
check("a fila conta as pendencias do gerador em abas proprias",
      contagem["gerador_cfop"] == 2 and contagem["gerador_cte"] == 2, contagem)
check("e as abas da auditoria nao recebem nada", contagem["cfop"] == 0
      and contagem["cte"] == 0, contagem)

r = as_("adm").get("/api/curation/list?rule_type=gerador_cfop")
check("admin abre a aba Gerador · NF-e", r.status_code == 200
      and "Gerador · NF-e" in r.text and "Gerador · CT-e" in r.text, r.status_code)
check("com o produto e o fornecedor por extenso",
      "CAFE TORRADO 500G" in r.text and "DIST LAURENCINI" in r.text)
check("operador nao entra na Curadoria",
      as_("ope").get("/api/curation/list?rule_type=gerador_cfop").status_code in (401, 403))

chave_cafe = f"{tA}|{EMPRESA}|{FORN}|5102|CAFE500"
r = as_("adm").post("/api/curation/rule/update", data={
    "rule_type": "gerador_cfop", "key": chave_cafe, "field": "entry_cfop", "value": "5102"})
ss = SessionLocal()
check("editar para CFOP de saida na Curadoria nao grava",
      ss.query(GeradorCfopRegra).filter_by(product_code="CAFE500").one().entry_cfop == "1102")
ss.close()
r = as_("adm").post("/api/curation/rule/approve", data={
    "rule_type": "gerador_cfop", "key": chave_cafe, "field": "entry_cfop", "value": "999"})
ss = SessionLocal()
check("Enter com CFOP invalido nao aprova (a linha volta pendente)",
      ss.query(GeradorCfopRegra).filter_by(product_code="CAFE500").one().status
      == "pendente_revisao")
ss.close()
r = as_("adm").post("/api/curation/rule/approve", data={
    "rule_type": "gerador_cfop", "key": chave_cafe})
ss = SessionLocal()
check("o admin aprova o CAFE na Curadoria",
      ss.query(GeradorCfopRegra).filter_by(product_code="CAFE500").one().status == "aprovado")
ss.close()

r = as_("ope").get(f"/api/gerar-sped/{pid}/mapa")
check("Atualizar: 1 aprovada, e ainda travado pelas outras 3",
      contador(r.text, "aprovadas") == 1 and gerar_travado(r.text),
      (contador(r.text, "aprovadas"), gerar_travado(r.text)))
check("linha aprovada nao tem campo para o operador",
      K_CAFE not in [k for k, _ in campos(r.text, "nfe")], campos(r.text, "nfe"))

r = as_("adm").post("/api/curation/batch-approve", data={
    "rule_type": "gerador_cte", "rule_keys": [f"{tA}|{EMPRESA}|5353|ES",
                                              f"{tA}|{EMPRESA}|6353|SP"]})
r = as_("adm").post("/api/curation/rule/approve", data={
    "rule_type": "gerador_cfop", "key": f"{tA}|{EMPRESA}|{FORN}|6102|OLEO900"})
ss = SessionLocal()
check("aprovadas em lote e uma a uma: as quatro",
      all(x.status == "aprovado" for x in ss.query(GeradorCfopRegra).all())
      and all(x.status == "aprovado" for x in ss.query(GeradorCteCfopRegra).all()))
ss.close()


# ============================================================================
print("\n=== 6. Com tudo aprovado: destrava, e o arquivo sai com as regras ===")

r = as_("ope").get(f"/api/gerar-sped/{pid}/mapa")
check("Atualizar: tudo aprovado, o botao destrava",
      "Tudo mapeado e aprovado" in r.text and not gerar_travado(r.text))
r = as_("ope").post("/api/gerar-sped/confirmar", data={"pacote_id": pid})
proc = re.search(r"gerar-sped/([0-9a-f-]{36})/arquivo", r.text)
check("gera, com o arquivo para baixar", r.status_code == 200 and bool(proc), r.text[:300])
txt = ""
if proc:
    txt = io.open(os.path.join(os.getcwd(), "temp_uploads", proc.group(1),
                               "sped_gerado.txt"), encoding="latin-1", newline="").read()
c170 = {ln.split("|")[3]: ln.split("|")[11] for ln in txt.split("\r\n")
        if ln.startswith("|C170|")}
check("os itens com o CFOP das regras do gerador",
      c170.get("CAFE500") == "1102" and c170.get("OLEO900") == "2102", c170)
d100_d190, ultimo = [], None
for ln in txt.split("\r\n"):
    if ln.startswith("|D100|"):
        ultimo = ln.split("|")[9]
    elif ln.startswith("|D190|"):
        d100_d190.append((ultimo, ln.split("|")[3]))
check("os fretes com o CFOP da regra do frete (ES -> 1353, SP -> 2353)",
      sorted(d100_d190) == [("11", "1353"), ("21", "1353"), ("31", "2353")], d100_d190)
check("o frete em que a empresa nao e tomadora ficou fora do arquivo",
      "|41|" not in txt)
check("nenhum CFOP deduzido foi escriturado (nem do item, nem do frete)",
      "conversão do primeiro dígito" not in r.text)
check("pacote de um mes ja gerado sai do servidor", not os.path.isdir(pasta))
if proc:
    shutil.rmtree(os.path.join(os.getcwd(), "temp_uploads", proc.group(1)),
                  ignore_errors=True)


# ============================================================================
print("\n=== 7. Regra aprovada: so o admin muda; o que ele grava ja vale ===")

ZIP2 = zip_de({"a.xml": ARQUIVOS["a.xml"],
               "c.xml": nfe("35260412345678000190550010000027791000002779", "2779", [MILHO])})
# Regra APROVADA da auditoria para o MILHO: não pode valer no gerador.
ss = SessionLocal()
ss.add(CfopClassification(tenant_id=str(tA), company_cnpj=EMPRESA, supplier_cnpj=FORN,
                          supplier_cfop="5102", product_code="MILHO1", entry_cfop="1556",
                          status="aprovado", created_by="auditoria@synax.com"))
ss.commit()
ss.close()

r = analisar("ope", ZIP2)
pid2 = pacote_de(r.text)
check("o MILHO aparece a definir, apesar da regra aprovada da auditoria",
      dict(campos(r.text, "nfe")).get(f"{FORN}|5102|MILHO1") == "1102"
      and gerar_travado(r.text), campos(r.text, "nfe"))
check("e o CAFE, ja aprovado no gerador, vem sem campo para o operador",
      K_CAFE not in dict(campos(r.text, "nfe")))

r = gravar("ope", pid2, nfe=[(K_CAFE, "1556")])
check("operador mandando outro CFOP para regra aprovada: recusado, com o motivo",
      "só o admin muda regra aprovada" in r.text, re.findall(r"Nada foi gravado[^<]*", r.text))
ss = SessionLocal()
cafe = ss.query(GeradorCfopRegra).filter_by(product_code="CAFE500").one()
check("e a regra continua aprovada e igual", (cafe.entry_cfop, cafe.status)
      == ("1102", "aprovado"), (cafe.entry_cfop, cafe.status))
ss.close()

r = analisar("adm", ZIP2)
pid3 = pacote_de(r.text)
check("para o admin, a linha aprovada vem com o campo TRAVADO (disabled)",
      re.search(r'name="chave_nfe" value="' + re.escape(K_CAFE) + r'" disabled>', r.text)
      is not None)
check("e o admin ve 'Salvar e aprovar'", "Salvar e aprovar" in r.text)
r = gravar("adm", pid3, nfe=[(f"{FORN}|5102|MILHO1", "1102"), (K_CAFE, "1403")])
ss = SessionLocal()
milho = ss.query(GeradorCfopRegra).filter_by(product_code="MILHO1").one()
cafe = ss.query(GeradorCfopRegra).filter_by(product_code="CAFE500").one()
check("o que o admin grava ja entra aprovado",
      (milho.entry_cfop, milho.status, milho.created_by)
      == ("1102", "aprovado", "adm@synax.com"), (milho.entry_cfop, milho.status))
check("e o admin pode alterar regra aprovada (destravando o campo)",
      (cafe.entry_cfop, cafe.status) == ("1403", "aprovado"))
check("a regra da auditoria do MILHO nao foi tocada",
      ss.query(CfopClassification).filter_by(product_code="MILHO1").one().entry_cfop
      == "1556")
ss.close()
check("com tudo aprovado, o admin ja pode gerar",
      "Tudo mapeado e aprovado" in r.text and not gerar_travado(r.text))
for _p in (pid2, pid3):
    shutil.rmtree(os.path.join(os.getcwd(), "temp_uploads", _p), ignore_errors=True)


# ============================================================================
print("\n=== 8. As portas: Curadoria registrada, rota sem a tabela da auditoria ===")

check("os dois tipos do gerador estao registrados na Curadoria",
      m._rule_models()["gerador_cfop"] is GeradorCfopRegra
      and m._rule_models()["gerador_cte"] is GeradorCteCfopRegra)
check("com a mesma chave exata da tabela (cProd por ultimo)",
      m.RULE_KEY_FIELDS["gerador_cfop"][-1] == "product_code"
      and m.RULE_KEY_FIELDS["gerador_cte"] == ('tenant_id', 'company_cnpj',
                                                'supplier_cfop', 'emitter_uf'))
check("as abas da auditoria seguem como eram (CFOP editavel como texto)",
      m.EDITABLE_FIELDS["cfop"] == {"entry_cfop": "str"}
      and m.EDITABLE_FIELDS["cte"] == {"entry_cfop": "str"})

ROTA = io.open(os.path.join("backend", "app", "api", "routes", "gerar_sped.py"),
               encoding="utf-8").read()
check("a rota do gerador nao usa as classes de regra da auditoria",
      "CfopClassification" not in ROTA)
GERADOR = io.open(os.path.join("backend", "app", "services", "sped_builder", "gerador.py"),
                  encoding="utf-8").read()
check("o gerador nao fala com o banco (recebe as regras prontas)",
      "domain.models" not in GERADOR and "Session" not in GERADOR)

import backend.app.services.painel_atividade as pa  # noqa: E402
check("o Radar conta as regras do gerador represadas",
      {GeradorCfopRegra, GeradorCteCfopRegra} <= {mod for mod, _ in pa.TABELAS_DE_REGRA})


# ============================================================================
print("\n=== 9. Tabela nova nasce com RLS (Postgres) ===")

from backend.app.infrastructure.database import schema_upgrades as su  # noqa: E402


class _Resultado:
    def __init__(self, nomes):
        self.nomes = nomes

    def scalars(self):
        return iter(self.nomes)


class _Conexao:
    """Responde ao SELECT do catálogo com as tabelas 'sem RLS' e anota o resto."""

    def __init__(self, sem_rls):
        self.sem_rls = sem_rls
        self.sql = []

    def execute(self, stmt, params=None):
        texto = str(stmt)
        self.sql.append(texto)
        if texto.lstrip().lower().startswith("select"):
            return _Resultado([n for n in self.sem_rls if n in (params or {}).get("nomes", [])])
        return None


conexao = _Conexao(["gerador_cfop_regras", "tabela_de_outro_sistema"])
ligadas = su.ligar_rls(conexao, ["gerador_cfop_regras", "gerador_cte_cfop_regras", "tenants"])
check("liga o RLS so na tabela do Synax que estava sem",
      ligadas == ["gerador_cfop_regras"], ligadas)
check("com o ALTER certo, e nenhum ALTER nas que ja tinham",
      sum("ENABLE ROW LEVEL SECURITY" in s for s in conexao.sql) == 1
      and any('ALTER TABLE "gerador_cfop_regras" ENABLE ROW LEVEL SECURITY' in s
              for s in conexao.sql), conexao.sql)
check("tabela de fora do Synax nunca e tocada",
      not any("tabela_de_outro_sistema" in s for s in conexao.sql if s.startswith("ALTER")))
su._ligar_rls_nas_tabelas_do_synax(_eng)
check("no SQLite (testes) nao faz nada e nao quebra", True)
check("as tabelas do gerador estao no metadata (nascem no create_all)",
      {"gerador_cfop_regras", "gerador_cte_cfop_regras"} <= set(Base.metadata.tables))
FONTE_UP = io.open(os.path.join("backend", "app", "infrastructure", "database",
                                "schema_upgrades.py"), encoding="utf-8").read()
check("o startup confere o RLS depois de criar as tabelas",
      "_ligar_rls_nas_tabelas_do_synax(engine)" in FONTE_UP.split("def aplicar")[1])


# ============================================================================
print("\n=== 10. O que a tela de 08/10 gravou na tabela da auditoria sai de la ===")

import importlib.util as _ilu  # noqa: E402
from datetime import datetime as _dt  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "separar_regras_do_gerador", os.path.join("scripts", "separar_regras_do_gerador.py"))
separar_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(separar_mod)

ss = SessionLocal()
uuid_ope = str(u_ope)
ss.add_all([
    # Assinatura da tela antiga do gerador: created_by = UUID do perfil.
    CfopClassification(tenant_id=str(tA), company_cnpj=EMPRESA, supplier_cnpj=FORN,
                       supplier_cfop="5102", product_code="GER1", entry_cfop="1102",
                       product_description="ITEM GRAVADO PELO GERADOR",
                       status="aprovado", created_by=uuid_ope),
    # Mesma assinatura, mas a chave já existe no gerador (CAFE = 1403 aprovado):
    # o que está lá não é sobrescrito.
    CfopClassification(tenant_id=str(tA), company_cnpj=EMPRESA, supplier_cnpj=FORN,
                       supplier_cfop="5102", product_code="CAFE500", entry_cfop="1556",
                       status="pendente_revisao", created_by=uuid_ope),
    # Regra da auditoria de verdade (e-mail): fica.
    CfopClassification(tenant_id=str(tA), company_cnpj=EMPRESA, supplier_cnpj=FORN,
                       supplier_cfop="5102", product_code="AUD1", entry_cfop="1403",
                       status="aprovado", created_by="ana@synax.com"),
    # Regra antiga alterada no período: só listada.
    CfopClassification(tenant_id=str(tA), company_cnpj=EMPRESA, supplier_cnpj=FORN,
                       supplier_cfop="5102", product_code="VELHA1", entry_cfop="1102",
                       status="aprovado", created_by="ana@synax.com",
                       created_at=_dt(2026, 9, 1), updated_at=_dt.utcnow()),
])
ss.commit()
antes = ss.query(CfopClassification).count()

simulado = separar_mod.separar(ss, aplicar=False)
check("simulacao: acha as duas gravadas pelo gerador",
      len(simulado["do_gerador"]) == 2
      and any("GER1" in s for s in simulado["do_gerador"])
      and any("CAFE500" in s for s in simulado["do_gerador"]), simulado["do_gerador"])
check("e lista a regra antiga alterada no periodo, so para conferir",
      len(simulado["alteradas"]) == 1 and "VELHA1" in simulado["alteradas"][0],
      simulado["alteradas"])
check("e nao grava nada", ss.query(CfopClassification).count() == antes
      and ss.query(GeradorCfopRegra).filter_by(product_code="GER1").count() == 0)

aplicado = separar_mod.separar(ss, aplicar=True)
ger1 = ss.query(GeradorCfopRegra).filter_by(product_code="GER1").one_or_none()
check("--aplicar: a do gerador vai para gerador_cfop_regras, com CFOP e status",
      ger1 is not None and (ger1.entry_cfop, ger1.status) == ("1102", "aprovado"),
      ger1 and (ger1.entry_cfop, ger1.status))
check("a que ja existia no gerador nao foi sobrescrita (CAFE continua 1403)",
      ss.query(GeradorCfopRegra).filter_by(product_code="CAFE500").one().entry_cfop
      == "1403" and aplicado["ja_existiam"] == 1, aplicado)
check("as duas sairam da tabela da auditoria",
      ss.query(CfopClassification).filter(
          CfopClassification.product_code.in_(["GER1", "CAFE500"])).count() == 0)
check("e as regras da auditoria de verdade ficaram",
      {r.product_code for r in ss.query(CfopClassification).all()} >= {"AUD1", "VELHA1", "MILHO1"})
check("rodar de novo nao acha mais nada (idempotente)",
      separar_mod.separar(ss, aplicar=False)["do_gerador"] == [])
ss.close()


# ============================================================================
_eng.dispose()
if os.path.exists("_gerar_mapa_test.db"):
    try:
        os.remove("_gerar_mapa_test.db")
    except OSError:
        pass
print("\n" + "=" * 60)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("FALHARAM:", ", ".join(_falhou))
print("=" * 60)
sys.exit(1 if FALHAS else 0)
