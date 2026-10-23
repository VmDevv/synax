# -*- coding: utf-8 -*-
"""Validar SPED no PVA oficial — o cliente do motor, a fila e a aba.

Desde 23/10/2026 a aba Validar SPED manda o arquivo ao PVA da EFD ICMS/IPI rodando sem tela
(`Desktop\\Projetos\\pva-motor`, HTTP em 127.0.0.1). Aqui o PVA é um MOTOR FALSO: um servidor HTTP de verdade, na
mesma máquina, que responde no contrato do motor (`GET /saude`, `POST /validar`). Nada depende do PVA instalado.

O que esta suíte protege:

1. **O contrato com o motor.** Corpo com Content-Length (o motor recusa `chunked`), veredito lido campo a campo, e
   "aprovado" só com VALIDADA/GERADA_PARA_ENTREGA, `valido`, zero erro e sem falha — nunca tela verde por engano.
2. **Só 127.0.0.1.** O motor não tem senha e recebe o SPED inteiro: endereço de outra máquina é recusado sem conexão.
3. **Motor fora do ar, EFD-Contribuições e arquivo de outra empresa respondem na hora**, com o motivo, e nada entra
   na fila nem chega ao PVA.
4. **Um arquivo por vez**, com a tela acompanhando (na fila, validando, resultado) e o resultado em colunas.
5. **O SPED do cliente não fica no servidor** depois que o PVA responde, com ou sem resposta.
6. **A aba respeita empresa e papel**, e o validador antigo roda junto só para o admin comparar.
"""
import json
import os
import re
import socket
import sys
import threading
import time
import types
import uuid as _uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.abspath('.'))

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
# O motor falso
# ============================================================================

class Motor:
    """Estado do motor falso. `respostas` é consumida em ordem; vazia, vale `padrao`."""
    padrao = None
    respostas = deque()
    pedidos = []            # (content-length, transfer-encoding, corpo)
    atraso = 0.0
    ao_mesmo_tempo = 0
    maximo_junto = 0
    guarda = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, status, dados):
        corpo = dados if isinstance(dados, bytes) else json.dumps(dados).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def do_GET(self):
        if self.path == "/saude":
            self._json(200, {"ok": True, "versaoPva": "6.1.1"})
        else:
            self._json(404, {"erro": "rota desconhecida"})

    def do_POST(self):
        if self.path != "/validar":
            self._json(404, {"erro": "rota desconhecida"})
            return
        tamanho = self.headers.get("Content-Length")
        em_pedacos = self.headers.get("Transfer-Encoding")
        corpo = self.rfile.read(int(tamanho)) if tamanho else b""
        with Motor.guarda:
            Motor.pedidos.append((tamanho, em_pedacos, corpo))
            Motor.ao_mesmo_tempo += 1
            Motor.maximo_junto = max(Motor.maximo_junto, Motor.ao_mesmo_tempo)
            resposta = Motor.respostas.popleft() if Motor.respostas else Motor.padrao
        try:
            if Motor.atraso:
                time.sleep(Motor.atraso)
            if resposta == "cair":
                # O motor morre no meio: fecha sem responder.
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if isinstance(resposta, tuple):
                self._json(*resposta)
            else:
                self._json(200, resposta)
        finally:
            with Motor.guarda:
                Motor.ao_mesmo_tempo -= 1


servidor = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
servidor.daemon_threads = True
threading.Thread(target=servidor.serve_forever, daemon=True).start()
URL_MOTOR = f"http://127.0.0.1:{servidor.server_address[1]}"


def porta_fechada() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    porta = s.getsockname()[1]
    s.close()
    return porta


URL_DESLIGADO = f"http://127.0.0.1:{porta_fechada()}"
os.environ["PVA_MOTOR_URL"] = URL_MOTOR


def item(tipo, codigo, descricao, registro, campo, linha, valor=None, esperado=None, conteudo=""):
    return {"tipo": tipo, "codigo": codigo, "descricao": descricao, "registro": registro, "campo": campo,
            "linha": linha, "valor": valor, "esperado": esperado, "conteudo": conteudo}


APROVADO = {"versaoPva": "6.1.1", "estado": "VALIDADA", "valido": True, "integrado": True, "totalErros": 0,
            "totalAdvertencias": 0, "erros": [], "mensagens": [], "ms": 8123}
