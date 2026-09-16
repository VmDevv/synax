# -*- coding: utf-8 -*-
"""Aba de Configurações: credenciais do escritório na tela, não no servidor.

Até 10/09/2026 as credenciais do cofre fiscal SIEG só existiam no `.env` da
máquina onde o servidor roda. Trocar a chave virava tarefa de quem tem acesso ao
servidor — num VPS, um chamado. Agora elas ficam no banco, editáveis pelo admin.

O que esta suíte protege:

1. **Só admin.** O que se edita aqui é a chave que abre as notas de TODAS as
   empresas da carteira. Operador e cliente não entram — nem na tela, nem na
   rota que grava.

2. **A chave é gravada CIFRADA.** Quem abrir a tabela (backup, dump, um SELECT
   de suporte) não lê a credencial. O teste confere a coluna crua.

3. **A chave nunca volta para a tela.** Só a máscara. Devolvê-la num
   `<input value="...">` a exporia no HTML para qualquer um que abrisse o
   inspetor.

4. **Campo em branco MANTÉM a chave.** Salvar só o e-mail não pode apagar a
   credencial — o erro apareceria na próxima consulta ao SIEG, longe daqui.

5. **O banco manda, o `.env` é reserva.** Quem trocou pela tela espera que a
   troca valha; uma variável esquecida no servidor sobrescrevendo a tela seria
   um bug impossível de enxergar. Sem nada no banco, o `.env` segue valendo.
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

os.environ["DATABASE_URL"] = "sqlite:///./_config_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
os.environ.pop("SIEG_API_KEY", None)
os.environ.pop("SIEG_EMAIL", None)
if os.path.exists("_config_test.db"):
    os.remove("_config_test.db")

from sqlalchemy import types as _satypes
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
from backend.app.domain.models.configuracao import AppSetting  # noqa: E402
from backend.app.services import configuracoes as cfg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

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


Base.metadata.create_all(bind=_eng)
db = SessionLocal()
tA = uuid.uuid4()
db.add(Tenant(id=tA, name="Empresa A", cnpj="11111111111111"))
u_adm, u_ope, u_cli = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
db.add_all([
    UserProfile(id=u_adm, tenant_id=tA, role="admin", name="Admin"),
    UserProfile(id=u_ope, tenant_id=tA, role="operador", name="Operador"),
    UserProfile(id=u_cli, tenant_id=tA, role="cliente", name="Cliente"),
])
db.add(UserTenant(user_id=u_ope, tenant_id=tA))
db.commit()
db.close()

TOKENS["adm"] = types.SimpleNamespace(id=str(u_adm), email="admin@synax.com")
TOKENS["ope"] = types.SimpleNamespace(id=str(u_ope), email="oper@synax.com")
TOKENS["cli"] = types.SimpleNamespace(id=str(u_cli), email="cli@synax.com")

from backend.app.api.dependencies.db import get_rls_db as _real_rls  # noqa: E402


def _fake_rls():
    ss = SessionLocal()
    try:
        yield ss
    finally:
        ss.close()


m.app.dependency_overrides[_real_rls] = _fake_rls
c = TestClient(m.app, raise_server_exceptions=False)


def as_(token):
    c.cookies.clear()
    if token:
        c.cookies.set("access_token", f"Bearer {token}")
    return c


CHAVE = "sieg-chave-secreta-abcdef123456"


# ============================================================================
print("\n=== 1. Quem entra na aba ===")

r = as_("adm").get("/admin/configuracoes")
check("admin abre a aba", r.status_code == 200, r.status_code)
check("com o bloco do cofre SIEG", "Cofre fiscal SIEG" in r.text)
check("operador nao entra",
      as_("ope").get("/admin/configuracoes").status_code == 403)
check("cliente tambem nao",
      as_("cli").get("/admin/configuracoes").status_code == 403)
check("anonimo barrado",
      as_(None).get("/admin/configuracoes").status_code in (401, 403))
check("e nem grava pela rota direta",
      as_("ope").post("/admin/configuracoes/sieg",
                      data={"sieg_email": "x@y.com",
                            "sieg_api_key": "invadindo"}).status_code == 403)


# ============================================================================
print("\n=== 2. Gravacao: cifrada, com trilha ===")

r = as_("adm").post("/admin/configuracoes/sieg",
                    data={"sieg_email": "cofre@vmcontadores.com.br",
                          "sieg_api_key": CHAVE})
check("salvar responde 200", r.status_code == 200, r.status_code)
check("e diz o que foi salvo", "Salvo:" in r.text and "chave de API" in r.text)

ss = SessionLocal()
linha = ss.query(AppSetting).filter_by(chave=cfg.SIEG_API_KEY).first()
check("a chave foi gravada", linha is not None)
check("CIFRADA — a coluna crua nao tem a chave",
      linha and CHAVE not in (linha.valor or ""), (linha.valor or "")[:20])
check("e marcada como segredo", linha and linha.segredo is True)
check("com quem alterou", linha and linha.atualizado_por == "Admin",
      linha and linha.atualizado_por)

email_linha = ss.query(AppSetting).filter_by(chave=cfg.SIEG_EMAIL).first()
check("o e-mail NAO e cifrado (nao e credencial)",
      email_linha and email_linha.valor == "cofre@vmcontadores.com.br"
      and email_linha.segredo is False, email_linha and email_linha.valor)
check("e o servico devolve a chave decifrada",
      cfg.ler(ss, cfg.SIEG_API_KEY) == CHAVE)
ss.close()


# ============================================================================
print("\n=== 3. A chave nunca volta para a tela ===")

r = as_("adm").get("/admin/configuracoes")
check("a tela NAO traz a chave em lugar nenhum", CHAVE not in r.text)
check("mostra so a mascara, com os 4 ultimos", "123456"[-4:] in r.text
      and "•" in r.text)
check("e o campo de chave e do tipo password",
      'name="sieg_api_key"' in r.text and 'type="password"' in r.text)
check("com placeholder explicando o branco",
      "manter a atual" in r.text)
check("o e-mail, sim, volta preenchido (nao e segredo)",
      'value="cofre@vmcontadores.com.br"' in r.text)

check("a mascara esconde tudo menos os 4 ultimos",
      cfg.mascarar("abcdefghij") == "••••••••ghij", cfg.mascarar("abcdefghij"))
check("e chave curta nao vaza nada", cfg.mascarar("ab") == "••")
check("sem chave, sem mascara", cfg.mascarar("") == "")


# ============================================================================
print("\n=== 4. Campo em branco MANTEM a chave ===")

r = as_("adm").post("/admin/configuracoes/sieg",
                    data={"sieg_email": "outro@vmcontadores.com.br",
                          "sieg_api_key": ""})
ss = SessionLocal()
check("trocar so o e-mail nao apaga a credencial",
      cfg.ler(ss, cfg.SIEG_API_KEY) == CHAVE)
check("e o e-mail novo valeu",
      cfg.ler(ss, cfg.SIEG_EMAIL) == "outro@vmcontadores.com.br")
ss.close()
check("a resposta diz que so o e-mail mudou",
      "e-mail" in r.text and "chave de API" not in r.text.split("Salvo:")[1][:60],
      r.text.split("Salvo:")[1][:60] if "Salvo:" in r.text else r.text[:80])

r = as_("adm").post("/admin/configuracoes/sieg",
                    data={"sieg_email": "outro@vmcontadores.com.br",
                          "sieg_api_key": ""})
check("salvar sem mudar nada NAO devolve caixa verde de sucesso",
      "Nada foi alterado" in r.text)


# ============================================================================
print("\n=== 5. Banco manda; .env e reserva ===")

ss = SessionLocal()
os.environ["SIEG_API_KEY"] = "chave-do-env"
os.environ["SIEG_EMAIL"] = "env@x.com"
email, chave = cfg.credenciais_sieg(ss)
check("com valor no banco, o .env NAO sobrescreve",
      chave == CHAVE and email == "outro@vmcontadores.com.br", (email, chave))

# Sem nada no banco, o .env continua valendo — instalacao antiga segue de pe.
ss.query(AppSetting).delete()
ss.commit()
email, chave = cfg.credenciais_sieg(ss)
check("sem valor no banco, o .env vale",
      chave == "chave-do-env" and email == "env@x.com", (email, chave))
check("e o sistema se declara configurado", cfg.sieg_configurado(ss) is True)

os.environ.pop("SIEG_API_KEY")
os.environ.pop("SIEG_EMAIL")
check("sem banco e sem .env, nao esta configurado",
      cfg.sieg_configurado(ss) is False)
ss.close()

r = as_("adm").get("/admin/configuracoes")
check("e a tela avisa que a busca no cofre nao funciona",
      "Nenhuma chave configurada" in r.text)


# ============================================================================
print("\n=== 6. O cliente do SIEG usa o que foi configurado ===")

ss = SessionLocal()
cfg.gravar(ss, cfg.SIEG_EMAIL, "cofre@vmcontadores.com.br", "Admin")
cfg.gravar(ss, cfg.SIEG_API_KEY, CHAVE, "Admin")
cliente = cfg.cliente_sieg(ss)
check("o SiegClient nasce com a chave da tela", cliente.api_key == CHAVE,
      cliente.api_key)
check("e com o e-mail da tela", cliente.email == "cofre@vmcontadores.com.br")

ss.query(AppSetting).delete()
ss.commit()
try:
    cfg.cliente_sieg(ss)
    falhou_certo = False
except RuntimeError:
    # Fail-closed: sem credencial o cliente do cofre não sobe meio funcionando.
    falhou_certo = True
check("sem credencial nenhuma, o cliente RECUSA subir", falhou_certo)
ss.close()

FONTE = io.open(os.path.join("backend", "app", "api", "routes", "sieg.py"),
                encoding="utf-8").read()
check("nenhuma rota cria SiegClient() sem credencial da tela",
      "SiegClient()" not in FONTE)


# ============================================================================
print("\n=== 7. A tabela precisa NASCER no banco ===")

# Em 10/09/2026 a aba subiu e a tabela nao existia no Postgres: `create_all` so
# cria as tabelas dos modelos JA IMPORTADOS, e `app_settings` so era alcancada
# via routes/sieg.py — importado DEPOIS do create_all em main.py. A tela abria
# 200 e cada leitura de configuracao estourava no log.
#
# Este teste vale para qualquer modelo futuro: percorre os arquivos de
# domain/models, junta os `__tablename__` declarados e confere que todos estao
# no metadata depois de importar a aplicacao.
import re as _re  # noqa: E402

PASTA_MODELS = os.path.join("backend", "app", "domain", "models")
declaradas = {}
for _arq in sorted(os.listdir(PASTA_MODELS)):
    if not _arq.endswith(".py") or _arq in ("__init__.py", "base.py"):
        continue
    _fonte = io.open(os.path.join(PASTA_MODELS, _arq), encoding="utf-8").read()
    for _nome in _re.findall(r"__tablename__\s*=\s*['\"]([^'\"]+)['\"]", _fonte):
        declaradas[_nome] = _arq

registradas = set(Base.metadata.tables)
faltando = {t: a for t, a in declaradas.items() if t not in registradas}
check("todo modelo declarado esta registrado antes do create_all",
      not faltando, faltando)
check("(e a busca achou modelos de verdade)", len(declaradas) >= 8,
      len(declaradas))
check("app_settings entre elas", "app_settings" in registradas)

MAIN = io.open(os.path.join("backend", "app", "main.py"), encoding="utf-8").read()
_lista = MAIN.split("from .domain.models import")[1].split(")")[0]
check("main.py importa o modulo de configuracao antes do create_all",
      "configuracao" in _lista
      # A CHAMADA, nao a palavra: o comentario acima do import tambem cita
      # `create_all`, e casar com ele mediria a ordem errada.
      and (MAIN.index("from .domain.models import")
           < MAIN.index("Base.metadata.create_all(")),
      _lista.strip())

# O rollback: erro engolido sem desfazer a transacao deixa a sessao inutil no
# Postgres ("current transaction is aborted") e derruba a requisicao inteira,
# nao so a leitura da configuracao.
class _SessaoQuebrada:
    def __init__(self):
        self.rollbacks = 0

    def query(self, *a, **k):
        raise RuntimeError("relation does not exist")

    def rollback(self):
        self.rollbacks += 1


_quebrada = _SessaoQuebrada()
check("leitura que falha devolve o padrao, sem levantar",
      cfg.ler(_quebrada, cfg.SIEG_EMAIL, "reserva") == "reserva")
check("e faz ROLLBACK, para a sessao continuar utilizavel",
      _quebrada.rollbacks == 1, _quebrada.rollbacks)


# ============================================================================
_eng.dispose()
if os.path.exists("_config_test.db"):
    try:
        os.remove("_config_test.db")
    except OSError:
        pass

print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
for f in _falhou:
    print("  -", f)
sys.exit(1 if FALHAS else 0)
