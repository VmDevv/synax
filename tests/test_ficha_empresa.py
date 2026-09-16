# -*- coding: utf-8 -*-
"""Ficha da empresa: o que ela afirma, o que ela se recusa a afirmar.

A aba "Empresas e Usuários" dizia quem é o cliente. A ficha (pedido do Sandro em
03/09/2026) diz como ele ESTÁ: última auditoria, curadoria represada,
certificado, cadastro, quem tem acesso. Abre num painel lateral, a partir da
linha da empresa.

As travas que esta suíte protege, em ordem de importância:

1. **`result_data` não chega à ficha.** Ele guarda o relatório inteiro de uma
   auditoria — alguns MB por linha — e a ficha abre com um clique numa lista de
   170 empresas. O teste inspeciona o SQL emitido e recusa qualquer consulta que
   mencione a coluna, além de travar o custo em 9 idas ao banco.

2. **A manchete é a ÚLTIMA auditoria, não a soma.** Rodar a mesma competência
   três vezes é rotina. Somando, a empresa já corrigida apareceria como a pior
   justamente por causa do retrabalho: a ALFA aqui tem 12 inconsistências e
   soma 142.

3. **Contador nulo não vira zero.** Auditoria anterior a 02/09/2026 não tem os
   contadores. Contá-la como zero faria a ficha afirmar "não achou nada" sobre o
   que ela não mediu — ela sai com travessão e é contada à parte.

4. **Alerta é só o que dá para agir hoje.** Certificado vencendo, cadastro
   incompleto, execução que falhou, pendência velha. Inconsistência alta NÃO
   vira alerta: é o trabalho normal do escritório, e aviso que aparece sempre
   deixa de ser lido.

5. **Pendência tem idade.** "3 regras represadas" não move ninguém; "parada há
   40 dias" move.

5b. **A régua de competências não inventa atraso.** Ela cobra o mês do SPED, não
   a data do upload; nunca cobra o mês corrente (cujo SPED nem venceu) nem mês
   anterior à entrada da empresa; e, quando o histórico não tem competência
   gravada, não afirma nada — pede o backfill.

6. **Fuso.** `created_at` é UTC e a tela é UTC-3. O serviço já entrega
   convertido — por isso o template NÃO aplica `| hora_br` de novo.

7. **A rota é admin, e empresa inexistente devolve 200 com aviso.** Em 4xx o
   htmx não troca o alvo: o usuário clicaria e nada aconteceria na tela.
"""
import os
import sys
import types
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_ficha_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

# A pasta de certificados é a única E/S fora do banco na ficha. Apontando para
# uma pasta local, o teste não depende do \\srv-ap02 nem trava esperando rede.
PASTA_CERT = os.path.abspath("_ficha_certs")
os.makedirs(PASTA_CERT, exist_ok=True)
os.environ["CERTIFICADOS_DIR"] = PASTA_CERT

if os.path.exists("_ficha_test.db"):
    os.remove("_ficha_test.db")

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
    raise SystemExit(f"ABORTADO: engine aponta para {_eng.url.get_backend_name()}.")

import backend.app.main as m  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.app.infrastructure.database.session import SessionLocal, engine  # noqa: E402
from backend.app.domain.models.base import Base  # noqa: E402
from backend.app.domain.models import rules, auth_models, audit, certificates  # noqa: F401,E402
from backend.app.domain.models.audit import AuditTask  # noqa: E402
from backend.app.domain.models.auth_models import Tenant, UserProfile, UserTenant  # noqa: E402
from backend.app.domain.models.certificates import TenantCertificate  # noqa: E402
from backend.app.domain.models.rules import (  # noqa: E402
    CestMvaRule, CfopClassification, CfopCreditRule, IpiCreditRule,
    ItemMatchRule, PisCofinsCreditRule)
from backend.app.core import competencia as comp  # noqa: E402
from backend.app.core.templating import build_env  # noqa: E402
from backend.app.services import ficha_empresa as fe  # noqa: E402

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
# Cenário: cinco empresas, cada uma com um problema diferente.
# ============================================================================
AGORA = datetime.utcnow()

