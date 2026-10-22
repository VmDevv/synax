# -*- coding: utf-8 -*-
"""Lucro Presumido dentro do Synax: a página que chama o programa SPED PIS/COFINS.

Pedido do Sandro em 22/10/2026 — a primeira página do programa trazida para o Synax, com o motor ficando LÁ: o Synax
chama a linha de comando e lê o resultado em JSON. Esta suíte NUNCA roda o programa de verdade (nem o `py`): as
funções de `programa` são trocadas por respostas fixas, e o lote roda um processo Python falso que imprime as mesmas
linhas que o programa imprime. O que ela trava:

1. **Quem vê qual empresa:** a lista do Domínio cruzada com os tenants pelo CNPJ; o admin vê todas (as sem cadastro
   marcadas), o operador só as dele, o cliente não entra.
2. **Gerar confere o acesso de novo no servidor** — POST montado com empresa de outro não chama o programa.
3. **Download** com os cabeçalhos de arquivo (nunca renderizado) e com acesso conferido; arquivo reprovado sai com
   o nome dizendo "não transmitir".
4. **Salvar na pasta** recusa arquivo com erro de validação, e a pasta é a do programa, nunca a do formulário.
5. **Programa fora / Domínio fora** viram frase na tela com 200, nunca 500.
6. **O contrato:** versão diferente, saída que não é JSON e `{"erro"}` viram `FalhaDoPrograma`.
7. **Lote:** um por vez (daqui e do programa), andamento lido das linhas de progresso, fim com a mensagem do
   programa; operador não manda empresa alheia nem vê lote alheio.
8. **Histórico:** o operador vê só as linhas das empresas dele e não baixa planilha/zip (têm todas as empresas).
9. **Menu:** o item novo em Ferramentas SPED para admin e operador.
"""
import json
import os
import shutil
import sys
import tempfile
import types
import uuid
from pathlib import Path

sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_lucro_presumido_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
if os.path.exists("_lucro_presumido_test.db"):
    os.remove("_lucro_presumido_test.db")

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
from backend.app.domain.models.base import Base  # noqa: E402
from backend.app.domain.models.auth_models import Tenant, UserProfile, UserTenant  # noqa: E402
from backend.app.api.routes import lucro_presumido as rota  # noqa: E402
from backend.app.services.efd_contribuicoes import lote, programa  # noqa: E402
from backend.app.services.efd_contribuicoes.programa import FalhaDoPrograma, ProgramaIndisponivel  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CNPJ_A, CNPJ_B, CNPJ_SEM = "11222333000181", "44555666000177", "99888777000166"

Base.metadata.create_all(bind=_eng)
db = SessionLocal()
tA, tB = uuid.uuid4(), uuid.uuid4()
# O CNPJ do cadastro vem formatado em parte da base: o cruzamento é pelos dígitos.
db.add_all([Tenant(id=tA, name="CLIENTE A", cnpj="11.222.333/0001-81", uf="ES"),
            Tenant(id=tB, name="CLIENTE B", cnpj=CNPJ_B, uf="ES")])
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


# ============================================================================
# O programa, trocado por respostas fixas.
# ============================================================================
EMPRESAS = [
    {"codi_emp": 10, "nome": "CLIENTE A LTDA", "cnpj": CNPJ_A, "presumido": True},
    {"codi_emp": 20, "nome": "CLIENTE B LTDA", "cnpj": CNPJ_B, "presumido": True},
    {"codi_emp": 30, "nome": "SO NO DOMINIO LTDA", "cnpj": CNPJ_SEM, "presumido": True},
    {"codi_emp": 40, "nome": "CLIENTE A REAL", "cnpj": CNPJ_A, "presumido": False},
]
PASTA_PADRAO = Path(tempfile.mkdtemp(prefix="lp-pasta-"))
CHAMADAS = []
FALHAR = {"empresas": None, "gerar": None}
REPROVAR = set()


def _empresas(atualizar=False):
    CHAMADAS.append(("empresas", atualizar))
    if FALHAR["empresas"]:
        raise FALHAR["empresas"]
    return EMPRESAS


