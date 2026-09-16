# -*- coding: utf-8 -*-
"""A competência do SPED: o mês auditado, e o que fazer quando ele não existe.

Até 03/09/2026 o sistema só sabia QUANDO alguém subiu o arquivo. Isso não
responde a pergunta que o escritório faz todo mês — "o abril desta empresa já
foi conferido?" —, porque rodar abril em setembro é rotina. A competência sai do
registro 0000 (DT_INI) e vira coluna em `audit_tasks`.

As travas que esta suíte protege:

1. **Sem 0000 legível, a competência é NULA — nunca um mês chutado.** O parser
   tinha `periodo_inicio = "2026-01-01"` fixo como padrão: SPED com cabeçalho
   ilegível era gravado como janeiro, e a régua da ficha afirmaria, com toda a
   confiança, que janeiro estava auditado.

2. **Quem consome a ausência trata a ausência.** A consulta ao cofre do SIEG
   fica indisponível na tela (buscar janeiro devolveria "nada faltando" sobre o
   mês errado) e a capa da planilha omite a linha do período em vez de escrever
   "None a None".

3. **O motor grava a competência ao concluir**, junto com os contadores, para
   que nenhuma tela precise abrir `result_data` para saber o mês.

4. **O backfill preenche o histórico lendo o JSON uma vez** — e deixa nula a
   auditoria cujo relatório não tem período.

5. **O mês corrente nunca é cobrado.** O SPED de setembro se entrega em
   outubro; `fechada()` é sempre o mês anterior.
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import uuid
from datetime import date

sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_competencia_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

if os.path.exists("_competencia_test.db"):
    os.remove("_competencia_test.db")

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

from backend.app.infrastructure.database.session import SessionLocal, engine  # noqa: E402
from backend.app.domain.models.base import Base  # noqa: E402
from backend.app.domain.models import rules, auth_models, audit  # noqa: F401,E402
from backend.app.domain.models.audit import AuditTask  # noqa: E402
from backend.app.core import competencia as comp  # noqa: E402
from backend.app.services.audit_engine.sped_parser import parse_sped  # noqa: E402
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


def sped(linha_0000: str | None) -> str:
    """Um SPED mínimo, com ou sem o cabeçalho 0000."""
    fd, caminho = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    linhas = []
    if linha_0000:
        linhas.append(linha_0000)
    linhas.append("|0150|F1|FORNECEDOR TESTE|1058|||||||")
    with io.open(caminho, "w", encoding="latin1") as f:
        f.write("\n".join(linhas) + "\n")
    return caminho


# ============================================================================
print("\n=== 1. As contas do calendário fiscal ===")

check("data do SPED vira competência", comp.de_data("2026-04-01") == "2026-04")
check("o dia é descartado, o mês não", comp.de_data("2026-12-31") == "2026-12")
check("data vazia não vira competência", comp.de_data("") is None)
check("None também não", comp.de_data(None) is None)
check("texto que não é data também não", comp.de_data("abril/2026") is None)
check("mês inexistente é recusado", comp.de_data("2026-13-01") is None)

check("o rótulo é o que a pessoa fala", comp.rotulo("2026-04") == "04/2026")
check("competência inválida vira travessão", comp.rotulo("2026-99") == "—")
check("e o mês curto serve para o eixo do gráfico",
      comp.mes_curto("2026-04") == "abr")

check("o mês anterior de fevereiro é janeiro", comp.anterior("2026-02") == "2026-01")
check("e o de janeiro vira dezembro do ano passado",
      comp.anterior("2026-01") == "2025-12")

check("a última competência cobrável é o mês ANTERIOR ao de hoje",
      comp.fechada(date(2026, 9, 3)) == "2026-08")
check("na virada do ano, dezembro do ano anterior",
      comp.fechada(date(2026, 1, 15)) == "2025-12")
check("o mês corrente NUNCA é cobrado — o SPED dele nem venceu",
      comp.fechada(date(2026, 9, 30)) != "2026-09")

serie = comp.sequencia(12, ate="2026-08")
check("a sequência tem 12 meses", len(serie) == 12, len(serie))
check("em ordem cronológica, do mais antigo ao mais novo",
      serie[0] == "2025-09" and serie[-1] == "2026-08", (serie[0], serie[-1]))
check("e ela é contígua — mês vazio precisa virar coluna vazia",
      all(comp.anterior(serie[i + 1]) == serie[i] for i in range(len(serie) - 1)))


# ============================================================================
print("\n=== 2. Sem 0000 legível, o período é NULO (nunca janeiro) ===")

com_0000 = sped("|0000|017|0|01042026|30042026|EMPRESA TESTE|" + EMPRESA + "|ES|||")
dados = parse_sped(com_0000)
check("com cabeçalho, o período sai do arquivo",
      dados["periodo_inicio"] == "2026-04-01" and dados["periodo_fim"] == "2026-04-30",
      (dados["periodo_inicio"], dados["periodo_fim"]))
check("e a competência é abril", comp.de_data(dados["periodo_inicio"]) == "2026-04")

sem_0000 = sped(None)
vazio = parse_sped(sem_0000)
check("sem cabeçalho, o início é NULO", vazio["periodo_inicio"] is None,
      vazio["periodo_inicio"])
check("e o fim também", vazio["periodo_fim"] is None, vazio["periodo_fim"])
check("nada de 2026-01-01 fixo, que virava 'janeiro auditado'",
      vazio["periodo_inicio"] != "2026-01-01")

data_ruim = sped("|0000|017|0|XX|YY|EMPRESA TESTE|" + EMPRESA + "|ES|||")
ilegivel = parse_sped(data_ruim)
check("data com formato errado também não vira chute",
      ilegivel["periodo_inicio"] is None, ilegivel["periodo_inicio"])

FONTE_PARSER = io.open(os.path.join("backend", "app", "services", "audit_engine",
                                    "sped_parser.py"), encoding="utf-8").read()
# O comentário CITA o padrão antigo para explicar por que ele saiu; a busca é
# na atribuição, que é o que roda.
check("o padrão fixo não voltou para o parser",
      "periodo_inicio = None" in FONTE_PARSER
      and "periodo_fim = None" in FONTE_PARSER
      and 'periodo_inicio = "2026' not in FONTE_PARSER,
      "o parser voltou a chutar o período")


# ============================================================================
print("\n=== 3. O motor grava a competência ao concluir ===")

TENANT = str(uuid.uuid4())


def rodar(caminho_sped):
    """Roda a auditoria e devolve a AuditTask já gravada."""
    task_id = str(uuid.uuid4())
    db = SessionLocal()
    tarefa = AuditTask(id=task_id, tenant_id=TENANT, status="pendente",
                       file_name="sped.txt")
    db.add(tarefa)
    db.commit()
    xml_dir = tempfile.mkdtemp(prefix="synax_comp_")
    execute_audit(task_id, TENANT, caminho_sped, xml_dir, db,
                  sped_only_mode=True)
    db.expire_all()
    resultado = db.query(AuditTask).filter(AuditTask.id == task_id).first()
    dados = (resultado.status, resultado.competencia,
             json.loads(resultado.result_data or "{}").get("periodo_inicio"))
    db.close()
    return dados


status, competencia_gravada, periodo = rodar(com_0000)
check("a auditoria conclui", status == "concluido", status)
check("e a competência de abril fica na coluna", competencia_gravada == "2026-04",
      competencia_gravada)
check("o período continua dentro do relatório, para o SIEG",
      periodo == "2026-04-01", periodo)

status2, competencia2, periodo2 = rodar(sem_0000)
check("SPED sem cabeçalho ainda conclui", status2 == "concluido", status2)
check("mas a competência fica NULA — o motor não inventa mês",
      competencia2 is None, competencia2)
check("e o período do relatório também é nulo", periodo2 is None, periodo2)


# ============================================================================
print("\n=== 4. Quem consome a ausência trata a ausência ===")

FONTE_CURATION = io.open(os.path.join("frontend", "templates", "partials",
                                      "curation", "curation_results.html"),
                         encoding="utf-8").read()
check("a tela só oferece o SIEG quando há período",
      "{% if result.periodo_inicio and result.periodo_fim %}" in FONTE_CURATION)
check("e explica por que o botão está desabilitado",
      "Consulta indisponível — SPED sem período" in FONTE_CURATION)

from backend.app.services.audit_engine import excel_exporter  # noqa: E402
FONTE_EXCEL = io.open(excel_exporter.__file__, encoding="utf-8").read()
check("a capa da planilha não escreve 'None a None'",
      'data.get("periodo_inicio") or ""' in FONTE_EXCEL, "sem o `or ''`")

FONTE_MODEL = io.open(os.path.join("backend", "app", "domain", "models",
                                   "audit.py"), encoding="utf-8").read()
check("a coluna é texto de 7 (AAAA-MM), que ordena sozinho",
      "competencia = Column(String(7)" in FONTE_MODEL)


# ============================================================================
print("\n=== 5. O backfill preenche o histórico, sem inventar mês ===")

db = SessionLocal()
ANTIGA_COM = str(uuid.uuid4())
ANTIGA_SEM = str(uuid.uuid4())
db.add_all([
    # Auditoria antiga: relatório com período, colunas em branco.
    AuditTask(id=ANTIGA_COM, tenant_id=TENANT, status="concluido",
              result_data=json.dumps({"periodo_inicio": "2026-03-01",
                                      "resumo": {"notas_processadas": 120,
                                                 "inconsistencias": 7}})),
    # Relatório sem período: a coluna tem de continuar nula.
    AuditTask(id=ANTIGA_SEM, tenant_id=TENANT, status="concluido",
              result_data=json.dumps({"resumo": {"notas_processadas": 5,
                                                 "inconsistencias": 0}})),
])
db.commit()
db.close()

_spec = importlib.util.spec_from_file_location(
    "backfill_resumo", os.path.join("scripts", "backfill_resumo_auditoria.py"))
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

argv_original = sys.argv
sys.argv = ["backfill_resumo_auditoria.py"]          # simulação
backfill.main()
db = SessionLocal()
antes = db.query(AuditTask).filter(AuditTask.id == ANTIGA_COM).first().competencia
db.close()
check("sem --aplicar, nada é gravado (simula por padrão)", antes is None, antes)

sys.argv = ["backfill_resumo_auditoria.py", "--aplicar"]
backfill.main()
sys.argv = argv_original

db = SessionLocal()
com = db.query(AuditTask).filter(AuditTask.id == ANTIGA_COM).first()
sem = db.query(AuditTask).filter(AuditTask.id == ANTIGA_SEM).first()
db.close()

check("a competência antiga é preenchida a partir do relatório",
      com.competencia == "2026-03", com.competencia)
check("junto com os contadores, na mesma leitura do JSON",
      com.notas_processadas == 120 and com.inconsistencias == 7,
      (com.notas_processadas, com.inconsistencias))
check("relatório sem período continua com competência nula",
      sem.competencia is None, sem.competencia)
check("mas os contadores dele são preenchidos assim mesmo",
      sem.notas_processadas == 5 and sem.inconsistencias == 0,
      (sem.notas_processadas, sem.inconsistencias))


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

engine.dispose()
for lixo in (com_0000, sem_0000, data_ruim, "_competencia_test.db"):
    if lixo and os.path.exists(lixo):
        try:
            os.remove(lixo)
        except OSError:
            pass

sys.exit(1 if FALHAS else 0)
