"""Configurações do escritório — leitura, gravação e o cofre SIEG.

Antes disto, as credenciais do SIEG só existiam no `.env` da máquina onde o
servidor roda. Funciona, mas troca de chave virava tarefa de quem tem acesso ao
servidor, e num VPS isso é um chamado. A tabela `app_settings` põe a mesma
configuração na tela do admin, com trilha de quem mudou.

Três regras que este módulo aplica:

1. **Credencial é gravada CIFRADA** (`core/security.py`, a mesma cifra dos
   certificados). Quem abrir a tabela — backup, dump, um SELECT de suporte —
   não lê a chave do cofre fiscal de todas as empresas da carteira.

2. **O banco manda; o `.env` é o que sobra.** A ordem é essa e não a inversa:
   quem trocou a chave pela tela espera que a troca valha, e uma variável de
   ambiente esquecida no servidor sobrescrevendo a tela seria um bug
   impossível de enxergar. Sem nada no banco, o `.env` continua valendo — foi
   assim que o sistema funcionou até aqui e nenhuma instalação precisa migrar
   para continuar de pé.

3. **A chave nunca volta para a tela.** A tela mostra máscara e os quatro
   últimos caracteres, o bastante para conferir qual chave está lá. Devolver o
   valor num `<input value="...">` a exporia no HTML para qualquer um que
   abrisse o inspetor — inclusive para quem só passou pela mesa.
"""
from __future__ import annotations

import logging
import os

from sqlalchemy.orm import Session

from ..core.security import decrypt_data, encrypt_data
from ..domain.models.configuracao import AppSetting

logger = logging.getLogger(__name__)

SIEG_EMAIL = "sieg_email"
SIEG_API_KEY = "sieg_api_key"

# Servidor de e-mail do escritório (o MCO2), usado pelo informativo da coleta
# automática de NFS-e. Mesmas regras do SIEG: banco primeiro, `.env` de reserva,
# senha cifrada e nunca devolvida para a tela.
SMTP_HOST = "smtp_host"
SMTP_PORTA = "smtp_porta"
SMTP_SEGURANCA = "smtp_seguranca"          # starttls | ssl | nenhuma
SMTP_VERIFICAR_TLS = "smtp_verificar_tls"  # "1" | "0"
SMTP_USUARIO = "smtp_usuario"
SMTP_SENHA = "smtp_senha"
SMTP_REMETENTE = "smtp_remetente"
SMTP_REMETENTE_NOME = "smtp_remetente_nome"

SEGURANCAS_SMTP = ("starttls", "ssl", "nenhuma")

# Leitura da caixa do escritório (IMAP), para pegar os XMLs que os clientes
# mandam e montar o resumo da manhã. Usuário e senha em branco = os mesmos do
# SMTP: é a mesma caixa, e pedir a senha duas vezes só cria a chance de uma
# delas ficar velha.
IMAP_LIGADO = "imap_ligado"                  # "1" | "0"
IMAP_HOST = "imap_host"
IMAP_PORTA = "imap_porta"
IMAP_SEGURANCA = "imap_seguranca"            # ssl | starttls | nenhuma
IMAP_VERIFICAR_TLS = "imap_verificar_tls"    # "1" | "0"
IMAP_USUARIO = "imap_usuario"
IMAP_SENHA = "imap_senha"
IMAP_PASTA = "imap_pasta"

SEGURANCAS_IMAP = ("ssl", "starttls", "nenhuma")
# Outras caixas do mesmo servidor (29/09/2026: o fiscal@ recebia XML de cliente
# e o sistema só lia a caixa pessoal). Lista em JSON — usuário, senha e pasta
# de cada uma —, gravada INTEIRA cifrada: tem senha dentro.
IMAP_CAIXAS_EXTRAS = "imap_caixas_extras"
# Portas de ENVIO: gravadas na leitura, o servidor que responde é o SMTP.
PORTAS_SMTP = ("25", "465", "587", "2525")

# Chaves que guardam credencial. Entrar aqui é o que decide a cifra — e não o
# nome parecer secreto.
SEGREDOS = {SIEG_API_KEY, SMTP_SENHA, IMAP_SENHA, IMAP_CAIXAS_EXTRAS}