def _gerar(codi, competencia, destino):
    CHAMADAS.append(("gerar", codi, competencia))
    if FALHAR["gerar"]:
        raise FALHAR["gerar"]
    Path(destino).write_bytes(b"|0000|006|0|||01092026|30092026|CLIENTE A LTDA|11222333000181|ES|\r\n"
                              b"|0001|0|\r\n|9999|3|\r\n")
    reprovado = codi in REPROVAR
    return {"contrato": 1, "codi_emp": codi, "empresa": "CLIENTE A LTDA", "cnpj": CNPJ_A, "competencia": competencia,
            "nome_arquivo": f"EFD_CONTRIB_{CNPJ_A}_{competencia.replace('-', '')[4:]}{competencia[:4]}.txt",
            "arquivo": str(destino), "linhas": 3, "situacao": "erro na validação" if reprovado else "confere",
            "diferenca": 0.0, "sem_movimento": False, "apuracao_existe": True, "pendencias": [],
            "avisos": ["Retenção das notas do mês 10,00 × controle do Domínio 9,00."], "correcoes": [],
            "erros_validacao": 1 if reprovado else 0,
            "alertas": [{"gravidade": "erro", "codigo": "A03", "titulo": "Bloco 9 não fecha", "detalhe": "teste"}]
            if reprovado else [],
            "resumo": [{"tributo": "PIS", "item": "Valor a recolher", "arquivo": 897.0, "dominio": 897.0, "diferenca": 0.0},
                       {"tributo": "COFINS", "item": "Valor a recolher", "arquivo": 4140.0, "dominio": 4140.0,
                        "diferenca": 0.0}],
            "pasta_padrao": str(PASTA_PADRAO)}


HISTORICO = []


def _historico(limite=20):
    return list(HISTORICO)


def _execucao(ident):
    return next((x for x in HISTORICO if x["id"] == ident), None)


programa.empresas = _empresas
programa.gerar = _gerar
programa.historico = _historico
programa.execucao = _execucao

COMPETENCIA = "2026-09"


def gerar(token, codi, competencia=COMPETENCIA):
    return as_(token).post("/api/lucro-presumido/gerar", data={"codi_emp": str(codi), "competencia": competencia})


def processo_de(html):
    i = html.find("/api/lucro-presumido/")
    trecho = html[i + len("/api/lucro-presumido/"):]
    return trecho.split("/")[0]


# ============================================================================
print("\n=== 1. Quem ve qual empresa ===")
r = as_("adm").get("/lucro-presumido")
check("a pagina abre para o admin", r.status_code == 200 and "Lucro Presumido" in r.text, r.status_code)
check("a lista vem depois da pagina (hx-trigger load)", "/api/lucro-presumido/empresas?para=uma" in r.text)
r = as_("cli").get("/lucro-presumido")
check("cliente nao entra", r.status_code == 403, r.status_code)
check("trocar empresa ou mes limpa o resultado (o Baixar dele seria da empresa errada)",
      "#lp-competencia, #lp-form-uma select" in as_("adm").get("/lucro-presumido").text)

# Vindo da página Domínio: empresa e mês já escolhidos.
r = as_("ope").get("/lucro-presumido?codi_emp=10&competencia=2026-08")
check("a URL pre-escolhe a competencia e pede o formulario com a empresa",
      'value="2026-08"' in r.text and "selecionada=10" in r.text, r.text[:200])
r = as_("ope").get("/lucro-presumido?codi_emp=10&competencia=lixo")
check("competencia invalida na URL cai no mes fechado", 'value="2026-13"' not in r.text and "lixo" not in r.text)
r = as_("ope").get("/api/lucro-presumido/empresas?para=uma&selecionada=10")
check("a empresa pre-escolhida vem marcada", '<option value="10" selected>' in r.text, r.text[:400])
r = as_("ope").get("/api/lucro-presumido/empresas?para=uma&selecionada=40")
check("pre-escolhida que nao e presumida abre a lista inteira, marcada",
      '<option value="40" selected>' in r.text and "checked" in r.text, r.text[:400])
r = as_("ope").get("/api/lucro-presumido/empresas?para=uma&selecionada=20")
check("pre-escolher empresa alheia nao a poe na lista", 'value="20"' not in r.text and " selected>" not in r.text.replace(
      '<option value="" disabled selected>', ''), r.text[:400])

r = as_("adm").get("/api/lucro-presumido/empresas?para=uma")
check("admin ve as presumidas de todos, inclusive a sem cadastro",
      all(f'value="{c}"' in r.text for c in (10, 20, 30)) and 'value="40"' not in r.text, r.text[:300])