U_ALFA, U_BETA, U_GAMA, U_DELTA, U_EPS = (uuid.uuid4() for _ in range(5))
ALFA, BETA, GAMA, DELTA, EPS = (str(u) for u in (U_ALFA, U_BETA, U_GAMA, U_DELTA, U_EPS))

U_ANA, U_BRUNO = uuid.uuid4(), uuid.uuid4()
ANA, BRUNO = str(U_ANA), str(U_BRUNO)

CNPJ_ALFA = "11111111000111"

db = SessionLocal()
db.add_all([
    # Auditada, com curadoria represada e certificado vencendo.
    Tenant(id=U_ALFA, name="ALFA COMERCIO", cnpj=CNPJ_ALFA, uf="ES"),
    # Sem UF, sem certificado, auditada há muito tempo.
    Tenant(id=U_BETA, name="BETA DISTRIBUIDORA", cnpj="22222222000122"),
    # Inativa e nunca auditada.
    Tenant(id=U_GAMA, name="GAMA TRANSPORTES", cnpj="33333333000133", uf="RJ", ativo=False),
    # A última execução falhou.
    Tenant(id=U_DELTA, name="DELTA ATACADO", cnpj="44444444000144", uf="ES"),
    # Só existe para conferir o fuso.
    Tenant(id=U_EPS, name="EPSILON LTDA", cnpj="55555555000155", uf="MG"),
    UserProfile(id=U_ANA, tenant_id=U_ALFA, role="operador", name="Ana Fiscal"),
    UserProfile(id=U_BRUNO, role="admin", name="Bruno Contador"),
])
db.add(UserTenant(user_id=U_ANA, tenant_id=U_BETA))
db.commit()


def tarefa(tenant, user, status, dias_atras, notas=None, incons=None, quando=None,
           competencia=None):
    t = AuditTask(
        id=str(uuid.uuid4()), tenant_id=tenant, user_id=user, status=status,
        file_name="sped.txt", notas_processadas=notas, inconsistencias=incons,
        competencia=competencia,
        # O relatório gordo existe de verdade: é ele que a ficha não pode ler.
        result_data='{"resumo": {"notas": 1}, "lixo": "' + ("x" * 5000) + '"}',
    )
    t.created_at = quando or (AGORA - timedelta(days=dias_atras))
    t.updated_at = t.created_at
    return t


HORA_UTC = datetime(2026, 5, 10, 23, 30)   # 20:30 em Brasília, mesmo dia

# A última competência COBRÁVEL é sempre o mês anterior: o SPED do mês corrente
# ainda nem venceu. As expectativas saem da mesma função que o serviço usa, para
# a suíte não quebrar sozinha na virada do mês.
FECHADA = comp.fechada()
UM_MES = comp.anterior(FECHADA)
TRES_MESES = comp.anterior(comp.anterior(UM_MES))

db.add_all([
    # ALFA: a MESMA competência rodada três vezes — 90, depois 40, depois 12.
    tarefa(ALFA, ANA, "concluido", 9, notas=2000, incons=90, competencia=FECHADA),
    tarefa(ALFA, ANA, "concluido", 5, notas=2000, incons=40, competencia=FECHADA),
    tarefa(ALFA, ANA, "concluido", 2, notas=2000, incons=12, competencia=FECHADA),
    # E uma execução de antes das colunas — sem contador e sem competência.
    tarefa(ALFA, BRUNO, "concluido", 6, notas=None, incons=None),
    # BETA: uma só, e de três meses atrás — deixa competências em aberto.
    tarefa(BETA, BRUNO, "concluido", 60, notas=60, incons=30, competencia=TRES_MESES),
    # DELTA: a última falhou; a anterior conferiu o mês fechado.
    tarefa(DELTA, ANA, "concluido", 10, notas=500, incons=5, competencia=FECHADA),
    tarefa(DELTA, ANA, "erro", 2),
    # EPSILON: hora fixa, para conferir a conversão de fuso.
    tarefa(EPS, ANA, "concluido", 0, notas=10, incons=1, quando=HORA_UTC,
           competencia=FECHADA),
])
db.commit()

