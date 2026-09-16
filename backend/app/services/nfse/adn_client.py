"""Cliente do ADN (Ambiente de Dados Nacional) da NFS-e.

Autenticação mTLS com certificado A1. O `.pfx` é recebido na requisição, usado
e descartado — NADA é gravado no banco nem persistido em disco de forma
duradoura.

Ressalva honesta: o `ssl` do Python só carrega cadeia de certificado a partir de
ARQUIVO — não existe API em memória no stdlib. Então o PEM é escrito num
temporário com permissão restrita e apagado no `finally`, sempre. É o mínimo
inevitável; o resto do fluxo nunca toca disco.
"""
import os
import re
import time
import gzip
import base64
import logging
import warnings
import tempfile
import contextlib
from typing import Iterator

import httpx
from cryptography.hazmat.primitives.serialization import (
    pkcs12, Encoding, PrivateFormat, NoEncryption,
)

logger = logging.getLogger(__name__)

URL_BASE = os.environ.get("ADN_NFSE_URL", "https://adn.nfse.gov.br/contribuintes/DFe")
LOTE_MAX = 50

# Um timeout único de 90s era veneno em lote: 167 empresas × 40 páginas × 90s
# de espera é tempo de sobra para a tela ficar "consultando" a manhã inteira.
# Separado assim, um servidor que não responde custa 10s, não 90.
TEMPO_LIMITE = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)

# Teto de relógio por empresa. Ao estourar, o que já foi baixado está gravado e
# o cursor guardado — a próxima execução continua de onde parou.
LIMITE_EMPRESA_SEG = 60.0


class CertificadoInvalido(Exception):
    """Senha errada, arquivo corrompido ou formato não suportado."""


class RespostaInesperada(Exception):
    """O ADN respondeu algo que não sabemos interpretar.

    Existe para que a consulta NUNCA termine em silêncio com zero notas: se o
    ADN recusa a chamada, o operador precisa ver o motivo na tela.
    """


def _url_do_nsu(nsu: int) -> str:
    """O NSU vai no caminho: GET /contribuintes/DFe/{NSU}."""
    return f"{URL_BASE.rstrip('/')}/{int(nsu)}"


