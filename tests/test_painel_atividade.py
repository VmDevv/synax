# -*- coding: utf-8 -*-
"""Painel do Radar de Atividade: os números e o que eles NÃO podem afirmar.

O Radar mostrava só a lista das últimas auditorias. O painel (pedido do Sandro em
02/09/2026) responde três perguntas que a lista não responde: quem está
produzindo, qual empresa dá mais trabalho, e quanta curadoria está represada.

As travas que esta suíte protege, em ordem de importância:

1. **`result_data` nunca chega ao painel.** Ele guarda o relatório inteiro de
   uma auditoria — alguns MB por linha. Somar inconsistências abrindo esse JSON
   seria baixar centenas de MB a cada abertura da tela. O teste inspeciona o SQL
   que sai e recusa qualquer consulta que mencione a coluna.

2. **Inconsistência por empresa é a da ÚLTIMA auditoria, não a soma.** Rodar a
   mesma competência três vezes é rotina (corrige regra, roda de novo). Somando,
   a empresa que já foi corrigida apareceria como a pior justamente por causa do
   retrabalho.

3. **Contador nulo não vira zero.** Auditoria anterior a 02/09/2026 não tem os
   contadores. Contá-la como zero faria o painel afirmar "não achou nada" sobre
   o que ele não mediu — elas são contadas à parte, em `sem_contador`.

4. **O dia vazio aparece no gráfico.** Um gráfico que só desenha os dias com
   movimento mostra uma linha cheia e esconde exatamente o que interessa ver: a
   semana em que ninguém rodou nada.

5. **Fuso.** `created_at` é UTC; o escritório é UTC-3. Sem converter, o que é
   feito depois das 21h entra no dia seguinte e toda hora aparece 3h adiantada.
"""
import io
import os
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_painel_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

if os.path.exists("_painel_test.db"):
    os.remove("_painel_test.db")

from sqlalchemy import event, types as _satypes  # noqa: E402
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

from backend.app.infrastructure.database.session import SessionLocal, engine  # noqa: E402
from backend.app.domain.models.base import Base  # noqa: E402
from backend.app.domain.models import rules, auth_models, audit  # noqa: F401,E402
from backend.app.domain.models.audit import AuditTask  # noqa: E402
from backend.app.domain.models.auth_models import Tenant, UserProfile  # noqa: E402
from backend.app.domain.models.rules import (  # noqa: E402
    CestMvaRule, CfopClassification, CfopCreditRule, IpiCreditRule,
    ItemMatchRule, PisCofinsCreditRule)
from backend.app.core import competencia as comp  # noqa: E402
from backend.app.core.fuso import hora_br, hora_local  # noqa: E402
from backend.app.core.templating import build_env, numero_br  # noqa: E402
from backend.app.services import painel_atividade as pa  # noqa: E402

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


SQL_EMITIDO = []


@event.listens_for(engine, "before_cursor_execute")
def _capturar(conn, cursor, statement, params, contexto, executemany):
    SQL_EMITIDO.append(statement)


# ============================================================================
# Cenário: 3 empresas, 3 pessoas, auditorias espalhadas no tempo.
# ============================================================================
AGORA = datetime.utcnow()

EMP_A = str(uuid.uuid4())   # muita inconsistência, empresa grande
EMP_B = str(uuid.uuid4())   # pouca nota, taxa alta
EMP_C = str(uuid.uuid4())   # limpa, mas com curadoria represada
EMP_D = str(uuid.uuid4())   # inativa, com competência antiga

# A última competência COBRÁVEL é o mês anterior — o SPED do mês corrente ainda
# nem venceu. As expectativas saem da mesma função que o serviço usa, para a
# suíte não quebrar sozinha na virada do mês.
FECHADA = comp.fechada()
MES_ANTERIOR = comp.anterior(FECHADA)
TRES_MESES = comp.anterior(comp.anterior(MES_ANTERIOR))

ANA = str(uuid.uuid4())
BRUNO = str(uuid.uuid4())

