# -*- coding: utf-8 -*-
"""Página Domínio dentro do Synax: o que o Synax lê do Domínio, pelo programa SPED PIS/COFINS.

Pedido do Sandro em 22/10/2026 — a segunda página do programa trazida para cá, com o mesmo desenho da primeira (o
motor fica LÁ; o Synax chama a linha de comando). Esta suíte NUNCA roda o programa: as funções de `programa` são
trocadas por respostas fixas. O que ela trava:

1. **A página e as abas por papel:** cliente não entra; a aba de consulta livre só aparece para quem pode.
2. **Situação da conexão e "Testar a conexão"** viram frase, e o teste esquece a lista guardada.
3. **Empresa e notas:** só as empresas do usuário — POST montado com empresa alheia não chama o programa.
4. **Planilha das notas:** baixada como arquivo, com a empresa conferida de novo.
5. **Lista de empresas:** filtrada pelo usuário; busca por nome, código e CNPJ.
6. **Consulta livre:** só admin ou operador COM a permissão; o SQL vai num arquivo; tamanho limitado; a planilha
   só baixa quem fez a consulta e ainda pode.
7. **A permissão é configurada na edição do usuário** e só vale para operador; a coluna nova está em
   `COLUNAS_NOVAS`, senão nunca chegaria ao Postgres.
8. **Menu:** o item novo em Ferramentas SPED.
"""
import json
import os
import shutil
import sys
import types
import uuid
from pathlib import Path

sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_dominio_programa_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
if os.path.exists("_dominio_programa_test.db"):
    os.remove("_dominio_programa_test.db")

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
from backend.app.infrastructure.database import schema_upgrades  # noqa: E402
from backend.app.domain.models.base import Base  # noqa: E402
from backend.app.domain.models.auth_models import Tenant, UserProfile, UserTenant  # noqa: E402
from backend.app.api.routes import dominio as rota  # noqa: E402
from backend.app.services.efd_contribuicoes import programa  # noqa: E402
from backend.app.services.efd_contribuicoes.programa import FalhaDoPrograma, ProgramaIndisponivel  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CNPJ_A, CNPJ_B, CNPJ_SEM = "11222333000181", "44555666000177", "99888777000166"

Base.metadata.create_all(bind=_eng)
db = SessionLocal()
tA, tB = uuid.uuid4(), uuid.uuid4()
db.add_all([Tenant(id=tA, name="CLIENTE A", cnpj=CNPJ_A, uf="ES"),
            Tenant(id=tB, name="CLIENTE B", cnpj=CNPJ_B, uf="ES")])
u_adm, u_ope, u_sql, u_cli = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
db.add_all([
    UserProfile(id=u_adm, tenant_id=tA, role="admin", name="Admin"),
    UserProfile(id=u_ope, tenant_id=tA, role="operador", name="Operador"),
    UserProfile(id=u_sql, tenant_id=tB, role="operador", name="Operador com SQL", pode_sql_dominio=True),
    UserProfile(id=u_cli, tenant_id=tA, role="cliente", name="Cliente"),
])
db.add(UserTenant(user_id=u_ope, tenant_id=tA))
db.add(UserTenant(user_id=u_sql, tenant_id=tB))
db.commit()
db.close()

TOKENS["adm"] = types.SimpleNamespace(id=str(u_adm), email="adm@synax.com")
TOKENS["ope"] = types.SimpleNamespace(id=str(u_ope), email="ope@synax.com")
TOKENS["sql"] = types.SimpleNamespace(id=str(u_sql), email="sql@synax.com")
TOKENS["cli"] = types.SimpleNamespace(id=str(u_cli), email="cli@synax.com")

c = TestClient(m.app, raise_server_exceptions=False)


def as_(token):
    c.cookies.clear()
    if token:
        c.cookies.set("access_token", f"Bearer {token}")
    return c


