from sqlalchemy import Column, String, Boolean, Float, Index
from .base import Base, TenantMixin, TimestampMixin

class CfopCreditRule(Base, TenantMixin, TimestampMixin):
    __table_args__ = (
        Index('idx_cfop_credit_status_tenant', 'status', 'tenant_id'),
        Index('idx_cfop_credit_status_cfop', 'status', 'cfop'),
    )
    __tablename__ = 'cfop_credit_rules'

    cfop = Column(String, primary_key=True)
    tenant_id = Column(String(36), primary_key=True)
    
    allow_credit = Column(Boolean, nullable=False, default=False)
    description = Column(String)
    status = Column(String(20), nullable=False, default="aprovado") # "aprovado" ou "pendente_revisao"
    is_global = Column(Boolean, default=False)
    created_by = Column(String)

class ItemMatchRule(Base, TenantMixin, TimestampMixin):
    """Par SPED×XML já conferido por uma pessoa — não se aprova duas vezes.

    O item que a IA relacionou e alguém aprovou na tela vira regra: na próxima
    auditoria daquele fornecedor o motor casa direto, sem oferecer à IA e sem
    pedir confirmação de novo.

    A CHAVE É EXATA e tem cinco campos, como a do CFOP (`CfopClassification`):
    empresa, fornecedor, código do SPED, código do XML e tenant. Ela funciona
    entre notas diferentes justamente porque os dois códigos são estáveis — o
    COD_ITEM é interno da empresa e o cProd é do fornecedor, e o mesmo produto
    do mesmo fornecedor carrega sempre o mesmo par. Curinga aqui criaria
    casamento que atravessa fornecedores: NUNCA grave "*" em campo de chave.
    """
    __table_args__ = (
        Index('idx_item_match_status_tenant', 'status', 'tenant_id'),
        Index('idx_item_match_status_supp', 'status', 'supplier_cnpj'),
        Index('idx_item_match_status_created_by', 'status', 'created_by'),
    )
    __tablename__ = 'item_match_rules'

    company_cnpj = Column(String, primary_key=True)
    supplier_cnpj = Column(String, primary_key=True)
    sped_cod = Column(String, primary_key=True)
    xml_cod = Column(String, primary_key=True)
    tenant_id = Column(String(36), primary_key=True)

    sped_descr = Column(String)
    xml_descr = Column(String)
    # 'ia' (o modelo sugeriu) ou 'poda' (resolvido localmente, sem API).
    origem = Column(String(20))
    confianca = Column(String(10))
    justificativa = Column(String)
    status = Column(String(20), nullable=False, default="aprovado")
    created_by = Column(String)


class CfopClassification(Base, TenantMixin, TimestampMixin):
    __table_args__ = (
        Index('idx_cfop_class_status_tenant', 'status', 'tenant_id'),
        Index('idx_cfop_class_status_supp_cfop', 'status', 'supplier_cfop'),
        Index('idx_cfop_class_status_supp_cnpj', 'status', 'supplier_cnpj'),
        Index('idx_cfop_class_status_created_by', 'status', 'created_by'),
    )
    __tablename__ = 'cfop_classifications'

    company_cnpj = Column(String, primary_key=True)
    supplier_cnpj = Column(String, primary_key=True)
    supplier_cfop = Column(String, primary_key=True)
    product_code = Column(String, primary_key=True)
    tenant_id = Column(String(36), primary_key=True)
    
    product_description = Column(String)
    entry_cfop = Column(String, nullable=False)
    status = Column(String(20), nullable=False, default="aprovado")
    is_global = Column(Boolean, default=False)
    created_by = Column(String)

class CteCfopClassification(Base, TenantMixin, TimestampMixin):
    __table_args__ = (
        Index('idx_cte_class_status_tenant', 'status', 'tenant_id'),
        Index('idx_cte_class_status_supp_cfop', 'status', 'supplier_cfop'),
        Index('idx_cte_class_status_created_by', 'status', 'created_by'),
    )
    __tablename__ = 'cte_cfop_classifications'

    company_cnpj = Column(String, primary_key=True)
    supplier_cfop = Column(String, primary_key=True)
    emitter_uf = Column(String, primary_key=True)
    tenant_id = Column(String(36), primary_key=True)
    
    entry_cfop = Column(String, nullable=False)
    status = Column(String(20), nullable=False, default="aprovado")
    is_global = Column(Boolean, default=False)
    created_by = Column(String)

class IpiCreditRule(Base, TenantMixin, TimestampMixin):
    __table_args__ = (
        Index('idx_ipi_credit_status_tenant', 'status', 'tenant_id'),
        Index('idx_ipi_credit_status_cfop', 'status', 'cfop'),
        Index('idx_ipi_credit_status_created_by', 'status', 'created_by'),
    )
    __tablename__ = 'ipi_credit_rules'

    cfop = Column(String, primary_key=True)
    cst_expected = Column(String, primary_key=True)
    tenant_id = Column(String(36), primary_key=True)
    
    allow_credit = Column(Boolean, nullable=False, default=False)
    description = Column(String)
    status = Column(String(20), nullable=False, default="aprovado")
    is_global = Column(Boolean, default=False)
    created_by = Column(String)

class PisCofinsCreditRule(Base, TenantMixin, TimestampMixin):
    __table_args__ = (
        Index('idx_piscofins_credit_status_tenant', 'status', 'tenant_id'),
        Index('idx_piscofins_credit_status_cfop', 'status', 'cfop'),
        Index('idx_piscofins_credit_status_created_by', 'status', 'created_by'),
    )
    __tablename__ = 'pis_cofins_credit_rules'

    cfop = Column(String, primary_key=True)
    cst_expected = Column(String, primary_key=True)
    tenant_id = Column(String(36), primary_key=True)
    
    allow_credit = Column(Boolean, nullable=False, default=False)
    description = Column(String)
    status = Column(String(20), nullable=False, default="aprovado")
    is_global = Column(Boolean, default=False)
    created_by = Column(String)