# Mensagens copiadas do resultado real do LIESA jul/2026 e do PERMOBILI (fase 0 do motor).
REPROVADO = {
    "versaoPva": "6.1.1", "estado": "EM_EDICAO", "valido": False, "integrado": True, "totalErros": 3,
    "totalAdvertencias": 1, "mensagens": [], "ms": 11196,
    "erros": [
        item("E", "MSG_EXISTE_APURACAO_IPI", "Contribuinte de IPI deve apresentar registros E500 e registros filhos.",
             "E500", None, 0, None, "E500"),
        item("E", "MSG_VL_TOT_CREDITOS_E110_17002_V1",
             "O valor deve ser igual à soma do campo VL_ICMS dos registros (C190, C590, D190, D590, D730 para CFOP "
             "iniciado por 1 (exceto 1605), 2, 3 e CFOP 5605.", "E110", "6 - VL_TOT_CREDITOS", 763, "0,00",
             "20100,77", "|E110|0,00|0,00|"),
        item("E", "MSG_QTD_REG_BLC_OBRIGATORIO",
             "É necessário totalizar os registros do tipo {0} no bloco 9.##MSG_QTD_REG_BLC_OBRIGATORIO_RESUMO",
             "9999", "1 - REG", 4626, "9999", "9900", "|9999|4626|"),
        item("A", "MSG_CST_ICMS_RED_BC",
             "O valor do campo deve ser maior que zero, se os dois últimos dígitos do CST_ICMS = 20 ou 70.",
             "C590", "10 - VL_RED_BC", 753, "0,00", None, "|C590|020|1252|"),
    ]}
RECUSADO = {"versaoPva": "6.1.1", "estado": None, "valido": False,
            "falha": "o PVA recusou o arquivo antes de importar: MSG_QUEBRA_ULTIMA_LINHA_INVALIDA: Estrutura da "
                     "linha inválida. A escrituração não será importada.",
            "totalErros": 0, "totalAdvertencias": 0, "erros": [],
            "mensagens": ["MSG_QUEBRA_ULTIMA_LINHA_INVALIDA: Estrutura da linha inválida."], "ms": 962}
NAO_INTEGRADO = {"versaoPva": "6.1.1", "estado": None, "valido": False, "integrado": False, "totalErros": 1,
                 "totalAdvertencias": 0, "mensagens": [], "ms": 12522,
                 "erros": [item("E", "MSG_QTD_LIN_BLOCOC", "Quantidade de linhas do bloco C informada não confere "
                                "com a esperada pelo sistema.", "C990", "2 - QTD_LIN_C", 3899, "2852", "2848")]}
Motor.padrao = APROVADO

EMPRESA = "11222333000181"
ARQ = (f"|0000|020|0|01042026|30042026|EMPRESA TESTE LTDA|{EMPRESA}||ES|082000000|3205309|||A|1|\r\n"
       "|0001|1|\r\n|0990|2|\r\n|9001|0|\r\n|9900|0000|1|\r\n|9990|3|\r\n|9999|6|\r\n").encode("latin-1")
ARQ_B = ARQ.replace(b"|0001|1|", b"|0001|0|")
CONTRIB = (f"|0000|006|0|||01042026|30042026|EMPRESA TESTE LTDA|{EMPRESA}|ES|3205309||00|1|\r\n"
           "|0001|1|\r\n|9999|2|\r\n").encode("latin-1")


# ============================================================================
print("\n=== 1. O cliente do motor: contrato e veredito ===")

from backend.app.services import pva_motor  # noqa: E402

check("saúde responde com a versão do PVA", pva_motor.saude().get("versaoPva") == "6.1.1")

import tempfile  # noqa: E402
with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
    tmp.write(ARQ)
Motor.respostas.append(REPROVADO)
r = pva_motor.validar_arquivo(tmp.name)
tamanho, em_pedacos, corpo = Motor.pedidos[-1]
check("o corpo vai com Content-Length do tamanho do arquivo", tamanho == str(len(ARQ)), tamanho)
check("e nunca em pedaços (o motor recusa chunked com 411)", em_pedacos is None, em_pedacos)
check("o arquivo chega byte a byte, sem conversão", corpo == ARQ)
check("erros e advertências contados pela lista", (r.erros, r.advertencias) == (3, 1), (r.erros, r.advertencias))
check("reprovado: integrou e saiu EM_EDICAO", r.veredito == "reprovado" and not r.aprovado and r.integrado)
i_e500 = next(i for i in r.itens if i.codigo == "MSG_EXISTE_APURACAO_IPI")
check("regra do arquivo inteiro (o PVA manda linha 0) fica sem linha", i_e500.linha is None, i_e500.linha)
i_9999 = next(i for i in r.itens if i.codigo == "MSG_QTD_REG_BLC_OBRIGATORIO")
check("a chave do resumo grudada na mensagem ('##MSG_...') sai do texto",
      "##" not in i_9999.descricao and i_9999.descricao.endswith("no bloco 9."), i_9999.descricao)
i_e110 = next(i for i in r.itens if i.codigo.startswith("MSG_VL_TOT_CREDITOS"))
check("linha, registro, campo, valor e esperado do PVA chegam inteiros",
      (i_e110.linha, i_e110.registro, i_e110.campo, i_e110.valor, i_e110.esperado)
      == (763, "E110", "6 - VL_TOT_CREDITOS", "0,00", "20100,77"))
