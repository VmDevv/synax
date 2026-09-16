import os
import httpx
import logging
from typing import List, Dict, Any
from sqlalchemy.orm import Session
from ...domain.models.integrations import TenantIntegration
from ...core.security import decrypt_data

logger = logging.getLogger(__name__)

class SiegIntegrationService:
    def __init__(self, session: Session, tenant_id: str):
        self.session = session
        self.tenant_id = tenant_id
        self.base_url = "https://api.sieg.com/aws/api-xml-search.ashx"
    
    def _get_credentials(self):
        integration = self.session.query(TenantIntegration).filter(
            TenantIntegration.tenant_id == self.tenant_id,
            TenantIntegration.provider == "SIEG",
            TenantIntegration.is_active == True
        ).first()
        
        if not integration:
            raise ValueError("Integração SIEG não configurada ou inativa para este Tenant.")
            
        return {
            "apikey": decrypt_data(integration.api_key_encrypted),
            "email": decrypt_data(integration.email_encrypted)
        }

    async def fetch_xmls(self, cnpj: str, data_inicio: str, data_fim: str) -> List[Dict[str, Any]]:
        """Busca os XMLs do período na API do SIEG. AINDA NÃO IMPLEMENTADO.

        Este método devolvia duas notas fictícias ("Fornecedor Teste Malha Fina"),
        com chaves inventadas e um xml_base64 que decodifica para `<nfe>...</nfe>`.
        O motor rodava em cima disso e a auditoria era entregue ao cliente como
        resultado fiscal real — pior do que não existir. Enquanto a chamada HTTP
        de verdade não for escrita, falha explícita.

        Ao implementar: usar httpx.AsyncClient contra self.base_url com as creds
        de _get_credentials(), e o período/CNPJ vindos do registro 0000 do SPED
        (sped_data["periodo_inicio"]/["periodo_fim"]), nunca valores fixos.
        """
        # Valida a configuração do tenant desde já: quem marcar a opção recebe o
        # erro de credencial antes do de indisponibilidade, que é mais acionável.
        self._get_credentials()
        logger.warning(
            "fetch_xmls chamado sem implementação real (tenant=%s cnpj=%s %s..%s)",
            self.tenant_id, cnpj, data_inicio, data_fim,
        )
        raise NotImplementedError(
            "A importação automática pelo SIEG ainda não está disponível. "
            "Envie o ZIP de XMLs ou use o Modo Somente SPED."
        )
