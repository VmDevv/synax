"""Conversa com o servidor IMAP — e SÓ LÊ.

A caixa é a geral do escritório, a mesma que os funcionários abrem no
Thunderbird. No IMAP, "lido", "movido" e "apagado" ficam gravados NO SERVIDOR:
se o sistema marcasse o e-mail como lido, ele apareceria lido no Thunderbird de
todo mundo, e alguém deixaria de abrir o que nunca viu. Por isso (pedido do
Sandro em 28/09/2026, "sem mexer nos e-mails"):

1. A pasta é aberta com `select(readonly=True)` — o comando EXAMINE do
   protocolo. Nesse modo o servidor não grava marca nenhuma.
2. O conteúdo é pedido com `BODY.PEEK[]`, que não liga a marca \\Seen nem numa
   pasta aberta para escrita. É o cinto e o suspensório: um servidor que
   ignorasse o EXAMINE ainda assim não veria o e-mail como lido.
3. Este módulo não tem STORE, COPY, MOVE nem EXPUNGE — e a suíte confere que
   nenhum desses comandos sai daqui.

O que já foi processado é controlado no banco do Synax (UIDVALIDITY + UID +
Message-ID), nunca por marca no e-mail.
"""
import email.utils
import imaplib
import re
import socket
import ssl
from datetime import datetime, timezone

TEMPO_LIMITE_SEG = 60
# Um e-mail acima disto não é baixado: fica registrado com o motivo. O maior
# pacote que a tela de auditoria aceita é bem maior, mas quem manda 50 MB por
# e-mail já estourou o limite de quase todo servidor de saída.
TAMANHO_MAXIMO = 50 * 1024 * 1024

_MESES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class FalhaLeitura(Exception):
    """A caixa não pôde ser lida. A mensagem é apresentável."""


def _contexto_tls(verificar: bool) -> ssl.SSLContext:
    contexto = ssl.create_default_context()
    if not verificar:
        # Servidor da rede interna com certificado próprio — escolha explícita
        # na tela, como no SMTP. Nunca o padrão.
        contexto.check_hostname = False
        contexto.verify_mode = ssl.CERT_NONE
    return contexto


def conectar(config: dict):
    """Abre a conexão, faz login e seleciona a pasta SÓ PARA LEITURA.

    Devolve (conexão, UIDVALIDITY). Levanta `FalhaLeitura` com o motivo.
    """
    host, porta = config.get("host"), int(config.get("porta") or 993)
    if not host:
        raise FalhaLeitura("Servidor IMAP não configurado (Administração → Configurações).")
    if not config.get("usuario") or not config.get("senha"):
        raise FalhaLeitura("Usuário ou senha da caixa de e-mail não configurados.")
    onde = f"{host}:{porta}"
    contexto = _contexto_tls(config.get("verificar_tls", True))
    try:
        if config.get("seguranca") == "ssl":
            conexao = imaplib.IMAP4_SSL(host, porta, ssl_context=contexto,
                                        timeout=TEMPO_LIMITE_SEG)
        else:
            conexao = imaplib.IMAP4(host, porta, timeout=TEMPO_LIMITE_SEG)
            if config.get("seguranca") == "starttls":
                conexao.starttls(ssl_context=contexto)
    except ssl.SSLCertVerificationError:
        raise FalhaLeitura(f"O certificado de {onde} não foi reconhecido. Se o servidor usa "
                           "certificado próprio da rede interna, desmarque \"Verificar o "
                           "certificado\" na leitura da caixa.")
    except (socket.timeout, TimeoutError):
        raise FalhaLeitura(f"{onde} não respondeu em {TEMPO_LIMITE_SEG}s.")
    except (OSError, imaplib.IMAP4.error) as err:
        # "220 ... ready" é a saudação do SMTP: a porta é a de ENVIO. Foi o
        # primeiro erro real (28/09/2026, porta 465 copiada do cartão do SMTP).
        if "b'220" in str(err) or 'b"220' in str(err):
            raise FalhaLeitura(f"Em {onde} quem responde é o servidor de ENVIO (SMTP), não o "
                               "de leitura. Use a porta 993 com SSL/TLS (ou 143 com STARTTLS).")
        raise FalhaLeitura(f"Não foi possível conectar a {onde}: {err}")

    try:
        conexao.login(config["usuario"], config["senha"])
    except imaplib.IMAP4.error:
        _fechar(conexao)
        raise FalhaLeitura("O servidor recusou o usuário ou a senha da caixa de e-mail.")

    pasta = config.get("pasta") or "INBOX"
    try:
        tipo, _dados = conexao.select(_nome_pasta(pasta), readonly=True)
    except imaplib.IMAP4.error as err:
        _fechar(conexao)
        raise FalhaLeitura(f"Não foi possível abrir a pasta \"{pasta}\": {err}")
    if tipo != "OK":
        _fechar(conexao)
        raise FalhaLeitura(f"A pasta \"{pasta}\" não existe nesta caixa.")
    uidvalidity = ""
    try:
        resposta = conexao.response("UIDVALIDITY")[1]
        if resposta and resposta[0]:
            uidvalidity = resposta[0].decode() if isinstance(resposta[0], bytes) else str(resposta[0])
    except Exception:                                     # noqa: BLE001
        uidvalidity = ""
    return conexao, uidvalidity or "0"