db = SessionLocal()
db.add_all([
    Tenant(id=EMP_A, name="ALFA COMERCIO", cnpj="11111111000111"),
    Tenant(id=EMP_B, name="BETA DISTRIBUIDORA", cnpj="22222222000122"),
    Tenant(id=EMP_C, name="GAMA TRANSPORTES", cnpj="33333333000133"),
    # Fora da carteira: não deve mês nenhum, e por isso não entra no heatmap.
    Tenant(id=EMP_D, name="DELTA INATIVA", cnpj="44444444000144", ativo=False),
    UserProfile(id=ANA, name="Ana Fiscal", role="operador"),
    UserProfile(id=BRUNO, name="Bruno Contador", role="admin"),
])
db.commit()


def tarefa(tenant, user, status, dias_atras, notas=None, incons=None,
           horas_atras=0, com_relatorio=True, competencia=None):
    t = AuditTask(
        id=str(uuid.uuid4()), tenant_id=tenant, user_id=user, status=status,
        file_name="sped.txt", notas_processadas=notas, inconsistencias=incons,
        competencia=competencia,
        # O relatório gordo existe de verdade: é ele que o painel não pode ler.
        result_data=('{"resumo": {"notas_processadas": 1}, "lixo": "'
                     + ("x" * 5000) + '"}') if com_relatorio else None,
    )
    t.created_at = AGORA - timedelta(days=dias_atras, hours=horas_atras)
    t.updated_at = t.created_at
    return t


# ALFA: auditada 3 vezes na mesma competência — a primeira achou 90, a última 12.
db.add_all([
    tarefa(EMP_A, ANA, "concluido", 9, notas=2000, incons=90, competencia=FECHADA),
    tarefa(EMP_A, ANA, "concluido", 5, notas=2000, incons=40, competencia=FECHADA),
    tarefa(EMP_A, ANA, "concluido", 2, notas=2000, incons=12, competencia=FECHADA),
    # BETA: 60 notas, 30 inconsistências — número menor, problema maior. E a
    # competência é de três meses atrás: fica com meses em aberto no heatmap.
    tarefa(EMP_B, BRUNO, "concluido", 3, notas=60, incons=30, competencia=TRES_MESES),
    # GAMA: limpa, e sem competência gravada em execução nenhuma (histórico
    # anterior à coluna) — some do heatmap e é contada à parte.
    tarefa(EMP_C, BRUNO, "concluido", 4, notas=500, incons=0),
    # DELTA está inativa: tem competência velha, mas não é cobrada por isso.
    # Fora de qualquer período do painel (200 dias) de propósito — o heatmap não
    # tem recorte de data, e assim ela não mexe nos KPIs das outras seções.
    tarefa(EMP_D, BRUNO, "concluido", 200, notas=90, incons=2, competencia=TRES_MESES),
    # Um erro e uma em curso, da Ana.
    tarefa(EMP_B, ANA, "erro", 1),
    tarefa(EMP_C, ANA, "processando", 0),
    # Auditoria antiga, de antes das colunas: contadores nulos.
    tarefa(EMP_A, BRUNO, "concluido", 6, notas=None, incons=None),
    # Fora do período de 7 dias, dentro do de 30.
    tarefa(EMP_A, ANA, "concluido", 20, notas=1000, incons=500,
           competencia=MES_ANTERIOR),
])
db.commit()

# Regras: pendentes (represadas) e aprovadas (não contam como pendência).
db.add_all([
    CfopClassification(company_cnpj="1", supplier_cnpj="9", supplier_cfop="1102",
                       product_code="P1", tenant_id=EMP_C, entry_cfop="1403",
                       status="pendente_revisao", created_by="ana@synax.com"),
    CfopClassification(company_cnpj="1", supplier_cnpj="9", supplier_cfop="1102",
                       product_code="P2", tenant_id=EMP_C, entry_cfop="1403",
                       status="pendente_revisao", created_by="ana@synax.com"),
    CfopClassification(company_cnpj="1", supplier_cnpj="9", supplier_cfop="1102",
                       product_code="P3", tenant_id=EMP_C, entry_cfop="1403",
                       status="aprovado", created_by="bruno@synax.com"),
    IpiCreditRule(cfop="1102", cst_expected="00", tenant_id=EMP_C,
                  allow_credit=True, status="pendente_revisao",
                  created_by="ana@synax.com"),
    CfopCreditRule(cfop="1556", tenant_id=EMP_A, allow_credit=False,
                   status="pendente_revisao", created_by="ana@synax.com"),
    PisCofinsCreditRule(cfop="1102", cst_expected="50", tenant_id=EMP_B,
                        allow_credit=True, status="aprovado",
                        created_by="bruno@synax.com"),
    # created_by com UUID (é assim que item_match_rules grava).
    ItemMatchRule(company_cnpj="1", supplier_cnpj="9", sped_cod="A", xml_cod="B",
                  tenant_id=EMP_A, status="aprovado", created_by=BRUNO),
    # Portaria 16: dado legal, NÃO é curadoria de ninguém.
    CestMvaRule(cest="28.038.00", tenant_id=EMP_A, ncm="1234",
                status="pendente_revisao", created_by="system_seeder"),
])
db.commit()
db.close()


