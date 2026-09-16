# -*- coding: utf-8 -*-
"""Busca única nas Regras por Empresa: no BANCO, e sem vazar entre empresas.

Pedido do Sandro em 02/09/2026: uma caixa só que ache a regra por qualquer campo
— CFOP, CST, descrição, código do produto, CNPJ, UF — nos seis quadros da tela.

As travas que esta suíte protege, em ordem de importância:

1. **A busca roda no banco, não no navegador.** As seis tabelas são paginadas de
   10 em 10 NO SERVIDOR. Um filtro em JavaScript enxergaria só as dez linhas da
   página aberta e diria "nada encontrado" sobre uma regra que existe na página
   40 — exatamente o defeito que a exportação do Relatório de NFs tinha, gerando
   planilha só com a página visível. O teste procura de propósito por uma regra
   que está fora da primeira página.

2. **Buscar não atravessa empresa.** O termo é livre e casa por `ilike`; se o
   filtro de tenant sair da consulta, a busca vira o caminho mais fácil do
   sistema para ver a regra de outro cliente. Aqui há duas empresas com regras
   propositalmente parecidas.

3. **CNPJ digitado como gente digita.** O banco guarda "36318376000196"; a
   pessoa digita "36.318.376/0001-96". Sem normalizar, a busca mais natural do
   usuário é justamente a que não acha nada.

4. **A busca sobrevive à paginação e ao filtro de status.** Se o termo não for
   junto no "Próximo", a página 2 vem sem filtro e a pessoa acha que a busca
   falhou.
"""
import io
import os
import sys
import types
import uuid

sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_busca_regras_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

if os.path.exists("_busca_regras_test.db"):
    os.remove("_busca_regras_test.db")

from sqlalchemy import types as _satypes  # noqa: E402
import sqlalchemy.dialects.postgresql as pg  # noqa: E402


class _UUIDShim(_satypes.TypeDecorator):
    impl = _satypes.Uuid
    cache_ok = True

    def __init__(self, *a, **kw):
        kw.pop('as_uuid', None)
        super().__init__()

    def process_bind_param(self, value, dialect):
        return uuid.UUID(value) if isinstance(value, str) else value


pg.UUID = _UUIDShim

import backend.app.infrastructure.auth.supabase_client as sb  # noqa: E402

TOKENS = {}


class _Auth:
    def get_user(self, token):
        u = TOKENS.get(token)
        if not u:
            raise Exception("invalid token")
        return types.SimpleNamespace(user=u)


class _Client:
    auth = _Auth()


sb.get_supabase_client = lambda: _Client()
sb.get_supabase_admin_client = lambda: _Client()
import backend.app.api.dependencies.auth as dep_auth  # noqa: E402
dep_auth.get_supabase_client = lambda: _Client()

from backend.app.infrastructure.database.session import engine as _eng  # noqa: E402
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit(f"ABORTADO: engine aponta para {_eng.url.get_backend_name()}")

import backend.app.main as m  # noqa: E402
from backend.app.infrastructure.database.session import SessionLocal, engine  # noqa: E402
from backend.app.domain.models.base import Base  # noqa: E402
from backend.app.domain.models.auth_models import Tenant, UserProfile  # noqa: E402
from backend.app.domain.models.rules import (  # noqa: E402
    CfopClassification, CfopCreditRule, CteCfopClassification,
    IcmsAnticipationRule, IpiCreditRule, PisCofinsCreditRule)
from fastapi.testclient import TestClient  # noqa: E402

Base.metadata.create_all(bind=engine)

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
# Duas empresas, com regras de propósito parecidas.
# ============================================================================
ADMIN = uuid.uuid4()
UA, UB = uuid.uuid4(), uuid.uuid4()
A, B = str(UA), str(UB)

CNPJ_FORN = "36318376000196"

db = SessionLocal()
db.add_all([
    Tenant(id=UA, name="ALFA COMERCIO", cnpj="11111111000111"),
    Tenant(id=UB, name="BETA DISTRIBUIDORA", cnpj="22222222000122"),
    UserProfile(id=ADMIN, name="Sandro Admin", role="admin"),
])

# 40 regras de CFOP na ALFA. A "agulha" fica no fim, MUITO além da 1ª página.
for i in range(40):
    db.add(CfopClassification(
        company_cnpj="11111111000111", supplier_cnpj=CNPJ_FORN,
        supplier_cfop="1102", product_code=f"PROD{i:03d}",
        tenant_id=A, entry_cfop="1403",
        product_description=("REFRESCO DE MORANGO 500ML" if i == 37
                             else f"MERCADORIA COMUM {i:03d}"),
        status="aprovado", created_by="admin@synax.com"))

