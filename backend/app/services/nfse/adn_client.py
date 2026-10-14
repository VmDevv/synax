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


def _url_do_nsu(nsu: int, cnpj: str | None = None) -> str:
    """O NSU vai no caminho: GET /contribuintes/DFe/{NSU}?lote=true&cnpjConsulta=.

    Sem `cnpjConsulta` o ADN entrega os documentos do DONO DO CERTIFICADO, e
    não do estabelecimento que se queria: 15 filiais estavam cadastradas com o
    certificado da matriz (MEGA, PERCAL, M.R., NORTE VISION, Associação dos
    Pescadores) e baixavam as notas da matriz como se fossem delas. Com o
    parâmetro, o certificado da matriz consulta a filial — o ADN só exige a
    mesma raiz de CNPJ (Manual dos Contribuintes das APIs do ADN).
    """
    url = f"{URL_BASE.rstrip('/')}/{int(nsu)}?lote=true"
    cnpj = re.sub(r"\D", "", cnpj or "")
    return f"{url}&cnpjConsulta={cnpj}" if len(cnpj) == 14 else url


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


# ICP-Brasil: o CNPJ do titular de um e-CNPJ vai num "otherName" do SAN com
# este OID (DOC-ICP-04). CN ("RAZAO:CNPJ") e serialNumber ficam de reserva.
OID_CNPJ_ICP = "2.16.76.1.3.3"


def cnpj_do_certificado(pfx: bytes, senha: str) -> str | None:
    """CNPJ do DONO do certificado, lido de dentro dele. None se não abrir ou
    não houver CNPJ (e-CPF, por exemplo) — quem chama não bloqueia por isso.

    Existe porque o ADN não recebe CNPJ na consulta: `GET /DFe/{NSU}` devolve
    os documentos de quem é dono do certificado. A filial da M.R. estava
    cadastrada com o certificado da matriz e "baixou" 49 notas da matriz como
    se fossem dela (01/10/2026) — e as notas da filial nunca vieram.
    """
    from cryptography import x509
    from cryptography.x509.oid import ExtensionOID, NameOID

    try:
        _, cert, _ = abrir_pkcs12(pfx, senha)
    except Exception:
        return None
    if cert is None:
        return None

    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        for outro in san.get_values_for_type(x509.OtherName):
            if outro.type_id.dotted_string == OID_CNPJ_ICP:
                achado = re.search(rb"\d{14}", outro.value or b"")
                if achado:
                    return achado.group().decode()
    except Exception:
        pass
    for atributo in cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME):
        achado = re.search(r":\s*(\d{14})(?!\d)", str(atributo.value))
        if achado:
            return achado.group(1)
    for atributo in cert.subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER):
        digitos = re.sub(r"\D", "", str(atributo.value))
        if len(digitos) == 14:
            return digitos
    return None


def certificado_de_outra_empresa(pfx: bytes, senha: str, cnpj: str) -> str | None:
    """Mensagem quando o certificado é de OUTRA empresa (raiz de CNPJ
    diferente) — aí o ADN recusaria a consulta. Matriz consultando filial é a
    mesma empresa e passa: quem separa é o `cnpjConsulta`. None = pode seguir."""
    dono = cnpj_do_certificado(pfx, senha)
    cnpj = re.sub(r"\D", "", cnpj or "")
    if not dono or len(cnpj) != 14 or dono[:8] == cnpj[:8]:
        return None
    return (f"O certificado é de outra empresa (CNPJ {dono}), e não do {cnpj}. "
            "Vincule o certificado da própria empresa ou da matriz dela.")


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


# "surrogateescape", e não "ignore": byte que não é UTF-8 válido vira um
# marcador reversível em vez de sumir, e `bytes_do_xml` devolve o arquivo
# EXATAMENTE como o ADN mandou. Com "ignore" o XML gravado podia diferir do
# original — e é esse arquivo que o fiscal guarda como a nota.
_DECODIFICACAO = "surrogateescape"


def bytes_do_xml(xml: str) -> bytes:
    """Os bytes originais do documento devolvido por `baixar_lote`."""
    return xml.encode("utf-8", _DECODIFICACAO)


def _descompactar(carga: str) -> str | None:
    """O ADN devolve o XML em Base64 + GZIP."""
    try:
        bruto = base64.b64decode(carga)
    except Exception:
        return None
    try:
        return gzip.decompress(bruto).decode("utf-8", _DECODIFICACAO)
    except (OSError, EOFError):
        # Alguns lotes vêm sem compactação
        try:
            return bruto.decode("utf-8", _DECODIFICACAO)
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
                  "NENHUM DOCUMENTO")