# ============================================================================
print("\n=== 1. O relatório gordo (result_data) nunca sai do banco ===")

db = SessionLocal()
SQL_EMITIDO.clear()
P = pa.montar_painel(db, 30)
idas = len(SQL_EMITIDO)
db.close()

vazou = [s for s in SQL_EMITIDO if "result_data" in s]
check("nenhuma consulta do painel menciona result_data", not vazou,
      (vazou[:1] or [""])[0][:160])
check("o painel custa 6 idas ao banco", idas == 6,
      f"{idas} idas — alguma consulta a mais entrou no caminho")
check("e nenhuma delas é um SELECT * da audit_tasks",
      not any("audit_tasks.error_message" in s for s in SQL_EMITIDO))

# A prova é o SQL que saiu, não o texto do arquivo: a consulta das auditorias
# precisa nomear as seis colunas leves e nenhuma das duas gordas.
sql_tarefas = next((s for s in SQL_EMITIDO if "FROM audit_tasks" in s), "")
check("a consulta das auditorias existe", bool(sql_tarefas))
check("ela traz as seis colunas leves",
      all(c in sql_tarefas for c in ("tenant_id", "user_id", "status", "created_at",
                                     "notas_processadas", "inconsistencias")),
      sql_tarefas[:200])
check("e nenhuma das duas colunas de texto gordo",
      "result_data" not in sql_tarefas and "error_message" not in sql_tarefas,
      sql_tarefas[:200])


# ============================================================================
print("\n=== 2. Inconsistência da empresa: a ÚLTIMA auditoria, não a soma ===")

topo = {e["nome"]: e for e in P["empresas_inconsistencias"]}
alfa = topo.get("ALFA COMERCIO")
check("a ALFA aparece no topo", alfa is not None, list(topo))
check("com 12 (a última), não 142 (a soma das três)",
      alfa and alfa["inconsistencias"] == 12,
      alfa and alfa["inconsistencias"])
check("nem 500 (a auditoria de 20 dias atrás, fora de 30? não — dentro)",
      alfa and alfa["inconsistencias"] != 500)

beta = topo.get("BETA DISTRIBUIDORA")
check("a BETA tem 30 inconsistências", beta and beta["inconsistencias"] == 30, beta)
check("e a taxa por 100 notas mostra que ela é o caso pior",
      beta and alfa and beta["taxa"] > alfa["taxa"],
      f"beta={beta and beta['taxa']} alfa={alfa and alfa['taxa']}")
check("50 a cada 100 notas na BETA (30 de 60)", beta and beta["taxa"] == 50.0,
      beta and beta["taxa"])
check("0,6 a cada 100 na ALFA (12 de 2000)", alfa and alfa["taxa"] == 0.6,
      alfa and alfa["taxa"])

check("a ordenação é por quantidade, a BETA na frente",
      [e["nome"] for e in P["empresas_inconsistencias"]][:2]
      == ["BETA DISTRIBUIDORA", "ALFA COMERCIO"],
      [e["nome"] for e in P["empresas_inconsistencias"]])
check("empresa sem inconsistência ainda assim aparece na lista",
      any(e["nome"] == "GAMA TRANSPORTES" for e in P["empresas_inconsistencias"]))
check("o topo é cortado em 5", len(P["empresas_inconsistencias"]) <= pa.TOPO)


# ============================================================================
print("\n=== 3. Contador nulo não vira zero ===")

check("a auditoria antiga é contada à parte", P["sem_contador"] == 1,
      P["sem_contador"])
check("e não entra como 'empresa com zero inconsistência'",
      alfa and alfa["inconsistencias"] == 12)