# ============================================================================
# O programa, trocado por respostas fixas.
# ============================================================================
EMPRESAS = [
    {"codi_emp": 10, "nome": "CLIENTE A LTDA", "cnpj": CNPJ_A, "presumido": True},
    {"codi_emp": 20, "nome": "CLIENTE B LTDA", "cnpj": CNPJ_B, "presumido": False},
    {"codi_emp": 30, "nome": "SO NO DOMINIO LTDA", "cnpj": CNPJ_SEM, "presumido": True},
]
CHAMADAS = []
FALHAR = {}


def _falha(nome):
    if FALHAR.get(nome):
        raise FALHAR[nome]


def _empresas(atualizar=False):
    _falha("empresas")
    return EMPRESAS


def _cadastro(codi, competencia):
    CHAMADAS.append(("cadastro", codi, competencia))
    _falha("cadastro")
    if codi == 30:
        return None
    return {"codi_emp": codi, "nome": "CLIENTE A LTDA", "regime": "cumulativo", "impostos": [4, 5],
            "tem_apuracao": True, "apuracao": [
                {"codigo": 4, "tributo": "PIS", "regime": "cumulativo", "a_recolher": 897.0, "saldo_credor": 0.0},
                {"codigo": 5, "tributo": "COFINS", "regime": "cumulativo", "a_recolher": 0.0, "saldo_credor": 12.5}]}


def _notas(codi, competencia, excel=None):
    CHAMADAS.append(("notas", codi, competencia))
    _falha("notas")
    vazio = codi == 30
    if excel and not vazio:
        Path(excel).write_bytes(b"PK planilha das notas")
    return {"contrato": 1, "saidas": 0 if vazio else 2, "entradas": 0 if vazio else 1,
            "por_cfop": {"colunas": ["tipo", "cfop", "notas", "vl_item", "pis"], "total": 0 if vazio else 2,
                         "linhas": [] if vazio else [["Entrada", "1102", 1, 70.0, 0.0], ["Saída", "5102", 1, 150.0, 0.98]]},
            "notas": {"colunas": ["origem", "tipo", "documento", "data", "cfop", "cst_pis", "aliq_pis", "vl_item"],
                      "total": 0 if vazio else 3,
                      "linhas": [] if vazio else [["DOMINIO", "S", "1", "2026-09-03", "5102", None, 1.65, 100.0],
                                                  ["DOMINIO", "S", "1", "2026-09-03 14:05:00", "5102", "", 0.65, 50.0]]},
            "excel": None if vazio else str(excel)}


def _consulta(arquivo, codi=None, competencia=None, excel=None):
    CHAMADAS.append(("consulta", Path(arquivo).read_text(encoding="utf-8"), codi, competencia))
    _falha("consulta")
    if excel:
        Path(excel).write_bytes(b"PK planilha da consulta")
    return {"contrato": 1, "resultado": {"colunas": ["codi_emp", "nome_emp", "vazio"], "total": 501,
                                         "linhas": [[243, "PATRICK", None]]}, "excel": str(excel)}


def _testar():
    CHAMADAS.append(("testar",))
    _falha("testar")
    return "Conectado: SQL Anywhere"


programa.empresas = _empresas
programa.info = lambda: {"contrato": 1, "pasta_padrao": "x", "conexao": "DSN=Contabil"}
programa.cadastro = _cadastro
programa.notas = _notas
programa.consulta = _consulta
programa.testar = _testar
programa.consultas_prontas = lambda: [{"nome": "01_saidas.sql", "sql": "SELECT * FROM x WHERE e = :empresa"}]
programa.tabelas = lambda esquema="bethadba", filtro="": {"colunas": ["esquema", "tabela", "tipo"], "total": 1,
                                                          "linhas": [[esquema, "efsdoimp", "TABLE"]]}
programa.colunas = lambda tabela, esquema="bethadba": {"colunas": ["coluna", "tipo", "tamanho"], "total": 1,
                                                       "linhas": [["codi_emp", "integer", 10]]}
