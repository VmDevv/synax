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
        # Sem a chave na URL: vai em `params`, junto com o e-mail que o lote exige.
        self.base_url_lote_v2 = "https://api.sieg.com/BaixarXmls"
        
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

    def buscar_por_chave(self, chave_acesso: str) -> "PaginaSieg":
        """Um XML pela chave, com o resultado explicado — o que o download em
        segundo plano precisa e `baixar_xml_por_chave` (da tela de DANFE) não
        diz: se foi limite de chamadas (esperar), se a nota não está no cofre
        (`xmls` vazio, sem erro) ou se foi falha."""
        chave = re.sub(r"\D", "", str(chave_acesso))
        if len(chave) != 44:
            return PaginaSieg(erro="chave de acesso inválida")
        params = {"api_key": self.api_key, "email": self.email,
                  "xmlType": "2" if chave[20:22] == "57" else "1", "downloadEvent": "false"}
        try:
            with httpx.Client(timeout=30.0) as client:
                resposta = client.post(self.base_url_xml, params=params,
                                       content=json.dumps(chave).encode("utf-8"),
                                       headers={"Content-Type": "application/json"})
        except Exception as e:                            # noqa: BLE001
            return PaginaSieg(erro=f"sem conexão com o SIEG ({type(e).__name__})")
        if resposta.status_code == 429:
            return PaginaSieg(esperar=_segundos(resposta.headers.get("retry-after"), 60),
                              erro="o SIEG pediu para esperar (limite de chamadas)")
        conteudo = resposta.text.strip()
        if resposta.status_code == 200 and ("<" in conteudo[:5] or conteudo.startswith('"<')):
            if conteudo.startswith('"') and conteudo.endswith('"'):
                conteudo = conteudo[1:-1]
            return PaginaSieg(xmls=[conteudo.replace('\\"', '"')])
        if resposta.status_code in (200, 404):
            return PaginaSieg(xmls=[])                    # não está no cofre
        return PaginaSieg(erro=f"o SIEG respondeu HTTP {resposta.status_code}: "
                               f"{_mensagem(resposta)}")

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

    def buscar_pagina(self, cnpj: str, papel: str, xml_type: int, data_inicio: str,
                      data_fim: str, skip: int = 0, take: int = 50) -> "PaginaSieg":
        """Uma página do lote (até 50 XMLs). Nunca levanta: o resultado diz o
        que houve — inclusive o "espere N segundos" do SIEG.

        Medido na conta do escritório em 29/09/2026: o lote aceita **2 chamadas
        por minuto** (a terceira volta 429 com `Retry-After`), exige `email`
        junto da `api_key` (sem ele: 401 "Não Autenticado") e devolve a lista
        EMBRULHADA: um texto JSON cujo conteúdo é outra lista JSON de base64.
        """
        payload = {
            "XmlType": xml_type, "Take": take, "Skip": skip,
            # Com eventos, a consulta por emissão sai errada (documentado por quem
            # integra a mesma API) — e evento não serve para auditar.
            "Downloadevent": False,
            papel: re.sub(r"\D", "", cnpj),
            "DataEmissaoInicio": data_inicio, "DataEmissaoFim": data_fim,
        }
        try:
            with httpx.Client(timeout=90.0) as client:
                resposta = client.post(self.base_url_lote_v2,
                                       params={"api_key": self.api_key, "email": self.email},
                                       json=payload)
        except Exception as e:                            # noqa: BLE001
            return PaginaSieg(erro=f"sem conexão com o SIEG ({type(e).__name__})")
        if resposta.status_code == 429:
            return PaginaSieg(esperar=_segundos(resposta.headers.get("retry-after"), 60),
                              erro="o SIEG pediu para esperar (limite de chamadas)")
        if resposta.status_code == 404:
            return PaginaSieg(xmls=[])             # nada no período
        if resposta.status_code != 200:
            return PaginaSieg(erro=f"o SIEG respondeu HTTP {resposta.status_code}: "
                                   f"{_mensagem(resposta)}")
        try:
            dados = resposta.json()
            if isinstance(dados, str):
                dados = json.loads(dados) if dados.strip().startswith("[") else []
        except ValueError:
            return PaginaSieg(erro="resposta do SIEG ilegível")
        if isinstance(dados, dict):
            # "Nenhum arquivo xml encontrado" chega assim, com 200.
            texto = str(dados.get("Message") or dados)
            if "encontrad" in texto.lower():
                return PaginaSieg(xmls=[])
            return PaginaSieg(erro=f"o SIEG respondeu: {texto[:150]}")
        xmls = []
        for item in dados if isinstance(dados, list) else []:
            try:
                xmls.append(base64.b64decode(item).decode("utf-8", errors="ignore"))
            except Exception:                             # noqa: BLE001
                continue
        return PaginaSieg(xmls=xmls, cheia=len(dados) >= take)

    def baixar_lote_xmls(
        self,
        cnpj: str,
        xml_type: int = 1,
        data_inicio: str = "2026-01-01",
        data_fim: str = "2026-01-31",
        tipo_cnpj: str = "CnpjEmit",
    ) -> list[str]:
        """Todas as páginas do período. Levanta `SiegIndisponivel` em vez de
        devolver lista vazia: até 29/09/2026 o 401 (faltava o e-mail) caía num
        `break` e a tela de "notas não lançadas" dizia "nenhuma" — sempre."""
        skip, xmls = 0, []
        while True:
            pagina = self.buscar_pagina(cnpj, tipo_cnpj, xml_type, data_inicio, data_fim, skip)
            if pagina.erro:
                raise SiegIndisponivel(pagina.erro, pagina.esperar)
            xmls.extend(pagina.xmls)
            if not pagina.cheia:
                return xmls
            skip += 50


class SiegIndisponivel(Exception):
    """O cofre não respondeu o lote. A mensagem é apresentável."""

    def __init__(self, mensagem: str, esperar: int = 0):
        super().__init__(mensagem)
        self.esperar = esperar


class PaginaSieg:
    def __init__(self, xmls=None, cheia: bool = False, erro: str = "", esperar: int = 0):
        self.xmls = xmls if xmls is not None else []
        self.cheia = cheia
        self.erro = erro
        self.esperar = esperar


def _segundos(valor, padrao: int) -> int:
    try:
        return max(1, int(str(valor).strip()))
    except (TypeError, ValueError):
        return padrao


def _mensagem(resposta) -> str:
    try:
        dados = resposta.json()
        if isinstance(dados, dict):
            return str(dados.get("Message") or dados)[:150]
    except ValueError:
        pass
    return resposta.text[:150]