check("a soma de notas ignora as nulas (2000+2000+2000+60+500+1000)",
      P["kpis"]["notas"] == 7560, P["kpis"]["notas"])
check("a soma de inconsistências também (90+40+12+30+0+500)",
      P["kpis"]["inconsistencias"] == 672, P["kpis"]["inconsistencias"])


# ============================================================================
print("\n=== 4. Gráfico por dia: o dia vazio aparece ===")

serie = P["por_dia"]
check("a série tem uma barra por dia do período", len(serie) == 30, len(serie))
check("em ordem cronológica", serie[0]["dia"] < serie[-1]["dia"])
check("o último dia é hoje (no horário local)",
      serie[-1]["dia"] == hora_local(datetime.utcnow()).date(), serie[-1]["dia"])
vazios = [d for d in serie if d["total"] == 0]
check("os dias sem movimento estão na série", len(vazios) > 10, len(vazios))
check("cada barra separa concluída, erro e em curso",
      all({"concluido", "erro", "outros", "total"} <= set(d) for d in serie))
check("o total de cada dia fecha com as partes",
      all(d["total"] == d["concluido"] + d["erro"] + d["outros"] for d in serie))
check("a soma da série bate com o total de auditorias do período",
      sum(d["total"] for d in serie) == P["kpis"]["auditorias"],
      f"{sum(d['total'] for d in serie)} vs {P['kpis']['auditorias']}")
check("o fim de semana vem marcado para sombrear",
      any(d["dia_semana"] >= 5 for d in serie))

P7 = pa.montar_painel(SessionLocal(), 7)
check("período de 7 dias devolve 7 barras", len(P7["por_dia"]) == 7)
check("e deixa de fora as auditorias de 9 e de 20 dias atrás (40+12+30+0)",
      P7["kpis"]["inconsistencias"] == 82,
      P7["kpis"]["inconsistencias"])
check("na janela de 7 dias a ALFA já cai para 12 (a última)",
      next((e["inconsistencias"] for e in P7["empresas_inconsistencias"]
            if e["nome"] == "ALFA COMERCIO"), None) == 12,
      P7["empresas_inconsistencias"])
check("período inválido cai no padrão de 30",
      pa.montar_painel(SessionLocal(), 999)["dias"] == 30)


# ============================================================================
print("\n=== 5. Fuso: o banco é UTC, a tela é Brasília ===")

check("o deslocamento é de 3 horas para trás",
      hora_local(datetime(2026, 9, 2, 18, 0)) == datetime(2026, 9, 2, 15, 0))
check("23h UTC é 20h do MESMO dia",
      hora_local(datetime(2026, 9, 2, 23, 0)).date() == datetime(2026, 9, 2).date())
check("1h UTC é 22h do dia ANTERIOR — o que sem isto ia para o dia errado",
      hora_local(datetime(2026, 9, 2, 1, 0)).date() == datetime(2026, 9, 1).date())
check("None continua None", hora_local(None) is None)
check("o filtro de template formata", hora_br(datetime(2026, 9, 2, 18, 30))
      == "02/09/2026 15:30", hora_br(datetime(2026, 9, 2, 18, 30)))
check("e vazio vira travessão, não erro", hora_br(None) == "—")

LISTA = io.open(os.path.join("frontend", "templates", "partials", "admin",
                             "activity_list.html"), encoding="utf-8").read()
check("a lista de registros usa o filtro, não strftime cru",
      "| hora_br" in LISTA and "created_at.strftime" not in LISTA)


# ============================================================================
print("\n=== 6. Uso por pessoa ===")

pessoas = {u["nome"]: u for u in P["pessoas"]}
ana = pessoas.get("Ana Fiscal")
bruno = pessoas.get("Bruno Contador")
check("as duas pessoas aparecem pelo nome, não pelo UUID",
      ana is not None and bruno is not None, list(pessoas))
check("a Ana rodou 6 auditorias", ana and ana["auditorias"] == 6,
      ana and ana["auditorias"])
check("com 1 erro", ana and ana["erros"] == 1, ana and ana["erros"])
check("e 4 concluídas", ana and ana["concluidas"] == 4, ana and ana["concluidas"])
check("a taxa de sucesso ignora a que ainda está em curso",
      ana and ana["taxa_sucesso"] == 80.0, ana and ana["taxa_sucesso"])