# Curadoria da ALFA: 3 pendentes (uma parada há 40 dias) e 3 aprovadas.
pendentes = [
    CfopClassification(company_cnpj="1", supplier_cnpj="9", supplier_cfop="1102",
                       product_code="P1", tenant_id=ALFA, entry_cfop="1403",
                       status="pendente_revisao", created_by="ana@synax.com"),
    CfopClassification(company_cnpj="1", supplier_cnpj="9", supplier_cfop="1102",
                       product_code="P2", tenant_id=ALFA, entry_cfop="1403",
                       status="pendente_revisao", created_by="ana@synax.com"),
    IpiCreditRule(cfop="1102", cst_expected="00", tenant_id=ALFA, allow_credit=True,
                  status="pendente_revisao", created_by="ana@synax.com"),
]
pendentes[0].created_at = AGORA - timedelta(days=40)   # a mais antiga
pendentes[1].created_at = AGORA - timedelta(days=3)
pendentes[2].created_at = AGORA - timedelta(days=1)

db.add_all(pendentes + [
    CfopCreditRule(cfop="1556", tenant_id=ALFA, allow_credit=False,
                   status="aprovado", created_by="bruno@synax.com"),
    PisCofinsCreditRule(cfop="1102", cst_expected="50", tenant_id=ALFA,
                        allow_credit=True, status="aprovado",
                        created_by="bruno@synax.com"),
    ItemMatchRule(company_cnpj="1", supplier_cnpj="9", sped_cod="A", xml_cod="B",
                  tenant_id=ALFA, status="aprovado", created_by=BRUNO),
    # Portaria 16: dado legal, NÃO é curadoria de ninguém — fica fora da conta.
    CestMvaRule(cest="28.038.00", tenant_id=ALFA, ncm="1234",
                status="pendente_revisao", created_by="system_seeder"),
])

# Certificado da ALFA: vence em 10 dias (alerta), com o .pfx na pasta.
with open(os.path.join(PASTA_CERT, f"EMPRESA_{CNPJ_ALFA}.pfx"), "wb") as fp:
    fp.write(b"pfx-de-mentira")
db.add(TenantCertificate(
    tenant_id=ALFA, cnpj=CNPJ_ALFA, arquivo=f"EMPRESA_{CNPJ_ALFA}.pfx",
    senha_encrypted="cifrado", apelido="A1 Alfa", ativo=True,
    validade=AGORA + timedelta(days=10)))
db.commit()
db.close()


# ============================================================================
print("\n=== 1. O relatório gordo (result_data) não chega à ficha ===")

db = SessionLocal()
SQL_EMITIDO.clear()
F = fe.montar_ficha(db, ALFA)
idas = len(SQL_EMITIDO)
db.close()

vazou = [s for s in SQL_EMITIDO if "result_data" in s]
check("nenhuma consulta da ficha menciona result_data", not vazou,
      (vazou[:1] or [""])[0][:160])
check("nem error_message (a outra coluna de texto gordo)",
      not any("error_message" in s for s in SQL_EMITIDO))
check("a ficha custa 9 idas ao banco", idas == 9,
      f"{idas} idas — alguma consulta a mais entrou no caminho")

sql_exec = next((s for s in SQL_EMITIDO
                 if "FROM audit_tasks" in s and "ORDER BY" in s), "")
check("a consulta das execuções nomeia as colunas leves",
      all(c in sql_exec for c in ("status", "created_at", "notas_processadas",
                                  "inconsistencias")), sql_exec[:200])


# ============================================================================
print("\n=== 2. A manchete é a ÚLTIMA auditoria, não a soma ===")

ultima = F["auditoria"]["ultima"]
check("a última execução da ALFA é a de 2 dias atrás",
      ultima["inconsistencias"] == 12, ultima["inconsistencias"])
check("e não a soma das quatro (142)", ultima["inconsistencias"] != 142)
check("nem a primeira, que achou 90", ultima["inconsistencias"] != 90)
check("a taxa é por 100 notas", ultima["taxa"] == 0.6, ultima["taxa"])
check("as quatro execuções continuam contadas", F["auditoria"]["total"] == 4,
      F["auditoria"]["total"])
