# -*- coding: utf-8 -*-
"""Idas ao banco no caminho quente — contadas, não estimadas.

O banco é o Supabase, na nuvem: cada ida custa ~140ms de REDE contra ~0.1ms de
execução (o número está no comentário de `_counts`, em main.py). O que pesa numa
tela lenta é o NÚMERO de viagens, não o tamanho das tabelas.

Dois desperdícios foram medidos e removidos em 01/09/2026:

1. `allowed_tenants` carregava preguiçosamente. A segunda consulta só disparava
   quando alguém tocava no atributo — em 14 pontos do sistema, sempre bem depois
   da primeira. Além de dobrar a espera de toda requisição que confere permissão,
   usava uma conexão que podia já ter caído: era o `server closed the connection
   unexpectedly` que aparecia no log justamente em
   `[str(t.id) for t in user_profile.allowed_tenants]`.

2. `BaseRepository.add()` fazia `refresh()` depois do `commit()` — uma segunda
   ida só para reler o que acabou de ser gravado, e nenhum chamador usa o objeto
   recarregado.

Este teste CONTA os comandos que chegam ao banco. Se alguém voltar ao
carregamento preguiçoso ou reintroduzir o refresh, o número sobe e o teste
quebra — que é a única forma de perceber, já que nada passa a estar "errado",
só mais lento.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_idas_banco_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

if os.path.exists("_idas_banco_test.db"):
    os.remove("_idas_banco_test.db")

from sqlalchemy import event, types as _satypes
import sqlalchemy.dialects.postgresql as pg


class _UUIDShim(_satypes.TypeDecorator):
    impl = _satypes.Uuid
    cache_ok = True

    def __init__(self, *a, **kw):
        kw.pop('as_uuid', None)
        super().__init__()

    def process_bind_param(self, value, dialect):
        return uuid.UUID(value) if isinstance(value, str) else value


pg.UUID = _UUIDShim

import backend.app.infrastructure.auth.supabase_client as sb
sb.get_supabase_client = lambda: type("C", (), {"auth": None})()
sb.get_supabase_admin_client = sb.get_supabase_client

from backend.app.infrastructure.database.session import SessionLocal, engine
from backend.app.domain.models.base import Base
from backend.app.domain.models import rules, auth_models, audit  # noqa: F401
from backend.app.domain.models.auth_models import UserProfile, Tenant
from backend.app.domain.models.rules import CfopCreditRule
from backend.app.infrastructure.repositories.rules_repository import RulesRepository

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


IDAS = []


@event.listens_for(engine, "before_cursor_execute")
def _contar(conn, cursor, statement, params, contexto, executemany):
    IDAS.append(statement.split()[0].upper())


# ============================================================================
print("\n=== 1. Ler o perfil e as empresas do operador: UMA ida ===")

UID = str(uuid.uuid4())
db = SessionLocal()
perfil = UserProfile(id=UID, name="Operador Teste", role="operador")
db.add(perfil)
for i in range(3):
    t = Tenant(id=str(uuid.uuid4()), name=f"EMPRESA {i}", cnpj=f"1122233300018{i}")
    db.add(t)
    perfil.allowed_tenants.append(t)
db.commit()
db.close()

db = SessionLocal()
IDAS.clear()
p = db.query(UserProfile).filter(UserProfile.id == UID).first()
idas_perfil = len(IDAS)
empresas = [str(t.id) for t in p.allowed_tenants]   # onde o lazy load disparava
idas_total = len(IDAS)
db.close()

check("o perfil vem em 1 consulta", idas_perfil == 1, f"{idas_perfil} idas")
check("as empresas vêm JUNTO, sem consulta extra",
      idas_total == 1, f"{idas_total} idas no total — o lazy load voltou?")
check("e as 3 empresas foram mesmo carregadas", len(empresas) == 3, empresas)

FONTE_MODEL = open(os.path.join("backend", "app", "domain", "models", "auth_models.py"),
                   encoding="utf-8").read()
check("a relação declara o carregamento junto", 'lazy="joined"' in FONTE_MODEL)


# ============================================================================
print("\n=== 2. Gravar uma regra: UMA ida ===")

TID = str(uuid.uuid4())
db = SessionLocal()
repo = RulesRepository(db, TID)
IDAS.clear()
repo.credit_rules.add(CfopCreditRule(
    cfop="9999", tenant_id=TID, allow_credit=True,
    description="teste", status="aprovado", is_global=False))
idas_add = len(IDAS)

check("gravar uma regra custa 1 ida", idas_add == 1,
      f"{idas_add} idas: {IDAS} — o refresh() voltou?")
check("e o que foi enviado é o INSERT", IDAS == ["INSERT"], IDAS)

# add_all continua sendo o caminho para volume: N regras, 1 ida.
IDAS.clear()
repo.credit_rules.add_all([
    CfopCreditRule(cfop=str(8000 + i), tenant_id=TID, allow_credit=True,
                   description="teste", status="aprovado", is_global=False)
    for i in range(10)])
idas_lote = len(IDAS)
check("10 regras em lote custam 1 ida", idas_lote == 1, f"{idas_lote} idas: {IDAS}")
check("o lote é MESMO melhor que 10 chamadas soltas", idas_lote < idas_add * 10)

db.close()

FONTE_REPO = open(os.path.join("backend", "app", "infrastructure", "repositories",
                               "base_repository.py"), encoding="utf-8").read()
bloco_add = FONTE_REPO[FONTE_REPO.find("def add(self"):]
bloco_add = bloco_add[:bloco_add.find("def add_all")]
check("o add() não faz refresh", "self.session.refresh" not in bloco_add, bloco_add)
check("mas continua injetando o tenant_id (a trava de isolamento)",
      "obj.tenant_id = self.tenant_id" in bloco_add)
check("e continua commitando", "self.session.commit()" in bloco_add)


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

engine.dispose()
if os.path.exists("_idas_banco_test.db"):
    try:
        os.remove("_idas_banco_test.db")
    except OSError:
        pass

sys.exit(1 if FALHAS else 0)
