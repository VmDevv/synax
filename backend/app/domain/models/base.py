from sqlalchemy.orm import DeclarativeBase, declared_attr
from sqlalchemy import Column, String, DateTime
from datetime import datetime

class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""
    pass

class TenantMixin:
    """Mixin to add tenant_id to all multi-tenant models."""
    @declared_attr
    def tenant_id(cls):
        return Column(String(36), index=True, nullable=False)

class TimestampMixin:
    """Mixin to add created_at and updated_at timestamps."""
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
