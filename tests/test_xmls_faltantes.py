# -*- coding: utf-8 -*-
"""XMLs Faltantes: a nota está no SPED e o arquivo não veio — com o CFOP.

A aba lista o que o cliente escriturou e não mandou o XML, para o escritório
cobrar. Até 11/09/2026 ela mostrava chave, fornecedor e operação; quem vai
atrás da nota no sistema do cliente procura pelo CFOP e pelo número, não por
uma chave de 44 dígitos. Pedido do Sandro.

O que esta suíte trava:

1. **O CFOP sai do documento, não é deduzido.** Na NF-e ele vive nos ITENS
   (C170) e uma nota pode ter vários; no CT-e vive no ANALÍTICO (D190), porque
   o D100 não tem registro de item. São dois caminhos diferentes no motor e os
   dois precisam entregar a mesma coisa.

2. **Vários CFOPs aparecem todos, sem repetir.** Nota com 1102 e 1556 tem de
   mostrar os dois: se a tela mostrasse só o primeiro, a pessoa procuraria a
   nota pelo CFOP errado.

3. **Documento sem CFOP legível fica com travessão.** CT-e sem D190 não tem de
   onde tirar o código, e inventar um mandaria procurar o que não existe.
"""
import io
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_faltantes_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

if os.path.exists("_faltantes_test.db"):
    os.remove("_faltantes_test.db")

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
sb.get_supabase_client = lambda: type("C", (), {"auth": None})()
sb.get_supabase_admin_client = sb.get_supabase_client

from backend.app.infrastructure.database.session import engine as _eng  # noqa: E402
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit(f"ABORTADO: engine aponta para {_eng.url.get_backend_name()}.")

import json  # noqa: E402
from backend.app.infrastructure.database.session import SessionLocal, engine  # noqa: E402
from backend.app.domain.models.base import Base  # noqa: E402
from backend.app.domain.models import rules, audit  # noqa: F401,E402
from backend.app.domain.models.audit import AuditTask  # noqa: E402
from backend.app.core.templating import build_env  # noqa: E402
from backend.app.services.audit_engine.cross_checker import execute_audit  # noqa: E402

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


EMPRESA = "11222333000181"
CHAVE_NF = "3526081122233300018155001000012345" + "1000012345"
CHAVE_CTE = "3526083499247200027057001000014983" + "1000014983"
CHAVE_CTE_SEM_D190 = "3526083499247200027057001000014984" + "1000014984"


def montar_sped() -> str:
    """SPED com uma NF-e de dois CFOPs e dois CT-e, um deles sem analítico."""
    linhas = [
        "|0000|018|0|01072026|31072026|EMPRESA TESTE|" + EMPRESA + "|ES|||",
        "|0150|F1|FORNECEDOR ALFA|1058|11111111000111||||||",
        "|0150|T1|TRANSPORTADORA BETA|1058|22222222000122||||||",
        "|0200|CAFE500|CAFE TORRADO 500G||UN|||||",
        "|0200|OLEO900|OLEO DE SOJA 900ML||UN|||||",
        "|C001|0|",
        # NF-e escriturada, XML não enviado. Dois itens em CFOPs diferentes, e
        # um terceiro repetindo o primeiro — a tela não pode mostrar "1102" duas
        # vezes.
        f"|C100|0|1|F1|55|00|1|12345|{CHAVE_NF}|12072026|12072026|1800,00|0|"
        "0,00|0,00|1800,00|9|0,00|0,00|0,00|1500,00|180,00|0,00|0,00|0,00|"
        "0,00|0,00|0,00|0,00|",
        "|C170|1|CAFE500||10,00000|UN|1000,00||0|000|1102||1000,00|12,00|120,00|"
        + "|" * 23,
        "|C170|2|OLEO900||5,00000|UN|500,00||0|060|1556||||"
        + "|" * 23,
        "|C170|3|CAFE500||2,00000|UN|300,00||0|000|1102||300,00|12,00|36,00|"
        + "|" * 23,
        "|C190|000|1102|12,00|1300,00|1300,00|156,00|0,00|0,00|0,00|0,00||",
        "|C190|060|1556|0,00|500,00|0,00|0,00|0,00|0,00|0,00|0,00||",
        "|C990|8|",
        "|D001|0|",
        # CT-e escriturado sem XML: o CFOP vem do D190.
        f"|D100|0|1|T1|57|00|1|0|149831|{CHAVE_CTE}|15072026|15072026|0||"
        "130,75|0,00|1|130,75|0,00|0,00|0,00||",
        "|D190|090|2352|0,00|130,75|0,00|0,00|0,00||",
        # E um CT-e sem analítico nenhum: não há de onde tirar o CFOP.
        f"|D100|0|1|T1|57|00|1|0|149832|{CHAVE_CTE_SEM_D190}|16072026|16072026|0||"
        "90,00|0,00|1|90,00|0,00|0,00|0,00||",
        "|D990|4|",
    ]
    fd, caminho = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    with io.open(caminho, "w", encoding="latin1") as f:
        f.write("\n".join(linhas) + "\n")
    return caminho