check("e as anteriores aparecem no histórico, onde retrabalho é informação",
      [h["inconsistencias"] for h in F["auditoria"]["historico"]] == [12, 40, None, 90],
      [h["inconsistencias"] for h in F["auditoria"]["historico"]])
check("o serviço não devolve soma de inconsistências para ninguém somar",
      "inconsistencias" not in F["auditoria"],
      list(F["auditoria"]))


# ============================================================================
print("\n=== 3. Contador nulo não vira zero ===")

check("a execução sem contador é contada à parte",
      F["auditoria"]["sem_contador"] == 1, F["auditoria"]["sem_contador"])
sem = [h for h in F["auditoria"]["historico"] if not h["medida"]]
check("ela aparece no histórico, marcada como não medida", len(sem) == 1, sem)
check("e sem taxa inventada", sem and sem[0]["taxa"] is None)
check("o minigráfico só desenha o que foi medido",
      len(F["auditoria"]["spark"]) == 3, len(F["auditoria"]["spark"]))
check("em ordem cronológica (o mais recente por último)",
      [s["inconsistencias"] for s in F["auditoria"]["spark"]] == [90, 40, 12],
      [s["inconsistencias"] for s in F["auditoria"]["spark"]])
check("a tendência compara as duas últimas medições",
      F["auditoria"]["tendencia"]["direcao"] == "queda"
      and F["auditoria"]["tendencia"]["de"] == 2.0
      and F["auditoria"]["tendencia"]["para"] == 0.6,
      F["auditoria"]["tendencia"])
check("barra de valor zero continua visível (piso de altura)",
      all(s["altura"] >= 6 for s in F["auditoria"]["spark"]))

check("sem nota conferida não existe taxa — e ela não vira 0.0",
      fe._taxa(0, 0) is None and fe._taxa(5, 0) is None)
check("com base, a taxa é calculada", fe._taxa(3, 200) == 1.5)


# ============================================================================
print("\n=== 4. Curadoria: volume, tipo e IDADE ===")

cur = F["curadoria"]
check("três regras represadas na ALFA", cur["pendentes"] == 3, cur["pendentes"])
check("a Portaria 16 (cest_mva) fica de fora da curadoria",
      cur["pendentes"] == 3 and "Portaria" not in str(cur["por_tipo"]))
check("agrupadas por tipo, a maior primeiro",
      cur["por_tipo"][0]["tipo"] == "CFOP de entrada"
      and cur["por_tipo"][0]["qtd"] == 2, cur["por_tipo"])
check("a idade da mais antiga está lá — é ela que dói",
      cur["dias_parada"] == 40, cur["dias_parada"])
check("as aprovadas são contadas separadamente", cur["aprovadas"] == 3,
      cur["aprovadas"])
check("e não se misturam com as pendentes",
      cur["pendentes"] + cur["aprovadas"] == 6)


# ============================================================================
print("\n=== 5. Alerta é o que dá para agir hoje ===")

textos = " | ".join(a["texto"] for a in F["alertas"])
check("certificado vencendo vira alerta", "Certificado vence em 10" in textos, textos)
check("curadoria parada há 40 dias vira alerta", "Curadoria parada há 40" in textos, textos)
check("inconsistência alta NÃO vira alerta",
      "inconsist" not in textos.lower(), textos)

db = SessionLocal()
F_BETA = fe.montar_ficha(db, BETA)
F_GAMA = fe.montar_ficha(db, GAMA)
F_DELTA = fe.montar_ficha(db, DELTA)
F_EPS = fe.montar_ficha(db, EPS)
db.close()

t_beta = " | ".join(a["texto"] for a in F_BETA["alertas"])
check("empresa com mês em aberto é cobrada pelo MÊS, não por dias",
      "competência(s) sem auditoria" in t_beta, t_beta)
check("e o alerta por dias não repete a mesma notícia",
      "Sem auditoria há" not in t_beta, t_beta)
check("empresa sem certificado é problema, não alerta",
      any(a["nivel"] == "problema" and "Certificado" in a["texto"] for a in F_BETA["alertas"]),
      t_beta)