def abrir_pkcs12(pfx: bytes, senha: str):
    """Abre o .pfx e devolve (chave, certificado, cadeia).

    72% dos certificados A1 do escritório vêm codificados em BER, não em DER —
    a AC emissora gera assim. O `cryptography` aceita, mas emite um aviso por
    arquivo aberto; num lote de 167 empresas isso enterra o log em ruído. O
    aviso é silenciado AQUI e só aqui, para que qualquer outro continue visível.

    Se um dia a biblioteca transformar o BER em erro (ela avisa que pode), a
    mensagem abaixo diz exatamente isso — em vez de acusar a senha errada.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*BER.*")
        try:
            return pkcs12.load_key_and_certificates(
                pfx, senha.encode("utf-8") if senha else None
            )
        except Exception as err:
            detalhe = str(err).lower()
            if "asn" in detalhe or "ber" in detalhe or "der" in detalhe:
                raise CertificadoInvalido(
                    "O arquivo do certificado não pôde ser interpretado (codificação "
                    "BER/DER). Não é a senha — é o formato do arquivo."
                ) from err
            raise CertificadoInvalido(
                "Não foi possível abrir o certificado. Verifique a senha.") from err


@contextlib.contextmanager
def _material_mtls(pfx: bytes, senha: str):
    """Converte o .pfx em par de arquivos PEM temporários e os remove ao sair."""
    chave, cert, extras = abrir_pkcs12(pfx, senha)

    if chave is None or cert is None:
        raise CertificadoInvalido("O arquivo não contém um par certificado/chave válido.")

    dir_tmp = tempfile.mkdtemp(prefix="adn_")
    cert_path = os.path.join(dir_tmp, "cert.pem")
    key_path = os.path.join(dir_tmp, "key.pem")
    try:
        # O 0600 vale no Linux. No Windows — que é onde isto roda — ele é
        # IGNORADO: o arquivo herda a ACL do %TEMP% (usuário do processo,
        # SISTEMA e Administradores). Testei restringir a ACL e não adianta:
        # administrador local pode tomar posse de qualquer arquivo. Ou seja, a
        # chave privada fica legível para quem já é admin da máquina durante os
        # segundos da consulta. O que de fato limita a exposição é a janela ser
        # curta e o `finally` abaixo apagar o arquivo sempre.
        with open(os.open(cert_path, os.O_CREAT | os.O_WRONLY, 0o600), "wb") as f:
            f.write(cert.public_bytes(Encoding.PEM))
            for extra in (extras or []):
                f.write(extra.public_bytes(Encoding.PEM))
        with open(os.open(key_path, os.O_CREAT | os.O_WRONLY, 0o600), "wb") as f:
            f.write(chave.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        yield cert_path, key_path
    finally:
        for p in (cert_path, key_path):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(dir_tmp)
        except OSError:
            pass


def _descompactar(carga: str) -> str | None:
    """O ADN devolve o XML em Base64 + GZIP."""
    try:
        bruto = base64.b64decode(carga)
    except Exception:
        return None
    try:
        return gzip.decompress(bruto).decode("utf-8", errors="ignore")
    except (OSError, EOFError):
        # Alguns lotes vêm sem compactação
        try:
            return bruto.decode("utf-8", errors="ignore")
        except Exception:
            return None


def _extrair_documentos(payload) -> list[tuple[int, str]]:
    """Normaliza a resposta em [(nsu, xml)]. Tolera variações de nome de campo."""
    if isinstance(payload, dict):
        for chave in ("LoteDFe", "loteDFe", "lote", "documentos", "Documentos", "DFe", "dfe"):
            if isinstance(payload.get(chave), list):
                payload = payload[chave]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        return []

    saida = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        nsu = item.get("NSU") or item.get("nsu") or item.get("Nsu")
        carga = (item.get("ArquivoXml") or item.get("arquivoXml")
                 or item.get("DocumentoXml") or item.get("documento")
                 or item.get("XmlCompactado") or item.get("conteudo"))
        if carga is None:
            continue
        xml = _descompactar(carga) if isinstance(carga, str) else None
        if xml:
            try:
                saida.append((int(nsu), xml))
            except (TypeError, ValueError):
                saida.append((0, xml))
    return saida


def _resumo_corpo(resposta) -> str:
    """Trecho curto do corpo, para a mensagem que o operador vê."""
    texto = (resposta.text or "").strip().replace("\n", " ")
    return (texto[:200] + "…") if len(texto) > 200 else (texto or "corpo vazio")


# O ADN sinaliza "nada a distribuir" no próprio corpo, com HTTP 200.
SEM_DOCUMENTOS = ("NENHUM_DOCUMENTO", "NAO_LOCALIZADO", "SEM_DOCUMENTO",
                  "NENHUM DOCUMENTO", "CONSUMO INDEVIDO")


def _tem_lote_vazio(corpo) -> bool:
    """Verdadeiro quando o ADN afirma que não há documentos (não é erro)."""
    if isinstance(corpo, list):
        return not corpo
    if not isinstance(corpo, dict):
        return False
    for chave in ("LoteDFe", "loteDFe", "lote", "documentos", "Documentos", "DFe", "dfe"):
        valor = corpo.get(chave)
        if isinstance(valor, list) and not valor:
            return True
    texto = " ".join(str(v) for v in corpo.values() if isinstance(v, (str, int))).upper()
    return any(marca in texto for marca in SEM_DOCUMENTOS)


def baixar_lote(pfx: bytes, senha: str, cnpj: str, ultimo_nsu: int = 0,
                max_paginas: int = 40,
                limite_seg: float = LIMITE_EMPRESA_SEG) -> Iterator[tuple[int, str]]:
    """Percorre o ADN a partir do NSU informado, devolvendo (nsu, xml).

    Gera os documentos em fluxo, para que quem consome possa gravar cada lote
    ANTES de avançar o cursor — perder o documento é irreversível (janela de
    90 dias no ADN), duplicar é inofensivo.

    Para no `limite_seg`. Parar cedo não perde nada: o consumidor já gravou as
    notas e o cursor, e a próxima execução retoma do mesmo ponto.
    """
    cnpj = re.sub(r"\D", "", cnpj or "")
    nsu = int(ultimo_nsu or 0)
    prazo = time.monotonic() + float(limite_seg)

    with _material_mtls(pfx, senha) as (cert_path, key_path):
        with httpx.Client(cert=(cert_path, key_path), timeout=TEMPO_LIMITE,
                          headers={"Accept": "application/json"}) as cliente:
            for _ in range(max_paginas):
                if time.monotonic() > prazo:
                    logger.info("Tempo esgotado para o CNPJ %s no NSU %s; retoma depois",
                                cnpj, nsu)
                    return
                url = _url_do_nsu(nsu)
                try:
                    resposta = cliente.get(url)
                except httpx.HTTPError as err:
                    logger.warning("Falha de rede no ADN (CNPJ %s, NSU %s)", cnpj, nsu, exc_info=True)
                    raise CertificadoInvalido(
                        f"Não foi possível conectar ao ADN ({type(err).__name__}). "
                        "Verifique o certificado e a rede."
                    ) from err

                if resposta.status_code in (204, 404):
                    return  # não há mais documentos
                if resposta.status_code in (401, 403):
                    raise CertificadoInvalido(
                        "O ADN recusou o certificado para este CNPJ "
                        f"(HTTP {resposta.status_code}). Confirme se o certificado pertence "
                        "à empresa ou se há procuração eletrônica."
                    )
                if resposta.status_code != 200:
                    # Silenciar aqui era o pior dos mundos: a tela mostrava
                    # "0 notas" sem dizer que o ADN tinha recusado a chamada.
                    logger.warning("ADN respondeu %s em %s: %s",
                                   resposta.status_code, url, resposta.text[:300])
                    raise RespostaInesperada(
                        f"ADN respondeu HTTP {resposta.status_code}: "
                        f"{_resumo_corpo(resposta)}")

                try:
                    corpo = resposta.json()
                except ValueError as err:
                    logger.warning("ADN devolveu corpo não-JSON em %s: %s", url, resposta.text[:300])
                    raise RespostaInesperada(
                        "ADN respondeu em formato inesperado (não é JSON): "
                        f"{_resumo_corpo(resposta)}") from err

                documentos = _extrair_documentos(corpo)
                if not documentos:
                    if _tem_lote_vazio(corpo):
                        return  # o ADN disse, com todas as letras, que não há nada
                    logger.warning("ADN 200 sem documentos reconhecíveis em %s: %s",
                                   url, str(corpo)[:300])
                    raise RespostaInesperada(
                        "ADN respondeu sem documentos reconhecíveis. "
                        f"Retorno: {str(corpo)[:200]}")

                for item in documentos:
                    yield item

                maior = max(n for n, _ in documentos)
                if maior <= nsu:
                    return  # sem avanço: evita laço infinito
                nsu = maior

                if len(documentos) < LOTE_MAX:
                    return  # última página
