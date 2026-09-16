import base64
import json
import re
import os
import logging
from urllib.parse import unquote
import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Credenciais do cofre fiscal do escritório. Estavam fixas neste arquivo: quem
# lesse o repositório (ou um backup dele) consultava as notas de todos os
# clientes. Vêm do .env, como as demais (SUPABASE_*, FERNET_SECRET_KEY).
SIEG_API_KEY = os.environ.get("SIEG_API_KEY", "")
SIEG_EMAIL = os.environ.get("SIEG_EMAIL", "")


class SiegClient:
    def __init__(self, api_key: str | None = None, email: str | None = None):
        chave = (api_key or SIEG_API_KEY).strip()
        conta = (email or SIEG_EMAIL).strip()
        # Fail-closed, no mesmo espírito de core/security.py: sem credencial o
        # proxy não deve subir meio funcionando.
        if not chave or not conta:
            raise RuntimeError(
                "SIEG_API_KEY e SIEG_EMAIL precisam estar configurados no .env "
                "para consultar o cofre fiscal."
            )
        self.api_key = unquote(chave)
        self.email = conta
        self.base_url_xml = "https://api.sieg.com/BaixarXml"
        self.base_url_pdf = "https://api.sieg.com/api/Arquivos/GerarDanfeViaXml"
        self.base_url_lote = f"https://api.sieg.com/BaixarXmls?api_key={self.api_key}"
        
        # Cache local em disco para PDFs gerados (evita requisições repetidas ao SIEG)
        self.cache_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../.sieg_cache_pdf"))
        os.makedirs(self.cache_dir, exist_ok=True)

    def baixar_xml_por_chave(self, chave_acesso: str) -> tuple[bool, str]:
        """Baixa um XML único via chave de acesso de 44 dígitos."""
        chave_acesso = re.sub(r"\D", "", str(chave_acesso))
        if len(chave_acesso) != 44:
            return False, "Chave de acesso deve conter exatamente 44 dígitos."
        # Se posições 20 a 22 forem 57, é CTe (XmlType = 2), senão é NFe (XmlType = 1)
        xml_type = 2 if chave_acesso[20:22] == "57" else 1
        params = {
            "api_key": self.api_key,
            "email": self.email,
            "xmlType": str(xml_type),
            "downloadEvent": "false",
        }
        headers = {"Content-Type": "application/json"}
        payload = json.dumps(chave_acesso)
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    self.base_url_xml,
                    params=params,
                    content=payload.encode("utf-8"),
                    headers=headers,
                )
            if response.status_code == 200:
                content = response.text.strip()
                if (
                    "xml" in content[:60].lower()
                    or content.startswith("<")
                    or content.startswith('"<')
                ):
                    if content.startswith('"') and content.endswith('"'):
                        content = content[1:-1]
                    content = content.replace('\\"', '"')
                    return True, content
                return False, f"XML recusado pela API SIEG: {content[:100]}"
            return (
                False,
                f"Erro HTTP {response.status_code}: {response.text[:100]}",
            )
        except Exception as e:
            return False, f"Exceção na conexão: {str(e)}"

    def gerar_danfe_pdf(self, xml_string: str) -> tuple[bool, bytes | str]:
        """Recebe a string do XML e retorna os bytes do PDF (DANFE)."""
        xml_b64 = base64.b64encode(xml_string.encode("utf-8")).decode("utf-8")
        params = {"api_key": self.api_key, "email": self.email}
        headers = {"Content-Type": "application/json"}
        payload = {"ArquivoXml": xml_b64}
        try:
            with httpx.Client(timeout=60.0) as client:
                res = client.post(
                    self.base_url_pdf,
                    params=params,
                    json=payload,
                    headers=headers,
                )
            if res.status_code == 200:
                pdf_b64 = res.text.strip().replace('"', "")
                if pdf_b64:
                    return True, base64.b64decode(pdf_b64)
                return False, "API retornou PDF vazio."
            return False, f"Erro HTTP {res.status_code} ao gerar DANFE."
        except Exception as e:
            return False, f"Exceção ao gerar DANFE: {str(e)}"

    def gerar_danfe_por_chave(self, chave_acesso: str, xml_string_opcional: str = None) -> tuple[bool, bytes | str]:
        """
        Gera e retorna os bytes do PDF DANFE por chave de acesso, 
        utilizando cache local se existir, ou baixando o XML do SIEG automaticamente se não fornecido.
        """
        chave_limpa = re.sub(r"\D", "", str(chave_acesso))
        if len(chave_limpa) != 44:
            return False, "Chave de acesso deve conter exatamente 44 dígitos."
            
        cache_file = os.path.join(self.cache_dir, f"{chave_limpa}.pdf")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "rb") as f:
                    return True, f.read()
            except Exception:
                pass
                
        xml_content = xml_string_opcional
        if not xml_content:
            sucesso_xml, res_xml = self.baixar_xml_por_chave(chave_limpa)
            if not sucesso_xml:
                return False, f"Falha ao buscar XML no SIEG: {res_xml}"
            xml_content = res_xml
            
        sucesso_pdf, pdf_bytes = self.gerar_danfe_pdf(xml_content)
        if sucesso_pdf and isinstance(pdf_bytes, bytes):
            try:
                with open(cache_file, "wb") as f:
                    f.write(pdf_bytes)
            except Exception:
                pass
        return sucesso_pdf, pdf_bytes

    def baixar_lote_xmls(
        self,
        cnpj: str,
        xml_type: int = 1,
        data_inicio: str = "2026-01-01",
        data_fim: str = "2026-01-31",
        tipo_cnpj: str = "CnpjEmit",
    ) -> list[str]:
        """Baixa um lote paginado de notas e retorna uma lista com as strings XML decodificadas."""
        cnpj = re.sub(r"\D", "", cnpj)
        skip = 0
        xmls_extraidos = []
        while True:
            payload = {
                "XmlType": xml_type,
                "Take": 50,
                "Skip": skip,
                "Downloadevent": True,
                tipo_cnpj: cnpj,
                "DataEmissaoInicio": data_inicio,
                "DataEmissaoFim": data_fim,
            }
            try:
                with httpx.Client(timeout=60.0) as client:
                    response = client.post(
                        self.base_url_lote, json=payload
                    )
                if response.status_code == 404:
                    break  # Fim da paginação
                if response.status_code == 200:
                    res_str = response.text.strip()
                    if "Message" in res_str and "An error" in res_str:
                        break
                    # Limpeza do array retornado
                    limpo = res_str.lstrip("[").rstrip("]")
                    itens = limpo.split(",")
                    lote_atual = [
                        i.replace('"', "").replace("\\", "").strip()
                        for i in itens
                        if len(i) > 10
                    ]
                    if not lote_atual:
                        break
                    for item_b64 in lote_atual:
                        try:
                            xml_bytes = base64.b64decode(item_b64)
                            xmls_extraidos.append(
                                xml_bytes.decode("utf-8", errors="ignore")
                            )
                        except Exception:
                            continue
                    if len(lote_atual) < 50:
                        break  # Última página
                    skip += 50
                else:
                    break
            except Exception:
                break
        return xmls_extraidos