check("a sem cadastro no Synax vem marcada", "sem cadastro no Synax" in r.text)
r = as_("ope").get("/api/lucro-presumido/empresas?para=uma")
check("operador ve so a dele (cruzada pelo CNPJ, com mascara no cadastro)",
      'value="10"' in r.text and 'value="20"' not in r.text and 'value="30"' not in r.text, r.text[:300])
check("operador nao ve a marca 'sem cadastro'", "sem cadastro no Synax" not in r.text)
r = as_("ope").get("/api/lucro-presumido/empresas?para=uma&todas=1")
check("'mostrar todas' traz a nao presumida dele", 'value="40"' in r.text)
r = as_("ope").get("/api/lucro-presumido/empresas?para=lote")
check("o formulario do lote tambem sai filtrado", 'value="10"' in r.text and 'value="20"' not in r.text)
CHAMADAS.clear()
as_("adm").get("/api/lucro-presumido/empresas?para=uma&atualizar=1")
check("'Atualizar a lista' pede a lista de novo ao programa", ("empresas", True) in CHAMADAS, CHAMADAS)

# ============================================================================
print("\n=== 2. Gerar confere o acesso no servidor ===")
CHAMADAS.clear()
r = gerar("ope", 10)
check("operador gera a dele: 200 com o resultado", r.status_code == 200 and "Confere com o Domínio" in r.text,
      r.text[:300])
check("mostra PIS e COFINS do arquivo x Dominio", "897,00" in r.text and "4.140,00" in r.text)
check("e os botoes de baixar e salvar", "Baixar EFD-Contribuições" in r.text and "Salvar na pasta" in r.text)
PROC_OK = processo_de(r.text)
CHAMADAS.clear()
r = gerar("ope", 20)
check("POST montado com empresa de outro: frase, sem chamar o programa",
      r.status_code == 200 and "fora da sua lista" in r.text and not any(x[0] == "gerar" for x in CHAMADAS),
      (r.text[:200], CHAMADAS))
r = gerar("ope", "abc")
check("codigo que nao e numero vira frase", r.status_code == 200 and "Escolha a empresa" in r.text)
r = gerar("ope", 10, competencia="2026-13")
check("competencia invalida vira frase", r.status_code == 200 and "competência" in r.text)
r = gerar("adm", 30)
check("admin gera empresa sem cadastro no Synax", r.status_code == 200 and "Confere com o Domínio" in r.text)
r = gerar("cli", 10)
check("cliente nao gera", r.status_code == 403, r.status_code)

# ============================================================================
print("\n=== 3. Download ===")
r = as_("ope").get(f"/api/lucro-presumido/{PROC_OK}/arquivo")
cd = r.headers.get("content-disposition", "")
check("baixa o arquivo", r.status_code == 200 and r.content.startswith(b"|0000|"), r.status_code)
check("como arquivo, nunca como pagina",
      r.headers.get("content-type", "").startswith("application/octet-stream") and "attachment" in cd
      and r.headers.get("x-content-type-options") == "nosniff", dict(r.headers))
check("o nome e o do arquivo gerado", "EFD_CONTRIB_11222333000181" in cd, cd)
r = as_("out").get(f"/api/lucro-presumido/{PROC_OK}/arquivo")
check("operador de outra empresa nao baixa (404, sem revelar)", r.status_code == 404, r.status_code)
r = as_("ope").get("/api/lucro-presumido/..%5C..%5Cmain.py/arquivo")
check("identificador que nao e UUID: 404", r.status_code == 404, r.status_code)
REPROVAR.add(10)
r = gerar("ope", 10)
PROC_ERRO = processo_de(r.text)
check("reprovado: aviso de nao transmitir e so 'baixar para analise'",
      "não transmita" in r.text and "Baixar para análise" in r.text and "Salvar na pasta" not in r.text, r.text[:200])
r = as_("ope").get(f"/api/lucro-presumido/{PROC_ERRO}/arquivo")
check("o nome do reprovado diz NAO_TRANSMITIR",
      "COM_ERRO_NAO_TRANSMITIR" in r.headers.get("content-disposition", ""), r.headers.get("content-disposition"))
REPROVAR.clear()