_esquecidas = []
programa.esquecer_empresas = lambda: _esquecidas.append(1)

COMP = "2026-09"
PASTAS = []


def processo_de(html):
    i = html.find("/api/dominio/arquivo/")
    if i < 0:
        return None
    p = html[i + len("/api/dominio/arquivo/"):].split('"')[0]
    PASTAS.append(p)
    return p


# ============================================================================
print("\n=== 1. A pagina e as abas por papel ===")
r = as_("adm").get("/dominio")
check("admin abre a pagina com a aba de consulta livre", r.status_code == 200 and 'data-aba="sql"' in r.text, r.status_code)
r = as_("ope").get("/dominio")
check("operador sem a permissao nao ve a aba de consulta livre", r.status_code == 200 and 'data-aba="sql"' not in r.text)
r = as_("sql").get("/dominio")
check("operador com a permissao ve a aba", r.status_code == 200 and 'data-aba="sql"' in r.text)
check("cliente nao entra", as_("cli").get("/dominio").status_code == 403)
pagina = as_("adm").get("/dominio").text
check("trocar empresa ou mes limpa as notas buscadas", "closest('#dom-filtros')" in pagina and "dom-notas" in pagina)
check("a explicacao fica depois das abas, nao entre a conexao e os filtros",
      pagina.find("Como o Synax usa o Domínio") > pagina.find('data-painel="lista"'))

# ============================================================================
print("\n=== 2. Conexao ===")
r = as_("ope").get("/api/dominio/status")
check("situacao: conectado, com as contagens e a fonte", "Conectado" in r.text and ">3 empresas" not in r.text
      and "3 empresas ativas" in r.text and "2 presumidas" in r.text and "DSN=Contabil" in r.text, r.text[:300])
FALHAR["empresas"] = ProgramaIndisponivel("O programa SPED PIS/COFINS não foi encontrado em X.")
r = as_("ope").get("/api/dominio/status")
check("sem o programa: 'Sem conexao' com o motivo, 200", r.status_code == 200 and "Sem conexão" in r.text
      and "não foi encontrado" in r.text)
FALHAR.clear()
r = as_("ope").post("/api/dominio/testar")
check("testar: a frase do driver, e a lista guardada e esquecida", "Conectado: SQL Anywhere" in r.text and _esquecidas)
FALHAR["testar"] = FalhaDoPrograma("RuntimeError: Conexão ODBC não configurada")
r = as_("ope").post("/api/dominio/testar")
check("testar falhando: frase com o motivo, 200", r.status_code == 200 and "Falhou" in r.text and "não configurada" in r.text)
FALHAR.clear()

# ============================================================================
print("\n=== 3. Seletor, empresa e notas ===")
r = as_("adm").get("/api/dominio/seletor")
check("admin: todas, inclusive nao presumida e sem cadastro (marcada)",
      all(f'value="{x}"' in r.text for x in (10, 20, 30)) and "sem cadastro no Synax" in r.text, r.text[:300])
r = as_("ope").get("/api/dominio/seletor")
check("operador: so a dele", 'value="10"' in r.text and 'value="20"' not in r.text and 'value="30"' not in r.text)
check("o seletor tem o id que as abas incluem", 'id="dom-empresa"' in r.text and 'name="codi_emp"' in r.text)
r = as_("ope").get("/api/dominio/empresa", params={"codi_emp": "", "competencia": COMP})
check("sem empresa: o convite para escolher", "Escolha a empresa" in r.text)
CHAMADAS.clear()
r = as_("ope").get("/api/dominio/empresa", params={"codi_emp": "10", "competencia": COMP})
check("empresa dele: cadastro e apuracao gravada", "Empresa 10" in r.text and "cumulativo" in r.text
      and "R$ 897,00 a recolher" in r.text and "saldo credor R$ 12,50" in r.text, r.text[:400])
