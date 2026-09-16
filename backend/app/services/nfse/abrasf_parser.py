"""Leitura de XML de NFS-e — layout NACIONAL e layout ABRASF municipal.

Cada município publica o ABRASF com pequenas variações (nomes de tag, presença
de namespace, aninhamento). Por isso a busca é feita por NOME LOCAL da tag,
ignorando namespace e hierarquia — é o que sobrevive à diversidade real dos
arquivos que chegam.

POR QUE EXISTEM DOIS LEITORES
-----------------------------
Este módulo só entendia ABRASF. Mas o que chega pelo ADN (Ambiente de Dados
Nacional) é o layout NACIONAL, do padrão `http://www.sped.fazenda.gov.br/nfse`,
que não compartilha um único nome de tag com o ABRASF:

    ABRASF                          NACIONAL
    <Numero>                        <nNFSe>
    <DataEmissao>                   <dhEmi>
    <Competencia>                   <dCompet>
    <PrestadorServico><RazaoSocial> <emit><xNome>
    <Tomador><RazaoSocial>          <toma><xNome>
    <ValorServicos>                 <vServPrest><vServ>
    <ValorIss>                      <valores><vISSQN>
    <ItemListaServico>              <cServ><cTribNac>
    <OptanteSimplesNacional>        <prest><regTrib><opSimpNac>

O resultado media-se na tela: das 251 notas baixadas do ADN, só NÚMERO, CNPJ do
prestador e UF vinham preenchidos — e os três por acidente, porque tinham busca
global no documento inteiro (`nNFSe` estava na lista, e `<CNPJ>` e `<UF>` foram
encontrados em qualquer lugar). Todo o resto — datas, razão social, tomador,
código do serviço, e TODOS os valores — ficava vazio ou zero.

Pior: `mun_prest` faz parte da chave primária de `NfseDocument` e ficava vazio,
e nas notas de prestador PESSOA FÍSICA (que não têm `<CNPJ>` no emitente) a
busca global pegava o CNPJ do TOMADOR e gravava como prestador.

O leitor escolhe o layout pelo formato do arquivo, não por configuração: quem
manda o XML não sabe (nem deveria saber) de que padrão ele é.

Nada aqui toca o motor de mercadoria/transporte.
"""
import re
import logging
import xml.etree.ElementTree as ET
from datetime import datetime

logger = logging.getLogger(__name__)


def _local(tag: str) -> str:
    """Nome da tag sem namespace: '{http://...}Numero' -> 'numero'."""
    return tag.rsplit('}', 1)[-1].lower()


def _indexar(raiz) -> dict:
    """Mapa nome_da_tag -> lista de textos, varrendo a árvore inteira."""
    achados: dict[str, list[str]] = {}
    for el in raiz.iter():
        nome = _local(el.tag)
        texto = (el.text or "").strip()
        if texto:
            achados.setdefault(nome, []).append(texto)
    return achados


def _txt(idx: dict, *nomes: str, default: str = "") -> str:
    """Primeiro valor não vazio entre os nomes informados, na ordem dada."""
    for n in nomes:
        vals = idx.get(n.lower())
        if vals:
            return vals[0]
    return default


def _num(idx: dict, *nomes: str) -> float:
    bruto = _txt(idx, *nomes)
    if not bruto:
        return 0.0
    v = bruto.replace(".", "").replace(",", ".") if "," in bruto else bruto
    try:
        return float(v)
    except ValueError:
        return 0.0


def _cnpj(valor: str) -> str:
    return re.sub(r"\D", "", valor or "")


