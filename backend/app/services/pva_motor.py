"""O cliente do motor do PVA da EFD ICMS/IPI — o PVA oficial da Receita, sem tela.

Pedido do Sandro em 23/10/2026: "vamos alterar o validador que temos no Synax por esse novo original". O validador
em Python (`services/sped_validador`) reimplementava as regras do PVA e corria atrás dele — o gabarito do LIESA
jul/2026 mostrou 3 regras faltando. O motor (`Desktop\\Projetos\\pva-motor`, fora do Synax) é o próprio PVA 6.1.1
dirigido por programa: na fase 0 deu o mesmo resultado da tela, campo a campo.

Mesma linha do Lucro Presumido: a tela é aqui, o motor é o programa de lá. O Synax só fala HTTP com ele — nunca
carrega o PVA nem toca o MySQL embutido dele (ao subir, o PVA derruba o MySQL que estiver na porta dele).

Quatro regras:

- **Só 127.0.0.1.** O motor não tem senha e recebe o SPED inteiro do cliente: endereço fora desta máquina é recusado
  aqui, antes de abrir conexão, mesmo que alguém configure `PVA_MOTOR_URL` assim.
- **O corpo vai com Content-Length, nunca em pedaços** (o servidor do motor recusa `chunked` com 411).
- **Um arquivo por vez** (`uso_exclusivo`): o PVA é um só por processo. Quem usa o motor — a fila da tela e a
  rotina automática — passa por esta trava.
- **Motor fora do ar é exceção com frase para a tela**, nunca resultado vazio: "sem erro" só sai quando o PVA disse.

Assinar (`assinar_arquivo`, desde 23/10/2026) é o `POST /assinar` do motor: valida e, sem erro, assina como a tela,
com o e-CNPJ que o admin cadastrou em Configurações. O certificado e a senha vão no corpo do pedido, a 127.0.0.1, e
o motor não guarda nenhum dos dois. Certificado recusado (senha errada, vencido, fora da ICP-Brasil) é
`AssinaturaRecusada`, com a frase do motor. **Transmitir não existe aqui**: o envio à Receita não está ligado.

Ligar e desligar é do Painel Dev (sobe junto com o Synax; desliga sempre pelo `POST /encerrar` — matar o processo
deixa o mysqld órfão segurando a porta). O Synax não liga o motor.
"""
import base64
import http.client
import json
import logging
import os
import socket
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

URL_PADRAO = "http://127.0.0.1:8095"
HOSTS_LOCAIS = frozenset({"127.0.0.1", "localhost", "::1"})
TEMPO_SAUDE_S = 3
TEMPO_CONEXAO_S = 5
# O maior arquivo da fase 0 (3,2 MB, 29 mil linhas) levou 33 s. O teto é largo de propósito: estourar aqui não para o
# PVA, que continua ocupado com o arquivo, e o próximo da fila só esperaria mais.
TEMPO_VALIDAR_S = 30 * 60
BLOCO = 1 << 16

# O PVA marca a escrituração assim quando ela passa. EM_EDICAO = reprovada; sem estado = não integrada.
ESTADOS_APROVADOS = frozenset({"VALIDADA", "GERADA_PARA_ENTREGA"})

COMANDO_LIGAR = r"motor\motor.cmd servir --porta 8095"

_trava = threading.Lock()


class MotorIndisponivel(Exception):
    """Não houve resposta do PVA. `mensagem` vai para a tela."""

    def __init__(self, mensagem: str):
        self.mensagem = mensagem
        super().__init__(mensagem)


class MotorForaDoAr(MotorIndisponivel):
    """Ninguém atende no endereço do motor (desligado, ou caiu no meio)."""


class EnderecoNaoLocal(MotorIndisponivel):
    """`PVA_MOTOR_URL` aponta para fora desta máquina."""


class RespostaInesperada(MotorIndisponivel):
    """O motor atendeu, mas fora do contrato (HTTP de erro, JSON quebrado, tempo esgotado)."""


class AssinaturaRecusada(Exception):
    """O motor recusou o certificado (ou o arquivo já vinha assinado) antes de validar. `mensagem` vai para a tela."""

    def __init__(self, mensagem: str):
        self.mensagem = mensagem
        super().__init__(mensagem)


def url() -> str:
    return (os.getenv("PVA_MOTOR_URL") or URL_PADRAO).strip().rstrip("/")