check("cadastro incompleto aponta o que falta (UF)", "uf preenchida" in t_beta.lower(), t_beta)
check("o problema vem antes do alerta na fila", F_BETA["alertas"][0]["nivel"] == "problema",
      [a["nivel"] for a in F_BETA["alertas"]])

t_gama = " | ".join(a["texto"] for a in F_GAMA["alertas"])
check("empresa inativa é informada", "Empresa inativa" in t_gama, t_gama)
check("e NÃO é cobrada por nunca ter sido auditada", "Nunca auditada" not in t_gama, t_gama)

t_delta = " | ".join(a["texto"] for a in F_DELTA["alertas"])
check("execução que falhou é problema", "A última execução falhou" in t_delta, t_delta)
check("e o histórico do DELTA mostra a concluída anterior",
      F_DELTA["auditoria"]["historico"][1]["inconsistencias"] == 5,
      F_DELTA["auditoria"]["historico"])


# ============================================================================
print("\n=== 6. Competências: a régua e o que ela se RECUSA a cobrar ===")

cA = F["competencias"]
check("a ALFA teve uma competência conferida, não três",
      cA["auditadas"] == 1, cA["auditadas"])
check("porque as três execuções são do mesmo mês — reprocessar é rotina",
      cA["primeira"] == cA["ultima"] == comp.rotulo(FECHADA), cA)
check("a régua desenha 12 meses, inclusive os vazios",
      len(cA["regua"]) == 12, len(cA["regua"]))
check("e termina na última competência FECHADA (o mês corrente não é cobrado)",
      cA["regua"][-1]["comp"] == FECHADA, cA["regua"][-1])
check("a ALFA não tem mês em aberto", cA["em_aberto"] == [], cA["em_aberto"])
check("mês anterior à entrada da empresa não é buraco, é 'fora do período'",
      all(not m["esperada"] for m in cA["regua"][:-1])
      and cA["regua"][-1]["esperada"], [m["esperada"] for m in cA["regua"]])
check("a célula conferida traz o número da ÚLTIMA execução do mês (12, não 90)",
      cA["regua"][-1]["inconsistencias"] == 12, cA["regua"][-1])
check("e a faixa de cor sai da taxa", cA["regua"][-1]["nivel"] == "baixo",
      cA["regua"][-1]["nivel"])

cB = F_BETA["competencias"]
check("a BETA parou três meses atrás e tem 3 meses em aberto",
      len(cB["em_aberto"]) == 3, cB["em_aberto"])
check("são os meses fechados DEPOIS do último conferido",
      cB["em_aberto"] == [comp.rotulo(comp.anterior(comp.anterior(FECHADA))),
                          comp.rotulo(comp.anterior(FECHADA)),
                          comp.rotulo(FECHADA)], cB["em_aberto"])

check("execução sem competência não vira mês auditado",
      all(m["comp"] != "" for m in cA["regua"]) and cA["auditadas"] == 1)

# Empresa cujo histórico é todo anterior à coluna `competencia`: a régua não
# pode inventar atraso a partir de um banco onde o mês nunca foi gravado.
U_ZETA = uuid.uuid4()
ZETA = str(U_ZETA)
db = SessionLocal()
db.add(Tenant(id=U_ZETA, name="ZETA LEGADO", cnpj="66666666000166", uf="ES"))
db.add(tarefa(ZETA, ANA, "concluido", 60, notas=100, incons=3))
db.commit()
F_ZETA = fe.montar_ficha(db, ZETA)
db.close()
cZ = F_ZETA["competencias"]
check("histórico sem competência gravada não afirma nada",
      cZ["sem_registro"] and cZ["regua"] == [] and cZ["em_aberto"] == [], cZ)
check("e não gera alerta de mês em aberto",
      not any("competência" in a["texto"] for a in F_ZETA["alertas"]),
      [a["texto"] for a in F_ZETA["alertas"]])
check("mas o alerta por dias volta a valer aí, porque não há o que dizer melhor",
      any("Sem auditoria há" in a["texto"] for a in F_ZETA["alertas"]),
      [a["texto"] for a in F_ZETA["alertas"]])

