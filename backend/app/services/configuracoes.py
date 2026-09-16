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

# Chaves que guardam credencial. Entrar aqui é o que decide a cifra — e não o
# nome parecer secreto.
SEGREDOS = {SIEG_API_KEY}


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