# ============================================================================
print("\n=== 4. Salvar na pasta ===")
r = as_("ope").post(f"/api/lucro-presumido/{PROC_ERRO}/salvar")
check("arquivo reprovado nao vai para a pasta dos prontos", r.status_code == 200 and "não vai para a pasta" in r.text
      and not any(PASTA_PADRAO.rglob("*.txt")), r.text[:200])
r = as_("ope").post(f"/api/lucro-presumido/{PROC_OK}/salvar", data={"pasta": "C:\\Windows"})
salvo = PASTA_PADRAO / COMPETENCIA / f"EFD_CONTRIB_{CNPJ_A}_092026.txt"
check("salva na pasta do programa, no mes", salvo.exists() and "Salvo em" in r.text, (r.text[:200], list(PASTA_PADRAO.rglob('*'))))
check("a pasta do formulario e ignorada", not Path("C:\\Windows", COMPETENCIA).exists())
r = as_("ope").post(f"/api/lucro-presumido/{PROC_OK}/salvar")
check("salvar de novo avisa que substituiu", "substituiu" in r.text, r.text[:200])
r = as_("out").post(f"/api/lucro-presumido/{PROC_OK}/salvar")
check("operador de outra empresa nao salva", r.status_code == 200 and "Salvo" not in r.text, r.text[:200])

# ============================================================================
print("\n=== 5. Programa fora e Dominio fora viram frase ===")
FALHAR["empresas"] = ProgramaIndisponivel("O programa SPED PIS/COFINS não foi encontrado em X.")
r = as_("adm").get("/api/lucro-presumido/empresas?para=uma")
check("lista sem o programa: 200 com o motivo", r.status_code == 200 and "não foi encontrado" in r.text, r.status_code)
r = gerar("adm", 10)
check("gerar sem o programa: 200 com o motivo", r.status_code == 200 and "não foi encontrado" in r.text, r.status_code)
FALHAR["empresas"] = None
FALHAR["gerar"] = FalhaDoPrograma("Error: ('08001', 'Domínio fora do ar')")
antes = set(os.listdir(rota.TEMP_DIR)) if os.path.isdir(rota.TEMP_DIR) else set()
r = gerar("adm", 10)
depois = set(os.listdir(rota.TEMP_DIR)) if os.path.isdir(rota.TEMP_DIR) else set()
check("Dominio fora: 200 com a frase do programa", r.status_code == 200 and "Domínio fora do ar" in r.text, r.text[:200])
check("e a pasta da geracao que falhou nao fica para tras", depois == antes, depois - antes)
FALHAR["gerar"] = None

# ============================================================================
print("\n=== 6. O contrato ===")
try:
    programa.ler_resposta('{"contrato": 2, "empresas": []}')
    check("contrato de outra versao e recusado", False)
except FalhaDoPrograma as exc:
    check("contrato de outra versao e recusado", "atualizados juntos" in str(exc), str(exc))
try:
    programa.ler_resposta("Traceback (most recent call last):\n  ...", "boom", 1)
    check("saida que nao e JSON vira falha", False)
except FalhaDoPrograma as exc:
    check("saida que nao e JSON vira falha", "formato combinado" in str(exc), str(exc))
try:
    programa.ler_resposta('  [1/2] x\n{"contrato": 1, "erro": "ValueError: empresa 9 não encontrada"}')
    check("{erro} vira a frase do programa", False)
except FalhaDoPrograma as exc:
    check("{erro} vira a frase do programa", str(exc) == "ValueError: empresa 9 não encontrada", str(exc))
check("a ultima linha e a que vale", programa.ler_resposta('  [1/1] a\n{"contrato": 1, "ok": true}\n')["ok"] is True)
args = programa.args_do_lote("2026-09", [10, 30], "Synax · adm@synax.com")
check("o lote vai como lista de argumentos, competencia MM/AAAA",
      args == ["lote", "--competencia", "09/2026", "--empresas", "10", "30", "--origem", "Synax · adm@synax.com",
               "--json"], args)
_pasta_original = os.environ.pop("SPEDPC_PASTA", None)
os.environ["SPEDPC_PASTA"] = tempfile.mkdtemp(prefix="lp-sem-programa-")
try:
    programa.comando(["info"])
    check("pasta sem o programa: ProgramaIndisponivel", False)
except ProgramaIndisponivel as exc:
    check("pasta sem o programa: ProgramaIndisponivel", "não foi encontrado" in str(exc), str(exc))
