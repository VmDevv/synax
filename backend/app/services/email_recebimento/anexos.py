"""Abre o e-mail, tira os XMLs dos anexos e diz de que empresa é cada um.

Três regras que não se deduzem do código:

1. **A empresa sai do CNPJ DE DENTRO do XML, nunca do remetente.** O "De:" de
   um e-mail é texto livre — qualquer um escreve o nome do cliente ali. É o
   mesmo princípio da trava do gerador de SPED: numa NF-e de entrada, a
   empresa é o DESTINATÁRIO; num CT-e, o TOMADOR. XML em que a empresa é a
   EMITENTE é nota de saída dela e não serve para nenhuma das duas telas.

2. **Anexo é entrada não confiável.** ZIP com teto de tamanho descomprimido
   e de quantidade conferido NO ÍNDICE, antes de descomprimir (bomba de
   descompressão); XML lido com `defusedxml` (o `xml_parser` da auditoria);
   RAR pelo mesmo extrator do upload, em pasta temporária descartável.

3. **Nada some.** Todo XML que não entrou sai com o motivo — de outra
   empresa, de saída, cancelado, ilegível —, e a tela o mostra. Mesma regra
   dos "Itens Não Conferidos": não aproveitar pode ser a resposta certa; não
   avisar, não.
"""
import html
import io
import os
import re
import shutil
import tempfile
import zipfile
from email import policy
from email.parser import BytesParser

from ...core.uploads_seguros import (ASSINATURAS_RAR, ASSINATURAS_ZIP,
                                     extrair_xmls_do_rar)

# Por e-mail, e não por pacote: um cliente que manda três ZIPs no mesmo
# e-mail não pode escapar do teto mandando em partes.
LIMITE_DESCOMPRIMIDO = 300 * 1024 * 1024
LIMITE_XMLS_POR_EMAIL = 5000
LIMITE_XML = 5 * 1024 * 1024          # uma NF-e com 990 itens tem ~1,5 MB
# O mesmo teto do upload de SPED na tela (core/uploads_seguros.py).
LIMITE_SPED = 300 * 1024 * 1024
BOM = b"\xef\xbb\xbf"
TAMANHO_TRECHO = 4000

AUTORIZADA = {"100", "150"}


def _so_digitos(texto) -> str:
    return "".join(c for c in str(texto or "") if c.isdigit())


# --------------------------------------------------------------- cabeçalho

def abrir(bruto: bytes):
    return BytesParser(policy=policy.default).parsebytes(bruto)


def remetente(msg) -> tuple[str, str]:
    """(nome, endereço)."""
    import email.utils
    nome, endereco = email.utils.parseaddr(str(msg.get("From") or ""))
    return (nome or "").strip()[:255], (endereco or "").strip().lower()[:320]


def texto_do_corpo(msg) -> str:
    """O corpo em texto, para o resumo. Prefere o texto puro; HTML vira texto
    de forma grosseira (tira tags, estilo e script) — basta para a IA entender
    o pedido, e não é exibido como HTML em lugar nenhum."""
    try:
        parte = msg.get_body(preferencelist=("plain", "html"))
    except Exception:                                     # noqa: BLE001
        parte = None
    if parte is None:
        return ""
    try:
        conteudo = parte.get_content()
    except Exception:                                     # noqa: BLE001
        return ""
    if not isinstance(conteudo, str):
        return ""
    if parte.get_content_subtype() == "html":
        conteudo = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", conteudo)
        conteudo = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", conteudo)
        conteudo = html.unescape(re.sub(r"<[^>]+>", " ", conteudo))
    # A resposta citada ("Em seg., fulano escreveu:", linhas com ">") é o
    # histórico da conversa, não o que chegou agora.
    linhas = []
    for linha in conteudo.splitlines():
        limpa = linha.strip()
        if limpa.startswith(">"):
            continue
        if re.match(r"^(Em .{5,120} escreveu:|On .{5,120} wrote:|-----\s*Original Message)", limpa):
            break
        linhas.append(limpa)
    texto = re.sub(r"\n{3,}", "\n\n", "\n".join(linhas)).strip()
    texto = re.sub(r"[ \t]{2,}", " ", texto)
    return texto[:TAMANHO_TRECHO]


# ----------------------------------------------------------------- anexos

def _anexos(msg):
    """(nome, bytes) de cada parte com arquivo — inclusive dentro de e-mail
    encaminhado como anexo, que é como a nota chega quando o cliente repassa
    o e-mail do fornecedor."""
    for parte in msg.walk():
        if parte.is_multipart():
            continue
        nome = parte.get_filename()
        if not nome:
            continue
        try:
            conteudo = parte.get_payload(decode=True)
        except Exception:                                 # noqa: BLE001
            conteudo = None
        if conteudo:
            yield str(nome), conteudo


