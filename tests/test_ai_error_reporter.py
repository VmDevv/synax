"""Diagnóstico automático de erros por IA (pedido do usuário em 20/08/2026).

Todo log de nível ERROR/CRITICAL do sistema deve gerar um diagnóstico via
Anthropic Claude, gravado em logs/erros_ia.log — sem quebrar o processo se a
IA falhar, sem vazar CNPJ/e-mail para fora, sem gastar uma chamada de IA por
cada repetição do mesmo erro, e funcionando (silenciosamente, com um aviso
único) mesmo sem ANTHROPIC_API_KEY configurada.

Duas camadas:
1. Unitária — direto no módulo `ai_error_reporter`, sem tocar banco/app.
2. Fiação — confirma que `backend.app.main` liga o handler no logger raiz.
"""
import os, sys, io, inspect, logging, re, threading, time, types
sys.path.insert(0, os.path.abspath('.'))

CAMINHO_LOG_TESTE = os.path.abspath("scratch/_test_erros_ia.log")
os.makedirs(os.path.dirname(CAMINHO_LOG_TESTE), exist_ok=True)
if os.path.exists(CAMINHO_LOG_TESTE):
    os.remove(CAMINHO_LOG_TESTE)
os.environ["AI_ERROR_LOG_PATH"] = CAMINHO_LOG_TESTE

# Cada erro vira um ARQUIVO próprio em logs/erros_ia/ — antes era tudo num
# `.log` só, que passou de 180 KB e virou um paredão para achar o erro de hoje.
# O `CAMINHO_LOG_TESTE` ficou só para os avisos do próprio diagnóstico.
import shutil as _shutil
PASTA_ERROS_TESTE = os.path.abspath("scratch/_test_erros_ia")
os.environ["AI_ERROR_DIR"] = PASTA_ERROS_TESTE


def _arquivos_de_erro():
    if not os.path.isdir(PASTA_ERROS_TESTE):
        return []
    return sorted(os.listdir(PASTA_ERROS_TESTE))


def _conteudo_dos_erros():
    """Junta os arquivos de diagnóstico, para os checks de conteúdo."""
    return "\n".join(
        io.open(os.path.join(PASTA_ERROS_TESTE, n), encoding="utf-8").read()
        for n in _arquivos_de_erro())


def _limpar_erros():
    _shutil.rmtree(PASTA_ERROS_TESTE, ignore_errors=True)


_limpar_erros()
os.environ.pop("ANTHROPIC_API_KEY", None)  # controlado por teste, nunca herdado do ambiente real

from backend.app.core import ai_error_reporter as reporter

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


def _fazer_record(nome_logger="backend.app.smoke", nivel=logging.ERROR, msg="erro de teste",
                  exc_info=None):
    return logging.LogRecord(nome_logger, nivel, __file__, 1, msg, (), exc_info)


# ============================================================================
print("\n=== 1. Mascaramento de dado sensível antes de sair para a IA ===")
texto = "Falha ao processar empresa 11222333000181 (cliente@empresa.com.br): timeout"
mascarado = reporter._redigir(texto)
check("CNPJ mascarado", "11222333000181" not in mascarado, mascarado)
check("e-mail mascarado", "cliente@empresa.com.br" not in mascarado, mascarado)
check("marcador [documento] presente", "[documento]" in mascarado, mascarado)
check("marcador [email] presente", "[email]" in mascarado, mascarado)
check("texto ao redor preservado", "Falha ao processar empresa" in mascarado, mascarado)

check("CPF (11 dígitos) também mascarado", "[documento]" in reporter._redigir("CPF 12345678901 inválido"))
check("número de 5 dígitos NÃO é confundido com documento",
      "[documento]" not in reporter._redigir("task_id parcial 12345"))
check("número de 10 dígitos (nem 11 nem 14) não é mascarado",
      "[documento]" not in reporter._redigir("código 1234567890 gerado"))


# ============================================================================
print("\n=== 2. Assinatura agrupa o MESMO erro e separa erros diferentes ===")
r1 = _fazer_record(msg="Falha ao conectar ao Supabase")
r2 = _fazer_record(msg="Falha ao conectar ao Supabase")  # mesmo módulo+nível+msg
r3 = _fazer_record(msg="Falha ao conectar ao Supabase", nome_logger="backend.app.outro")
r4 = _fazer_record(nivel=logging.CRITICAL, msg="Falha ao conectar ao Supabase")

