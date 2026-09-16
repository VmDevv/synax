from sqlalchemy.orm import Session
from sqlalchemy import select
from .base_repository import BaseRepository
from ...domain.models.rules import CfopCreditRule, CfopClassification, CteCfopClassification, IpiCreditRule, PisCofinsCreditRule, IcmsAnticipationRule, ItemMatchRule
from ...domain.models.audit import AuditTask

class RulesRepository:
    def __init__(self, session: Session, tenant_id: str):
        self.session = session
        self.tenant_id = tenant_id
        
        self.credit_rules = BaseRepository[CfopCreditRule](session, CfopCreditRule, tenant_id)
        self.cfop_classifications = BaseRepository[CfopClassification](session, CfopClassification, tenant_id)
        self.cte_classifications = BaseRepository[CteCfopClassification](session, CteCfopClassification, tenant_id)
        self.ipi_credit_rules = BaseRepository[IpiCreditRule](session, IpiCreditRule, tenant_id)
        self.pis_cofins_credit_rules = BaseRepository[PisCofinsCreditRule](session, PisCofinsCreditRule, tenant_id)
        self.icms_anticipation_rules = BaseRepository[IcmsAnticipationRule](session, IcmsAnticipationRule, tenant_id)
        # Pares SPED×XML já aprovados numa auditoria anterior — evitam repetir a
        # confirmação do mesmo item, nota após nota.
        self.item_match_rules = BaseRepository[ItemMatchRule](session, ItemMatchRule, tenant_id)
        self.audit_tasks = BaseRepository[AuditTask](session, AuditTask, tenant_id)

    # NÃO confiar no RLS do Postgres: a aplicação conecta com um papel que tem
    # BYPASSRLS e a maioria das tabelas nem tem RLS habilitado. Todo filtro de
    # tenant e de status precisa ser explícito aqui.
    def _tenant_scope(self, model):
        """Regras do próprio tenant, somente aprovadas.

        NÃO existe regra global. Uma regra gravada sob um tenant fantasma e
        marcada is_global valia para todas as empresas do sistema — decisão de
        crédito que ninguém daquela empresa aprovou. Decisão do escritório
        (25/08/2026): ICMS e IPI seguem a mesma linha do CFOP, cada tenant
        responde pelas suas regras.
        """
        return (
            model.tenant_id == self.tenant_id,
            model.status == 'aprovado',
        )

    def get_credit_rule(self, cfop: str) -> CfopCreditRule | None:
        stmt = select(CfopCreditRule).where(
            CfopCreditRule.cfop == cfop,
            *self._tenant_scope(CfopCreditRule)
        )
        return self.session.scalars(stmt).first()

    def get_cfop_classification(self, company_cnpj: str, supplier_cnpj: str, supplier_cfop: str, product_code: str) -> CfopClassification | None:
        stmt = select(CfopClassification).where(
            CfopClassification.company_cnpj == company_cnpj,
            CfopClassification.supplier_cnpj == supplier_cnpj,
            CfopClassification.supplier_cfop == supplier_cfop,
            CfopClassification.product_code == product_code,
            *self._tenant_scope(CfopClassification)
        )
        return self.session.scalars(stmt).first()

    def get_cte_classification(self, company_cnpj: str, supplier_cfop: str, emitter_uf: str) -> CteCfopClassification | None:
        stmt = select(CteCfopClassification).where(
            CteCfopClassification.company_cnpj == company_cnpj,
            CteCfopClassification.supplier_cfop == supplier_cfop,
            CteCfopClassification.emitter_uf == emitter_uf,
            *self._tenant_scope(CteCfopClassification)
        )
        return self.session.scalars(stmt).first()

    def get_audit_task(self, task_id: str) -> AuditTask | None:
        stmt = select(AuditTask).where(
            AuditTask.id == task_id,
            AuditTask.tenant_id == self.tenant_id
        )
        return self.session.scalars(stmt).first()