# A mesma "agulha" na BETA: se o escopo por empresa cair, ela aparece na ALFA.
db.add(CfopClassification(
    company_cnpj="22222222000122", supplier_cnpj=CNPJ_FORN,
    supplier_cfop="1102", product_code="PROD999",
    tenant_id=B, entry_cfop="1403",
    product_description="REFRESCO DE MORANGO 500ML",
    status="aprovado", created_by="admin@synax.com"))

db.add_all([
    CfopCreditRule(cfop="1556", tenant_id=A, allow_credit=False,
                   description="Uso e consumo — sem crédito", status="aprovado"),
    CfopCreditRule(cfop="1102", tenant_id=A, allow_credit=True,
                   description="Compra para revenda", status="aprovado"),
    IpiCreditRule(cfop="1101", cst_expected="00", tenant_id=A, allow_credit=True,
                  description="Industrializacao com IPI", status="aprovado"),
    PisCofinsCreditRule(cfop="1102", cst_expected="50", tenant_id=A,
                        allow_credit=True, description="Revenda tributada",
                        status="pendente_revisao"),
    IcmsAnticipationRule(uf_origem="MG", uf_destino="ES", cfop="2102", tenant_id=A,
                         mva_percentage=40.0, aliquota_interna_percentage=17.0,
                         aliquota_interestadual_percentage=12.0,
                         reducao_base_calculo_percentage=0.0,
                         description="Antecipacao MG para ES", status="aprovado"),
    CteCfopClassification(company_cnpj="11111111000111", supplier_cfop="2353",
                          emitter_uf="RJ", tenant_id=A, entry_cfop="2353",
                          status="aprovado"),
])
db.commit()
db.close()

TOKENS["tok_admin"] = types.SimpleNamespace(id=str(ADMIN), email="admin@synax.com")

from backend.app.api.dependencies.db import get_rls_db as _real_rls  # noqa: E402


def _fake_rls():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


m.app.dependency_overrides[_real_rls] = _fake_rls
c = TestClient(m.app, raise_server_exceptions=False)
c.cookies.set("access_token", "Bearer tok_admin")


def buscar(tabela, termo, tenant=A, **extra):
    params = {"tenant_id": tenant, "busca": termo}
    params.update(extra)
    return c.get(f"/admin/rules/{tabela}", params=params)


# ============================================================================
print("\n=== 1. A busca é no BANCO, não na página aberta ===")

# Sem busca: a agulha está na posição 38 de 40, ou seja, na página 4.
r = c.get("/admin/rules/cfop", params={"tenant_id": A, "page": 1, "per_page": 10})
check("sem busca, a primeira página responde", r.status_code == 200, r.status_code)
check("e a agulha NÃO está nela (é a prova de que o teste vale algo)",
      "REFRESCO DE MORANGO" not in r.text)

r = buscar("cfop", "MORANGO")
check("buscando na página 1, a agulha aparece", r.status_code == 200 and
      "REFRESCO DE MORANGO 500ML" in r.text, r.status_code)
# A descrição sai duas vezes na linha: no texto e no title= do truncamento.
check("e vem só uma linha", r.text.count("PROD037") == 1,
      r.text.count("PROD037"))
check("o contador mostra 1 resultado", "<b>1</b> resultado(s)" in r.text)
check("as 40 comuns sumiram", "MERCADORIA COMUM" not in r.text)


# ============================================================================
print("\n=== 2. Buscar NÃO atravessa empresa ===")

r = buscar("cfop", "MORANGO", tenant=A)
check("a ALFA acha 1 (a dela)", "<b>1</b> resultado(s)" in r.text, r.text[:0])
check("e o código do produto é o da ALFA", "PROD037" in r.text)
check("o produto da BETA não aparece na ALFA", "PROD999" not in r.text)

r = buscar("cfop", "MORANGO", tenant=B)
check("a BETA acha 1 (a dela)", "<b>1</b> resultado(s)" in r.text)
check("e é o produto da BETA", "PROD999" in r.text)
check("o produto da ALFA não aparece na BETA", "PROD037" not in r.text)

# O mesmo CNPJ de fornecedor existe nas duas: buscar por ele não pode juntar.
r = buscar("cfop", CNPJ_FORN, tenant=B)
check("busca por CNPJ do fornecedor não traz as 40 regras da ALFA",
      "<b>1</b> resultado(s)" in r.text and "MERCADORIA COMUM" not in r.text)