check("mesmo erro -> mesma assinatura", reporter._assinatura(r1) == reporter._assinatura(r2))
check("módulo diferente -> assinatura diferente", reporter._assinatura(r1) != reporter._assinatura(r3))
check("nível diferente -> assinatura diferente", reporter._assinatura(r1) != reporter._assinatura(r4))

try:
    raise ValueError("boom")
except ValueError:
    r_exc = _fazer_record(msg="algo quebrou", exc_info=sys.exc_info())
try:
    raise KeyError("outra")
except KeyError:
    r_exc2 = _fazer_record(msg="algo quebrou", exc_info=sys.exc_info())
check("tipo de exceção diferente -> assinatura diferente (mesma mensagem)",
      reporter._assinatura(r_exc) != reporter._assinatura(r_exc2))


# ============================================================================
print("\n=== 3. Sem ANTHROPIC_API_KEY: nunca tenta rede, avisa uma única vez ===")
handler = reporter.AIErrorHandler()
logger_teste = logging.getLogger("backend.app._teste_sem_chave")
logger_teste.addHandler(handler)
logger_teste.propagate = False
logger_teste.setLevel(logging.ERROR)

for _ in range(3):
    logger_teste.error("erro repetido sem chave configurada")

handler._executor.shutdown(wait=True)  # espera as threads em segundo plano

check("arquivo de log foi criado mesmo sem chave", os.path.exists(CAMINHO_LOG_TESTE))
conteudo = io.open(CAMINHO_LOG_TESTE, encoding="utf-8").read()
check("aviso de 'configure a chave' está no arquivo", "ANTHROPIC_API_KEY" in conteudo, conteudo[:200])
check("aviso aparece exatamente 1 vez, mesmo com 3 erros", conteudo.count("está DESATIVADO") == 1,
      f"apareceu {conteudo.count('está DESATIVADO')} vezes")
check("nenhum bloco de diagnóstico foi gravado (não tentou chamar IA sem chave)",
      "DIAGNÓSTICO DA IA" not in conteudo)

os.remove(CAMINHO_LOG_TESTE)
logger_teste.removeHandler(handler)


# ============================================================================
print("\n=== 4. Com chave (Anthropic mockado): chama 1x por erro único, respeita cooldown ===")

class _FakeTextBlock:
    type = "text"
    def __init__(self, text): self.text = text

class _FakeResponse:
    def __init__(self, text): self.content = [_FakeTextBlock(text)]

CHAMADAS = []

class _FakeMessages:
    def create(self, model, max_tokens, system, messages):
        CHAMADAS.append({"model": model, "conteudo": messages[0]["content"]})
        return _FakeResponse("## Severidade\nAlta\n\n## O que aconteceu\nTeste.\n")

class _FakeClient:
    def __init__(self, api_key=None, **kwargs):
        # **kwargs: o codigo passa timeout=, e um mock rigido demais quebra por
        # um detalhe do cliente que o teste nao esta verificando.
        self.messages = _FakeMessages()

class _AuthenticationError(Exception): pass
class _RateLimitError(Exception): pass
class _APIConnectionError(Exception): pass
class _APIStatusError(Exception):
    def __init__(self, status_code=500): self.status_code = status_code

_fake_anthropic = types.ModuleType("anthropic")
_fake_anthropic.Anthropic = _FakeClient
_fake_anthropic.AuthenticationError = _AuthenticationError
_fake_anthropic.RateLimitError = _RateLimitError
_fake_anthropic.APIConnectionError = _APIConnectionError
_fake_anthropic.APIStatusError = _APIStatusError
sys.modules["anthropic"] = _fake_anthropic

os.environ["ANTHROPIC_API_KEY"] = "chave-de-teste-fake"
if os.path.exists(CAMINHO_LOG_TESTE):
    os.remove(CAMINHO_LOG_TESTE)
_limpar_erros()

handler2 = reporter.AIErrorHandler()
logger_teste2 = logging.getLogger("backend.app._teste_com_chave")
logger_teste2.addHandler(handler2)
logger_teste2.propagate = False
logger_teste2.setLevel(logging.ERROR)

logger_teste2.error("CNPJ da empresa 33444555000199 falhou ao processar")
logger_teste2.error("CNPJ da empresa 33444555000199 falhou ao processar")  # mesma assinatura
logger_teste2.error("erro completamente diferente: conexão recusada")     # assinatura nova
handler2._executor.shutdown(wait=True)

