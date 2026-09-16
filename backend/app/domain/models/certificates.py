"""Certificados digitais A1 por empresa.

O ARQUIVO .pfx fica na rede (\\srv-ap02\\Desenvolvimento\\Certificados); aqui
guardamos apenas o vínculo empresa → arquivo e a SENHA CIFRADA.

A senha nunca é gravada em texto puro: usa o mesmo Fernet de core/security.py,
cuja chave vive fora do banco. Assim, um dump do banco não entrega as senhas.
"""
from sqlalchemy import Column, String, Boolean, DateTime, Index
from .base import Base, TenantMixin, TimestampMixin


class TenantCertificate(Base, TenantMixin, TimestampMixin):
    __tablename__ = 'tenant_certificates'
    __table_args__ = (
        Index('idx_cert_tenant', 'tenant_id'),
        Index('idx_cert_cnpj', 'cnpj'),
    )

    tenant_id = Column(String(36), primary_key=True)
    cnpj = Column(String(14), primary_key=True)

    # Nome do arquivo dentro da pasta de certificados. Guardamos o NOME, não o
    # caminho completo: se a pasta mudar, basta ajustar a configuração.
    arquivo = Column(String(300))

    senha_encrypted = Column(String, nullable=False)

    apelido = Column(String(120))
    razao_social = Column(String(255))
    validade = Column(DateTime)          # preenchida ao validar o certificado
    ativo = Column(Boolean, default=True)
    ultimo_erro = Column(String(300))

    def senha(self) -> str:
        """Senha em claro. Use apenas no momento da conexão, nunca registre."""
        from ...core.security import decrypt_data
        return decrypt_data(self.senha_encrypted)