os.environ.pop("SPEDPC_PASTA")
if _pasta_original:
    os.environ["SPEDPC_PASTA"] = _pasta_original

# ============================================================================
print("\n=== 7. Lote ===")
EXECUCAO = {"id": "ex-1", "tipo": "presumido_lote", "origem": "Synax · adm@synax.com", "titulo": "Lote do Presumido 09/2026",
            "inicio": "2026-10-22T10:00:00", "fim": "2026-10-22T10:01:00", "status": "com_avisos",
            "mensagem": "09/2026: 1 confere · 1 diferente · 0 com erro",
            "resumo": {}, "linhas": [
                {"codigo": 10, "empresa": "CLIENTE A LTDA", "cnpj": CNPJ_A, "situacao": "confere", "pis_arquivo": 897.0,
                 "pis_dominio": 897.0, "cofins_arquivo": 4140.0, "cofins_dominio": 4140.0, "avisos": ""},
                {"codigo": 30, "empresa": "SO NO DOMINIO LTDA", "cnpj": CNPJ_SEM, "situacao": "diferente",
                 "pis_arquivo": 10.0, "pis_dominio": float("nan"), "cofins_arquivo": 46.0, "cofins_dominio": None,
                 "avisos": "PIS a recolher 10,00 no arquivo × 0,00 no Domínio"}],
            "arquivos": []}
PEDIDOS_LOTE = []
SCRIPT = (
    "import json, sys, time\n"
    "print('  [1/2] 10 — CLIENTE A LTDA', flush=True)\n"
    "time.sleep(float(sys.argv[1]))\n"
    "print('  [2/2] 30 — SO NO DOMINIO LTDA', flush=True)\n"
    "print(json.dumps({'contrato': 1, 'execucao': json.loads(sys.argv[2])}), flush=True)\n")


def _comando(args, espera="1.0"):
    PEDIDOS_LOTE.append(args)
    return [sys.executable, "-c", SCRIPT, espera, json.dumps(EXECUCAO)], Path(tempfile.gettempdir()), dict(os.environ)


programa.comando = _comando
r = as_("ope").post("/api/lucro-presumido/lote", data={"competencia": COMPETENCIA, "quais": "escolhidas",
                                                        "codigos": ["10", "20"]})
check("operador nao manda empresa alheia no lote", "fora da sua lista" in r.text and not PEDIDOS_LOTE, r.text[:200])
r = as_("ope").post("/api/lucro-presumido/lote", data={"competencia": COMPETENCIA, "quais": "escolhidas"})
check("lote sem empresa nao comeca", "Nenhuma empresa" in r.text and not PEDIDOS_LOTE, r.text[:200])

HISTORICO[:] = [dict(EXECUCAO, id="ex-0", status="rodando", origem="tela",
                     inicio=__import__("datetime").datetime.now().isoformat(timespec="seconds"))]
r = as_("adm").post("/api/lucro-presumido/lote", data={"competencia": COMPETENCIA, "quais": "todas"})
check("lote rodando no programa (tela de la ou automacao) recusa o pedido",
      "rodando outro lote" in r.text and not PEDIDOS_LOTE, r.text[:300])
HISTORICO[:] = [dict(EXECUCAO, id="ex-velho", status="rodando", origem="tela", inicio="2026-01-01T08:00:00")]
r = as_("adm").post("/api/lucro-presumido/lote", data={"competencia": COMPETENCIA, "quais": "todas"})
check("'rodando' muito antigo (servidor caiu) nao trava", PEDIDOS_LOTE and "Lote do Presumido 09/2026" in r.text,
      r.text[:300])
check("'todas' manda as presumidas da lista do admin",
      PEDIDOS_LOTE and PEDIDOS_LOTE[0][4:7] == ["10", "20", "30"], PEDIDOS_LOTE[:1])
check("e a origem leva quem pediu", PEDIDOS_LOTE and "Synax · adm@synax.com" in PEDIDOS_LOTE[0], PEDIDOS_LOTE[:1])
check("enquanto roda, a tela se pergunta de novo a cada 2 s", 'hx-trigger="every 2s"' in r.text)
r = as_("adm").post("/api/lucro-presumido/lote", data={"competencia": COMPETENCIA, "quais": "todas"})
check("segundo lote enquanto o primeiro roda e recusado", "Já há um lote rodando" in r.text, r.text[:300])
r = as_("ope").get("/api/lucro-presumido/lote/andamento")
check("operador nao ve o andamento do lote do admin (tem empresa alheia)",
      r.status_code == 200 and "Lote do Presumido" not in r.text, r.text[:200])