def ler(db: Session, chave: str, padrao: str = "") -> str:
    """O valor da configuração, já decifrado quando for segredo."""
    try:
        linha = db.query(AppSetting).filter(AppSetting.chave == chave).first()
    except Exception as erro:                             # noqa: BLE001
        # Banco sem a tabela ainda (deploy antes do create_all) não pode
        # derrubar quem só queria consultar uma configuração.
        #
        # O ROLLBACK não é zelo: no Postgres, um erro deixa a transação em
        # estado abortado e TODA consulta seguinte da mesma sessão falha com
        # "current transaction is aborted" — inclusive as que nada têm a ver
        # com configuração. Engolir a exceção sem desfazer a transação
        # transforma um erro localizado em falha da requisição inteira.
        try:
            db.rollback()
        except Exception:                                 # noqa: BLE001
            pass
        # Só o TIPO no log: tabela ausente é condição de configuração, e o
        # traceback inteiro a cada leitura de chave esconde o que importa.
        logger.warning("Não foi possível ler a configuração %s: %s",
                       chave, type(erro).__name__)
        return padrao
    if not linha or not linha.valor:
        return padrao
    if not linha.segredo:
        return linha.valor
    try:
        return decrypt_data(linha.valor)
    except Exception:                                     # noqa: BLE001
        # Cifra que não abre quase sempre é FERNET_SECRET_KEY trocada. Dizer
        # isso no log é melhor do que devolver lixo como se fosse a chave.
        logger.error("Não foi possível decifrar a configuração %s — a "
                     "FERNET_SECRET_KEY mudou desde que ela foi gravada?", chave)
        return padrao


def gravar(db: Session, chave: str, valor: str, por: str = "") -> None:
    """Grava (ou atualiza) a configuração. Cifra sozinha o que é segredo."""
    segredo = chave in SEGREDOS
    guardado = encrypt_data(valor) if (segredo and valor) else valor

    linha = db.query(AppSetting).filter(AppSetting.chave == chave).first()
    if linha is None:
        linha = AppSetting(chave=chave)
        db.add(linha)
    linha.valor = guardado
    linha.segredo = segredo
    linha.atualizado_por = por or ""
    db.commit()


def mascarar(valor: str) -> str:
    """"••••••••3f9a" — o bastante para conferir qual chave está gravada."""
    limpo = (valor or "").strip()
    if not limpo:
        return ""
    if len(limpo) <= 4:
        return "•" * len(limpo)
    return "•" * 8 + limpo[-4:]


def credenciais_sieg(db: Session) -> tuple[str, str]:
    """(e-mail, api_key) do cofre fiscal. Banco primeiro, `.env` como reserva."""
    email = ler(db, SIEG_EMAIL) or os.environ.get("SIEG_EMAIL", "")
    chave = ler(db, SIEG_API_KEY) or os.environ.get("SIEG_API_KEY", "")
    return email.strip(), chave.strip()


def cliente_sieg(db: Session):
    """O `SiegClient` com as credenciais configuradas na tela.

    Existe para as rotas não precisarem saber de onde vem a credencial — e para
    o dia em que ela passar a vir de outro lugar não virar uma caçada por
    `SiegClient()` espalhado pelo código.
    """
    from .integrations.sieg_client import SiegClient

    email, chave = credenciais_sieg(db)
    return SiegClient(api_key=chave, email=email)


def sieg_configurado(db: Session) -> bool:
    email, chave = credenciais_sieg(db)
    return bool(email and chave)


def config_smtp(db: Session) -> dict:
    """Servidor de e-mail. Banco primeiro, `.env` (SMTP_*) como reserva.

    A porta padrão acompanha a segurança escolhida: 587 no STARTTLS, 465 no
    SSL e 25 sem criptografia — que é o que um servidor interno costuma usar.
    """
    def valor(chave, env, padrao=""):
        return (ler(db, chave) or os.environ.get(env, "") or padrao).strip()

    seguranca = valor(SMTP_SEGURANCA, "SMTP_SEGURANCA", "starttls").lower()
    if seguranca not in SEGURANCAS_SMTP:
        seguranca = "starttls"
    porta_padrao = {"starttls": "587", "ssl": "465", "nenhuma": "25"}[seguranca]
    porta = valor(SMTP_PORTA, "SMTP_PORTA", porta_padrao)
    return {
        "host": valor(SMTP_HOST, "SMTP_HOST"),
        "porta": int(porta) if porta.isdigit() else int(porta_padrao),
        "seguranca": seguranca,
        "verificar_tls": valor(SMTP_VERIFICAR_TLS, "SMTP_VERIFICAR_TLS", "1") != "0",
        "usuario": valor(SMTP_USUARIO, "SMTP_USUARIO"),
        "senha": ler(db, SMTP_SENHA) or os.environ.get("SMTP_SENHA", ""),
        "remetente": valor(SMTP_REMETENTE, "SMTP_REMETENTE"),
        "remetente_nome": valor(SMTP_REMETENTE_NOME, "SMTP_REMETENTE_NOME", "Synax"),
    }