def _data(bruto: str):
    """Texto -> datetime SEM fuso.

    O layout nacional traz o fuso junto (`2023-02-08T13:30:39-03:00`) e o
    `fromisoformat` devolveria um datetime "consciente". As colunas de data do
    banco são todas ingênuas: misturar os dois tipos faz qualquer comparação
    entre datas levantar TypeError. Descartar o deslocamento preserva a hora que
    está escrita na nota, que é a que o contador confere.
    """
    if not bruto:
        return None
    bruto = bruto.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(bruto[:len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    try:  # ISO com fuso
        d = datetime.fromisoformat(bruto.replace("Z", ""))
    except ValueError:
        return None
    return d.replace(tzinfo=None)


def _bloco(raiz, *nomes: str):
    """Sub-árvore de um bloco (ex.: PrestadorServico), pelo nome local."""
    alvos = {n.lower() for n in nomes}
    for el in raiz.iter():
        if _local(el.tag) in alvos:
            return el
    return None


def _filho(el, *nomes):
    """Filho DIRETO com um dos nomes locais, ou None.

    Direto, e não descendente, porque o layout nacional repete nomes em níveis
    diferentes: existe `infNFSe/valores` (ISS, base, alíquota, líquido) e
    `infDPS/valores` (valor do serviço, retenções federais). Uma busca em
    profundidade pegaria o primeiro que aparecesse e trocaria um pelo outro.
    """
    if el is None:
        return None
    alvo = {n.lower() for n in nomes}
    for f in el:
        if _local(f.tag) in alvo:
            return f
    return None


# Duas primeiras casas do código IBGE do município = código da UF. O layout
# nacional não traz a UF do tomador em lugar nenhum — só o município — e sem
# esta tabela a coluna "UF Tomador" ficaria eternamente vazia.
_UF_POR_CODIGO_IBGE = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP",
    "17": "TO", "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB",
    "26": "PE", "27": "AL", "28": "SE", "29": "BA", "31": "MG", "32": "ES",
    "33": "RJ", "35": "SP", "41": "PR", "42": "SC", "43": "RS", "50": "MS",
    "51": "MT", "52": "GO", "53": "DF",
}


def uf_do_municipio(codigo: str) -> str:
    """'3204906' -> 'ES'. Vazio quando o código não é reconhecível."""
    c = re.sub(r"\D", "", str(codigo or ""))
    return _UF_POR_CODIGO_IBGE.get(c[:2], "") if len(c) >= 2 else ""


# cStat do layout nacional. 100 e 107 são os dois desfechos de "deu certo".
_SITUACAO_NACIONAL = {
    "100": "Autorizado o uso da NFS-e",
    "107": "Autorizado o uso da NFS-e",
    "101": "Cancelada",
    "135": "Cancelada",
}


def _parse_nacional(raiz) -> dict | None:
    """Layout nacional (sped.fazenda.gov.br/nfse), que é o que vem do ADN."""
    inf = _filho(raiz, "infNFSe")
    if inf is None:
        inf = raiz if _local(raiz.tag) == "infnfse" else None
    if inf is None:
        return None

    emit = _filho(inf, "emit")
    ender_emit = _filho(emit, "enderNac", "end")
    val_nfse = _filho(inf, "valores")

    dps = _filho(inf, "DPS")
    infdps = _filho(dps, "infDPS")
    prest = _filho(infdps, "prest")
    toma = _filho(infdps, "toma")
    serv = _filho(infdps, "serv")
    val_dps = _filho(infdps, "valores")

    i_inf = _indexar(inf)
    i_emit = _indexar(emit) if emit is not None else {}
    i_end_emit = _indexar(ender_emit) if ender_emit is not None else {}
    i_val_nfse = _indexar(val_nfse) if val_nfse is not None else {}
    i_dps = _indexar(infdps) if infdps is not None else {}
    i_prest = _indexar(prest) if prest is not None else {}
    i_toma = _indexar(toma) if toma is not None else {}
    i_serv = _indexar(serv) if serv is not None else {}
    i_val_dps = _indexar(val_dps) if val_dps is not None else {}

    numero = _txt(i_inf, "nNFSe")
    # CPF antes de desistir: 4 das 251 notas reais têm prestador pessoa física,
    # e é justamente nelas que a busca global pegava o CNPJ do TOMADOR.
    prestador = _cnpj(_txt(i_emit, "Cnpj", "Cpf") or _txt(i_prest, "Cnpj", "Cpf"))
    if not numero or not prestador:
        return None

    cod_mun_toma = _txt(i_toma, "cMun")
    ret_issqn = _txt(i_val_dps, "tpRetISSQN")
    optante = _txt(i_prest, "opSimpNac")
    situacao = _txt(i_inf, "cStat")

    return {
        "numero": numero,
        "prestador": prestador,
        # cMun do emitente; cLocEmi/cLocIncid como reserva. Este campo é PARTE
        # DA CHAVE do documento — vazio aqui degrada a deduplicação.
        "mun_prest": _txt(i_end_emit, "cMun") or _txt(i_dps, "cLocEmi") or
                     _txt(i_inf, "cLocIncid"),
        "dt_emissao": _data(_txt(i_dps, "dhEmi")) or _data(_txt(i_inf, "dhProc")),
        "dt_competencia": _data(_txt(i_dps, "dCompet")) or _data(_txt(i_dps, "dhEmi")),
        "rz_prestador": _txt(i_emit, "xNome") or _txt(i_prest, "xNome"),
        "uf_prest": _txt(i_end_emit, "UF") or uf_do_municipio(_txt(i_end_emit, "cMun")),
        "insc_prest": _txt(i_emit, "IM") or _txt(i_prest, "IM"),
        "tomador": _cnpj(_txt(i_toma, "Cnpj", "Cpf")),
        "rz_tomador": _txt(i_toma, "xNome"),
        # A UF do tomador NÃO existe no layout nacional: sai do código IBGE.
        "uf_toma": uf_do_municipio(cod_mun_toma),
        "cod_mun_toma": cod_mun_toma,
        # opSimpNac: 1 = não optante, 2 = MEI, 3 = ME/EPP.
        "optante_sn": "" if not optante else ("Sim" if optante in ("2", "3") else "Não"),
        "cod_servico": _txt(i_serv, "cTribNac") or _txt(i_serv, "cTribMun"),
        # CNAE não existe neste layout. Fica vazio de propósito: preencher com o
        # cNBS (que é outra classificação) seria rotular errado dado fiscal.
        "cnae": "",
        "valor_servico": _num(i_val_dps, "vServ") or _num(i_val_nfse, "vLiq"),
        "ir": _num(i_val_dps, "vRetIRRF"),
        "iss": _num(i_val_nfse, "vISSQN"),
        # tpRetISSQN: 1 = não retido, 2 = retido pelo tomador, 3 = pelo intermediário.
        "iss_retido": "Sim" if ret_issqn in ("2", "3") else "Não",
        "csll": _num(i_val_dps, "vRetCSLL"),
        "deducoes": _num(i_val_dps, "vDR"),
        "pis": _num(i_val_dps, "vPis"),
        "cofins": _num(i_val_dps, "vCofins"),
        "inss": _num(i_val_dps, "vRetCP"),
        "desconto_incondic": _num(i_val_dps, "vDescIncond"),
        "desconto_condic": _num(i_val_dps, "vDescCond"),
        # `vTotalRet` é o TOTAL retido, não "outras retenções". Somá-lo aqui
        # duplicaria o IR/CSLL/PIS/COFINS em `total_retencoes`.
        "out_retencoes": 0.0,
        "aliquota": _num(i_val_nfse, "pAliqAplic") or _num(i_val_dps, "pAliq"),
        "base_calculo": _num(i_val_nfse, "vBC") or _num(i_val_dps, "vServ"),
        "valor_liquido": _num(i_val_nfse, "vLiq") or _num(i_val_dps, "vServ"),
        "dt_cancelamento": None,
        "motivo_cancel": "-",
        "numero_nota_cancel": "",
        "status": _SITUACAO_NACIONAL.get(situacao,
                                         f"Situação {situacao}" if situacao else
                                         "Autorizado o uso da NFS-e"),
        "discriminacao": _txt(i_serv, "xDescServ") or _txt(i_serv, "xInfComp"),
        # A NFS-e nacional não tem "código de verificação": quem identifica a
        # nota é a chave de acesso, que vive no Id do infNFSe.
        "cod_verificacao": (inf.get("Id") or "")[:60],
    }


def _tem_doctype(caminho: str) -> bool:
    """Procura uma declaração DOCTYPE no início do arquivo.

    NFS-e não usa DTD — nem o ABRASF municipal, nem o layout nacional. Quem
    manda um DOCTYPE está declarando entidades, e é assim que se monta uma
    "bomba de entidades": 316 bytes que viram 1 MB na memória, e cada nível a
    mais multiplica por 10 (nove níveis chegam a ~1 GB). Basta um upload desses
    para derrubar o servidor, então o arquivo é recusado antes de ser lido.
    """
    try:
        with open(caminho, "rb") as f:
            inicio = f.read(64 * 1024)
    except OSError:
        return False
    return b"<!doctype" in inicio.lower()


def parse_nfse_xml(caminho: str) -> dict | None:
    """Campos do relatório a partir de um XML de NFS-e. None se não for NFS-e.

    Reconhece os dois layouts que chegam ao escritório: o NACIONAL (ADN) e o
    ABRASF municipal. A escolha é pelo conteúdo do arquivo, nunca por
    configuração — quem envia o XML não sabe de que padrão ele é.
    """
    if _tem_doctype(caminho):
        logger.warning("XML de NFS-e recusado: traz DOCTYPE (possível bomba de entidades)")
        return None
    try:
        raiz = ET.parse(caminho).getroot()
    except Exception:
        logger.warning("XML de NFS-e ilegível: %s", caminho, exc_info=True)
        return None

    # `infNFSe` só existe no layout nacional; nenhum ABRASF tem essa tag.
    if _local(raiz.tag) == "infnfse" or _filho(raiz, "infNFSe") is not None:
        return _parse_nacional(raiz)

    idx = _indexar(raiz)
    if not idx:
        return None

    # Prestador e tomador ficam em blocos próprios; buscar o CNPJ global
    # pegaria o primeiro que aparecesse, misturando os dois.
    bl_prest = _bloco(raiz, "PrestadorServico", "Prestador", "IdentificacaoPrestador")
    bl_toma = _bloco(raiz, "TomadorServico", "Tomador", "IdentificacaoTomador")
    idx_prest = _indexar(bl_prest) if bl_prest is not None else {}
    idx_toma = _indexar(bl_toma) if bl_toma is not None else {}

    numero = _txt(idx, "Numero", "NumeroNfse", "nNFSe")
    prestador = _cnpj(_txt(idx_prest, "Cnpj", "CpfCnpj", "Cpf") or _txt(idx, "Cnpj"))
    if not numero or not prestador:
        return None  # não é uma NFS-e reconhecível

    endereco_prest = _bloco(bl_prest, "Endereco") if bl_prest is not None else None
    idx_end_prest = _indexar(endereco_prest) if endereco_prest is not None else {}
    endereco_toma = _bloco(bl_toma, "Endereco") if bl_toma is not None else None
    idx_end_toma = _indexar(endereco_toma) if endereco_toma is not None else {}

    cancelamento = _data(_txt(idx, "DataCancelamento", "DtCancelamento"))
    status = _txt(idx, "Status", "SituacaoNfse")
    if not status:
        status = "Cancelada" if cancelamento else "Autorizado o uso da NFS-e"

    return {
        # identidade
        "numero": numero,
        "prestador": prestador,
        "mun_prest": _txt(idx_end_prest, "CodigoMunicipio") or _txt(idx, "CodigoMunicipio"),
        # datas
        "dt_emissao": _data(_txt(idx, "DataEmissao", "DataEmissaoRps")),
        "dt_competencia": _data(_txt(idx, "Competencia", "DataCompetencia")) or
                          _data(_txt(idx, "DataEmissao")),
        # prestador
        "rz_prestador": _txt(idx_prest, "RazaoSocial", "NomeFantasia", "xNome"),
        "uf_prest": _txt(idx_end_prest, "Uf", "UF") or _txt(idx, "Uf"),
        "insc_prest": _txt(idx_prest, "InscricaoMunicipal"),
        # tomador
        "tomador": _cnpj(_txt(idx_toma, "Cnpj", "CpfCnpj", "Cpf")),
        "rz_tomador": _txt(idx_toma, "RazaoSocial", "xNome"),
        "uf_toma": _txt(idx_end_toma, "Uf", "UF"),
        "cod_mun_toma": _txt(idx_end_toma, "CodigoMunicipio"),
        # classificação
        "optante_sn": _txt(idx, "OptanteSimplesNacional"),
        "cod_servico": _txt(idx, "ItemListaServico", "CodigoTributacaoMunicipio"),
        "cnae": _txt(idx, "CodigoCnae"),
        # valores
        "valor_servico": _num(idx, "ValorServicos"),
        "ir": _num(idx, "ValorIr"),
        "iss": _num(idx, "ValorIss"),
        "iss_retido": "Sim" if _txt(idx, "IssRetido") in ("1", "true", "True") else "Não",
        "csll": _num(idx, "ValorCsll"),
        "deducoes": _num(idx, "ValorDeducoes"),
        "pis": _num(idx, "ValorPis"),
        "cofins": _num(idx, "ValorCofins"),
        "inss": _num(idx, "ValorInss"),
        "desconto_incondic": _num(idx, "DescontoIncondicionado"),
        "desconto_condic": _num(idx, "DescontoCondicionado"),
        "out_retencoes": _num(idx, "OutrasRetencoes"),
        "aliquota": _num(idx, "Aliquota"),
        "base_calculo": _num(idx, "BaseCalculo"),
        "valor_liquido": _num(idx, "ValorLiquidoNfse") or _num(idx, "ValorServicos"),
        # cancelamento e situação
        "dt_cancelamento": cancelamento,
        "motivo_cancel": _txt(idx, "MotivoCancelamento", default="-"),
        "numero_nota_cancel": _txt(idx, "NumeroNfseSubstituidora", "NfseSubstituidora"),
        "status": status,
        # apoio
        "discriminacao": _txt(idx, "Discriminacao"),
        "cod_verificacao": _txt(idx, "CodigoVerificacao"),
    }