check("advertência marcada como tal", next(i for i in r.itens if i.tipo == "A").advertencia)
check("contagem por mensagem, a mais frequente primeiro", r.por_codigo()[0][1] == 1 and len(r.por_codigo()) == 4)
check("o resultado volta igual do disco (como_dict -> de_dict)",
      pva_motor.Resultado.de_dict(json.loads(json.dumps(r.como_dict()))) == r)

lr = pva_motor.ler_resposta
check("aprovado: VALIDADA, válido, sem erro", lr(APROVADO).aprovado and lr(APROVADO).veredito == "aprovado")
check("GERADA_PARA_ENTREGA também é aprovado",
      lr({**APROVADO, "estado": "GERADA_PARA_ENTREGA"}).aprovado)
check("'valido' com erro na lista NUNCA é aprovado",
      not lr({**APROVADO, "erros": REPROVADO["erros"][:1]}).aprovado)
check("'valido' com falha no relatório NUNCA é aprovado",
      not lr({**APROVADO, "falha": "relatório de inconsistências indisponível"}).aprovado)
check("'valido' com estado estranho NUNCA é aprovado", not lr({**APROVADO, "estado": "EM_EDICAO"}).aprovado)
check("só advertência continua aprovado (a tela avisa)",
      lr({**APROVADO, "erros": REPROVADO["erros"][3:]}).aprovado)
check("não integrado: erros de importação, sem estado", lr(NAO_INTEGRADO).veredito == "nao_integrado")
check("recusado: nem importou, com o motivo do PVA",
      lr(RECUSADO).veredito == "recusado" and "QUEBRA_ULTIMA_LINHA" in lr(RECUSADO).falha)
check("tipo desconhecido conta como erro (na dúvida, não passa)",
      lr({**APROVADO, "erros": [item("X", "MSG_NOVA", "?", "C100", "", 5)]}).erros == 1)
check("mensagem vazia vira o código, nunca linha em branco",
      lr({**APROVADO, "erros": [item("E", "MSG_SEM_TEXTO", None, "C100", "", 5)]}).itens[0].descricao
      == "MSG_SEM_TEXTO")


def levanta(classe, funcao, *a, **k):
    try:
        funcao(*a, **k)
    except classe as exc:
        return exc
    except Exception as exc:                              # noqa: BLE001
        return f"levantou {type(exc).__name__}: {exc}"
    return "não levantou"


exc = levanta(pva_motor.RespostaInesperada, lr, {"estado": "VALIDADA"})
check("resposta sem 'valido'/'erros' é fora do contrato", isinstance(exc, pva_motor.RespostaInesperada), exc)

Motor.respostas.append((413, {"erro": "arquivo maior que o limite"}))
exc = levanta(pva_motor.RespostaInesperada, pva_motor.validar_arquivo, tmp.name)
check("HTTP de erro do motor vira exceção com o motivo dele",
      isinstance(exc, pva_motor.RespostaInesperada) and "maior que o limite" in exc.mensagem, exc)
Motor.respostas.append((200, b"<html>nao sou json</html>"))
exc = levanta(pva_motor.RespostaInesperada, pva_motor.validar_arquivo, tmp.name)
check("resposta que não é JSON vira exceção, não resultado vazio",
      isinstance(exc, pva_motor.RespostaInesperada), exc)
Motor.respostas.append("cair")
exc = levanta(pva_motor.MotorForaDoAr, pva_motor.validar_arquivo, tmp.name)
check("motor que cai no meio da validação: fora do ar", isinstance(exc, pva_motor.MotorForaDoAr), exc)
Motor.atraso = 1.5
exc = levanta(pva_motor.RespostaInesperada, pva_motor.validar_arquivo, tmp.name, timeout=0.3)
Motor.atraso = 0.0
check("PVA que não termina no prazo: exceção com o tempo", isinstance(exc, pva_motor.RespostaInesperada)
      and "minutos" in exc.mensagem, exc)
time.sleep(1.5)   # o pedido que estourou o prazo ainda está no motor falso

os.environ["PVA_MOTOR_URL"] = URL_DESLIGADO
exc = levanta(pva_motor.MotorForaDoAr, pva_motor.saude)
check("motor desligado: saúde diz fora do ar, com o endereço",
      isinstance(exc, pva_motor.MotorForaDoAr) and "127.0.0.1" in exc.mensagem, exc)
exc = levanta(pva_motor.MotorForaDoAr, pva_motor.validar_arquivo, tmp.name)
check("e validar também", isinstance(exc, pva_motor.MotorForaDoAr), exc)
antes = len(Motor.pedidos)
for fora in ("http://192.168.2.136:8095", "http://exemplo.com.br:8095", "https://127.0.0.1:8095",
             "http://127.0.0.1.exemplo.com:8095"):
    os.environ["PVA_MOTOR_URL"] = fora
    exc = levanta(pva_motor.EnderecoNaoLocal, pva_motor.validar_arquivo, tmp.name)
    check(f"endereço fora desta máquina é recusado: {fora}", isinstance(exc, pva_motor.EnderecoNaoLocal), exc)
