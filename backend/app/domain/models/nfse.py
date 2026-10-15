"""Notas Fiscais de Serviço (NFS-e) — módulo independente.

Não altera nem depende do motor de auditoria de mercadoria/transporte. Tabela
própria, isolada por tenant como todo o resto do sistema.

As colunas espelham o relatório ABRASF usado hoje pelo escritório, para que a
tela mostre exatamente as mesmas informações da planilha.
"""
from sqlalchemy import Column, String, Float, DateTime, Index
from .base import Base, TenantMixin, TimestampMixin


class NfseCursor(Base, TenantMixin, TimestampMixin):
    """Último NSU já consumido do ADN, por empresa/CNPJ.

    REGRA CRÍTICA: só avance este número DEPOIS de confirmar a gravação dos
    documentos do lote. O ADN mantém cada documento por apenas 90 dias — se o
    cursor passar à frente sem que o XML tenha sido salvo, aquele documento se
    perde em definitivo. Reprocessar lote repetido é inofensivo (a nota é
    deduplicada pela chave natural); pular não tem volta.
    """
    __tablename__ = 'nfse_cursores'

    tenant_id = Column(String(36), primary_key=True)
    cnpj = Column(String(14), primary_key=True)

    ultimo_nsu = Column(String(30), default="0")
    ultima_consulta = Column(DateTime)
    ultimo_erro = Column(String(500))

    @property
    def nsu(self) -> int:
        try:
            return int(self.ultimo_nsu or 0)
        except (TypeError, ValueError):
            return 0


class NfseDocument(Base, TenantMixin, TimestampMixin):
    __tablename__ = 'nfse_documents'
    __table_args__ = (
        Index('idx_nfse_tenant_comp', 'tenant_id', 'dt_competencia'),
        Index('idx_nfse_tenant_prestador', 'tenant_id', 'prestador'),
        Index('idx_nfse_tenant_tomador', 'tenant_id', 'tomador'),
        Index('idx_nfse_tenant_status', 'tenant_id', 'status'),
        Index('idx_nfse_lote', 'lote_id'),
    )

    # Identidade natural: a mesma nota não entra duas vezes.
    # NFS-e ABRASF não tem chave de 44 dígitos como a NF-e; o par
    # prestador + número + município identifica o documento.
    tenant_id = Column(String(36), primary_key=True)
    prestador = Column(String(14), primary_key=True)
    numero = Column(String(50), primary_key=True)
    mun_prest = Column(String(10), primary_key=True)

    # --- Colunas do relatório ABRASF ---
    dt_emissao = Column(DateTime)
    dt_competencia = Column(DateTime)
    rz_prestador = Column(String(255))
    uf_prest = Column(String(2))
    insc_prest = Column(String(50))

    tomador = Column(String(14))
    rz_tomador = Column(String(255))
    uf_toma = Column(String(2))
    cod_mun_toma = Column(String(10))

    optante_sn = Column(String(5))
    cod_servico = Column(String(20))
    cnae = Column(String(20))

    valor_servico = Column(Float, default=0.0)
    ir = Column(Float, default=0.0)
    iss = Column(Float, default=0.0)
    iss_retido = Column(String(10))
    csll = Column(Float, default=0.0)
    deducoes = Column(Float, default=0.0)
    pis = Column(Float, default=0.0)
    cofins = Column(Float, default=0.0)
    inss = Column(Float, default=0.0)
    desconto_incondic = Column(Float, default=0.0)
    desconto_condic = Column(Float, default=0.0)
    out_retencoes = Column(Float, default=0.0)
    aliquota = Column(Float, default=0.0)
    base_calculo = Column(Float, default=0.0)
    valor_liquido = Column(Float, default=0.0)

    dt_cancelamento = Column(DateTime)
    motivo_cancel = Column(String(255))
    numero_nota_cancel = Column(String(50))
    status = Column(String(120))

    # --- Operacional (não aparece na planilha) ---
    discriminacao = Column(String)          # texto livre do serviço
    cod_verificacao = Column(String(60))    # usado por alguns municípios
    arquivo_xml = Column(String(500))       # caminho relativo do XML guardado
    arquivo_pdf = Column(String(500))       # caminho relativo do PDF, quando houver
    origem = Column(String(20), default="upload")  # upload | adn | sieg
    # Último lote de NFS-e que gravou esta nota (services/nfse/lote.py). É o
    # que deixa a tela abrir só no que acabou de chegar — com 5.539 notas
    # guardadas, a busca de hoje se perdia no meio do histórico. Nota que vem
    # de novo num lote seguinte passa a ser daquele lote.
    lote_id = Column(String(36))

    @property
    def cancelada(self) -> bool:
        return bool(self.dt_cancelamento) or "cancel" in (self.status or "").lower()

    @property
    def total_retencoes(self) -> float:
        return sum(v or 0.0 for v in (
            self.ir, self.csll, self.pis, self.cofins, self.inss, self.out_retencoes
        ))