check("2 chamadas à IA (1 por assinatura distinta, não 3)", len(CHAMADAS) == 2, f"chamadas={len(CHAMADAS)}")
check("cai para a Anthropic quando a OpenAI nao tem chave",
      all(c["model"] == "claude-opus-5" for c in CHAMADAS),
      [c["model"] for c in CHAMADAS])
check("CNPJ mascarado no texto que saiu para a IA",
      all("33444555000199" not in c["conteudo"] for c in CHAMADAS),
      [c["conteudo"] for c in CHAMADAS])

conteudo2 = _conteudo_dos_erros()
_nomes = _arquivos_de_erro()

# UM ARQUIVO POR ERRO — dois erros distintos, dois arquivos.
check("2 arquivos de diagnóstico, um por erro", len(_nomes) == 2, _nomes)
check("conteúdo do diagnóstico (mockado) está no arquivo", "## Severidade" in conteudo2)
check("os arquivos são .md, para abrir formatado",
      all(n.endswith(".md") for n in _nomes), _nomes)

# O NOME diz o que é sem precisar abrir: data, de onde veio e o tipo do erro.
check("o nome começa pela data, para a pasta ordenar sozinha",
      all(re.match(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}__", n) for n in _nomes), _nomes)
check("o nome diz de onde veio o erro",
      all("_teste_com_chave" in n for n in _nomes), _nomes)
check("cada arquivo traz a mensagem original do sistema",
      conteudo2.count("## Mensagem original do sistema") == 2, conteudo2[:400])
check("e diz quando aconteceu, em português", "**Quando:**" in conteudo2, conteudo2[:400])

os.remove(CAMINHO_LOG_TESTE) if os.path.exists(CAMINHO_LOG_TESTE) else None
_limpar_erros()
logger_teste2.removeHandler(handler2)


# ============================================================================
print("\n=== 5. Modelo customizável via AI_ERROR_MODEL ===")
os.environ["AI_ERROR_MODEL"] = "claude-haiku-4-5"
# Sem OPENAI_API_KEY o primario e pulado e quem responde e o mock do anthropic.
CHAMADAS.clear()
handler3 = reporter.AIErrorHandler()
logger_teste3 = logging.getLogger("backend.app._teste_modelo")
logger_teste3.addHandler(handler3)
logger_teste3.propagate = False
logger_teste3.setLevel(logging.ERROR)
logger_teste3.error("erro para checar o modelo customizado")
handler3._executor.shutdown(wait=True)

check("AI_ERROR_MODEL substitui o padrão", CHAMADAS and CHAMADAS[0]["model"] == "claude-haiku-4-5",
      [c["model"] for c in CHAMADAS])

os.environ.pop("AI_ERROR_MODEL", None)
if os.path.exists(CAMINHO_LOG_TESTE):
    os.remove(CAMINHO_LOG_TESTE)
_limpar_erros()
logger_teste3.removeHandler(handler3)


# ============================================================================
print("\n=== 6. O próprio módulo nunca entra em loop de log ===")
# A defesa direta é a checagem `record.name == __name__` logo no início de
# emit() — o cooldown por assinatura (seção 4) também limita uma recursão-
# repetição a 1 salto extra na prática, mas a checagem explícita é o que
# garante isso por design, não por efeito colateral do cooldown.
src_emit = inspect.getsource(reporter.AIErrorHandler.emit)
check("emit() ignora logs vindos do próprio módulo (guarda anti-recursão)",
      "record.name == __name__" in src_emit, src_emit)

CHAMADAS.clear()

class _MessagesQueQuebra:
    def create(self, **kw):
        raise RuntimeError("falha simulada dentro da chamada à IA")

_fake_anthropic.Anthropic = lambda api_key=None, **kw: types.SimpleNamespace(messages=_MessagesQueQuebra())

handler4 = reporter.AIErrorHandler()
logger_teste4 = logging.getLogger("backend.app._teste_loop")
logger_teste4.addHandler(handler4)
logger_teste4.propagate = False
logger_teste4.setLevel(logging.ERROR)

concluiu = threading.Event()
def _disparar():
    logger_teste4.error("erro que faz a própria chamada de IA falhar")
    concluiu.set()

t = threading.Thread(target=_disparar)
t.start()
t.join(timeout=10)
handler4._executor.shutdown(wait=True)

check("não travou nem estourou o processo mesmo com a IA falhando internamente",
      concluiu.is_set(), "thread não terminou em 10s — possível loop/deadlock")

_fake_anthropic.Anthropic = _FakeClient  # restaura para não vazar estado
logger_teste4.removeHandler(handler4)
if os.path.exists(CAMINHO_LOG_TESTE):
    os.remove(CAMINHO_LOG_TESTE)
_limpar_erros()


# ============================================================================
print("\n=== 7. Fiação: main.py liga o handler no logger raiz ===")
del sys.modules["anthropic"]  # usa o SDK de verdade (ou ausência dele) na importação real

os.environ["DATABASE_URL"] = "sqlite:///./_ai_reporter_wiring_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
os.environ["SIEG_API_KEY"] = "teste"
os.environ["SIEG_EMAIL"] = "teste@x.com"
os.environ.pop("ANTHROPIC_API_KEY", None)

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

if os.path.exists("_ai_reporter_wiring_test.db"):
    os.remove("_ai_reporter_wiring_test.db")

import backend.app.main  # noqa: F401 — importar já chama configurar_logging()

handlers_raiz = logging.getLogger().handlers
check("handler de IA está pendurado no logger raiz após importar main.py",
      any(isinstance(h, reporter.AIErrorHandler) for h in handlers_raiz),
      [type(h).__name__ for h in handlers_raiz])

if os.path.exists("_ai_reporter_wiring_test.db"):
    try:
        os.remove("_ai_reporter_wiring_test.db")
    except OSError:
        pass


# ============================================================================
print("\n=== 8. Startup resiliente: banco fora do ar não mata o processo ===")
# Um soluco de DNS no Supabase derrubava o worker inteiro no import de main.py
# (Base.metadata.create_all no nivel do modulo). Com --reload, isso acontecia a
# cada arquivo salvo. Agora o erro vai para o log e a aplicacao sobe.
main_py = io.open(os.path.join("backend", "app", "main.py"), encoding="utf-8").read()
# A CHAMADA, com parenteses: desde 10/09/2026 o comentario acima do bloco de
# imports tambem cita `Base.metadata.create_all` em prosa, e ancorar na palavra
# fazia a janela cair no comentario em vez do codigo.
_i = main_py.index("Base.metadata.create_all(bind=engine)")
trecho = main_py[max(0, _i - 900):_i + 400]
check("create_all do startup esta protegido por try/except",
      "try:" in trecho and "except Exception:" in trecho, trecho[-300:])
check("a falha de startup e registrada no log (e vai para o diagnostico por IA)",
      "logger.exception(" in trecho)
check("schema_upgrades tambem esta dentro da protecao",
      "schema_upgrades.aplicar(engine)" in trecho)


# ============================================================================
print("\n=== 9. Dois provedores: OpenAI primario, com queda automatica ===")
#
# Ate 01/09/2026 o diagnostico era so Anthropic. A conta ficou sem credito, a
# API passou a responder HTTP 400, e 28 erros sairam com "[IA] Erro da API da
# Anthropic" no lugar do diagnostico — calado, justamente na ferramenta que se
# consulta quando algo ja deu errado. Agora ha um segundo provedor e a queda e
# automatica.

check("a OpenAI e o provedor primario por padrao",
      reporter.provedores_na_ordem()[0] == "openai", reporter.provedores_na_ordem())
check("a Anthropic fica de reserva, na ordem",
      reporter.provedores_na_ordem() == ["openai", "anthropic"],
      reporter.provedores_na_ordem())
check("os dois provedores tem modelo padrao",
      set(reporter.MODELOS_PADRAO) == {"openai", "anthropic"}, reporter.MODELOS_PADRAO)
check("cada provedor le a propria variavel de chave",
      reporter.CHAVES_ENV == {"openai": "OPENAI_API_KEY",
                              "anthropic": "ANTHROPIC_API_KEY"}, reporter.CHAVES_ENV)

# Chave de qualquer um dos dois basta: antes exigia a da Anthropic nominalmente,
# entao com so a da OpenAI configurada o diagnostico ficava desligado sem motivo.
for _k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(_k, None)
check("sem chave nenhuma, o diagnostico fica desligado",
      reporter.ia_de_erros_disponivel() is False)
os.environ["OPENAI_API_KEY"] = "fake-openai"
check("so com a chave da OpenAI ja funciona",
      reporter.ia_de_erros_disponivel() is True)
os.environ.pop("OPENAI_API_KEY", None)
os.environ["ANTHROPIC_API_KEY"] = "fake-anthropic"
check("so com a chave da Anthropic tambem funciona",
      reporter.ia_de_erros_disponivel() is True)

# A queda de verdade. Mocks PROPRIOS desta secao: as anteriores trocam
# `_fake_anthropic.Anthropic` varias vezes, e herdar esse estado torna o teste
# dependente da ordem — foi o que aconteceu na primeira versao deste bloco.
_CHAMADAS_FB = []


class _AnthropicResponde:
    class _Msgs:
        def create(self, **kw):
            _CHAMADAS_FB.append(kw.get("model"))
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text="## Severidade\nAlta")])

    def __init__(self, api_key=None, **kwargs):
        self.messages = self._Msgs()