check("e nada foi enviado", len(Motor.pedidos) == antes)
os.environ["PVA_MOTOR_URL"] = URL_MOTOR
os.remove(tmp.name)

fonte = open(os.path.join("backend", "app", "services", "pva_motor.py"), encoding="utf-8").read()
rotas = set(re.findall(r'(?:request|putrequest)\("(?:GET|POST)", "([^"]+)"', fonte))
check("o Synax só pede /saude e /validar: nunca liga, desliga, assina nem transmite",
      rotas == {"/saude", "/validar"}, rotas)


# ============================================================================
print("\n=== 2. A aba: banco e usuários de teste ===")

import dotenv  # noqa: E402
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main  # noqa: E402
dotenv.main.load_dotenv = lambda *a, **k: False

BANCO = "_validar_sped_pva_test.db"
os.environ["DATABASE_URL"] = f"sqlite:///./{BANCO}"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists(BANCO):
    os.remove(BANCO)

from sqlalchemy import types as _satypes  # noqa: E402
import sqlalchemy.dialects.postgresql as pg  # noqa: E402


class _UUIDShim(_satypes.TypeDecorator):
    impl = _satypes.Uuid
    cache_ok = True

    def __init__(self, *a, **kw):
        kw.pop('as_uuid', None)
        super().__init__()

    def process_bind_param(self, value, dialect):
        return _uuid.UUID(value) if isinstance(value, str) else value


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
from backend.app.domain.models.audit import SpedValidacao  # noqa: E402
from backend.app.domain.models.auth_models import (  # noqa: E402
    Tenant, UserProfile, UserTenant)
from backend.app.services import validar_sped_fila as fila  # noqa: E402
from backend.app.services.sped_entregas import hash_do_corpo  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import null  # noqa: E402

Base.metadata.create_all(bind=_eng)
db = SessionLocal()
tA, tB, tC = _uuid.uuid4(), _uuid.uuid4(), _uuid.uuid4()
db.add_all([
    # O cadastro guarda com máscara: a comparação é só por letras e dígitos.
    Tenant(id=tA, name="EMPRESA TESTE LTDA", cnpj="11.222.333/0001-81", uf="ES"),
    Tenant(id=tB, name="OUTRA EMPRESA LTDA", cnpj="11444777000161", uf="ES"),
    Tenant(id=tC, name="EMPRESA DE OUTRO OPERADOR", cnpj="99888777000100", uf="ES"),
])
u_ope, u_ope2, u_cli, u_adm = _uuid.uuid4(), _uuid.uuid4(), _uuid.uuid4(), _uuid.uuid4()
db.add_all([
    UserProfile(id=u_ope, tenant_id=tA, role="operador", name="Operador"),
    UserProfile(id=u_ope2, tenant_id=tC, role="operador", name="Operador 2"),
    UserProfile(id=u_cli, tenant_id=tA, role="cliente", name="Cliente"),
    UserProfile(id=u_adm, tenant_id=tA, role="admin", name="Admin"),
])
db.add_all([UserTenant(user_id=u_ope, tenant_id=tA),
            UserTenant(user_id=u_ope, tenant_id=tB),
            UserTenant(user_id=u_ope2, tenant_id=tC)])
db.commit()
db.close()

TOKENS["ope"] = types.SimpleNamespace(id=str(u_ope), email="oper@synax.com")
TOKENS["ope2"] = types.SimpleNamespace(id=str(u_ope2), email="oper2@synax.com")
TOKENS["cli"] = types.SimpleNamespace(id=str(u_cli), email="cli@synax.com")
TOKENS["adm"] = types.SimpleNamespace(id=str(u_adm), email="adm@synax.com")

from backend.app.api.dependencies.db import get_rls_db as _real_rls  # noqa: E402


def _fake_rls():
    ss = SessionLocal()
    try:
        yield ss
    finally:
        ss.close()


m.app.dependency_overrides[_real_rls] = _fake_rls
c = TestClient(m.app, raise_server_exceptions=False)
HX = {"HX-Request": "true"}


def as_(token):
    c.cookies.clear()
    if token:
        c.cookies.set("access_token", f"Bearer {token}")
    return c


def enviar(token, tenant, conteudo, htmx=False):
    return as_(token).post(
        "/api/validar-sped", data={"tenant_id": str(tenant) if tenant else ""},
        files={"sped_file": ("sped.txt", conteudo, "text/plain")},
        headers=HX if htmx else {})


def linha_de(processo):
    ss = SessionLocal()
    try:
        return ss.get(SpedValidacao, processo)
    finally:
        ss.close()


def texto(r):
    return re.sub(r"\s+", " ", r.text)


PROCESSOS = []
check("banco de teste montado", True)


# ============================================================================
print("\n=== 3. A aba: acesso e o motor na tela ===")