class _Orcamento:
    def __init__(self):
        self.bytes = LIMITE_DESCOMPRIMIDO
        self.xmls = LIMITE_XMLS_POR_EMAIL

    def cabe(self, tamanho: int) -> bool:
        return self.xmls > 0 and tamanho <= self.bytes and tamanho <= LIMITE_XML

    def gastar(self, tamanho: int) -> None:
        self.bytes -= tamanho
        self.xmls -= 1


def e_sped(dados: bytes) -> bool:
    """O arquivo começa pelo registro 0000 — com ou sem BOM. Pelo CONTEÚDO:
    SPED chega como .txt, e .txt é também leia-me, log e mais nada."""
    inicio = dados[:16]
    return inicio.startswith(b"|0000|") or inicio.startswith(BOM + b"|0000|")


def _sped_do_zip(pacote, info, nome: str, orcamento: _Orcamento, recusas: list, speds: list):
    if info.file_size > LIMITE_SPED or info.file_size > orcamento.bytes:
        recusas.append({"arquivo": nome, "motivo": "arquivo texto grande demais para abrir"})
        return
    try:
        with pacote.open(info) as origem:
            dados = origem.read(LIMITE_SPED + 1)
    except Exception:                                     # noqa: BLE001
        recusas.append({"arquivo": nome, "motivo": "não foi possível abrir no ZIP"})
        return
    if e_sped(dados):
        orcamento.bytes -= len(dados)
        speds.append((nome, dados))


def _xmls_do_zip(nome_pacote: str, conteudo: bytes, orcamento: _Orcamento,
                 recusas: list, speds: list | None = None):
    try:
        pacote = zipfile.ZipFile(io.BytesIO(conteudo))
    except zipfile.BadZipFile:
        recusas.append({"arquivo": nome_pacote, "motivo": "ZIP corrompido ou incompleto"})
        return
    with pacote:
        for info in pacote.infolist():
            if info.is_dir():
                continue
            nome = f"{nome_pacote} › {info.filename}"
            if info.filename.lower().endswith(".txt") and speds is not None:
                _sped_do_zip(pacote, info, nome, orcamento, recusas, speds)
                continue
            if not info.filename.lower().endswith(".xml"):
                continue
            # O tamanho vem do ÍNDICE, conferido antes de descomprimir.
            if not orcamento.cabe(info.file_size):
                recusas.append({"arquivo": nome, "motivo": "passou do limite de tamanho "
                                "ou de quantidade de XMLs por e-mail"})
                continue
            try:
                with pacote.open(info) as origem:
                    dados = origem.read(LIMITE_XML + 1)
            except Exception:                             # noqa: BLE001
                recusas.append({"arquivo": nome, "motivo": "não foi possível abrir no ZIP"})
                continue
            if len(dados) > LIMITE_XML:
                recusas.append({"arquivo": nome, "motivo": "XML grande demais"})
                continue
            orcamento.gastar(len(dados))
            yield nome, dados


def _xmls_do_rar(nome_pacote: str, conteudo: bytes, orcamento: _Orcamento,
                 recusas: list):
    pasta = tempfile.mkdtemp(prefix="email_rar_")
    try:
        caminho = os.path.join(pasta, "pacote.rar")
        with open(caminho, "wb") as f:
            f.write(conteudo)
        destino = os.path.join(pasta, "xmls")
        os.makedirs(destino)
        try:
            extrair_xmls_do_rar(caminho, destino)
        except Exception as err:                          # noqa: BLE001
            recusas.append({"arquivo": nome_pacote,
                            "motivo": getattr(err, "detail", None) or "RAR não pôde ser aberto"})
            return
        for arquivo in sorted(os.listdir(destino)):
            alvo = os.path.join(destino, arquivo)
            tamanho = os.path.getsize(alvo)
            nome = f"{nome_pacote} › {arquivo}"
            if not orcamento.cabe(tamanho):
                recusas.append({"arquivo": nome, "motivo": "passou do limite de tamanho "
                                "ou de quantidade de XMLs por e-mail"})
                continue
            with open(alvo, "rb") as f:
                dados = f.read()
            orcamento.gastar(len(dados))
            yield nome, dados
    finally:
        shutil.rmtree(pasta, ignore_errors=True)


def xmls_do_email(msg) -> tuple[list, list, int]:
    """([(nome, bytes)], recusas, total de anexos) — só os XMLs."""
    xmls, _speds, recusas, total = arquivos_do_email(msg)
    return xmls, recusas, total


