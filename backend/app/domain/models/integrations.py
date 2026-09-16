from sqlalchemy import Column, String, Boolean
from .base import Base, TenantMixin, TimestampMixin

class TenantIntegration(Base, TenantMixin, TimestampMixin):
    __tablename__ = 'tenant_integrations'

    tenant_id = Column(String(36), primary_key=True)
    provider = Column(String(50), primary_key=True) # Ex: "SIEG"
    
    api_key_encrypted = Column(String, nullable=False)
    email_encrypted = Column(String, nullable=False)
    
    is_active = Column(Boolean, default=True)
    status_integracao = Column(String(50), default="ok") # ok, erro_auth, pendente

class MissingInvoiceLog(Base, TenantMixin, TimestampMixin):
    __tablename__ = 'missing_invoices_log'

    access_key = Column(String(255), primary_key=True)
    tenant_id = Column(String(36), primary_key=True)
    
    invoice_type = Column(String(20), nullable=False) # NFE, CTE, NFCE
    status = Column(String(50), nullable=False, default="pendente_proximo_mes") # pendente_proximo_mes, ignorado, resolvido
    emit_cnpj = Column(String(14))
    emit_name = Column(String(255))
    vl_total = Column(String(50)) # Guardando como string temporariamente ou float, usar string pra simplificar
    
    task_id = Column(String(36)) # ID da tarefa que descobriu