def smtp_configurado(db: Session) -> bool:
    """Tem o mínimo para tentar enviar: servidor e endereço de remetente."""
    c = config_smtp(db)
    return bool(c["host"] and c["remetente"])


def config_imap(db: Session) -> dict:
    """Caixa de entrada do escritório. Banco primeiro, `.env` (IMAP_*) de
    reserva, e usuário/senha em branco herdam os do SMTP.

    `herda_credencial` diz à tela de onde veio o login — sem isso, "senha
    gravada" num campo vazio pareceria defeito.
    """
    def valor(chave, env, padrao=""):
        return (ler(db, chave) or os.environ.get(env, "") or padrao).strip()

    seguranca = valor(IMAP_SEGURANCA, "IMAP_SEGURANCA", "ssl").lower()
    if seguranca not in SEGURANCAS_IMAP:
        seguranca = "ssl"
    porta_padrao = "993" if seguranca == "ssl" else "143"
    porta = valor(IMAP_PORTA, "IMAP_PORTA", porta_padrao)
    usuario = valor(IMAP_USUARIO, "IMAP_USUARIO")
    senha = ler(db, IMAP_SENHA) or os.environ.get("IMAP_SENHA", "")
    smtp = None
    if not usuario or not senha:
        smtp = config_smtp(db)
    return {
        "ligado": valor(IMAP_LIGADO, "IMAP_LIGADO", "0") == "1",
        "host": valor(IMAP_HOST, "IMAP_HOST"),
        "porta": int(porta) if porta.isdigit() else int(porta_padrao),
        "seguranca": seguranca,
        "verificar_tls": valor(IMAP_VERIFICAR_TLS, "IMAP_VERIFICAR_TLS", "1") != "0",
        "usuario": usuario or (smtp["usuario"] if smtp else ""),
        "senha": senha or (smtp["senha"] if smtp else ""),
        "pasta": valor(IMAP_PASTA, "IMAP_PASTA", "INBOX"),
        "herda_credencial": not (usuario and senha),
    }


def imap_configurado(db: Session) -> bool:
    """Tem o mínimo para tentar ler: servidor, usuário e senha."""
    c = config_imap(db)
    return bool(c["host"] and c["usuario"] and c["senha"])


def caixas_extras(db: Session) -> list[dict]:
    """[{usuario, senha, pasta}] das outras caixas. Lista vazia se não há."""
    import json
    try:
        lista = json.loads(ler(db, IMAP_CAIXAS_EXTRAS) or "[]")
    except ValueError:
        return []
    return [c for c in lista if isinstance(c, dict) and c.get("usuario")]


def gravar_caixas_extras(db: Session, caixas: list[dict], por: str = "") -> None:
    import json
    limpas = [{"usuario": c["usuario"].strip().lower(), "senha": c.get("senha") or "",
               "pasta": (c.get("pasta") or "INBOX").strip() or "INBOX"} for c in caixas]
    gravar(db, IMAP_CAIXAS_EXTRAS, json.dumps(limpas, ensure_ascii=False), por)


def caixas_imap(db: Session) -> list[dict]:
    """Todas as caixas a ler: a principal primeiro, depois as outras.

    Cada uma é a configuração completa (servidor, porta e segurança são os da
    principal — é o mesmo servidor de e-mail) mais duas chaves:

    - `chave`: como a caixa aparece em `email_mensagens.pasta`. A principal
      continua sendo só a pasta ("INBOX"), como antes de existirem outras; as
      outras levam o usuário na frente ("fiscal@x/INBOX"). É o que impede o UID
      42 de uma caixa de ser confundido com o UID 42 da outra — os UIDs são
      por caixa, e a trava de repetição é (pasta, UIDVALIDITY, UID).
    - `chave_estado`: onde o "até onde li" de cada caixa fica em app_settings.
    """
    principal = config_imap(db)
    if not principal["host"]:
        return []
    caixas = []
    if principal["usuario"] and principal["senha"]:
        caixas.append({**principal, "chave": principal["pasta"] or "INBOX",
                       "chave_estado": "imap_estado", "rotulo": principal["usuario"]})
    for extra in caixas_extras(db):
        pasta = extra.get("pasta") or "INBOX"
        caixas.append({**principal, "usuario": extra["usuario"], "senha": extra.get("senha", ""),
                       "pasta": pasta, "chave": f"{extra['usuario']}/{pasta}"[:120],
                       "chave_estado": f"imap_estado:{extra['usuario']}"[:80],
                       "rotulo": extra["usuario"]})
    return caixas