lote.esperar(15)
r = as_("adm").get("/api/lucro-presumido/lote/andamento")
check("terminou: mostra a mensagem do programa", "1 confere · 1 diferente" in r.text, r.text[:400])
check("e para de se perguntar", 'every 2s' not in r.text)
check("as empresas do lote aparecem na tabela", "SO NO DOMINIO LTDA" in r.text and "4.140,00" in r.text)
check("valor do Dominio vazio ou NaN vira travessao", ">nan<" not in r.text.lower() and ">—<" in r.text, "nan")
check("o andamento leu as linhas de progresso", lote.atual().total == 2 and lote.atual().feitos == 2,
      (lote.atual().total, lote.atual().feitos))

# ============================================================================
print("\n=== 8. Historico ===")
pasta_lote = Path(tempfile.mkdtemp(prefix="lp-lote-"))
planilha = pasta_lote / "RESUMO_PRESUMIDO_202609.xlsx"
planilha.write_bytes(b"PK planilha")
script = pasta_lote / "malicioso.py"
script.write_text("print(1)")
HISTORICO[:] = [dict(EXECUCAO, arquivos=[str(planilha), str(script), str(pasta_lote / "sumiu.zip")])]
r = as_("adm").get("/api/lucro-presumido/historico")
check("admin ve o lote, as empresas e a planilha para baixar",
      "Lote do Presumido 09/2026" in r.text and "SO NO DOMINIO LTDA" in r.text
      and "/api/lucro-presumido/execucao/ex-1/arquivo/0" in r.text, r.text[:300])
r = as_("ope").get("/api/lucro-presumido/historico")
check("operador ve so as linhas das empresas dele", "CLIENTE A LTDA" in r.text and "SO NO DOMINIO" not in r.text,
      r.text[:300])
check("e nao tem link para a planilha (ela tem todas as empresas)", "/arquivo/0" not in r.text)
r = as_("out").get("/api/lucro-presumido/historico")
check("operador sem empresa no lote nao ve o lote", "Lote do Presumido" not in r.text and "Nenhum lote" in r.text,
      r.text[:300])
r = as_("adm").get("/api/lucro-presumido/execucao/ex-1/arquivo/0")
check("admin baixa a planilha, como arquivo", r.status_code == 200 and r.content == b"PK planilha"
      and "attachment" in r.headers.get("content-disposition", ""), r.status_code)
r = as_("ope").get("/api/lucro-presumido/execucao/ex-1/arquivo/0")
check("operador nao baixa arquivo de lote", r.status_code == 403, r.status_code)
r = as_("adm").get("/api/lucro-presumido/execucao/ex-1/arquivo/1")
check("so .xlsx/.zip/.txt: o resto e 404", r.status_code == 404, r.status_code)
r = as_("adm").get("/api/lucro-presumido/execucao/ex-1/arquivo/2")
check("arquivo que saiu da pasta: 404", r.status_code == 404, r.status_code)
r = as_("adm").get("/api/lucro-presumido/execucao/ex-1/arquivo/9")
check("indice fora da lista: 404", r.status_code == 404, r.status_code)
r = as_("adm").get("/api/lucro-presumido/execucao/nao-existe/arquivo/0")
check("execucao que nao existe: 404", r.status_code == 404, r.status_code)

# ============================================================================
print("\n=== 9. Menu ===")
r = as_("ope").get("/lucro-presumido")
check("o item novo aparece em Ferramentas SPED", 'href="/lucro-presumido"' in r.text)
check("e o programa completo continua no menu, em outra aba", ":8782/" in r.text and "programa completo" in r.text)

# ============================================================================
for pasta in (PASTA_PADRAO, pasta_lote):
    shutil.rmtree(pasta, ignore_errors=True)
for proc in (PROC_OK, PROC_ERRO):
    shutil.rmtree(os.path.join(rota.TEMP_DIR, proc), ignore_errors=True)

print(f"\nRESULTADO: {OK}/{OK + FALHAS} passaram")
if FALHAS:
    print("Falharam:", *_falhou, sep="\n  - ")
    sys.exit(1)