check("o programa recebeu empresa e competencia", ("cadastro", 10, COMP) in CHAMADAS, CHAMADAS)
check("'Gerar a EFD do Lucro Presumido' vai com a empresa e o mes",
      f"/lucro-presumido?codi_emp=10&competencia={COMP}" in r.text.replace("&amp;", "&"), r.text[-600:])
CHAMADAS.clear()
r = as_("ope").get("/api/dominio/empresa", params={"codi_emp": "20", "competencia": COMP})
check("empresa alheia: frase, sem chamar o programa", "fora da sua lista" in r.text and not CHAMADAS, CHAMADAS)
r = as_("adm").get("/api/dominio/empresa", params={"codi_emp": "30", "competencia": COMP})
check("CNPJ fora do cadastro do Dominio: aviso", "não foi encontrado no cadastro" in r.text)
r = as_("ope").get("/api/dominio/empresa", params={"codi_emp": "10", "competencia": "2026-13"})
check("competencia invalida: frase", "competência" in r.text)

CHAMADAS.clear()
r = as_("ope").post("/api/dominio/notas", data={"codi_emp": "10", "competencia": COMP})
check("notas: resumo por CFOP e as notas", "Por CFOP" in r.text and "5102" in r.text and "150,00" in r.text
      and "2 linha(s) de saída" in r.text, r.text[:400])
check("o JSON cortado diz quantas linhas ha de verdade", "Mostrando 2 de 3 linhas" in r.text, r.text[-500:])
check("data ISO do programa sai como DD/MM/AAAA (com a hora quando ha)",
      ">03/09/2026<" in r.text and ">03/09/2026 14:05<" in r.text and "2026-09-03" not in r.text, r.text[:200])
PROC_NOTAS = processo_de(r.text)
check("e a planilha para baixar", PROC_NOTAS is not None)
check("o 'Baixar em Excel' fica no cabecalho, antes das tabelas",
      r.text.find("Baixar em Excel") < r.text.find("Por CFOP"), r.text[:300])
check("colunas com nome legivel", ">Valor<" in r.text and ">Data<" in r.text and ">vl_item<" not in r.text)
check("coluna sem informacao some da tela, com aviso", ">Origem<" not in r.text and ">CST PIS<" not in r.text
      and "Colunas sem informação ocultas na tela: Origem, CST PIS." in r.text, r.text[-700:])
check("e as celulas delas somem junto (a linha nao desalinha)", ">DOMINIO<" not in r.text)
check("numero com casas decimais sai com virgula (aliquota 1,65, nao 1.65)", ">1,65<" in r.text and ">1.65<" not in r.text)
CHAMADAS.clear()
r = as_("ope").post("/api/dominio/notas", data={"codi_emp": "20", "competencia": COMP})
check("notas de empresa alheia: frase, sem chamar o programa", "fora da sua lista" in r.text and not CHAMADAS)
r = as_("adm").post("/api/dominio/notas", data={"codi_emp": "30", "competencia": COMP})
check("mes sem nota: frase, sem planilha", "Nenhuma nota" in r.text and "/api/dominio/arquivo/" not in r.text)
FALHAR["notas"] = FalhaDoPrograma("Error: Domínio fora do ar")
antes = set(os.listdir(rota.TEMP_DIR))
r = as_("ope").post("/api/dominio/notas", data={"codi_emp": "10", "competencia": COMP})
check("Dominio fora: frase, e a pasta da busca nao fica para tras",
      "Domínio fora do ar" in r.text and set(os.listdir(rota.TEMP_DIR)) == antes)
FALHAR.clear()

# ============================================================================
print("\n=== 4. Planilha das notas ===")
r = as_("ope").get(f"/api/dominio/arquivo/{PROC_NOTAS}")
check("baixa como arquivo", r.status_code == 200 and r.content == b"PK planilha das notas"
      and "attachment" in r.headers.get("content-disposition", "")
      and r.headers.get("x-content-type-options") == "nosniff", r.status_code)
