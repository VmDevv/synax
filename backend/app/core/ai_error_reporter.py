"""Diagnóstico automático de erros por IA.

Todo log de nível ERROR/CRITICAL do sistema — falha de auditoria, upload,
integração SIEG/ADN, conexão com o Supabase, o que for — passa por aqui (o
handler é pendurado no logger RAIZ em core/logging_config.py, então pega
qualquer módulo sem precisar tocar em cada try/except).

Para cada erro (com um limite de frequência por assinatura, para não gastar
chamada de IA em erro repetido num loop), manda o texto para a API da
Anthropic com um prompt de "especialista sênior analisando este sistema" e
grava o diagnóstico em `logs/erros_ia.log`.

Sem `ANTHROPIC_API_KEY` configurada, o sistema continua funcionando
normalmente — só não há diagnóstico. A PRIMEIRA vez que isso acontecer, uma
única linha é gravada no log avisando para configurar a chave.

Dados potencialmente sensíveis (CNPJ/CPF, e-mail) são mascarados antes de
sair para a API — best-effort por regex, não uma garantia formal de que nada
sensível jamais vaza; erros deste sistema hoje já evitam ecoar dado de
cliente na mensagem (ver core/uploads_seguros.py e os `except` de
cross_checker.py), então o texto que chega aqui tende a ser só tipo de
exceção + trecho de código, mas o filtro fica como camada extra.
"""
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

logger = logging.getLogger(__name__)

COOLDOWN_SEGUNDOS = 15 * 60  # mesmo erro não gera 2ª chamada de IA em 15min
RELATORIO_PATH = os.environ.get(
    "AI_ERROR_LOG_PATH", os.path.join(os.getcwd(), "logs", "erros_ia.log")
)

# Um arquivo por erro. O `erros_ia.log` continua existindo, mas só para os
# avisos do próprio diagnóstico (ex.: "sem chave configurada") — os erros do
# sistema vão cada um para o seu arquivo aqui dentro.
DIRETORIO_ERROS = os.environ.get(
    "AI_ERROR_DIR", os.path.join(os.path.dirname(RELATORIO_PATH), "erros_ia")
)

# Módulos internos com nome de gente. O logger chega como
# "backend.app.services.audit_engine.cross_checker", que não diz nada para quem
# não escreveu o código — e é essa pessoa que abre a pasta procurando o erro.
ORIGENS_AMIGAVEIS = {
    "cross_checker": "auditoria",
    "sped_parser": "leitura-do-sped",
    "xml_parser": "leitura-do-xml",
    "uploads": "envio-de-arquivos",
    "ia_matcher": "casamento-por-ia",
    "excel_exporter": "planilha",
    "sieg_client": "sieg",
    "sieg_integration": "sieg",
    "adn_client": "nfse-adn",
    "pdf_ia_auditor": "auditoria-de-pdf",
    "certificados": "certificado-digital",
    "auth": "login",
    "admin": "administracao",
    "session": "banco-de-dados",
}

_RE_NOME_ARQUIVO = re.compile(r"[^A-Za-z0-9._-]+")


def _origem_amigavel(nome_logger: str) -> str:
    """Nome legível do módulo que gerou o erro."""
    ultimo = (nome_logger or "").rsplit(".", 1)[-1]
    return ORIGENS_AMIGAVEIS.get(ultimo, ultimo or "sistema")


def _tipo_do_erro(record: logging.LogRecord) -> str:
    """Tipo da exceção (KeyError, OperationalError...) ou 'erro' quando não há."""
    if record.exc_info and record.exc_info[0] is not None:
        return record.exc_info[0].__name__
    return "erro"


def _parte_de_nome(texto: str) -> str:
    """Deixa o pedaço seguro para virar nome de arquivo no Windows."""
    limpo = _RE_NOME_ARQUIVO.sub("-", str(texto or "")).strip("-._")
    return (limpo or "sistema")[:40]

# Dois provedores, e o diagnóstico não pode depender de um só.
#
# Por que isto existe: até 01/09/2026 o diagnóstico era só Anthropic. A conta
# ficou sem crédito e a API passou a devolver HTTP 400 — 28 erros seguidos
# sairam com "[IA] Erro da API da Anthropic" em vez de diagnóstico, e ninguém
# percebeu, porque quem lê `erros_ia.log` vai lá justamente quando algo já deu
# errado. Ferramenta de diagnóstico que morre calada é pior que não ter.
#
# Agora o provedor primário é configurável e existe QUEDA AUTOMÁTICA: se o
# primeiro falhar por crédito, cota ou credencial — as falhas que não adianta
# repetir no mesmo provedor —, o outro é tentado na mesma hora. O diagnóstico
# gravado diz qual respondeu.
PROVEDOR_PADRAO = os.environ.get("AI_ERROR_PROVIDER", "openai").strip().lower()