# ============================================================================
print("\n=== 3. Acha por qualquer campo ===")

casos = [
    ("cfop", "1102", "CFOP lançado", "MERCADORIA COMUM"),
    ("cfop", "1403", "CFOP de entrada", "MERCADORIA COMUM"),
    ("cfop", "PROD037", "código do produto", "REFRESCO"),
    ("icms", "1556", "CFOP da regra de ICMS", "Uso e consumo"),
    ("icms", "consumo", "descrição da regra de ICMS", "1556"),
    ("ipi", "Industrializacao", "descrição da regra de IPI", "1101"),
    ("pis_cofins", "50", "CST de PIS/COFINS", "Revenda"),
    ("pis_cofins", "Revenda", "descrição de PIS/COFINS", "1102"),
    ("anticipation", "MG", "UF de origem da antecipação", "2102"),
    ("anticipation", "Antecipacao", "descrição da antecipação", "2102"),
    # O termo procurado aparece na MENSAGEM de "nada encontrado" também, então
    # aqui a prova é o id da <tr>: ele só existe quando veio linha de verdade.
    ("cte", "2353", "CFOP do CT-e", 'id="row-cte-'),
    ("cte", "RJ", "UF do emitente do CT-e", 'id="row-cte-'),
]
for tabela, termo, oque, esperado in casos:
    r = buscar(tabela, termo)
    check(f"acha por {oque} ({tabela}: '{termo}')",
          r.status_code == 200 and esperado in r.text,
          f"status={r.status_code}")

# A listagem de CT-e comparava o tenant com `cast(..., UUID)` — o único cast do
# arquivo — e devolvia zero linha. Sem isto, a busca no quadro de CT-e nunca
# acharia nada, e a mensagem de vazio esconderia a causa.
r = c.get("/admin/rules/cte", params={"tenant_id": A})
check("a listagem de CT-e devolve linha sem busca nenhuma",
      'id="row-cte-' in r.text, "o cast de UUID no tenant voltou?")

r = buscar("icms", "CONSUMO")
check("a busca não diferencia maiúscula de minúscula", "1556" in r.text)


# ============================================================================
print("\n=== 4. CNPJ do jeito que a pessoa digita ===")

r = buscar("cfop", "36318376000196")
check("CNPJ só com dígitos acha", "MERCADORIA COMUM" in r.text)

r = buscar("cfop", "36.318.376/0001-96")
check("CNPJ com ponto e barra acha o mesmo", "MERCADORIA COMUM" in r.text,
      "a normalização de dígitos caiu")

r = buscar("cfop", "36.318.376")
check("CNPJ parcial, com pontos, também acha", "MERCADORIA COMUM" in r.text)


# ============================================================================
print("\n=== 5. Busca vazia não filtra nada ===")

def linhas(resposta):
    """Conta linhas da tabela pelo id da <tr>, que é único por linha."""
    return resposta.text.count('id="row-cfop-')


r = c.get("/admin/rules/cfop", params={"tenant_id": A, "per_page": 100})
sem_param = linhas(r)
r_vazio = buscar("cfop", "", per_page=100)
com_vazio = linhas(r_vazio)
check("sem o parâmetro e com o parâmetro vazio dão o mesmo",
      sem_param == com_vazio == 40, f"{sem_param} vs {com_vazio}")
check("busca só de espaços também não filtra",
      linhas(buscar("cfop", "   ", per_page=100)) == 40,
      linhas(buscar("cfop", "   ", per_page=100)))
check("o contador de resultados só aparece quando há busca",
      "resultado(s) para" not in r_vazio.text)


# ============================================================================
print("\n=== 6. Busca combina com status e sobrevive à paginação ===")

# O select da tela manda "Pendente"; o banco guarda "pendente_revisao". O código
# fazia status.lower() seco, então este filtro devolvia lista vazia SEMPRE — em
# todos os quadros que usam o rótulo, não só junto com a busca.
r = c.get("/admin/rules/pis_cofins", params={"tenant_id": A, "status": "Pendente"})
check("filtro 'Pendente' acha a regra pendente_revisao",
      "1102" in r.text and "Revenda tributada" in r.text,
      "status.lower() voltou a comparar 'pendente' com 'pendente_revisao'")