def endereco() -> tuple[str, int]:
    partes = urlsplit(url())
    host = (partes.hostname or "").lower()
    if partes.scheme != "http" or host not in HOSTS_LOCAIS:
        raise EnderecoNaoLocal(
            f"O endereço do motor do PVA ({url()}) não é desta máquina. O motor não tem senha e recebe o SPED inteiro "
            "do cliente: ele só é chamado em http://127.0.0.1. Corrija PVA_MOTOR_URL no .env do servidor.")
    try:
        porta = partes.port or 80
    except ValueError as exc:
        raise EnderecoNaoLocal(f"A porta do motor do PVA em PVA_MOTOR_URL ({url()}) não é um número.") from exc
    return host, porta


def _fora_do_ar() -> MotorForaDoAr:
    return MotorForaDoAr(
        f"O motor do PVA não está respondendo em {url()}. Nada foi validado. Ele roda no servidor do Synax e precisa "
        "ser ligado lá — avise quem cuida do servidor.")


def _ler_json(resp) -> dict:
    corpo = resp.read()
    try:
        dados = json.loads(corpo.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        dados = None
    if not isinstance(dados, dict):
        logger.error("Motor do PVA respondeu fora do contrato (HTTP %s): %r", resp.status, corpo[:2000])
        raise RespostaInesperada("O motor do PVA respondeu fora do formato combinado — o motivo está no log do "
                                 "servidor. Nada foi validado.")
    if resp.status != 200:
        raise RespostaInesperada(f"O motor do PVA recusou o pedido (HTTP {resp.status}): "
                                 f"{dados.get('erro') or 'sem motivo'}. Nada foi validado.")
    return dados


def saude(timeout: float = TEMPO_SAUDE_S) -> dict:
    """`{"ok": True, "versaoPva": "6.1.1"}`, ou a exceção que diz por que não."""
    host, porta = endereco()
    conn = http.client.HTTPConnection(host, porta, timeout=timeout)
    try:
        conn.request("GET", "/saude")
        dados = _ler_json(conn.getresponse())
    except (ConnectionError, socket.timeout, OSError, http.client.HTTPException) as exc:
        raise _fora_do_ar() from exc
    finally:
        conn.close()
    if dados.get("ok") is not True:
        raise RespostaInesperada("O motor do PVA atendeu, mas disse que não está pronto. Nada foi validado.")
    return dados


@contextmanager
def uso_exclusivo():
    """Um arquivo por vez no PVA. O servidor do motor também serializa, mas esperar aqui deixa o tempo de cada
    arquivo ser só o dele, e o teto de `TEMPO_VALIDAR_S` não estoura por fila de outro."""
    with _trava:
        yield


# ------------------------------------------------------------------ resultado

@dataclass
class Item:
    """Uma inconsistência, como o PVA a grava na tabela dele."""
    tipo: str                       # "E" (erro) ou "A" (advertência)
    codigo: str                     # MSG_VL_ICMS_ANALIT
    descricao: str                  # o texto oficial do PVA
    registro: str = ""
    campo: str = ""
    linha: int | None = None        # None quando a regra é do arquivo, não de uma linha (o PVA manda 0)
    valor: str = ""
    esperado: str = ""
    conteudo: str = ""

    @property
    def advertencia(self) -> bool:
        return self.tipo == "A"


@dataclass
class Resultado:
    versao_pva: str
    estado: str | None
    valido: bool
    integrado: bool | None
    itens: list[Item]
    mensagens: list[str] = field(default_factory=list)
    falha: str | None = None
    leiaute: str | None = None
    assinatura_removida: bool = False
    ms: int | None = None

    @property
    def erros(self) -> int:
        return sum(1 for i in self.itens if i.tipo == "E")

    @property
    def advertencias(self) -> int:
        return sum(1 for i in self.itens if i.tipo == "A")

    @property
    def aprovado(self) -> bool:
        # As quatro juntas: um `valido` com erro na lista ou com falha no relatório nunca vira tela verde.
        return (self.valido is True and self.estado in ESTADOS_APROVADOS and self.erros == 0
                and not self.falha)

    @property
    def veredito(self) -> str:
        """aprovado | reprovado (integrou com erro) | nao_integrado (erros de importação) | recusado (nem importou)."""
        if self.aprovado:
            return "aprovado"
        if self.integrado is True:
            return "reprovado"
        if self.itens:
            return "nao_integrado"
        return "recusado"

    def por_codigo(self) -> list[tuple[str, int]]:
        contagem: dict[str, int] = {}
        for i in self.itens:
            contagem[i.codigo] = contagem.get(i.codigo, 0) + 1
        return sorted(contagem.items(), key=lambda kv: (-kv[1], kv[0]))

    def como_dict(self) -> dict:
        return {**asdict(self), "erros": self.erros, "advertencias": self.advertencias,
                "aprovado": self.aprovado, "veredito": self.veredito}

    @classmethod
    def de_dict(cls, d: dict) -> "Resultado":
        """Relê o que `como_dict` gravou no disco."""
        return cls(versao_pva=d.get("versao_pva") or "", estado=d.get("estado"), valido=d.get("valido") is True,
                   integrado=d.get("integrado"), itens=[Item(**i) for i in d.get("itens") or []],
                   mensagens=list(d.get("mensagens") or []), falha=d.get("falha"), leiaute=d.get("leiaute"),
                   assinatura_removida=bool(d.get("assinatura_removida")), ms=d.get("ms"))


def _texto(v) -> str:
    return "" if v is None else str(v)


def _descricao(codigo: str, descricao) -> str:
    # Algumas mensagens trazem a chave do resumo grudada ("...no bloco 9.##MSG_..._RESUMO"); a tela do PVA não mostra.
    texto = _texto(descricao).split("##", 1)[0].strip()
    return texto or codigo


def ler_resposta(dados: dict) -> Resultado:
    """A resposta do `POST /validar`, conferida. Fora do contrato vira `RespostaInesperada`."""
    erros = dados.get("erros")
    if not isinstance(erros, list) or not isinstance(dados.get("valido"), bool):
        logger.error("Motor do PVA sem 'erros'/'valido' na resposta: %r", str(dados)[:2000])
        raise RespostaInesperada("O motor do PVA respondeu sem o veredito ou sem a lista de erros — o motivo está no "
                                 "log do servidor. Nada foi validado.")
    itens = []
    for e in erros:
        if not isinstance(e, dict):
            continue
        tipo = _texto(e.get("tipo")).upper()[:1]
        if tipo not in ("E", "A"):
            # Tipo desconhecido conta como erro: na dúvida, o arquivo não passa.
            logger.warning("Motor do PVA: inconsistência de tipo %r tratada como erro", e.get("tipo"))
            tipo = "E"
        codigo = _texto(e.get("codigo"))
        try:
            linha = int(e.get("linha") or 0) or None
        except (TypeError, ValueError):
            linha = None
        itens.append(Item(tipo=tipo, codigo=codigo, descricao=_descricao(codigo, e.get("descricao")),
                          registro=_texto(e.get("registro")), campo=_texto(e.get("campo")), linha=linha,
                          valor=_texto(e.get("valor")), esperado=_texto(e.get("esperado")),
                          conteudo=_texto(e.get("conteudo"))))
    resultado = Resultado(
        versao_pva=_texto(dados.get("versaoPva")), estado=dados.get("estado") or None, valido=dados["valido"],
        integrado=dados.get("integrado") if isinstance(dados.get("integrado"), bool) else None, itens=itens,
        mensagens=[_texto(m) for m in dados.get("mensagens") or []], falha=dados.get("falha") or None,
        leiaute=dados.get("leiaute") or None, assinatura_removida=dados.get("assinaturaRemovida") is True,
        ms=dados.get("ms") if isinstance(dados.get("ms"), int) else None)
    if (dados.get("totalErros"), dados.get("totalAdvertencias")) != (resultado.erros, resultado.advertencias):
        logger.warning("Motor do PVA: totais %s/%s diferentes da lista (%s/%s); vale a lista",
                       dados.get("totalErros"), dados.get("totalAdvertencias"),
                       resultado.erros, resultado.advertencias)
    return resultado


@dataclass
class Assinatura:
    """O que o motor disse da assinatura. `arquivo` são os bytes do SPED assinado (nunca vão para o JSON gravado)."""
    assinado: bool
    motivo: str = ""
    titular: str = ""               # "emitido para" do e-CNPJ
    valido_ate: str = ""
    arquivo: bytes | None = None

    def como_dict(self) -> dict:
        return {"assinado": self.assinado, "motivo": self.motivo, "titular": self.titular,
                "valido_ate": self.valido_ate}


def ler_assinatura(dados: dict) -> Assinatura:
    a = dados.get("assinatura")
    if not isinstance(a, dict):
        logger.error("Motor do PVA sem 'assinatura' na resposta do /assinar: %r", str(dados)[:500])
        raise RespostaInesperada("O motor do PVA respondeu sem dizer se assinou — o motivo está no log do servidor.")
    cert = a.get("certificado") if isinstance(a.get("certificado"), dict) else {}
    arquivo = None
    if a.get("assinado") is True:
        try:
            arquivo = base64.b64decode(dados.get("arquivoAssinado") or "", validate=True)
        except (ValueError, TypeError):
            arquivo = None
    # Assinado sem o arquivo de volta não serve para nada: vale como não assinado, com o motivo.
    assinado = a.get("assinado") is True and bool(arquivo)
    motivo = _texto(a.get("motivo")) or ("" if assinado else "o motor não devolveu o arquivo assinado")
    return Assinatura(assinado=assinado, motivo=motivo, titular=_texto(cert.get("emitidoPara")),
                      valido_ate=_texto(cert.get("validoAte")), arquivo=arquivo if assinado else None)


def _enviar(rota: str, caminho: str, timeout: float, prefixo: bytes = b"", cabecalhos: dict | None = None,
            acao: str = "validação") -> dict:
    """POST do TXT (com um prefixo opcional no corpo) ao motor. Devolve o JSON; 422 vira `AssinaturaRecusada`."""
    host, porta = endereco()
    tamanho = len(prefixo) + os.path.getsize(caminho)
    conn = http.client.HTTPConnection(host, porta, timeout=TEMPO_CONEXAO_S)
    try:
        try:
            conn.connect()
        except (ConnectionError, socket.timeout, OSError) as exc:
            raise _fora_do_ar() from exc
        conn.sock.settimeout(timeout)
        try:
            # putrequest + Content-Length à mão: assim o http.client nunca decide mandar em pedaços.
            conn.putrequest("POST", rota)
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(tamanho))
            for nome, valor in (cabecalhos or {}).items():
                conn.putheader(nome, valor)
            conn.endheaders()
            if prefixo:
                conn.send(prefixo)
            with open(caminho, "rb") as f:
                while bloco := f.read(BLOCO):
                    conn.send(bloco)
            resp = conn.getresponse()
            if resp.status == 422:
                corpo = resp.read()
                try:
                    erro = json.loads(corpo.decode("utf-8")).get("erro")
                except (UnicodeDecodeError, ValueError, AttributeError):
                    erro = None
                raise AssinaturaRecusada(_texto(erro) or "o motor recusou o certificado")
            return _ler_json(resp)
        except socket.timeout as exc:
            raise RespostaInesperada(f"O PVA não terminou a {acao} em {int(timeout // 60)} minutos. Nada foi "
                                     "aprovado; tente de novo mais tarde ou avise quem cuida do servidor.") from exc
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            raise MotorForaDoAr(f"O motor do PVA parou de responder no meio da {acao}. Nada foi aprovado; tente "
                                "de novo quando ele voltar.") from exc
    finally:
        conn.close()