class _StatusErro(Exception):
    def __init__(self, code=400):
        self.status_code = code


def _montar_openai(quebra: bool):
    mod = types.ModuleType("openai")

    class _Cli:
        def __init__(self, api_key=None, **kwargs):
            if quebra:
                raise _StatusErro(400)      # exatamente o erro de conta sem credito

    mod.OpenAI = _Cli
    return mod


_anthropic_salvo = _fake_anthropic.Anthropic
sys.modules["anthropic"].Anthropic = _AnthropicResponde
sys.modules["openai"] = _montar_openai(quebra=True)
os.environ["OPENAI_API_KEY"] = "fake-openai"
os.environ["ANTHROPIC_API_KEY"] = "fake-anthropic"
if os.path.exists(CAMINHO_LOG_TESTE):
    os.remove(CAMINHO_LOG_TESTE)
_limpar_erros()

handler9 = reporter.AIErrorHandler()
logger9 = logging.getLogger("backend.app._teste_fallback")
logger9.addHandler(handler9)
logger9.propagate = False
logger9.setLevel(logging.ERROR)
logger9.error("erro para exercitar a queda de provedor")
handler9._executor.shutdown(wait=True)

_saida = _conteudo_dos_erros()
check("com a OpenAI falhando, a Anthropic responde", len(_CHAMADAS_FB) == 1, _CHAMADAS_FB)
check("o diagnostico sai, apesar da falha do primario",
      "## Severidade" in _saida, _saida[:200])