MODELOS_PADRAO = {
    "openai": os.environ.get("OPENAI_MODELO", "gpt-5.1"),
    "anthropic": "claude-opus-5",
}
CHAVES_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_RE_CNPJ_CPF = re.compile(r"(?<!\d)(\d{11}|\d{14})(?!\d)")

PROMPT_SISTEMA = """\
Você é um especialista sênior em Python, FastAPI, SQLAlchemy e infraestrutura \
de sistemas fiscais brasileiros (SPED, NF-e/CT-e, integrações como Supabase e \
SIEG). Você analisa erros do sistema "Synax — Conferência de Entradas", um \
auditor fiscal multi-tenant que cruza SPED com XML de notas fiscais.

Analise CUIDADOSAMENTE o erro abaixo — mensagem, exceção, traceback e o \
módulo/logger de origem — e explique por que aconteceu e como resolver.

Baseie-se SOMENTE nos dados concretos do erro fornecido. Se a causa não \
estiver clara com o que foi dado, diga isso explicitamente em vez de inventar \
uma causa, e liste as hipóteses mais prováveis em ordem de probabilidade.

QUEM LÊ ISTO PRIMEIRO NÃO É PROGRAMADOR. É o contador responsável pelo \
escritório. Ele precisa saber, antes de tudo: isso atrapalha o trabalho agora? \
Alguma auditoria saiu errada? Ele mesmo consegue resolver, ou tem que chamar \
alguém? Só DEPOIS disso vem a parte técnica, para quem for mexer no código.

Regras de escrita das três primeiras seções:
- Sem jargão de programação. Nada de "exceção", "traceback", "instância", \
"null", "endpoint", "query". Se precisar citar um arquivo ou função, deixe \
para a parte técnica.
- Fale do efeito no trabalho: a auditoria travou, a tela não abriu, a nota não \
foi conferida, o upload não terminou. É isso que a pessoa quer saber.
- Frases curtas. Nada de parágrafo longo.

Responda SEMPRE no formato abaixo, em português:

## Em uma frase
O que deu errado, como você explicaria para alguém no telefone.

## Isso atrapalha o quê
O efeito prático: o que deixou de funcionar, se alguma auditoria ficou \
incompleta ou incorreta, e se dá para continuar trabalhando enquanto não se \
resolve. Se não afetar o resultado de nenhuma auditoria, diga isso com todas \
as letras.

## Gravidade
[Crítica / Alta / Média / Baixa] — e o que a pessoa deve fazer AGORA:
- **resolver sozinho** (e como, em passos simples), ou
- **chamar o programador**, ou
- **não precisa fazer nada** (o sistema se recupera sozinho).

---

## Detalhe técnico
A partir daqui é para quem for mexer no código. Explicação objetiva do erro, \
com o tipo da exceção e o ponto onde ocorreu.

## Causa mais provável
A causa raiz, com base nos dados concretos do erro — não uma suposição \
genérica de manual. Se a causa não estiver clara com o que foi dado, diga isso \
e liste as hipóteses em ordem de probabilidade.

## Como corrigir
Passo a passo acionável. Aponte arquivo e função quando o contexto permitir \
identificá-los.

## Se persistir
O que verificar a seguir, ou onde investigar mais fundo.
"""


def provedores_na_ordem() -> list[str]:
    """O primário configurado primeiro, o outro logo atrás como reserva."""
    primario = PROVEDOR_PADRAO if PROVEDOR_PADRAO in CHAVES_ENV else "openai"
    return [primario] + [p for p in CHAVES_ENV if p != primario]


def ia_de_erros_disponivel() -> bool:
    """Há chave de ALGUM provedor? Sem nenhuma, o diagnóstico fica desligado."""
    return any(os.environ.get(v) for v in CHAVES_ENV.values())