r = c.get("/admin/rules/pis_cofins", params={"tenant_id": A, "status": "Aprovado"})
check("filtro 'Aprovado' não traz a pendente", "Revenda tributada" not in r.text)
r = c.get("/admin/rules/pis_cofins", params={"tenant_id": A, "status": "Todos"})
check("'Todos' continua trazendo tudo", "Revenda tributada" in r.text)
r = c.get("/admin/rules/cte", params={"tenant_id": A, "status": "pendente_revisao"})
check("o valor cru que o quadro de CT-e manda continua valendo",
      r.status_code == 200 and "2353" not in r.text.split("hx-get")[1][:200],
      "o CT-e é aprovado, não deveria aparecer")

r = buscar("pis_cofins", "Revenda", status="Pendente")
check("busca + status pendente acha a regra", "1102" in r.text, r.status_code)
r = buscar("pis_cofins", "Revenda", status="Aprovado")
check("busca + status aprovado não acha (ela é pendente)",
      "casa com" in r.text or "Nenhuma" in r.text)

r = buscar("cfop", "MERCADORIA", per_page=10, page=1)
check("busca com 39 resultados pagina", "Página 1 de 4" in r.text, "paginação")
r = buscar("cfop", "MERCADORIA", per_page=10, page=4)
check("a página 4 da busca responde", r.status_code == 200 and "Página 4 de 4" in r.text)
check("e continua filtrada (39, não 40)", "<b>39</b> resultado(s)" in r.text)

r = buscar("cfop", "MORANGO", page=9)
check("página além do fim volta para a última, sem erro",
      r.status_code == 200 and "Página 1 de 1" in r.text, r.status_code)


# ============================================================================
print("\n=== 7. A tela leva o termo junto em todo lugar que recarrega ===")

RULES = io.open(os.path.join("frontend", "templates", "partials", "admin",
                             "rules_list.html"), encoding="utf-8").read()
check("a caixa de busca existe, com id próprio", 'id="busca-regras"' in RULES)
check("e manda o campo com o nome que a rota espera", 'name="busca"' in RULES)
check("o disparo é debounced (senão é 6 requisições por tecla)",
      "setTimeout" in RULES and "buscaRegras" in RULES)
check("tem botão de limpar", 'id="limpar-busca-regras"' in RULES)
check("o 'x' nativo do input[type=search] também dispara",
      "addEventListener('search'" in RULES)

for tabela, cid in (("cfop", "table-container-cfop"), ("cte", "table-container-cte"),
                    ("icms", "table-container-icms"), ("ipi", "table-container-ipi"),
                    ("pis_cofins", "table-container-pis-cofins"),
                    ("anticipation", "table-container-anticipation")):
    T = io.open(os.path.join("frontend", "templates", "partials", "admin", "tables",
                             f"{tabela}.html"), encoding="utf-8").read()
    # O listener precisa estar na RAIZ do partial: a paginação troca o invólucro
    # do rules_list por outerHTML, e o que estiver declarado lá some no 1º clique.
    raiz = T[:T.find(">", T.find(f'id="{cid}"'))]
    check(f"{tabela}: a raiz escuta buscaRegras",
          'hx-trigger="buscaRegras from:body"' in raiz, raiz[-120:])
    check(f"{tabela}: e inclui a caixa por ID",
          'hx-include="#busca-regras"' in raiz, raiz[-120:])
    # Todo controle que recarrega a tabela leva o termo.
    corpo = T[T.find("<!-- Pagina"):] or T
    check(f"{tabela}: paginação e filtros levam a busca",
          corpo.count("#busca-regras") >= 3, corpo.count("#busca-regras"))

check("nenhum hx-include por NAME (pegaria os campos das linhas)",
      "hx-include=\"[name='busca']\"" not in RULES)


# ============================================================================
print("\n=== 8. Termo malicioso não vira HTML ===")

r = buscar("cfop", "<script>alert(1)</script>")
check("a resposta não devolve o script cru",
      "<script>alert(1)</script>" not in r.text, "XSS refletido na busca")
check("ele aparece escapado", "&lt;script&gt;" in r.text)
check("e a busca simplesmente não acha nada",
      "casa com" in r.text, "esperava o estado vazio")

r = buscar("cfop", "%")
check("curinga do SQL digitado à mão não quebra a consulta",
      r.status_code == 200, r.status_code)
r = buscar("cfop", "'; DROP TABLE cfop_classifications; --")
check("injeção de SQL não derruba nada", r.status_code == 200, r.status_code)
db = SessionLocal()
check("e a tabela continua lá",
      db.query(CfopClassification).count() == 41,
      db.query(CfopClassification).count())
db.close()


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

engine.dispose()
if os.path.exists("_busca_regras_test.db"):
    try:
        os.remove("_busca_regras_test.db")
    except OSError:
        pass

sys.exit(1 if FALHAS else 0)