r = as_("ope").get("/validar-sped")
check("operador abre a aba", r.status_code == 200, r.status_code)
check("com o seletor de empresa", 'name="tenant_id"' in r.text)
check("a aba diz que quem valida é o PVA oficial", "PVA" in r.text and "oficial" in r.text)
check("e confere o motor ao abrir", "/api/validar-sped/motor" in r.text)
check("a aba traz o painel do mês, que escuta o fim da validação",
      "/api/validar-sped/painel" in r.text and "validarSpedConcluida" in r.text)
check("cliente não entra", as_("cli").get("/validar-sped").status_code == 403)
check("anônimo também não", as_(None).get("/validar-sped").status_code in (401, 403))

r = as_("ope").get("/api/validar-sped/motor", headers=HX)
check("motor ligado aparece na tela com a versão", "PVA 6.1.1 ligado" in r.text, r.text[:300])
check("pela API também", as_("ope").get("/api/validar-sped/motor").json() == {"ligado": True, "versao": "6.1.1"})
os.environ["PVA_MOTOR_URL"] = URL_DESLIGADO
r = as_("ope").get("/api/validar-sped/motor", headers=HX)
check("motor desligado: 'fora do ar', sem nada verde",
      "fora do ar" in r.text and "emerald" not in r.text, r.text[:300])
check("o operador não vê o comando de ligar", "motor.cmd" not in r.text)
r = as_("adm").get("/api/validar-sped/motor", headers=HX)
check("o admin vê como ligar e o aviso de nunca matar o processo",
      "motor.cmd servir" in r.text and "/encerrar" in r.text, r.text[:400])
check("cliente não consulta o motor", as_("cli").get("/api/validar-sped/motor").status_code == 403)

antes = len(Motor.pedidos)
r = enviar("ope", tA, ARQ, htmx=True)
check("subir com o motor desligado: a tela diz na hora, em 200",
      r.status_code == 200 and "não está respondendo" in r.text and "Aprovado" not in r.text, r.text[:300])
r = enviar("ope", tA, ARQ)
check("pela API, 503 com o motivo", r.status_code == 503 and "Nada foi validado" in r.json().get("erro", ""),
      r.text[:200])
ss = SessionLocal()
check("e nada entrou no histórico", ss.query(SpedValidacao).count() == 0)
ss.close()
os.environ["PVA_MOTOR_URL"] = URL_MOTOR

r = enviar("ope", tA, CONTRIB)
check("EFD-Contribuições: 422 com o motivo (o PVA dela não está instalado)",
      r.status_code == 422 and "EFD-Contribuições" in r.json().get("erro", ""), r.text[:200])
r = enviar("ope", tB, ARQ)
check("arquivo de OUTRA empresa é recusado (CNPJ do 0000 x cadastro)",
      r.status_code == 422 and "outra empresa" in r.json().get("erro", ""), r.text[:200])
r = enviar("ope", tB, ARQ, htmx=True)
check("e na tela a recusa vem em 200, com o motivo visível",
      r.status_code == 200 and "outra empresa" in r.text)
r = enviar("ope", tA, b"isto nao e um sped\r\n")
check("arquivo sem 0000: recusado com o motivo", r.status_code == 422 and "0000" in r.json().get("erro", ""))
check("nada disso chegou ao PVA", len(Motor.pedidos) == antes, len(Motor.pedidos) - antes)
check("operador não valida empresa que não é dele", enviar("ope", tC, ARQ).status_code == 403)
r = enviar("ope", "", ARQ, htmx=True)
check("sem empresa: 200 com aviso (htmx não troca o alvo em 4xx)",
      r.status_code == 200 and "Escolha a empresa" in r.text)
check("cliente não usa a API", enviar("cli", tA, ARQ).status_code == 403)
ss = SessionLocal()
check("recusa não entra no histórico", ss.query(SpedValidacao).count() == 0)
ss.close()
sobras = [p for p in os.listdir(fila.TEMP_DIR) if os.path.exists(os.path.join(fila.TEMP_DIR, p, fila.ARQUIVO_SPED))] \
    if os.path.isdir(fila.TEMP_DIR) else []
check("e o arquivo recusado não ficou no servidor", not sobras, sobras)


# ============================================================================
print("\n=== 4. Na fila, no PVA e o resultado ===")

Motor.respostas.append(APROVADO)
r = enviar("ope", tA, ARQ)
check("API: 202 com o processo na fila", r.status_code == 202 and r.json().get("situacao") == "na_fila",
      r.text[:200])
p_ok = r.json().get("processo", "")
PROCESSOS.append(p_ok)
check("a API devolve o hash do corpo e a competência do 0000",
      r.json().get("hash_corpo") == hash_do_corpo(ARQ) and r.json().get("competencia") == "2026-04")