check("com o nome da empresa e do mes", "notas_dominio_10_202609.xlsx" in r.headers.get("content-disposition", ""))
r = as_("sql").get(f"/api/dominio/arquivo/{PROC_NOTAS}")
check("operador de outra empresa nao baixa (404)", r.status_code == 404, r.status_code)
check("identificador que nao e UUID: 404", as_("ope").get("/api/dominio/arquivo/..%5Cx").status_code == 404)

# ============================================================================
print("\n=== 5. Lista de empresas ===")
r = as_("ope").get("/api/dominio/lista")
check("operador: so a dele", "CLIENTE A LTDA" in r.text and "CLIENTE B" not in r.text and "SO NO DOMINIO" not in r.text)
r = as_("adm").get("/api/dominio/lista", params={"busca": "cliente"})
check("busca por nome", "CLIENTE A LTDA" in r.text and "CLIENTE B LTDA" in r.text and "SO NO DOMINIO" not in r.text)
r = as_("adm").get("/api/dominio/lista", params={"busca": "30"})
check("busca por codigo", "SO NO DOMINIO" in r.text and "CLIENTE A" not in r.text, r.text[:300])
r = as_("adm").get("/api/dominio/lista", params={"busca": "44.555"})
check("busca por CNPJ com mascara", "CLIENTE B LTDA" in r.text and "CLIENTE A" not in r.text)
r = as_("adm").get("/api/dominio/lista", params={"so_presumidas": "1"})
check("so presumidas", "CLIENTE B LTDA" not in r.text and "CLIENTE A LTDA" in r.text)

# ============================================================================
print("\n=== 6. Consulta livre ===")
CHAMADAS.clear()
r = as_("ope").get("/api/dominio/sql")
check("operador sem permissao: aviso, nada da tela de consulta", "edição do usuário" in r.text and "dom-sql" not in r.text)
r = as_("ope").post("/api/dominio/sql/executar", data={"sql": "SELECT 1", "competencia": COMP})
check("operador sem permissao: POST montado nao chama o programa", "edição do usuário" in r.text and not CHAMADAS)
r = as_("ope").get("/api/dominio/sql/tabelas")
check("operador sem permissao: explorador tambem barrado", "edição do usuário" in r.text)
r = as_("adm").get("/api/dominio/sql")
check("admin: a tela, com as consultas prontas", 'id="dom-sql"' in r.text and "01_saidas.sql" in r.text)
SQL = "SELECT codi_emp, nome_emp\nFROM bethadba.geempre WHERE codi_emp = :empresa AND nome_emp <> 'x'"
r = as_("adm").post("/api/dominio/sql/executar", data={"sql": SQL, "codi_emp": "10", "competencia": COMP})
chamada = next((x for x in CHAMADAS if x[0] == "consulta"), None)
check("o SQL vai para o programa num arquivo, inteiro", chamada and chamada[1] == SQL, chamada)
check("com a empresa e a competencia", chamada and chamada[2:] == (10, COMP), chamada)
check("resultado na tela: total de verdade e a linha", "501 linha(s)" in r.text and "PATRICK" in r.text, r.text[:300])
PROC_SQL = processo_de(r.text)
check("e a planilha da consulta", PROC_SQL is not None)
r = as_("adm").post("/api/dominio/sql/executar", data={"sql": "   ", "competencia": COMP})
check("consulta vazia: frase", "Escreva a consulta" in r.text)
r = as_("adm").post("/api/dominio/sql/executar", data={"sql": "SELECT 1 " + "x" * rota.LIMITE_SQL, "competencia": COMP})
check("consulta grande demais: frase, sem chamar o programa", "grande demais" in r.text)
FALHAR["consulta"] = FalhaDoPrograma("PermissionError: Só consultas de leitura são permitidas (encontrado: DELETE)")
r = as_("adm").post("/api/dominio/sql/executar", data={"sql": "DELETE FROM x", "competencia": COMP})
check("a trava de escrita do programa chega como frase", "Só consultas de leitura" in r.text, r.text[:300])
FALHAR.clear()
CHAMADAS.clear()
r = as_("sql").post("/api/dominio/sql/executar", data={"sql": "SELECT 1", "competencia": COMP})
check("operador COM a permissao consulta", "501 linha(s)" in r.text and CHAMADAS, r.text[:200])
PROC_SQL_OPE = processo_de(r.text)
r = as_("sql").post("/api/dominio/sql/executar", data={"sql": "SELECT 1", "codi_emp": "10", "competencia": COMP})
check("mas o parametro :empresa so com empresa da lista dele", "fora da sua lista" in r.text)
r = as_("adm").get(f"/api/dominio/arquivo/{PROC_SQL}")
check("quem fez a consulta baixa a planilha", r.status_code == 200 and r.content == b"PK planilha da consulta")
r = as_("adm").get(f"/api/dominio/arquivo/{PROC_SQL_OPE}")
check("outra pessoa nao baixa a planilha da consulta, nem o admin", r.status_code == 404, r.status_code)
r = as_("sql").get("/api/dominio/sql/tabelas", params={"filtro": "ef"})
check("explorador: as tabelas, clicaveis", "efsdoimp" in r.text and "/api/dominio/sql/colunas" in r.text)
r = as_("sql").get("/api/dominio/sql/colunas", params={"tabela": "efsdoimp"})
check("explorador: as colunas", "codi_emp" in r.text and "integer" in r.text)

