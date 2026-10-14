"""Guarda dos XMLs de NFS-e em disco, na pasta das NFS-e do fiscal.

Caminho: <RAIZ>/<NOME DA EMPRESA> <CNPJ>/<AAAA-MM-DD>/NFSe_<chave>.xml

Um padrão só para tudo que o Synax grava aqui (lote, busca avulsa no ADN,
coleta automática e importação de arquivos), e é o padrão que a pasta da rede
JÁ tinha em 01/10/2026: uma pasta permanente por estabelecimento, nomeada pela
razão social e pelo CNPJ — 158 delas, criadas pelo robô do SIEG. Até ali o
Synax gravava em outro servidor e por CNPJ do TOMADOR: a nota que a empresa
emitia para pessoa física virava uma pasta com o CPF do cliente (72 de 100
pastas no primeiro lote grande), e as notas de uma empresa ficavam espalhadas.

Três regras que não se deduzem do código:

1. **A pasta da empresa é ACHADA pelo CNPJ antes de ser criada.** O nome que
   existe na rede não sai do nome do arquivo do certificado (só 62 de 175
   batem: há "_", "SENHA 13579046", "2026-2027" e até CNPJ com um dígito a
   menos no nome do .pfx). Por isso vale a pasta que termina com " <CNPJ>"; só
   na falta dela o nome é montado — pelo nome do .pfx até o primeiro bloco de
   11 ou mais dígitos (pedido do Sandro em 01/10/2026), ou pela razão social do
   cadastro quando o arquivo não ajuda. Matriz e filial têm CNPJ diferente,
   então nunca caem na mesma pasta.

2. **Uma subpasta por DIA de execução**, não por competência: cada uma guarda o
   que chegou naquela busca (o ADN entrega por NSU, não por mês). Rodar duas
   vezes no mesmo dia reaproveita a mesma subpasta, e ela só é criada quando
   chega o primeiro documento — empresa sem nota não deixa pasta vazia.

3. **O arquivo leva a CHAVE da nota**, não o NSU: é o que identifica a NFS-e
   fora do Synax. Evento (cancelamento, por exemplo) carrega a chave da nota a
   que se refere, então ganha prefixo próprio — com o mesmo nome, o evento
   SOBRESCREVERIA a nota.

A leitura procura também nas raízes antigas (`RAIZES_ANTERIORES`): o banco
guarda o caminho RELATIVO, e as notas gravadas antes da mudança continuam
abrindo e entrando no ZIP sem mover arquivo nenhum.
"""
import os
import io
import re
import logging
import zipfile
from datetime import datetime, timedelta

from defusedxml import ElementTree as ET

from ...core.fuso import FUSO_LOCAL_HORAS

logger = logging.getLogger(__name__)

RAIZ_PADRAO = (r"\\srv-dc02\documentos\Contratos e Alterações\Fiscal"
               r"\_____________ ARQUIVOS APURAÇÃO\NFS-E-")

# Onde o Synax gravava até 01/10/2026. Só leitura: nada novo vai para lá.
RAIZES_ANTERIORES = (r"\\srv-ap02\Desenvolvimento\NFS-E",)

# O robô do SIEG corta a razão social em 40 caracteres; seguir o mesmo corte
# mantém as pastas novas com a cara das que já estão lá.
TAMANHO_NOME = 40

_PROIBIDOS = re.compile(r'[\\/*?:"<>|\x00-\x1f]')


def raiz() -> str:
    return os.environ.get("NFSE_STORAGE_DIR") or RAIZ_PADRAO


def _raizes_de_leitura() -> list[str]:
    # Com NFSE_STORAGE_DIR definido (testes, cópia do sistema), as raízes
    # antigas não entram: leitura fora da raiz configurada seria surpresa.
    if os.environ.get("NFSE_STORAGE_DIR"):
        return [raiz()]
    return [raiz(), *RAIZES_ANTERIORES]


def _seguro(parte: str) -> str:
    """Impede que dado vindo do XML escape do diretório (../, barras, etc.)."""
    limpo = re.sub(r"[^A-Za-z0-9._-]", "", str(parte or ""))
    return limpo or "desconhecido"


def _limpar_nome(texto: str) -> str:
    """Nome de pasta válido no Windows, sem os caracteres que ele recusa."""
    limpo = _PROIBIDOS.sub("", str(texto or ""))
    limpo = re.sub(r"\s+", " ", limpo).strip()
    limpo = limpo[:TAMANHO_NOME].strip()
    # Pasta terminada em ponto ou espaço o Windows grava sem eles e depois
    # não acha pelo nome pedido.
    return limpo.rstrip(" .")


def nome_do_certificado(arquivo: str | None) -> str:
    """Nome da empresa a partir do arquivo do certificado: tudo até o primeiro
    bloco de 11 ou mais dígitos (CPF ou CNPJ). Vazio se não sobrar um nome.

    "BIANCARDI AUTO PECAS LTDA 20149617000180.pfx" -> "BIANCARDI AUTO PECAS LTDA"
    """
    base = os.path.splitext(os.path.basename(arquivo or ""))[0]
    achado = re.search(r"\d{11,}", base)
    nome = base[:achado.start()] if achado else base
    nome = re.sub(r"[_\s]+", " ", nome).strip(" -_.")
    # "1.pfx", "2.pfx": sem letra suficiente o nome não identifica ninguém.
    return nome if len(re.sub(r"[^A-Za-zÀ-ÿ]", "", nome)) >= 3 else ""