def arquivos_do_email(msg) -> tuple[list, list, list, int]:
    """(xmls, speds, recusas, total de anexos); xmls e speds são [(nome, bytes)].

    Pacote é reconhecido pela ASSINATURA, não pela extensão — mesma regra do
    upload: o WinRAR grava os dois formatos e renomear à mão é rotina. O SPED
    também é reconhecido pelo conteúdo (começa por |0000|), solto ou dentro
    de ZIP. Dentro de RAR, não: o extrator de RAR só traz .xml, e SPED em RAR
    por e-mail não apareceu.
    """
    orcamento = _Orcamento()
    xmls, speds, recusas, total = [], [], [], 0
    for nome, conteudo in _anexos(msg):
        total += 1
        minusculo = nome.lower()
        if e_sped(conteudo):
            if len(conteudo) > LIMITE_SPED or len(conteudo) > orcamento.bytes:
                recusas.append({"arquivo": nome, "motivo": "SPED grande demais"})
                continue
            orcamento.bytes -= len(conteudo)
            speds.append((nome, conteudo))
        elif conteudo.startswith(ASSINATURAS_ZIP):
            xmls.extend(_xmls_do_zip(nome, conteudo, orcamento, recusas, speds))
        elif conteudo.startswith(ASSINATURAS_RAR):
            xmls.extend(_xmls_do_rar(nome, conteudo, orcamento, recusas))
        elif minusculo.endswith(".xml"):
            if not orcamento.cabe(len(conteudo)):
                recusas.append({"arquivo": nome, "motivo": "passou do limite de tamanho "
                                "ou de quantidade de XMLs por e-mail"})
                continue
            orcamento.gastar(len(conteudo))
            xmls.append((nome, conteudo))
        elif minusculo.endswith((".zip", ".rar")):
            recusas.append({"arquivo": nome, "motivo": "diz ser pacote, mas o conteúdo não é "
                            "ZIP nem RAR"})
        # PDF, imagem, planilha: não são XML e não são recusa — só não
        # interessam a esta leitura. O resumo da manhã cita o e-mail do mesmo jeito.
    return xmls, speds, recusas, total


# ---------------------------------------------------------- classificação

def mapa_de_empresas(tenants) -> dict:
    """{CNPJ só dígitos: tenant}."""
    return {_so_digitos(t.cnpj): t for t in tenants if len(_so_digitos(t.cnpj)) == 14}


def ler_documento(dados: bytes):
    """(documento lido, "NF-e"|"CT-e", CNPJ da empresa do lado de entrada) ou
    None quando não é NF-e nem CT-e."""
    from ..audit_engine.xml_parser import parse_cte_xml, parse_nfe_xml

    pasta = tempfile.mkdtemp(prefix="email_xml_")
    try:
        caminho = os.path.join(pasta, "doc.xml")
        with open(caminho, "wb") as f:
            f.write(dados)
        nfe = parse_nfe_xml(caminho)
        # Mesmo crivo do gerador de SPED: o parser de NF-e devolve lista vazia
        # e nenhum erro para QUALQUER XML (inclusive CT-e); o número da nota é
        # o que só existe em NF-e.
        if "error" not in nfe and nfe.get("num") and nfe.get("items"):
            return nfe, "NF-e", nfe.get("dest_id")
        cte = parse_cte_xml(caminho)
        if "error" in cte or not cte.get("num"):
            return None
        return cte, "CT-e", cte.get("tomador_id")
    finally:
        shutil.rmtree(pasta, ignore_errors=True)


def campos_do_documento(doc: dict, modelo: str) -> dict:
    """Os campos do EmailXml que saem do próprio XML."""
    from ..sped_builder.gerador import _entrada_iso

    try:
        valor = float(doc.get("vl_doc") or 0)
    except (TypeError, ValueError):
        valor = None
    return {"chave": _so_digitos(doc.get("chave")), "modelo": modelo,
            "competencia": _entrada_iso(doc, modelo)[:7] or None,
            "numero": str(doc.get("num") or "")[:20],
            "emitente_nome": str(doc.get("emit_name") or "")[:255],
            "emitente_cnpj": _so_digitos(doc.get("emit_id"))[:14], "valor": valor}