# ============================================================================
print("\n=== 7. A permissao, na edicao do usuario ===")
check("a coluna nova esta em COLUNAS_NOVAS (senao nunca chega ao Postgres)",
      any(col == "pode_sql_dominio" for col, _ in schema_upgrades.COLUNAS_NOVAS.get("user_profiles", [])))
r = as_("adm").get(f"/admin/modals/user/edit/{u_ope}")
check("o modal tem a caixa da consulta livre", r.status_code == 200 and 'name="pode_sql_dominio"' in r.text, r.status_code)


def editar(uid, role, marca, tenant):
    dados = {"name": "Fulano", "role": role, "email": "", "tenant_ids": [str(tenant)]}
    if marca:
        dados["pode_sql_dominio"] = "1"
    return as_("adm").post(f"/admin/users/{uid}/edit", data=dados)


def marca_de(uid):
    s = SessionLocal()
    try:
        return s.query(UserProfile).filter(UserProfile.id == uid).first().pode_sql_dominio
    finally:
        s.close()


editar(u_ope, "operador", True, tA)
check("admin marca: o operador passa a poder", marca_de(u_ope) is True)
check("e a aba aparece para ele", 'data-aba="sql"' in as_("ope").get("/dominio").text)
editar(u_ope, "operador", False, tA)
check("admin desmarca: deixa de poder", marca_de(u_ope) is False)
editar(u_ope, "cliente", True, tA)
check("marca enviada para cliente nao grava", marca_de(u_ope) is False)
editar(u_ope, "operador", False, tA)
editar(u_sql, "operador", False, tB)
r = as_("sql").get(f"/api/dominio/arquivo/{PROC_SQL_OPE}")
check("operador que perdeu a permissao nao baixa mais a planilha da consulta", r.status_code == 404, r.status_code)

# ============================================================================
print("\n=== 8. Menu ===")
r = as_("ope").get("/dominio")
check("o item novo aparece em Ferramentas SPED", 'href="/dominio"' in r.text)
check("e o programa completo nao diz mais que o Dominio mora la",
      "Conferência do Lucro Real, automações e configurações" in r.text)

# ============================================================================
for p in PASTAS:
    shutil.rmtree(os.path.join(rota.TEMP_DIR, p), ignore_errors=True)

print(f"\nRESULTADO: {OK}/{OK + FALHAS} passaram")
if FALHAS:
    print("Falharam:", *_falhou, sep="\n  - ")
    sys.exit(1)