check("conta quantas empresas a pessoa tocou",
      ana and ana["empresas"] == 3, ana and ana["empresas"])
check("a última atividade vem no horário local",
      ana and ana["ultima"] is not None)
check("ordena por volume, a Ana na frente",
      P["pessoas"][0]["nome"] == "Ana Fiscal", P["pessoas"][0]["nome"])
check("as notas somadas por pessoa batem com o total",
      sum(u["notas"] for u in P["pessoas"]) == P["kpis"]["notas"])


# ============================================================================
print("\n=== 7. Curadoria: represada por empresa e por autor ===")

pend = {e["nome"]: e for e in P["empresas_pendencias"]}
gama = pend.get("GAMA TRANSPORTES")
check("a GAMA lidera o represado", P["empresas_pendencias"][0]["nome"] == "GAMA TRANSPORTES",
      [e["nome"] for e in P["empresas_pendencias"]])
check("com 3 regras (2 de CFOP + 1 de IPI)", gama and gama["total"] == 3,
      gama and gama["total"])
check("abertas por tipo", gama and gama["por_tipo"].get("CFOP de entrada") == 2
      and gama["por_tipo"].get("Crédito de IPI") == 1, gama and gama["por_tipo"])
check("a regra APROVADA da GAMA não conta como pendência",
      gama and gama["total"] == 3)
check("a BETA, que só tem regra aprovada, fica fora da lista",
      "BETA DISTRIBUIDORA" not in pend, list(pend))
check("o KPI de represadas soma tudo (3 da GAMA + 1 da ALFA)",
      P["kpis"]["regras_pendentes"] == 4, P["kpis"]["regras_pendentes"])
check("a Portaria 16 (cest_mva_rules) NÃO entra como curadoria",
      P["kpis"]["regras_pendentes"] == 4 and
      not any("Portaria" in t for e in P["empresas_pendencias"] for t in e["por_tipo"]))
check("cest_mva_rules não está na lista de tabelas de curadoria",
      CestMvaRule not in [m for m, _ in pa.TABELAS_DE_REGRA])

curadores = {c["nome"]: c for c in P["curadores"]}
check("o autor por e-mail aparece como o e-mail",
      "ana@synax.com" in curadores, list(curadores))
check("e é marcado como NÃO identificado por conta",
      curadores.get("ana@synax.com") and not curadores["ana@synax.com"]["por_conta"])
check("a Ana cadastrou 4 regras (3 pendentes + 0 aprovadas dela)",
      curadores.get("ana@synax.com") and curadores["ana@synax.com"]["total"] == 4,
      curadores.get("ana@synax.com"))
check("o autor gravado por UUID é resolvido para o nome",
      "Bruno Contador" in curadores, list(curadores))
check("e vem marcado como identificado por conta",
      curadores.get("Bruno Contador") and curadores["Bruno Contador"]["por_conta"])
check("a regra da Portaria 16 não aparece em autor nenhum",
      "system_seeder" not in curadores, list(curadores))


# ============================================================================
print("\n=== 8. As nove tabelas de regra estão cobertas ===")

# As duas do gerador de SPED (09/10/2026) são separadas das da auditoria, mas
# passam pela mesma fila de aprovação — pendência ali segura um arquivo.
nomes_tabelas = {m.__tablename__ for m, _ in pa.TABELAS_DE_REGRA}
esperadas = {"cfop_classifications", "cte_cfop_classifications", "cfop_credit_rules",
             "ipi_credit_rules", "pis_cofins_credit_rules",
             "icms_anticipation_rules", "item_match_rules",
             "gerador_cfop_regras", "gerador_cte_cfop_regras"}
check("todas as tabelas de maker-checker entram na conta",
      nomes_tabelas == esperadas, nomes_tabelas ^ esperadas)
check("cada uma tem rótulo legível na tela",
      all(rotulo and not rotulo.islower() for _, rotulo in pa.TABELAS_DE_REGRA))


# ============================================================================
print("\n=== 8b. Heatmap de competências: quem está com mês em aberto ===")

HM = P["competencias"]
por_nome = {linha["nome"]: linha for linha in HM["linhas"]}

check("a régua tem 12 meses", len(HM["meses"]) == 12, len(HM["meses"]))
check("e termina na última competência FECHADA — o mês corrente não é cobrado",
      HM["meses"][-1]["comp"] == FECHADA, HM["meses"][-1])