check("a fila esvaziou", fila.esperar(10))
ln = linha_de(p_ok)
check("o resultado vai em colunas: aprovado, estado, contagens, versão, tempo",
      ln and ln.motor == "pva" and ln.situacao == "concluida" and ln.aprovado is True
      and ln.estado_pva == "VALIDADA" and ln.erros == 0 and ln.advertencias == 0 and ln.integrado is True
      and ln.versao_pva == "6.1.1" and ln.duracao_ms == 8123 and ln.concluida_em is not None,
      ln and (ln.situacao, ln.aprovado, ln.estado_pva, ln.erros, ln.versao_pva, ln.duracao_ms))
check("com competência, CNPJ do 0000, original/substituta, COD_VER, linhas e quem validou",
      ln and ln.competencia == "2026-04" and ln.cnpj == EMPRESA and ln.cod_fin == "0" and ln.leiaute == "020"
      and ln.linhas == 7 and ln.user_name == "Operador", ln and (ln.competencia, ln.leiaute, ln.linhas))
check("o validador antigo rodou junto (comparação)", ln and isinstance(ln.py_erros, int), ln and ln.py_erros)
check("o SPED do cliente não ficou no servidor",
      not os.path.exists(os.path.join(fila.pasta(p_ok), fila.ARQUIVO_SPED)))
check("o PVA recebeu o arquivo com Content-Length, sem chunked",
      Motor.pedidos[-1][:2] == (str(len(ARQ)), None) and Motor.pedidos[-1][2] == ARQ)

r = as_("ope").get(f"/api/validar-sped/{p_ok}")
check("o andamento pela API traz o veredito do PVA",
      r.status_code == 200 and r.json()["situacao"] == "concluida" and r.json()["resultado"]["aprovado"] is True,
      r.text[:300])
r = as_("ope").get(f"/api/validar-sped/{p_ok}", headers=HX)
check("na tela: 'Aprovado pelo PVA', em verde", "Aprovado pelo PVA" in r.text and "bg-emerald-700" in r.text,
      texto(r)[:400])
check("o fim avisa o painel do mês (HX-Trigger)", r.headers.get("HX-Trigger") == "validarSpedConcluida")
check("e o cartão para de se repetir", "load delay" not in r.text)
check("o operador não vê a comparação com o validador antigo", "validador antigo achou" not in r.text)
r = as_("adm").get(f"/api/validar-sped/{p_ok}", headers=HX)
check("o admin vê a comparação e o CSV do antigo",
      "validador antigo achou" in r.text and "csv?antigo=1" in r.text, texto(r)[-600:])

Motor.respostas.append(REPROVADO)
r = enviar("ope", tA, ARQ_B, htmx=True)
p_rep = re.search(r"/api/validar-sped/([0-9a-f-]{36})", r.text).group(1)
PROCESSOS.append(p_rep)
check("htmx: o cartão de fila se repete sozinho até o fim",
      "Na fila do PVA" in r.text and 'hx-trigger="load delay:2s"' in r.text, texto(r)[:300])
fila.esperar(10)
r = as_("ope").get(f"/api/validar-sped/{p_rep}", headers=HX)
t = texto(r)
check("reprovado: 'Reprovado pelo PVA: 3 erro(s)', em vermelho, com 1 advertência",
      "Reprovado pelo PVA: 3 erro(s)" in t and "1 advertência(s)" in t and "bg-red-700" in t, t[:400])
check("com a mensagem oficial do PVA, o código, o valor e o esperado",
      "Contribuinte de IPI deve apresentar registros E500" in t and "MSG_VL_TOT_CREDITOS_E110_17002_V1" in t
      and "20100,77" in t)
check("sem a chave de resumo grudada no texto", "##MSG" not in t)
check("e o mesmo MESMO arquivo? não: avisa que já houve validação e que este é DIFERENTE",
      "já tinha 1 validação" in t and "DIFERENTE" in t, t[-1200:])
ln = linha_de(p_rep)
check("colunas do reprovado: aprovado False, 3 erros, 1 advertência, EM_EDICAO",
      ln.aprovado is False and ln.erros == 3 and ln.advertencias == 1 and ln.estado_pva == "EM_EDICAO")

r = as_("ope").get(f"/api/validar-sped/{p_rep}/csv")
check("CSV baixa como arquivo, nunca renderizado",
      r.status_code == 200 and r.headers.get("content-type") == "application/octet-stream"
      and r.headers.get("content-disposition", "").startswith("attachment;"))
csv_txt = r.content.decode("utf-8-sig")
check("CSV com as colunas do PVA e as quatro linhas",
      csv_txt.startswith("tipo;linha;registro;campo;valor;esperado;mensagem;codigo;conteudo")
      and len(csv_txt.strip().splitlines()) == 5 and "advertência;753;C590" in csv_txt, csv_txt[:300])
check("o nome do CSV diz empresa e competência",
      "Validacao_PVA_EMPRESA TESTE LTDA_2026-04.csv" in r.headers.get("content-disposition", ""),
      r.headers.get("content-disposition"))
check("operador não baixa o CSV do validador antigo", as_("ope").get(f"/api/validar-sped/{p_rep}/csv?antigo=1")
      .status_code == 403)