check("a régua não cobra o mês corrente em empresa nenhuma",
      comp.fechada() < f"{datetime.utcnow().year}-{datetime.utcnow().month:02d}",
      comp.fechada())


# ============================================================================
print("\n=== 7. Fuso: o banco é UTC, a tela é Brasília ===")

q = F_EPS["auditoria"]["ultima"]["quando"]
check("23:30 UTC aparece como 20:30", q.hour == 20 and q.minute == 30, q)
check("e continua no dia 10, não pula para o 11", q.day == 10, q)


# ============================================================================
print("\n=== 8. Cadastro e acesso ===")

cad = F["cadastro"]
check("a ALFA tem os cinco itens de cadastro em ordem",
      cad["completos"] == 5, [i["rotulo"] for i in cad["itens"] if not i["ok"]])
uf_beta = next(i for i in F_BETA["cadastro"]["itens"] if i["rotulo"] == "UF preenchida")
check("a BETA é marcada sem UF", not uf_beta["ok"])
check("e o motivo explica o que trava lá na frente",
      "1933" in uf_beta["falta"] or "Domínio" in uf_beta["falta"], uf_beta["falta"])

check("a ALFA mostra quem tem acesso por perfil principal",
      any(p["nome"] == "Ana Fiscal" for p in F["equipe"]), F["equipe"])
check("a BETA mostra quem tem acesso por user_tenants",
      any(p["nome"] == "Ana Fiscal" and "operação" in p["vinculo"]
          for p in F_BETA["equipe"]), F_BETA["equipe"])
check("o admin sem empresa primária não é contado como equipe da ALFA",
      not any(p["nome"] == "Bruno Contador" for p in F["equipe"]), F["equipe"])
check("mas continua identificado no histórico da execução que ele rodou",
      any(h["quem"] == "Bruno Contador" for h in F["auditoria"]["historico"]),
      [h["quem"] for h in F["auditoria"]["historico"]])

check("empresa que não existe devolve None, não estoura",
      fe.montar_ficha(SessionLocal(), str(uuid.uuid4())) is None)
check("id que nem é UUID também devolve None",
      fe.montar_ficha(SessionLocal(), "nao-e-uuid") is None)
check("o CNPJ sai formatado para leitura humana",
      F["empresa"]["cnpj"] == "11.111.111/0001-11", F["empresa"]["cnpj"])


# ============================================================================
print("\n=== 9. Resumo da listagem: duas consultas para 170 empresas ===")

db = SessionLocal()
SQL_EMITIDO.clear()
R = fe.resumo_para_lista(db)
idas_lista = len(SQL_EMITIDO)
db.close()

check("o resumo da lista custa 2 consultas", idas_lista == 2, f"{idas_lista} idas")
check("e nenhuma delas abre o relatório",
      not any("result_data" in s for s in SQL_EMITIDO))
check("a coluna da lista traz a ÚLTIMA auditoria da ALFA",
      R[ALFA]["ultima"]["inconsistencias"] == 12, R[ALFA]["ultima"])
check("a pendência da ALFA vem com o total e a idade",
      R[ALFA]["pendencia"]["total"] == 3
      and R[ALFA]["pendencia"]["dias_parada"] == 40, R[ALFA]["pendencia"])
check("empresa nunca auditada não aparece com auditoria",
      GAMA not in R or R.get(GAMA, {}).get("ultima") is None, R.get(GAMA))
check("a execução com erro do DELTA não vira 'última auditoria'",
      R[DELTA]["ultima"]["inconsistencias"] == 5, R[DELTA]["ultima"])


# ============================================================================
print("\n=== 10. A tela desenha — inclusive a empresa vazia ===")

env = build_env()
tpl = env.get_template("partials/admin/tenant_ficha.html")

html = tpl.render(f=F)
check("a ficha renderiza", "ALFA COMERCIO" in html)
check("com o painel lateral, não tela cheia",
      "fixed top-0 right-0" in html and "w-full sm:w-[560px]" in html)
check("mostra a curadoria represada por tipo", "CFOP de entrada" in html)
check("mostra o certificado", "Vence em 10 dia(s)" in html)
check("mostra quem tem acesso", "Ana Fiscal" in html)
check("e o minigráfico de inconsistências", "por 100 notas" in html)
check("sem nenhum 'None' vazando para a tela", ">None<" not in html)