# ============================================================================
print("\n=== 1. O motor entrega o CFOP escriturado de cada faltante ===")

SPED = montar_sped()
XML_DIR = tempfile.mkdtemp(prefix="synax_faltantes_")   # vazio de propósito
TASK = str(uuid.uuid4())
TENANT = str(uuid.uuid4())

db = SessionLocal()
db.add(AuditTask(id=TASK, tenant_id=TENANT, status="pendente",
                 file_name="sped.txt"))
db.commit()
execute_audit(TASK, TENANT, SPED, XML_DIR, db)
db.expire_all()
tarefa = db.query(AuditTask).filter(AuditTask.id == TASK).first()
STATUS = tarefa.status
RESULTADO = json.loads(tarefa.result_data or "{}")
db.close()

check("a auditoria conclui mesmo sem nenhum XML", STATUS == "concluido", STATUS)

faltantes = {f["key"]: f for f in RESULTADO.get("missing_xmls", [])}
check("os três documentos entram como faltantes", len(faltantes) == 3,
      sorted(faltantes))

nfe = faltantes.get(CHAVE_NF)
check("a NF-e traz os CFOPs dos itens", nfe and nfe.get("cfops") == ["1102", "1556"],
      nfe and nfe.get("cfops"))
check("sem repetir o que aparece em dois itens",
      nfe and nfe["cfops"].count("1102") == 1, nfe and nfe.get("cfops"))

cte = faltantes.get(CHAVE_CTE)
check("o CT-e traz o CFOP do analítico D190",
      cte and cte.get("cfops") == ["2352"], cte and cte.get("cfops"))

sem_d190 = faltantes.get(CHAVE_CTE_SEM_D190)
check("CT-e sem D190 fica sem CFOP, em vez de herdar o do vizinho",
      sem_d190 and sem_d190.get("cfops") == [], sem_d190 and sem_d190.get("cfops"))


# ============================================================================
print("\n=== 2. A lista que vai para a tela ===")

para_tela = {f["chave"]: f for f in RESULTADO.get("xmls_faltantes", [])}
check("a tela recebe os mesmos três", len(para_tela) == 3, sorted(para_tela))
check("com o CFOP já pronto para exibir",
      para_tela.get(CHAVE_NF, {}).get("cfop") == "1102, 1556",
      para_tela.get(CHAVE_NF, {}).get("cfop"))
check("o CT-e com um CFOP só não ganha vírgula",
      para_tela.get(CHAVE_CTE, {}).get("cfop") == "2352",
      para_tela.get(CHAVE_CTE, {}).get("cfop"))
check("e o sem analítico vem vazio, para o template decidir o travessão",
      para_tela.get(CHAVE_CTE_SEM_D190, {}).get("cfop") == "",
      para_tela.get(CHAVE_CTE_SEM_D190, {}).get("cfop"))
check("os campos que já existiam continuam lá",
      all(k in para_tela[CHAVE_NF] for k in
          ("chave", "fornecedor", "ind_oper", "tipo_operacao")),
      list(para_tela[CHAVE_NF]))


# ============================================================================
print("\n=== 3. A coluna na aba XMLs Faltantes ===")

FONTE = io.open(os.path.join("frontend", "templates", "partials", "curation",
                             "curation_results.html"), encoding="utf-8").read()
# O recorte é da aba ATÉ a próxima: sem isso um "CFOP (SPED)" escrito em
# qualquer outro painel faria estes checks passarem sem a coluna existir aqui.
_inicio = FONTE.index('id="panel-tab-missing-xmls"')
_resto = FONTE[_inicio + 30:]
_fim = _resto.find('id="panel-tab-')
aba = FONTE[_inicio:_inicio + 30 + _fim] if _fim != -1 else FONTE[_inicio:]

check("a aba tem o cabeçalho da coluna", "CFOP (SPED)" in aba)
check("e a célula lê o campo que o motor entrega", "item.cfop" in aba)
check("documento sem CFOP mostra travessão, não string vazia",
      "—" in aba and "{% if item.cfop %}" in aba)

env = build_env()
html = env.get_template("partials/curation/curation_results.html").render(
    result=RESULTADO, task_id=TASK, request=None)
check("a tela renderiza com os dados reais da auditoria",
      "CFOP (SPED)" in html, "template quebrou ao desenhar a aba")
check("e mostra os CFOPs da NF-e faltante", "1102, 1556" in html)
check("com o travessão no CT-e sem analítico",
      html.count("—") >= 1, "o travessão sumiu")


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

engine.dispose()
for lixo in (SPED, "_faltantes_test.db"):
    if lixo and os.path.exists(lixo):
        try:
            os.remove(lixo)
        except OSError:
            pass
try:
    os.rmdir(XML_DIR)
except OSError:
    pass

sys.exit(1 if FALHAS else 0)