check("e o log diz qual provedor respondeu e por que houve queda",
      "[IA via anthropic;" in _saida and "openai:" in _saida, _saida[:300])

# Os dois quebrados: o erro original ainda e gravado, com o motivo de cada um.
_CHAMADAS_FB.clear()
if os.path.exists(CAMINHO_LOG_TESTE):
    os.remove(CAMINHO_LOG_TESTE)
_limpar_erros()


class _AnthropicQuebra:
    def __init__(self, api_key=None, **kwargs):
        raise _StatusErro(429)


sys.modules["anthropic"].Anthropic = _AnthropicQuebra

handler10 = reporter.AIErrorHandler()
logger10 = logging.getLogger("backend.app._teste_ambos_fora")
logger10.addHandler(handler10)
logger10.propagate = False
logger10.setLevel(logging.ERROR)
logger10.error("erro com os dois provedores fora do ar")
handler10._executor.shutdown(wait=True)

_saida2 = _conteudo_dos_erros()
check("com os dois fora, o erro original AINDA e gravado",
      "erro com os dois provedores fora do ar" in _saida2, _saida2[:300])
check("e a linha diz que nenhum provedor respondeu",
      "Nenhum provedor conseguiu responder" in _saida2, _saida2[:400])
check("citando o motivo de cada um", "openai:" in _saida2 and "anthropic:" in _saida2,
      _saida2[:400])

sys.modules["anthropic"].Anthropic = _anthropic_salvo
sys.modules.pop("openai", None)
if os.path.exists(CAMINHO_LOG_TESTE):
    os.remove(CAMINHO_LOG_TESTE)
_limpar_erros()

FONTE_REP = io.open(os.path.join("backend", "app", "core", "ai_error_reporter.py"),
                    encoding="utf-8").read()
check("o aviso de 'sem chave' cita a variavel da OpenAI",
      "OPENAI_API_KEY (ou ANTHROPIC_API_KEY)" in FONTE_REP)
check("as chamadas tem timeout (o handler roda em thread)",
      FONTE_REP.count("timeout=60") == 2, FONTE_REP.count("timeout=60"))


# ============================================================================
print("\n" + "=" * 60)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("FALHAS:")
    for nome in _falhou:
        print(f"  - {nome}")
sys.exit(1 if FALHAS else 0)