# O serviço JÁ converte para o horário de Brasília. Aplicar `| hora_br` no
# template subtrairia três horas uma segunda vez — a prova é o fonte.
import re as _re  # noqa: E402

FONTE_TPL = open("frontend/templates/partials/admin/tenant_ficha.html",
                 encoding="utf-8").read()
# Os comentários {# ... #} citam o filtro para explicar por que ele não entra;
# a busca é no que o Jinja realmente executa.
SEM_COMENTARIO = _re.sub(r"\{#.*?#\}", "", FONTE_TPL, flags=_re.S)
check("o template não converte o fuso de novo", "hora_br" not in SEM_COMENTARIO)
check("e a hora desenhada é a de Brasília",
      "20:30" in tpl.render(f=F_EPS), "hora do EPSILON")

html_gama = tpl.render(f=F_GAMA)
check("empresa sem nada renderiza igual", "GAMA TRANSPORTES" in html_gama)
check("e diz que nunca passou pelo motor", "nunca passou pelo motor" in html_gama)

html_nulo = tpl.render(f=None)
check("empresa inexistente vira aviso na tela, não erro",
      "Empresa não encontrada" in html_nulo)


# ============================================================================
print("\n=== 11. A rota: só admin, e sem 4xx que o htmx engole ===")

TOKENS["tok_admin"] = types.SimpleNamespace(id=BRUNO, email="bruno@synax.com")
TOKENS["tok_oper"] = types.SimpleNamespace(id=ANA, email="ana@synax.com")

from backend.app.api.dependencies.db import get_rls_db as _real_rls  # noqa: E402


def _fake_rls():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


m.app.dependency_overrides[_real_rls] = _fake_rls
c = TestClient(m.app, raise_server_exceptions=False)


def as_(tok):
    c.cookies.clear()
    c.cookies.set("access_token", f"Bearer {tok}")
    return c


r = as_("tok_admin").get(f"/admin/tenants/{ALFA}/ficha")
check("admin abre a ficha", r.status_code == 200, r.status_code)
check("e ela vem com o conteúdo da empresa", "ALFA COMERCIO" in r.text)

r = as_("tok_oper").get(f"/admin/tenants/{ALFA}/ficha")
check("operador não abre a ficha (router é admin)", r.status_code == 403, r.status_code)

r = as_("tok_admin").get(f"/admin/tenants/{uuid.uuid4()}/ficha")
check("empresa inexistente devolve 200, não 404", r.status_code == 200, r.status_code)
check("com aviso visível — em 4xx o htmx não trocaria a tela",
      "Empresa não encontrada" in r.text)

r = as_("tok_admin").get("/admin/tenants/nao-e-uuid/ficha")
check("id inválido também avisa em vez de estourar",
      r.status_code == 200 and "Empresa não encontrada" in r.text, r.status_code)

r = as_("tok_admin").get("/admin/tenants/list")
check("a lista de empresas abre", r.status_code == 200, r.status_code)
check("com a coluna da última auditoria", "Última auditoria" in r.text)
check("com a coluna de curadoria represada", "Curadoria" in r.text)
check("e o nome abre a ficha no painel lateral",
      f"/admin/tenants/{ALFA}/ficha" in r.text and "drawer-container" in r.text)
FONTE_LISTA = open("frontend/templates/partials/admin/tenant_list.html",
                   encoding="utf-8").read()
check("o estado vazio cobre as 7 colunas da tabela",
      'colspan="7"' in FONTE_LISTA,
      "a tabela ganhou colunas e o colspan do vazio ficou para trás")


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

engine.dispose()
for lixo in ("_ficha_test.db",):
    if os.path.exists(lixo):
        try:
            os.remove(lixo)
        except OSError:
            pass
try:
    for nome in os.listdir(PASTA_CERT):
        os.remove(os.path.join(PASTA_CERT, nome))
    os.rmdir(PASTA_CERT)
except OSError:
    pass

sys.exit(1 if FALHAS else 0)