# "Consumo indevido" NÃO é "nada a distribuir": é o ADN recusando a consulta
# por excesso de chamadas em pouco tempo. Até 24/09/2026 ele estava na lista
# acima e virava "0 notas novas" — engano pequeno na tela, onde alguém acabou de
# clicar, e grave na coleta automática, que mandaria um informativo dizendo que
# a empresa não recebeu nota quando a consulta nem aconteceu.
CONSUMO_INDEVIDO = ("CONSUMO INDEVIDO", "CONSUMO_INDEVIDO")


class ConsumoIndevido(RespostaInesperada):
    """O ADN adiou a consulta. Nada se perde: o cursor fica onde estava."""


def _consumo_indevido(corpo) -> bool:
    if isinstance(corpo, dict):
        texto = " ".join(str(v) for v in corpo.values() if isinstance(v, (str, int)))
    elif isinstance(corpo, str):
        texto = corpo
    else:
        return False
    texto = texto.upper()
    return any(marca in texto for marca in CONSUMO_INDEVIDO)


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
                limite_seg: float = LIMITE_EMPRESA_SEG,
                estado: dict | None = None) -> Iterator[tuple[int, str]]:
    """Percorre o ADN a partir do NSU informado, devolvendo (nsu, xml).

    Gera os documentos em fluxo, para que quem consome possa gravar cada lote
    ANTES de avançar o cursor — perder o documento é irreversível (janela de
    90 dias no ADN), duplicar é inofensivo.

    Para no `limite_seg`. Parar cedo não perde nada: o consumidor já gravou as
    notas e o cursor, e a próxima execução retoma do mesmo ponto.

    `estado["parada"]` diz por que parou: "fim" (o ADN não tem mais nada),
    "tempo" ou "paginas" (ainda há documentos — a próxima busca continua).
    Sem isso, "a empresa está em dia" e "faltou tempo" saíam iguais na tela.
    """
    cnpj = re.sub(r"\D", "", cnpj or "")
    nsu = int(ultimo_nsu or 0)
    prazo = time.monotonic() + float(limite_seg)
    estado = estado if estado is not None else {}
    estado["parada"] = "paginas"

    with _material_mtls(pfx, senha) as (cert_path, key_path):
        with httpx.Client(cert=(cert_path, key_path), timeout=TEMPO_LIMITE,
                          headers={"Accept": "application/json"}) as cliente:
            for _ in range(max_paginas):
                if time.monotonic() > prazo:
                    logger.info("Tempo esgotado para o CNPJ %s no NSU %s; retoma depois",
                                cnpj, nsu)
                    estado["parada"] = "tempo"
                    return
                url = _url_do_nsu(nsu, cnpj)
                try:
                    resposta = cliente.get(url)
                except httpx.HTTPError as err:
                    logger.warning("Falha de rede no ADN (CNPJ %s, NSU %s)", cnpj, nsu, exc_info=True)
                    raise CertificadoInvalido(
                        f"Não foi possível conectar ao ADN ({type(err).__name__}). "
                        "Verifique o certificado e a rede."
                    ) from err

                if resposta.status_code in (204, 404):
                    estado["parada"] = "fim"
                    return  # não há mais documentos
                if resposta.status_code == 429:
                    # Limite de requisições do ADN. Não é "sem notas" (o VmHub
                    # mostrava "SUCESSO (Vazio)" aqui) nem falha da empresa: o
                    # que veio antes já está gravado e a próxima busca continua.
                    raise ConsumoIndevido(
                        "O ADN limitou as consultas (HTTP 429, muitas chamadas em pouco "
                        "tempo). O que já veio está gravado; a próxima busca continua "
                        "do mesmo ponto.")
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
                    if _consumo_indevido(corpo):
                        raise ConsumoIndevido(
                            "O ADN adiou a consulta por consumo indevido (chamadas "
                            "repetidas em pouco tempo). Nada se perdeu: a próxima "
                            "consulta continua do mesmo ponto.")
                    if _tem_lote_vazio(corpo):
                        estado["parada"] = "fim"
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
                    estado["parada"] = "fim"
                    return  # sem avanço: evita laço infinito
                nsu = maior

                if len(documentos) < LOTE_MAX:
                    estado["parada"] = "fim"
                    return  # última página