beta = por_nome.get("BETA DISTRIBUIDORA")
check("a BETA parou três meses atrás", beta is not None, list(por_nome))
check("e aparece com 3 competências em aberto", beta and beta["em_aberto"] == 3,
      beta and beta["em_aberto"])
check("quem tem mais mês em aberto vem primeiro",
      HM["linhas"][0]["nome"] == "BETA DISTRIBUIDORA",
      [l["nome"] for l in HM["linhas"]])

alfa = por_nome.get("ALFA COMERCIO")
check("a ALFA está em dia (dois meses seguidos conferidos)",
      alfa and alfa["em_aberto"] == 0, alfa and alfa["em_aberto"])
check("mês anterior à entrada da empresa não conta como buraco",
      alfa and sum(1 for c in alfa["celulas"] if c["esperada"]) == 2,
      alfa and [c["esperada"] for c in alfa["celulas"]])
celula_fechada = next(c for c in alfa["celulas"] if c["comp"] == FECHADA)
check("a célula do mês reprocessado mostra a ÚLTIMA execução (12, não 90)",
      celula_fechada["inconsistencias"] == 12, celula_fechada)

check("empresa INATIVA não é cobrada por mês nenhum",
      "DELTA INATIVA" not in por_nome, list(por_nome))
check("empresa sem competência gravada não vira empresa em dia",
      "GAMA TRANSPORTES" not in por_nome, list(por_nome))
check("ela é contada à parte, para a tela pedir o backfill",
      HM["sem_competencia"] == 1, HM["sem_competencia"])
check("o total de meses em aberto é a soma das linhas",
      HM["meses_em_aberto"] == sum(l["em_aberto"] for l in HM["linhas"]),
      HM["meses_em_aberto"])


# ============================================================================
print("\n=== 9. Colunas novas, índices e a rota ===")

from backend.app.infrastructure.database.schema_upgrades import COLUNAS_NOVAS  # noqa: E402
check("as colunas novas estão registradas no schema_upgrades",
      {c for c, _ in COLUNAS_NOVAS.get("audit_tasks", [])}
      == {"notas_processadas", "inconsistencias", "competencia",
          # 25/09/2026: o valor em jogo, para o resumo das auditorias por e-mail.
          "vl_estornar", "vl_aproveitar", "vl_recolher", "vl_conferir"},
      COLUNAS_NOVAS.get("audit_tasks"))
check("sem DEFAULT, para que nulo continue significando 'nunca medido'",
      all("DEFAULT" not in ddl.upper() for _, ddl in COLUNAS_NOVAS["audit_tasks"]),
      COLUNAS_NOVAS["audit_tasks"])

UPG = io.open(os.path.join("backend", "app", "infrastructure", "database",
                           "schema_upgrades.py"), encoding="utf-8").read()
check("o schema_upgrades também cria índice que faltar",
      "_criar_indices_faltantes" in UPG and "checkfirst=True" in UPG)

CC = io.open(os.path.join("backend", "app", "services", "audit_engine",
                          "cross_checker.py"), encoding="utf-8").read()
check("o motor grava os contadores ao concluir",
      "task.notas_processadas =" in CC and "task.inconsistencias =" in CC)

ADMIN = io.open(os.path.join("backend", "app", "api", "routes", "admin.py"),
                encoding="utf-8").read()
check("a rota do painel existe", '"/activity/painel"' in ADMIN)
check("a rota antiga de registros continua de pé", '@router.get("/activity"' in ADMIN)

MAIN = io.open(os.path.join("backend", "app", "main.py"), encoding="utf-8").read()
check("o /admin inteiro (painel incluso) é travado em require_admin",
      'prefix="/admin", dependencies=[Depends(require_admin)]' in MAIN)

SCRIPT = io.open(os.path.join("scripts", "backfill_resumo_auditoria.py"),
                 encoding="utf-8").read()
check("o backfill simula por padrão",
      'action="store_true"' in SCRIPT and '"--aplicar"' in SCRIPT)
check("e deixa nulo o relatório ilegível, em vez de gravar zero",
      "ilegiveis += 1" in SCRIPT and "continue" in SCRIPT)


# ============================================================================
print("\n=== 10. A tela: templates existem e casam com os dados ===")

