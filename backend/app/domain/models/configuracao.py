from sqlalchemy import Boolean, Column, String, Text

from .base import Base, TimestampMixin


class AppSetting(Base, TimestampMixin):
    """Configuração do escritório, editada na aba Configurações do admin.

    **Não tem `tenant_id` de propósito.** O que mora aqui é do ESCRITÓRIO, não
    de um cliente: a conta do cofre fiscal SIEG é uma só e serve para consultar
    as notas de todas as empresas da carteira. Uma configuração por empresa
    precisaria de outra tabela, com escopo por tenant e a validação que vem
    junto — não de uma coluna a mais nesta.

    `valor` guarda texto puro para o que é ajuste (um e-mail, um limite) e
    texto CIFRADO para o que é credencial (`segredo = True`, cifra do
    `core/security.py`, a mesma dos certificados). A distinção fica na linha, e
    não no nome da chave, porque quem lê a tabela precisa saber o que está
    olhando sem consultar código.
    """

    __tablename__ = 'app_settings'

    chave = Column(String(80), primary_key=True)
    valor = Column(Text, nullable=True)
    segredo = Column(Boolean, nullable=False, default=False)

    # Trilha: credencial trocada sem saber por quem é o tipo de mudança que
    # ninguém consegue investigar depois.
    atualizado_por = Column(String(255), nullable=True)