r = as_("adm").get(f"/api/validar-sped/{p_rep}/csv?antigo=1")
check("o admin baixa (período em paralelo)", r.status_code == 200
      and r.content.decode("utf-8-sig").startswith("linha;registro;campo"), r.status_code)
check("operador de outra empresa não vê o andamento",
      as_("ope2").get(f"/api/validar-sped/{p_rep}", headers=HX).status_code == 403)
check("nem baixa o relatório", as_("ope2").get(f"/api/validar-sped/{p_rep}/csv").status_code == 403)
check("identificador que não é UUID não vira caminho",
      as_("ope").get("/api/validar-sped/..%2F..%2Fmain/csv").status_code in (400, 404)
      and as_("ope").get("/api/validar-sped/nao-e-uuid").status_code == 404)

Motor.respostas.append(APROVADO)
r = enviar("ope", tA, ARQ)
PROCESSOS.append(r.json()["processo"])
fila.esperar(10)
r = as_("ope").get(f"/api/validar-sped/{PROCESSOS[-1]}", headers=HX)
check("o MESMO arquivo validado de novo: a tela avisa",
      "MESMO arquivo já tinha sido validado" in texto(r), texto(r)[-900:])

Motor.respostas.append(RECUSADO)
r = enviar("ope", tA, ARQ)
p_recusa = r.json()["processo"]
PROCESSOS.append(p_recusa)
fila.esperar(10)
t = texto(as_("ope").get(f"/api/validar-sped/{p_recusa}", headers=HX))
check("recusado na entrada: a tela diz e mostra o motivo do PVA",
      "recusou o arquivo antes de importar" in t and "MSG_QUEBRA_ULTIMA_LINHA_INVALIDA" in t
      and "Aprovado" not in t, t[:500])
check("e fica registrado como não aprovado, com a falha",
      linha_de(p_recusa).aprovado is False and "QUEBRA" in (linha_de(p_recusa).falha or ""))

Motor.respostas.append(NAO_INTEGRADO)
r = enviar("ope", tA, ARQ)
PROCESSOS.append(r.json()["processo"])
fila.esperar(10)
t = texto(as_("ope").get(f"/api/validar-sped/{PROCESSOS[-1]}", headers=HX))
check("não integrado: a tela diz, com os erros de importação",
      "não integrou a escrituração: 1 erro(s) de importação" in t and "MSG_QTD_LIN_BLOCOC" in t, t[:400])


# ============================================================================
print("\n=== 5. Um por vez, motor que cai e servidor que reinicia ===")

Motor.maximo_junto = 0
Motor.atraso = 0.4
Motor.respostas.extend([APROVADO, REPROVADO, APROVADO])
ids = []
for conteudo in (ARQ, ARQ_B, ARQ):
    ids.append(enviar("ope", tA, conteudo).json()["processo"])
PROCESSOS.extend(ids)
time.sleep(0.1)
r = as_("ope").get(f"/api/validar-sped/{ids[2]}", headers=HX)
check("o terceiro espera na fila, com quantos estão na frente",
      "Na fila do PVA" in r.text and "2 arquivo(s) na frente" in texto(r), texto(r)[:300])
r = as_("ope").get(f"/api/validar-sped/{ids[0]}", headers=HX)
check("o primeiro aparece 'validando' no PVA", "está validando" in r.text, texto(r)[:300])
r = as_("ope").get(f"/api/validar-sped/{ids[2]}")
check("pela API, a posição na fila", r.json().get("situacao") == "na_fila" and r.json().get("na_frente") == 2,
      r.text[:200])
check("enquanto espera, erros e advertências ficam NULOS (zero seria lido como 'sem erro')",
      (linha_de(ids[2]).erros, linha_de(ids[2]).advertencias, linha_de(ids[2]).aprovado) == (None, None, None))
fila.esperar(15)
Motor.atraso = 0.0
check("o PVA nunca recebeu dois arquivos ao mesmo tempo", Motor.maximo_junto == 1, Motor.maximo_junto)
check("e respondeu na ordem de chegada",
      [linha_de(i).aprovado for i in ids] == [True, False, True], [linha_de(i).aprovado for i in ids])

Motor.respostas.append("cair")
r = enviar("ope", tA, ARQ)
p_caiu = r.json()["processo"]
PROCESSOS.append(p_caiu)
fila.esperar(10)
ln = linha_de(p_caiu)
check("motor que cai no meio: 'falhou', sem veredito",
      ln.situacao == "falhou" and ln.aprovado is None and ln.erros is None and "parou de responder" in ln.falha,
      (ln.situacao, ln.aprovado, ln.falha))
t = texto(as_("ope").get(f"/api/validar-sped/{p_caiu}", headers=HX))
check("a tela diz o motivo, sem nada verde", "parou de responder" in t and "emerald" not in t, t[:300])
check("o SPED não ficou no servidor nem quando o PVA não respondeu",
      not os.path.exists(os.path.join(fila.pasta(p_caiu), fila.ARQUIVO_SPED)))