def _nome_pasta(pasta: str) -> str:
    # Nome com espaço precisa de aspas no protocolo; INBOX não.
    return f'"{pasta}"' if " " in pasta and not pasta.startswith('"') else pasta


def _fechar(conexao) -> None:
    try:
        conexao.logout()
    except Exception:                                     # noqa: BLE001
        pass


def fechar(conexao) -> None:
    """Sai da caixa. Sem CLOSE: numa pasta aberta para escrita o CLOSE apaga o
    que estiver marcado para exclusão — e isso não é decisão deste sistema."""
    _fechar(conexao)


def data_imap(quando: datetime) -> str:
    """ "28-Sep-2026" — o formato do SEARCH SINCE, em inglês sempre (não
    depende do idioma da máquina, que `strftime("%b")` seguiria)."""
    return f"{quando.day:02d}-{_MESES[quando.month - 1]}-{quando.year}"


def uids_novos(conexao, depois_do_uid: int | None, desde: datetime) -> list[int]:
    """UIDs a processar, em ordem.

    Com um UID conhecido, pede os posteriores a ele. Atenção à regra do
    protocolo: "UID 124:*" devolve o ÚLTIMO e-mail mesmo quando ele é o 123 —
    o `*` vale o maior UID existente —, por isso o filtro depois. Sem UID
    conhecido (primeira leitura, ou a pasta foi recriada), vale a data.
    """
    if depois_do_uid:
        tipo, dados = conexao.uid("SEARCH", None, f"UID {depois_do_uid + 1}:*")
    else:
        tipo, dados = conexao.uid("SEARCH", None, "SINCE", data_imap(desde))
    if tipo != "OK":
        raise FalhaLeitura("O servidor recusou a busca por e-mails novos.")
    uids = []
    for bloco in dados or []:
        for parte in (bloco or b"").split():
            if parte.isdigit():
                uid = int(parte)
                if not depois_do_uid or uid > depois_do_uid:
                    uids.append(uid)
    return sorted(set(uids))


def tamanho(conexao, uid: int) -> int | None:
    tipo, dados = conexao.uid("FETCH", str(uid), "(RFC822.SIZE)")
    if tipo != "OK":
        return None
    for item in dados or []:
        texto = item[0] if isinstance(item, tuple) else item
        if isinstance(texto, bytes):
            achado = re.search(rb"RFC822\.SIZE (\d+)", texto)
            if achado:
                return int(achado.group(1))
    return None


def baixar(conexao, uid: int) -> bytes | None:
    """O e-mail inteiro, sem marcá-lo como lido (`BODY.PEEK[]`)."""
    tipo, dados = conexao.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if tipo != "OK":
        return None
    for item in dados or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return None


def data_do_cabecalho(valor) -> datetime | None:
    """Cabeçalho Date -> datetime UTC ingênuo (como o resto do banco)."""
    if not valor:
        return None
    try:
        quando = email.utils.parsedate_to_datetime(str(valor))
    except (TypeError, ValueError, IndexError):
        return None
    if quando is None:
        return None
    if quando.tzinfo is not None:
        quando = quando.astimezone(timezone.utc).replace(tzinfo=None)
    return quando