PAINEL = io.open(os.path.join("frontend", "templates", "partials", "admin",
                              "activity_panel.html"), encoding="utf-8").read()
ABAS = io.open(os.path.join("frontend", "templates", "partials", "admin",
                            "_activity_tabs.html"), encoding="utf-8").read()
PAGINA = io.open(os.path.join("frontend", "templates", "pages", "admin.html"),
                 encoding="utf-8").read()

check("a aba Radar abre o painel", "/admin/activity/painel" in PAGINA)
check("dá para voltar para os registros", "/admin/activity\"" in ABAS)
check("e do painel dá para ir para o painel de novo (aba ativa)",
      "/admin/activity/painel" in ABAS)
check("o painel troca o #admin-content inteiro, como as outras abas",
      'id="admin-content"' in PAINEL and 'hx-target="#admin-content"' in PAINEL)
check("os três períodos estão na tela", "p.periodos" in PAINEL)
check("nenhuma biblioteca de gráfico foi puxada por CDN",
      "chart.js" not in PAINEL.lower() and "cdn" not in PAINEL.lower())
check("as barras são % do pico, com piso para não sumir",
      "pico" in PAINEL and "| max" in PAINEL)
# A faixa do backfill saiu da tela em 10/09/2026 (pedido do Sandro). O que NÃO
# saiu é a medição: `sem_contador` continua apurado e continua fora das somas —
# ver o teste "a auditoria antiga é contada à parte", acima. Tratar essas
# auditorias como zero seria afirmar que elas não acharam nada.
check("a faixa do backfill não polui mais o topo do painel",
      "auditoria(s) deste período são anteriores" not in PAINEL)
check("a tela explica que o top é da última auditoria, não da soma",
      "não da soma" in PAINEL or "não da soma das execuções" in PAINEL)
check("e explica por que as duas tabelas de pessoa são separadas",
      "e-mail de login" in PAINEL)

env = build_env()
check("o filtro num_br está registrado", "num_br" in env.filters)
check("o filtro hora_br está registrado", "hora_br" in env.filters)
check("1234567 vira 1.234.567", numero_br(1234567) == "1.234.567", numero_br(1234567))
check("valor inválido não quebra a tela", numero_br(None) == "0")

# A prova real: renderizar o painel com os dados de verdade.
env.loader.searchpath = ["frontend/templates"]
try:
    html = env.get_template("partials/admin/activity_panel.html").render(p=P)
    renderizou = True
    erro = ""
except Exception as e:  # noqa: BLE001
    renderizou, html, erro = False, "", f"{type(e).__name__}: {e}"

check("o painel renderiza com os dados reais", renderizou, erro)
check("e mostra o nome das empresas do topo", "BETA DISTRIBUIDORA" in html)
check("e o nome das pessoas", "Ana Fiscal" in html and "Bruno Contador" in html)
check("com os números formatados em pt-BR", "7.560" in html, "notas somadas")
check("sem nenhum 'None' vazando para a tela", ">None<" not in html)

# Painel vazio não pode quebrar: divisão por zero no pico é o caminho fácil.
vazio = pa.montar_painel(SessionLocal(), 7)
db2 = SessionLocal()
db2.query(AuditTask).delete()
db2.commit()
db2.close()
sem_nada = pa.montar_painel(SessionLocal(), 30)
try:
    html2 = env.get_template("partials/admin/activity_panel.html").render(p=sem_nada)
    ok_vazio, erro2 = True, ""
except Exception as e:  # noqa: BLE001
    ok_vazio, html2, erro2 = False, "", f"{type(e).__name__}: {e}"

check("sem nenhuma auditoria, o painel ainda renderiza", ok_vazio, erro2)
check("e diz que não houve movimento, em vez de mostrar tabela vazia",
      "Nenhuma auditoria nos últimos" in html2)
check("a série de dias continua completa mesmo sem movimento",
      len(sem_nada["por_dia"]) == 30)
check("e a curadoria represada continua aparecendo (ela não depende de auditoria)",
      sem_nada["kpis"]["regras_pendentes"] == 4,
      sem_nada["kpis"]["regras_pendentes"])


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

engine.dispose()
if os.path.exists("_painel_test.db"):
    try:
        os.remove("_painel_test.db")
    except OSError:
        pass

sys.exit(1 if FALHAS else 0)