def classificar(nome: str, dados: bytes, empresas: dict) -> dict:
    """Lê um XML e diz o que ele é.

    Devolve {"ok": True, ...campos do EmailXml...} ou {"ok": False, "motivo"}.
    """
    lido = ler_documento(dados)
    if lido is None:
        return {"ok": False, "arquivo": nome,
                "motivo": "não é XML de NF-e nem de CT-e (pode ser NFS-e, evento ou "
                          "outro documento)"}
    doc, modelo, empresa_id = lido

    chave = _so_digitos(doc.get("chave"))
    rotulo = f"{modelo} {doc.get('num') or ''}".strip()
    if len(chave) != 44:
        return {"ok": False, "arquivo": nome, "documento": rotulo,
                "motivo": "XML sem chave de acesso válida"}
    c_stat = str(doc.get("c_stat") or "").strip()
    if c_stat and c_stat not in AUTORIZADA:
        return {"ok": False, "arquivo": nome, "documento": rotulo, "chave": chave,
                "motivo": f"{modelo} com cStat {c_stat}: não está autorizada (cancelada "
                          "ou denegada)"}

    alvo = _so_digitos(empresa_id)
    empresa = empresas.get(alvo)
    if empresa is None:
        emitente = empresas.get(_so_digitos(doc.get("emit_id")))
        if emitente is not None:
            motivo = (f"nota emitida por {emitente.name} (saída dela), não é entrada"
                      if modelo == "NF-e" else
                      f"CT-e emitido por {emitente.name}, que não é a tomadora")
        elif modelo == "NF-e":
            motivo = (f"nenhuma empresa cadastrada com o CNPJ do destinatário "
                      f"({alvo or 'sem CNPJ'})")
        else:
            motivo = f"nenhuma empresa cadastrada com o CNPJ do tomador ({alvo or 'sem CNPJ'})"
        return {"ok": False, "arquivo": nome, "documento": rotulo, "chave": chave,
                "motivo": motivo}
    if not getattr(empresa, "ativo", True):
        return {"ok": False, "arquivo": nome, "documento": rotulo, "chave": chave,
                "motivo": f"{empresa.name} está inativa no cadastro"}

    return {"ok": True, "arquivo": nome, "tenant_id": str(empresa.id),
            "empresa": empresa.name, **campos_do_documento(doc, modelo)}


def classificar_sped(nome: str, dados: bytes, empresas: dict) -> dict:
    """De qual empresa e de qual mês é o SPED, pelo registro 0000.

    O leiaute sai da MESMA `detectar_leiaute` do upload: os dois arquivos
    começam com |0000| e só diferem de posição, e ler um pelo outro daria o
    CNPJ errado sem erro nenhum.
    """
    from ..audit_engine.sped_contribuicoes import LEIAUTE_CONTRIBUICOES
    from ..audit_engine.sped_parser import (CONTRIBUICOES, LEIAUTE_FISCAL,
                                            _decodificar, detectar_leiaute)

    pasta = tempfile.mkdtemp(prefix="email_sped_")
    try:
        caminho = os.path.join(pasta, "sped.txt")
        with open(caminho, "wb") as f:
            f.write(dados[:8192])
        leiaute = detectar_leiaute(caminho)
    finally:
        shutil.rmtree(pasta, ignore_errors=True)
    if not leiaute:
        # ECD e ECF também começam por |0000|, e o campo 02 diz qual é. O
        # primeiro caso real (29/09/2026) foram 6 arquivos de uma transferência
        # de contabilidade, recusados como "cabeçalho ilegível" — verdade
        # técnica que não dizia nada a quem lê.
        primeira = dados[:200].replace(BOM, b"").split(b"|")
        registro = primeira[2].decode("latin-1").strip().upper() if len(primeira) > 2 else ""
        tipo = {"LECD": "SPED Contábil (ECD)", "LECF": "ECF"}.get(registro)
        motivo = (f"é {tipo}, não EFD ICMS/IPI nem Contribuições — a auditoria de "
                  "entradas não se aplica" if tipo else
                  "SPED com cabeçalho (0000) ilegível: não dá para saber o leiaute")
        return {"ok": False, "arquivo": nome, "motivo": motivo}
    posicoes = LEIAUTE_CONTRIBUICOES if leiaute == CONTRIBUICOES else LEIAUTE_FISCAL
    campos = []
    for linha in _decodificar(dados[:8192]).splitlines():
        if linha.lstrip("\ufeff").startswith("|0000|"):
            campos = linha.lstrip("\ufeff").strip().split("|")
            break

    def campo(n):
        return campos[n].strip() if len(campos) > n else ""

    cnpj = _so_digitos(campo(posicoes.cnpj))
    dt_ini = campo(posicoes.dt_ini)
    competencia = f"{dt_ini[4:8]}-{dt_ini[2:4]}" if len(dt_ini) == 8 and dt_ini.isdigit() else None
    empresa = empresas.get(cnpj)
    if empresa is None:
        return {"ok": False, "arquivo": nome,
                "motivo": f"SPED de empresa não cadastrada (CNPJ {cnpj or 'ilegível'})"}
    if not getattr(empresa, "ativo", True):
        return {"ok": False, "arquivo": nome, "motivo": f"SPED de {empresa.name}, inativa no cadastro"}
    return {"ok": True, "arquivo": nome, "tenant_id": str(empresa.id), "empresa": empresa.name,
            "leiaute": leiaute, "competencia": competencia}