class IcmsAnticipationRule(Base, TenantMixin, TimestampMixin):
    __table_args__ = (
        Index('idx_icms_antic_status_tenant', 'status', 'tenant_id'),
        Index('idx_icms_antic_status_cfop', 'status', 'cfop'),
        Index('idx_icms_antic_status_created_by', 'status', 'created_by'),
    )
    __tablename__ = 'icms_anticipation_rules'

    uf_origem = Column(String(2), primary_key=True)
    uf_destino = Column(String(2), primary_key=True)
    cfop = Column(String(4), primary_key=True)
    tenant_id = Column(String(36), primary_key=True)
    
    mva_percentage = Column(Float, nullable=False, default=0.0)
    aliquota_interna_percentage = Column(Float, nullable=False, default=0.0)
    aliquota_interestadual_percentage = Column(Float, nullable=False, default=0.0)
    reducao_base_calculo_percentage = Column(Float, nullable=False, default=0.0)
    
    description = Column(String)
    status = Column(String(20), nullable=False, default="aprovado")
    is_global = Column(Boolean, default=False)
    created_by = Column(String)

class CestMvaRule(Base, TenantMixin, TimestampMixin):
    __tablename__ = 'cest_mva_rules'

    cest = Column(String(20), primary_key=True)
    tenant_id = Column(String(36), primary_key=True)
    
    ncm = Column(String(50))
    description = Column(String)
    linha_item = Column(String(20))
    
    mva_original = Column(Float, nullable=False, default=0.0)
    mva_aj_4 = Column(Float, nullable=False, default=0.0)
    mva_aj_7 = Column(Float, nullable=False, default=0.0)
    mva_aj_12 = Column(Float, nullable=False, default=0.0)
    
    status = Column(String(20), nullable=False, default="aprovado")
    is_global = Column(Boolean, default=True)
    created_by = Column(String)


# ---------------------------------------------------------------------------
# Regras do GERADOR DE SPED (services/sped_builder) — separadas das da auditoria.
#
# O gerador escritura empresa que NÃO tem SPED, e o CFOP de entrada não existe
# no XML: sai daqui. Pedido do Sandro em 09/10/2026: as regras dele ficam em
# tabelas próprias, porque a regra aprovada para montar o arquivo de um cliente
# não pode mudar o que a auditoria acusa nos outros — nem o contrário. Até
# então a tela do gerador gravava em `cfop_classifications`, a tabela que o
# motor da auditoria lê. O maker-checker é o mesmo (`rule_status_for`): o
# operador grava pendente, o admin aprova na Curadoria, e sem tudo aprovado o
# arquivo não sai. O status nasce pendente por padrão — falhar fechado.
# ---------------------------------------------------------------------------

class GeradorCfopRegra(Base, TenantMixin, TimestampMixin):
    """CFOP de entrada de um ITEM de NF-e no SPED gerado dos XMLs.

    Chave exata, sem curinga, com a mesma forma da `CfopClassification`:
    empresa, fornecedor, CFOP do fornecedor e cProd. O cProd é o código do
    FORNECEDOR — a empresa que não escritura não tem código interno, e é ele
    que vira o COD_ITEM do 0200.
    """
    __table_args__ = (
        Index('idx_gerador_cfop_status_tenant', 'status', 'tenant_id'),
        Index('idx_gerador_cfop_status_created_by', 'status', 'created_by'),
    )
    __tablename__ = 'gerador_cfop_regras'

    company_cnpj = Column(String, primary_key=True)
    supplier_cnpj = Column(String, primary_key=True)
    supplier_cfop = Column(String, primary_key=True)
    product_code = Column(String, primary_key=True)
    tenant_id = Column(String(36), primary_key=True)

    # O que a Curadoria mostra para julgar a regra sem abrir a nota.
    supplier_name = Column(String)
    product_description = Column(String)
    entry_cfop = Column(String(4), nullable=False)
    status = Column(String(20), nullable=False, default="pendente_revisao")
    created_by = Column(String)


class GeradorCteCfopRegra(Base, TenantMixin, TimestampMixin):
    """CFOP de entrada do FRETE (D190 do CT-e) no SPED gerado dos XMLs.

    Chave: empresa, CFOP do CT-e e UF do transportador — a forma da
    `CteCfopClassification` da auditoria (decisão do Sandro em 09/10/2026). O
    CFOP de aquisição de transporte depende da atividade da empresa e de a
    prestação ser interna ou não, e não de qual transportadora levou: por
    transportadora, o mesmo 5353/ES pediria uma aprovação por empresa de frete.
    """
    __table_args__ = (
        Index('idx_gerador_cte_status_tenant', 'status', 'tenant_id'),
        Index('idx_gerador_cte_status_created_by', 'status', 'created_by'),
    )
    __tablename__ = 'gerador_cte_cfop_regras'

    company_cnpj = Column(String, primary_key=True)
    supplier_cfop = Column(String, primary_key=True)
    emitter_uf = Column(String(2), primary_key=True)
    tenant_id = Column(String(36), primary_key=True)

    # Transportadoras vistas com esta combinação, só para a Curadoria julgar.
    carrier_names = Column(String)
    entry_cfop = Column(String(4), nullable=False)
    status = Column(String(20), nullable=False, default="pendente_revisao")
    created_by = Column(String)