def validar_arquivo(caminho: str, timeout: float = TEMPO_VALIDAR_S) -> Resultado:
    """Manda o TXT cru para o PVA e devolve o veredito dele. Chame dentro de `uso_exclusivo()`."""
    return ler_resposta(_enviar("/validar", caminho, timeout))


def assinar_arquivo(caminho: str, pfx: bytes, senha: str,
                    timeout: float = TEMPO_VALIDAR_S) -> tuple[Resultado, Assinatura]:
    """Valida e, sem erro, assina com o e-CNPJ. Chame dentro de `uso_exclusivo()`.

    O corpo é o .pfx, a senha (UTF-8) e o TXT, nessa ordem; os tamanhos dos dois primeiros vão nos cabeçalhos.
    """
    senha_bytes = (senha or "").encode("utf-8")
    dados = _enviar("/assinar", caminho, timeout, prefixo=pfx + senha_bytes,
                    cabecalhos={"X-Pfx-Bytes": str(len(pfx)), "X-Senha-Bytes": str(len(senha_bytes))},
                    acao="validação e assinatura")
    resultado = ler_resposta(dados)
    assinatura = ler_assinatura(dados)
    if assinatura.assinado and not resultado.aprovado:
        # O motor só assina o que o PVA aprovou; se as duas coisas discordarem, não entregamos o arquivo.
        logger.error("Motor do PVA assinou um arquivo que não saiu aprovado (estado %s)", resultado.estado)
        assinatura = Assinatura(assinado=False, motivo="o PVA não aprovou o arquivo; a assinatura foi descartada",
                                titular=assinatura.titular, valido_ate=assinatura.valido_ate)
    return resultado, assinatura