# Servidor reiniciado: a linha ficou "na fila" e a memória da fila não tem o pedido.
p_parada = str(_uuid.uuid4())
PROCESSOS.append(p_parada)
ss = SessionLocal()
ss.add(SpedValidacao(id=p_parada, tenant_id=str(tB), user_name="Operador", competencia="2026-04",
                     motor="pva", situacao="na_fila", erros=null(), advertencias=null()))
ss.commit()
ss.close()
t = texto(as_("ope").get(f"/api/validar-sped/{p_parada}", headers=HX))
check("pedido perdido num reinício: 'interrompida', nunca 'validando' para sempre",
      "interrompida" in t and "load delay" not in t, t[:300])

# O fim chegou, mas o UPDATE não: a memória da fila diz como terminou.
p_sem_registro = str(_uuid.uuid4())
PROCESSOS.append(p_sem_registro)
ss = SessionLocal()
ss.add(SpedValidacao(id=p_sem_registro, tenant_id=str(tA), competencia="2026-05", motor="pva",
                     situacao="validando", erros=null(), advertencias=null()))
ss.commit()
ss.close()
os.makedirs(fila.pasta(p_sem_registro), exist_ok=True)
fila._gravar_relatorio(p_sem_registro, pva_motor.ler_resposta(APROVADO))
fila._finais[p_sem_registro] = {"situacao": "concluida", "registrada": False}
t = texto(as_("ope").get(f"/api/validar-sped/{p_sem_registro}", headers=HX))
check("resultado que não chegou ao banco: a tela mostra e avisa que NÃO ficou registrado",
      "Aprovado pelo PVA" in t and "NÃO ficou registrado" in t, t[:600])

fila_max = fila.MAX_FILA
fila.MAX_FILA = 0
antes = len(Motor.pedidos)
r = enviar("ope", tA, ARQ)
fila.MAX_FILA = fila_max
check("fila cheia: 503 com o motivo, e nada entra", r.status_code == 503 and "fila do PVA está cheia"
      in r.json().get("erro", "") and len(Motor.pedidos) == antes, r.text[:200])


# ============================================================================
print("\n=== 6. Painel do mês ===")

ss = SessionLocal()
# Uma validação do validador antigo (antes do PVA): o painel não pode lê-la como aprovada.
ss.add(SpedValidacao(id=str(_uuid.uuid4()), tenant_id=str(tC), user_name="Operador 2", competencia="2026-04",
                     erros=0, advertencias=0, nao_conferidas=40))
ss.commit()
ss.close()
r = as_("ope").get("/api/validar-sped/painel?competencia=2026-04")
t = texto(r)
check("painel responde ao operador", r.status_code == 200, r.status_code)
check("a tentativa que caiu (A) e a interrompida (B) aparecem ao lado da última concluída",
      "EMPRESA TESTE LTDA" in t and t.count("última tentativa não concluída") == 2, t[:900])
check("B, com o pedido interrompido, não conta como validada", "1</strong> de 2" in t, t[:200])
check("e não mostra empresa de outro operador", "EMPRESA DE OUTRO OPERADOR" not in t)
t = texto(as_("ope2").get("/api/validar-sped/painel?competencia=2026-04"))
check("a validação antiga aparece como do validador antigo, nunca 'aprovado'",
      "validador antigo: 0 erro(s)" in t and "aprovado pelo PVA" not in t, t[:600])
ss = SessionLocal()
ss.query(SpedValidacao).filter(SpedValidacao.id == p_caiu).delete()
ss.commit()
ss.close()
t = texto(as_("ope").get("/api/validar-sped/painel?competencia=2026-04"))
check("sem tentativa pendente, A mostra 'aprovado pelo PVA'", "aprovado pelo PVA" in t, t[:900])
t = texto(as_("ope").get("/api/validar-sped/painel?competencia=2026-03"))
check("em outro mês, nada validado", "0</strong> de 2" in t, t[:300])
check("competência inválida não derruba (vale a última fechada)",
      as_("ope").get("/api/validar-sped/painel?competencia=lixo").status_code == 200)
check("cliente não vê o painel", as_("cli").get("/api/validar-sped/painel").status_code == 403)


# ============================================================================
import shutil as _shutil  # noqa: E402

fila.esperar(10)
servidor.shutdown()
ss = SessionLocal()
_todos = [r.id for r in ss.query(SpedValidacao.id).all()]
ss.close()
for p in set(_todos) | set(PROCESSOS):
    _shutil.rmtree(fila.pasta(p), ignore_errors=True)
_eng.dispose()
if os.path.exists(BANCO):
    try:
        os.remove(BANCO)
    except OSError:
        pass

print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
for f in _falhou:
    print("  -", f)
sys.exit(1 if FALHAS else 0)
