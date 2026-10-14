"""Envio de e-mail pelo servidor do escritório (MCO2).

Até 25/09/2026 o Synax só REDIGIA e-mail (o gerador de comunicado ao cliente
monta o texto e a pessoa copia). Este é o primeiro ponto que envia de verdade,
para o informativo da coleta automática de NFS-e.

Duas regras:

1. **Falha de envio vira frase, não traceback.** Quem lê é o admin na tela de
   histórico, e "SMTPAuthenticationError(535, b'5.7.3')" não diz o que fazer.
   `FalhaEnvio` carrega a mensagem que diz.

2. **Timeout curto.** O envio roda dentro da coleta agendada; um servidor de
   e-mail que não responde não pode segurar a thread do agendador por minutos.
"""
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

TEMPO_LIMITE_SEG = 30
CABECALHO_PROPRIO = "X-Synax"


class FalhaEnvio(Exception):
    """O e-mail não saiu. A mensagem é apresentável."""


def _contexto_tls(verificar: bool) -> ssl.SSLContext:
    contexto = ssl.create_default_context()
    if not verificar:
        # Servidor interno com certificado próprio (comum em Exchange de rede
        # local). Desligar a verificação é escolha explícita na tela, com aviso
        # — nunca o padrão.
        contexto.check_hostname = False
        contexto.verify_mode = ssl.CERT_NONE
    return contexto


def montar_mensagem(config: dict, destinatarios: list[str], assunto: str,
                    html: str, texto: str, anexos=()) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = formataddr((config.get("remetente_nome") or "Synax", config["remetente"]))
    msg["To"] = ", ".join(destinatarios)
    dominio = (config["remetente"].split("@", 1) + ["synax.local"])[1]
    msg["Message-ID"] = make_msgid(domain=dominio)
    # O informativo costuma ir para a mesma caixa que o sistema LÊ. Sem esta
    # marca, o resumo da manhã resumiria o resumo de ontem.
    msg[CABECALHO_PROPRIO] = "informativo"
    msg.set_content(texto or "")
    if html:
        msg.add_alternative(html, subtype="html")
    for nome, conteudo, tipo in anexos:
        principal, _, sub = (tipo or "application/octet-stream").partition("/")
        msg.add_attachment(conteudo, maintype=principal, subtype=sub or "octet-stream",
                           filename=nome)
    return msg


def enviar(config: dict, destinatarios: list[str], assunto: str,
           html: str = "", texto: str = "", anexos=()) -> None:
    """Envia. Levanta `FalhaEnvio` com o motivo em português."""
    if not config.get("host"):
        raise FalhaEnvio("Servidor de e-mail não configurado (Administração → Configurações).")
    if not config.get("remetente"):
        raise FalhaEnvio("Endereço de remetente não configurado (Administração → Configurações).")
    if not destinatarios:
        raise FalhaEnvio("Nenhum destinatário informado.")

    msg = montar_mensagem(config, destinatarios, assunto, html, texto, anexos)
    host, porta = config["host"], int(config.get("porta") or 25)
    onde = f"{host}:{porta}"
    contexto = _contexto_tls(config.get("verificar_tls", True))
    try:
        if config.get("seguranca") == "ssl":
            conexao = smtplib.SMTP_SSL(host, porta, timeout=TEMPO_LIMITE_SEG, context=contexto)
        else:
            conexao = smtplib.SMTP(host, porta, timeout=TEMPO_LIMITE_SEG)
        with conexao as smtp:
            smtp.ehlo()
            if config.get("seguranca") == "starttls":
                smtp.starttls(context=contexto)
                smtp.ehlo()
            if config.get("usuario"):
                smtp.login(config["usuario"], config.get("senha") or "")
            recusados = smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as err:
        raise FalhaEnvio(f"O servidor {onde} recusou o usuário ou a senha.") from err
    except smtplib.SMTPRecipientsRefused as err:
        raise FalhaEnvio("O servidor recusou os destinatários: "
                         + ", ".join(sorted(err.recipients))) from err
    except smtplib.SMTPSenderRefused as err:
        raise FalhaEnvio(f"O servidor recusou o remetente {config['remetente']} — "
                         "confira se a conta pode enviar por ele.") from err
    except smtplib.SMTPNotSupportedError as err:
        raise FalhaEnvio(f"O servidor {onde} não aceita a segurança escolhida "
                         "(STARTTLS/SSL). Tente outra opção em Configurações.") from err
    except ssl.SSLCertVerificationError as err:
        raise FalhaEnvio(f"O certificado do servidor {onde} não foi reconhecido. Se for um "
                         "servidor interno com certificado próprio, desmarque "
                         "\"verificar certificado\" em Configurações.") from err
    except ssl.SSLError as err:
        raise FalhaEnvio(f"Falha de criptografia com {onde} ({err.reason or 'SSL'}). "
                         "Confira a porta e a segurança escolhidas.") from err
    except (socket.gaierror, ConnectionRefusedError, TimeoutError, socket.timeout,
            OSError) as err:
        raise FalhaEnvio(f"Não foi possível conectar a {onde}. Confira o endereço, a "
                         "porta e se o servidor está na rede.") from err
    except smtplib.SMTPException as err:
        raise FalhaEnvio(f"O servidor {onde} recusou o envio: {err}") from err
    if recusados:
        raise FalhaEnvio("Parte dos destinatários foi recusada: " + ", ".join(sorted(recusados)))