def _redigir(texto: str) -> str:
    """Mascara padrões que parecem e-mail ou CNPJ/CPF antes de sair para a IA."""
    texto = _RE_EMAIL.sub("[email]", texto)
    texto = _RE_CNPJ_CPF.sub("[documento]", texto)
    return texto


def _assinatura(record: logging.LogRecord) -> str:
    """Identifica 'o mesmo erro' para o limite de frequência.

    Módulo + nível + tipo da exceção (se houver) + início da mensagem: erros
    diferentes no mesmo módulo não se confundem, mas o mesmo erro repetindo
    num loop cai sempre na mesma chave.
    """
    tipo_excecao = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else ""
    inicio_msg = record.getMessage()[:120]
    return f"{record.name}|{record.levelname}|{tipo_excecao}|{inicio_msg}"


class AIErrorHandler(logging.Handler):
    """Handler de logging que dispara diagnóstico por IA em segundo plano.

    `emit()` só enfileira — a chamada à API roda numa thread separada, para um
    erro logado durante uma requisição HTTP ou durante o processamento de uma
    auditoria nunca ficar esperando resposta de rede da Anthropic.
    """

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ia-erro")
        self._lock = threading.Lock()
        self._ultima_analise: dict[str, float] = {}
        self._avisou_sem_chave = False

    def emit(self, record: logging.LogRecord) -> None:
        # Evita loop: se a própria chamada à IA falhar e logar um erro (ex.:
        # logger.exception aqui dentro), esse novo record NÃO pode reentrar.
        if record.name == __name__:
            return
        try:
            mensagem_formatada = self.format(record)
            self._executor.submit(self._processar, record, mensagem_formatada)
        except Exception:
            # Handler de log nunca pode derrubar quem gerou o log original.
            self.handleError(record)

    def _processar(self, record: logging.LogRecord, mensagem_formatada: str) -> None:
        try:
            # Basta a chave de UM provedor. Antes exigia a da Anthropic
            # nominalmente: com ela vazia e a da OpenAI preenchida, o
            # diagnóstico ficava desligado sem motivo.
            if not ia_de_erros_disponivel():
                self._avisar_sem_chave_uma_vez()
                return
            chave = ""

            assinatura = _assinatura(record)
            agora = time.monotonic()
            with self._lock:
                ultima = self._ultima_analise.get(assinatura, 0.0)
                if agora - ultima < COOLDOWN_SEGUNDOS:
                    return
                self._ultima_analise[assinatura] = agora

            diagnostico = self._analisar_com_ia(chave, mensagem_formatada)
            self._gravar(record, mensagem_formatada, diagnostico)
        except Exception:
            # Log interno, fora do handler (senão reentra em si mesmo).
            logger.exception("Falha interna no diagnóstico automático de erros")

    def _analisar_com_ia(self, chave: str, mensagem_formatada: str) -> str:
        """Diagnóstico do provedor primário; se ele não puder responder, do outro.

        `chave` chega só por compatibilidade com quem já chamava este método —
        cada provedor lê a própria variável de ambiente em `_chamar_provedor`.
        """
        texto = _redigir(mensagem_formatada)
        motivos = []

        for provedor in provedores_na_ordem():
            resultado, motivo = self._chamar_provedor(provedor, texto)
            if resultado is not None:
                # Só anota a queda quando de fato houve uma: no caminho feliz o
                # diagnóstico sai limpo, sem ruído de infraestrutura.
                if motivos:
                    return (f"[IA via {provedor}; {'; '.join(motivos)}]\n\n{resultado}")
                return resultado
            motivos.append(f"{provedor}: {motivo}")

        return ("[IA] Nenhum provedor conseguiu responder — "
                + "; ".join(motivos or ["nenhuma chave configurada"]) + ".")

    def _chamar_provedor(self, provedor: str, texto: str) -> tuple[str | None, str]:
        """(diagnóstico, "") em caso de sucesso; (None, motivo) quando falhou."""
        chave = os.environ.get(CHAVES_ENV[provedor])
        if not chave:
            return None, "sem chave"

        modelo = os.environ.get("AI_ERROR_MODEL") or MODELOS_PADRAO[provedor]
        try:
            if provedor == "openai":
                from openai import OpenAI
                resposta = OpenAI(api_key=chave, timeout=60).chat.completions.create(
                    model=modelo,
                    messages=[
                        {"role": "system", "content": PROMPT_SISTEMA},
                        {"role": "user", "content": texto},
                    ],
                )
                return (resposta.choices[0].message.content or "").strip(), ""

            import anthropic
            resposta = anthropic.Anthropic(api_key=chave, timeout=60).messages.create(
                model=modelo,
                max_tokens=1200,
                system=PROMPT_SISTEMA,
                messages=[{"role": "user", "content": texto}],
            )
            return "".join(b.text for b in resposta.content if b.type == "text").strip(), ""

        except Exception as err:  # noqa: BLE001 — a causa vira texto, não exceção
            # O nome da classe basta para saber se vale tentar o outro provedor,
            # e serve para os dois SDKs sem importar um por causa do outro.
            nome = type(err).__name__
            status = getattr(err, "status_code", None)
            if nome not in ("AuthenticationError", "PermissionDeniedError",
                            "RateLimitError", "APIStatusError", "APIConnectionError",
                            "APITimeoutError", "BadRequestError", "NotFoundError"):
                logger.exception("Falha inesperada ao consultar a IA de diagnóstico (%s)",
                                 provedor)
            return None, f"{nome}" + (f" HTTP {status}" if status else "")

    def _avisar_sem_chave_uma_vez(self) -> None:
        with self._lock:
            if self._avisou_sem_chave:
                return
            self._avisou_sem_chave = True
        self._escrever_no_arquivo(
            "Diagnóstico automático de erros está DESATIVADO: configure "
            "OPENAI_API_KEY (ou ANTHROPIC_API_KEY) no arquivo .env para habilitar. "
            "(Este aviso aparece só uma vez por execução do servidor.)"
        )

    def _gravar(self, record: logging.LogRecord, mensagem_formatada: str, diagnostico: str) -> None:
        """UM ARQUIVO POR ERRO, em logs/erros_ia/.

        Antes tudo ia para um `erros_ia.log` só, que passou de 180 KB e virou um
        paredão: para achar o erro de hoje era preciso rolar por todos os
        anteriores. Agora cada erro tem o seu arquivo, e o NOME já diz o que é —
        data, hora, de onde veio e o tipo do problema —, então dá para escolher
        qual abrir só olhando a pasta, ordenada por data.
        """
        agora = datetime.now()
        nome = "{data}__{origem}__{tipo}.md".format(
            data=agora.strftime("%Y-%m-%d_%H-%M-%S"),
            origem=_parte_de_nome(_origem_amigavel(record.name)),
            tipo=_parte_de_nome(_tipo_do_erro(record)),
        )

        conteudo = (
            f"# {_origem_amigavel(record.name)} — {_tipo_do_erro(record)}\n\n"
            f"**Quando:** {agora.strftime('%d/%m/%Y às %H:%M:%S')}  \n"
            f"**Onde:** {record.name}  \n"
            f"**Nível:** {record.levelname}\n\n"
            f"---\n\n"
            f"{diagnostico}\n\n"
            f"---\n\n"
            f"## Mensagem original do sistema\n\n"
            f"```\n{mensagem_formatada}\n```\n"
        )

        # Dois erros no mesmo segundo não podem sobrescrever um ao outro. O
        # handler processa em DUAS threads, então "verificar se existe e depois
        # gravar" é uma corrida: as duas veem o arquivo livre e as duas escrevem
        # no mesmo nome. `O_CREAT | O_EXCL` resolve no nível do sistema — quem
        # perder a corrida recebe FileExistsError e tenta o sufixo seguinte.
        try:
            os.makedirs(DIRETORIO_ERROS, exist_ok=True)
            for tentativa in range(1, 50):
                alvo = nome if tentativa == 1 else f"{nome[:-3]}_{tentativa}.md"
                try:
                    fd = os.open(os.path.join(DIRETORIO_ERROS, alvo),
                                 os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    continue
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(conteudo)
                return
            logger.error("Não foi possível achar nome livre para o diagnóstico em %s",
                         DIRETORIO_ERROS)
        except OSError:
            logger.exception("Não foi possível gravar o diagnóstico em %s", DIRETORIO_ERROS)

    def _escrever_no_arquivo(self, texto: str) -> None:
        try:
            os.makedirs(os.path.dirname(RELATORIO_PATH), exist_ok=True)
            with open(RELATORIO_PATH, "a", encoding="utf-8") as f:
                f.write(texto if texto.startswith("\n") else f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] {texto}\n")
        except OSError:
            logger.exception("Não foi possível gravar em %s", RELATORIO_PATH)
