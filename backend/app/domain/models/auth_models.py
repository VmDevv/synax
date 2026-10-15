from sqlalchemy import Column, String, ForeignKey, DateTime, Boolean
from sqlalchemy.dialects.postgresql import UUID
from datetime import datetime
from .base import Base
import uuid

class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    cnpj = Column(String(20), unique=True)

    # UF da empresa TOMADORA. É ela que decide se uma nota de serviço é operação
    # interna (CFOP 1933) ou interestadual (2933). Já esteve fixa como "ES" no
    # gerador de lançamentos, e toda nota RJ×RJ de um cliente do RJ saiu como
    # 2933. A UF é da empresa, não do escritório.
    uf = Column(String(2))

    created_at = Column(DateTime, default=datetime.utcnow)

    # Empresa que saiu da carteira do escritório, mas cujo histórico (auditorias,
    # regras, NFS-e) precisa continuar acessível — daí desativar em vez de
    # excluir. Some das telas de quem não é admin; o admin ainda a vê, com tag.
    ativo = Column(Boolean, nullable=False, default=True, server_default="true")

class UserTenant(Base):
    __tablename__ = "user_tenants"

    user_id = Column(UUID(as_uuid=True), ForeignKey("user_profiles.id", ondelete="CASCADE"), primary_key=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id = Column(UUID(as_uuid=True), primary_key=True) # ForeignKey para auth.users
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True) # Pode ser o "escritório" primário ou nulo
    role = Column(String(50), default="cliente") # admin, cliente, operador
    name = Column(String(255))
    created_at = Column(DateTime, default=datetime.utcnow)
    # Data (AAAA-MM-DD) da última novidade que a pessoa viu na tela principal
    # (`services/novidades.py`). No banco, não no navegador: o escritório usa
    # mais de um computador. NULO = nunca viu nenhuma.
    novidades_vistas_ate = Column(String(10), nullable=True)
    
    from sqlalchemy.orm import relationship
    # `lazy="joined"`: as empresas do operador vêm JUNTO com o perfil, numa
    # consulta só. Com o carregamento preguiçoso (o padrão), a segunda consulta
    # só disparava quando alguém tocava em `allowed_tenants` — em 14 pontos do
    # sistema, sempre bem depois da primeira. Duas consequências, as duas ruins:
    #
    #   1. dobrava a espera de toda requisição que confere permissão, e cada ida
    #      ao Supabase custa ~140ms de rede contra ~0.1ms de execução;
    #   2. a consulta atrasada usava uma conexão que podia já ter caído — é
    #      exatamente o `server closed the connection unexpectedly` que aparecia
    #      no log em `[str(t.id) for t in user_profile.allowed_tenants]`.
    #
    # A lista de empresas de um operador é curta, então o JOIN não pesa.
    allowed_tenants = relationship("Tenant", secondary="user_tenants",
                                   backref="operators", lazy="joined")