def pastas_existentes() -> list[str]:
    """Nomes das pastas no primeiro nível da raiz. Lista vazia se a rede cair."""
    try:
        return sorted(e.name for e in os.scandir(raiz()) if e.is_dir())
    except OSError:
        logger.warning("Não foi possível listar a pasta das NFS-e: %s", raiz(), exc_info=True)
        return []


def pasta_da_empresa(cnpj: str, nome: str, existentes: list[str] | None = None) -> str:
    """Nome da pasta permanente do estabelecimento (relativo à raiz).

    `existentes` deixa o lote listar a rede UMA vez para todas as empresas.
    """
    cnpj = re.sub(r"\D", "", cnpj or "")
    nomes = pastas_existentes() if existentes is None else existentes
    if len(cnpj) in (11, 14):
        for pasta in nomes:
            if pasta.endswith(" " + cnpj):
                return pasta
    nome_limpo = _limpar_nome(nome) or "SEM NOME"
    return f"{nome_limpo} {cnpj}" if cnpj else nome_limpo


def dia_de_hoje() -> str:
    """AAAA-MM-DD no horário de Brasília: rodar às 22h não pode cair no dia
    seguinte (o servidor guarda UTC)."""
    return (datetime.utcnow() + timedelta(hours=FUSO_LOCAL_HORAS)).strftime("%Y-%m-%d")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def identificar(conteudo: bytes) -> dict:
    """O que é o documento e qual a chave dele — lido do próprio XML.

    {"tipo": "nfse" | "evento" | "outro", "chave": str, "id": str}
    """
    try:
        raiz_xml = ET.fromstring(conteudo)
    except Exception:
        return {"tipo": "outro", "chave": "", "id": ""}

    nome_raiz = _local(raiz_xml.tag)
    chave = id_inf = ""
    for el in raiz_xml.iter():
        tag = _local(el.tag)
        if tag in ("infnfse", "infevento", "infpedreg") and not id_inf:
            id_inf = el.get("Id") or ""
        elif tag == "chnfse" and not chave:
            chave = (el.text or "").strip()

    so_digitos = re.sub(r"^\D+", "", id_inf)
    if "evento" in nome_raiz:
        return {"tipo": "evento", "chave": chave, "id": so_digitos}
    if id_inf.upper().startswith("NFS"):
        # Na nota, a chave mora no Id do infNFSe ("NFS" + 50 dígitos), e não
        # num elemento — por isso o VmHub gravava "NFSe_<número>_.xml".
        return {"tipo": "nfse", "chave": so_digitos, "id": id_inf}
    return {"tipo": "outro", "chave": chave, "id": so_digitos}


def nome_do_arquivo(conteudo: bytes, reserva: str, prestador: str = "",
                    numero: str = "") -> str:
    """NFSe_<chave>.xml para nota, Evento_<id>.xml para evento.

    Nota ABRASF (municipal) não tem chave nacional: vale NFSe_<prestador>_
    <número>, que é a chave dela no banco — com o nome original, dois
    "nota.xml" do mesmo dia se sobrescreveriam. `reserva` (NSU ou nome
    original) entra quando o XML não traz identificação nenhuma: o arquivo é
    gravado do mesmo jeito, porque documento do ADN que não for salvo se perde
    quando o cursor anda.
    """
    info = identificar(conteudo)
    if info["tipo"] == "nfse" and info["chave"]:
        return f"NFSe_{_seguro(info['chave'])}.xml"
    if info["tipo"] == "evento":
        ident = info["id"] or (f"{info['chave']}_{reserva}" if info["chave"] else reserva)
        return f"Evento_{_seguro(ident)}.xml"
    if prestador and numero:
        return f"NFSe_{_seguro(prestador)}_{_seguro(numero)}.xml"
    base = os.path.splitext(os.path.basename(str(reserva or "")))[0]
    return f"{_seguro(base)}.xml"


def gravar_em(pasta_rel: str, nome: str, conteudo: bytes) -> str | None:
    """Grava os bytes COMO VIERAM em <raiz>/<pasta_rel>/<nome>.

    Devolve o caminho relativo, ou None se a rede estiver fora — quem chama
    decide se isso para o trabalho (não para: a nota fica no banco).
    """
    partes = [p for p in re.split(r"[\\/]+", pasta_rel or "") if p not in ("", ".", "..")]
    rel = os.path.join(*partes, os.path.basename(nome)) if partes else os.path.basename(nome)
    destino = os.path.join(raiz(), rel)
    try:
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        with open(destino, "wb") as f:
            f.write(conteudo)
        return rel
    except OSError:
        logger.warning("Não foi possível gravar NFS-e em %s", destino, exc_info=True)
        return None


def caminho_completo(rel: str) -> str:
    return os.path.join(raiz(), rel or "")


def ler(rel: str) -> bytes | None:
    if not rel:
        return None
    for base in _raizes_de_leitura():
        try:
            with open(os.path.join(base, rel), "rb") as f:
                return f.read()
        except OSError:
            continue
    return None


def zipar(itens: list[tuple[str, str]]) -> io.BytesIO:
    """Monta um ZIP em memória. `itens` = [(caminho_relativo, nome_no_zip)]."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, nome in itens:
            dados = ler(rel)
            if dados is not None:
                z.writestr(nome, dados)
    buffer.seek(0)
    return buffer


def disponivel() -> bool:
    """A raiz está acessível para escrita?"""
    try:
        os.makedirs(raiz(), exist_ok=True)
        return os.access(raiz(), os.W_OK)
    except OSError:
        return False
