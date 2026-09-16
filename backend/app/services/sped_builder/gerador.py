"""A montagem do arquivo, registro a registro.

A ordem aqui não é estilo: o SPED é lido sequencialmente e cada bloco tem de
aparecer uma vez, na ordem, com o contador certo. O que este módulo faz é
percorrer os XMLs uma vez juntando o que precisa (participantes, unidades,
produtos) e só então escrever — porque o Bloco 0 vem ANTES das notas no arquivo
e só fica pronto depois de ler todas elas.

O escritor e os contadores são os do `sped_rewriter`: `Registro`, `Documento`,
`reconstruir_c190`, `recalcular_c100` e `reconstruir_bloco_9` já sabem montar
linha, refazer analítico, fechar o documento e contar bloco. Reimplementar isso
aqui criaria duas verdades sobre o mesmo leiaute — e a segunda estaria errada
em algum lugar.

POLÍTICA DE ESCRITURAÇÃO DA ENTRADA — o que sai em cada campo de imposto, e por
quê. O XML traz os impostos do VENDEDOR; o C170 escritura o direito do
COMPRADOR, e os dois não são a mesma coisa:

- **ICMS próprio**: crédito pelo XML (base, alíquota, valor; CST = origem +
  CST). Fornecedor do Simples (CSOSN) não destaca ICMS: o item sai com CST x90
  e sem crédito, e o vCredICMSSN que ele informa vira presunção para decisão.
- **ICMS ST e FCP ST**: o adquirente substituído não tem crédito. O Guia manda
  (perguntas do C100) somar ao valor da mercadoria — VL_ITEM e VL_MERC —, com
  os campos 16 a 18 zerados. É a mesma regra que a glosa da correção aplica
  (`cascata.aplicar_glosas`), e é como o Domínio grava as entradas do escritório.
- **IPI**: idem. Todo item sai como mercadoria para revenda (TIPO_ITEM 00), e
  revenda não credita IPI; vai para o custo, campos 20 a 24 em branco.
- **PIS/COFINS**: CST 99 e sem valores. O crédito depende do regime da
  empresa, que não está no XML; o Domínio apura pelo cadastro dele. Copiar o
  CST do vendedor (01, 06...) escreveria código de SAÍDA num campo de entrada.

Cada uma dessas escolhas sai como `Presuncao`, com o total em dinheiro, para
quem confere no Domínio saber o que foi decidido pelo sistema.
"""
from __future__ import annotations

import calendar
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from ..sped_rewriter.cascata import (fechar_componentes, recalcular_c100,
                                     reconstruir_bloco_9, reconstruir_c190,
                                     reconstruir_totais_de_blocos,
                                     soma_base_dos_itens)
from ..sped_rewriter.documento import Documento, Registro, nova_linha
from ..sped_rewriter.patch import decimal_para_texto, texto_para_decimal

ZERO = Decimal("0")

# Versão do leiaute (0000 campo 02) por ano da competência, tabela do Ato
# COTEPE/ICMS 44/2018. O número muda todo janeiro e o PVA recusa versão que não
# vale para o período: "018" num SPED de 2026 reprova antes de ler a primeira
# nota. O 020 está confirmado no SPED real da B&C (competência 2026); os
# anteriores seguem a sequência anual da tabela. Ano fora daqui usa a última
# conhecida e sai avisado.
COD_VER_POR_ANO = {2023: "017", 2024: "018", 2025: "019", 2026: "020"}
COD_FIN_ORIGINAL = "0"

# Blocos que este gerador NÃO tem como preencher a partir de XML de entrada.
# Vão declarados como "sem dados" (IND_MOV = 1) porque o leiaute exige que todo
# bloco exista: bloco ausente é erro de estrutura, e bloco inventado é pior.
BLOCOS_VAZIOS = ("E", "G", "H", "K", "1")
IND_MOV_COM_DADOS = "0"
IND_MOV_SEM_DADOS = "1"

# Tamanho de cada registro, em número de campos (incluindo o REG), conferido
# contra o SPED real do Domínio. O 0100 tem 14: NOME, CPF, CRC, CNPJ, CEP, END,
# NUM, COMPL, BAIRRO, FONE, FAX, EMAIL e COD_MUN — saía com 13 e o importador
# recusava a linha por número de campos.
TAMANHOS = {"0000": 15, "0005": 10, "0100": 14, "0150": 13, "0190": 3,
            "0200": 13, "C100": 29, "C170": 38, "C190": 12, "D100": 25,
            "D190": 9}

# cStat do protocolo que significa "autorizada": 100 (uso autorizado) e 150
# (autorizado fora de prazo). Denegada (110, 301, 302) e cancelada (101, 135,
# 151) não são entrada de ninguém.
AUTORIZADA = {"100", "150"}

# C100 campo 13 (IND_PGTO) quando o XML não traz a forma de pagamento, e campo
# 17 (IND_FRT) quando não traz a modalidade do frete. A tabela do IND_FRT é a
# mesma do modFrete da NF-e (0 CIF, 1 FOB, 2 terceiros, 3/4 próprio, 9 sem
# frete), por isso o valor do XML entra direto.
IND_PGTO_OUTROS = "2"
IND_FRT_SEM_FRETE = "9"
MOD_FRETE_VALIDOS = {"0", "1", "2", "3", "4", "9"}
IND_PGTO_VALIDOS = {"0", "1", "2"}

# D100 campo 17 pelo papel do tomador no CT-e: quem paga o frete é o tomador.
# Remetente paga → por conta do remetente (0); destinatário → do destinatário
# (1); expedidor, recebedor ou "outros" → terceiros (2).
IND_FRT_POR_TOMADOR = {"rem": "0", "dest": "1"}
IND_FRT_TERCEIROS = "2"

CST_PIS_COFINS_OUTRAS = "99"
CST_ICMS_SEM_CREDITO = "90"
CST_COM_REDUCAO = ("20", "70")
COD_ITEM_MAX = 60           # 0200 campo 02 é C 060


@dataclass
class Presuncao:
    """Um campo que o gerador preencheu sem ter a informação no XML."""

    campo: str
    valor: str
    motivo: str
    documento: str = ""
    quantidade: int = 1


@dataclass
class Cadastro:
    """O que o registro 0000 exige e nenhum XML responde.

    CNPJ é obrigatório e não tem padrão: é ele que decide quais documentos do
    pacote são ENTRADA da empresa. Sem isso o gerador escrituraria as notas que
    ela emitiu, e as de outros clientes que estivessem no mesmo ZIP.

    `cod_ver` em branco (o normal) sai da competência das notas, pela tabela
    `COD_VER_POR_ANO`; preenchido, manda.
    """

    cnpj: str
    nome: str = ""
    ie: str = ""
    uf: str = ""
    cod_mun: str = ""
    perfil: str = "A"          # 0000 campo 14 — A, B ou C
    ind_ativ: str = "1"        # 0000 campo 15 — 0 industrial, 1 outros
    cod_ver: str = ""


@dataclass
class Resultado:
    documento: Documento | None = None
    presuncoes: list[Presuncao] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    notas: int = 0
    itens: int = 0
    fretes: int = 0
    ignorados: list[tuple] = field(default_factory=list)   # (arquivo, motivo)
    competencia: str = ""

    @property
    def vazio(self) -> bool:
        return not (self.notas or self.fretes)


# ------------------------------------------------------------------ auxiliares

def _so_digitos(texto) -> str:
    return "".join(c for c in str(texto or "") if c.isdigit())


def _data(iso: str) -> str:
    """"2026-04-12T10:30:00-03:00" -> "12042026". Vazio devolve vazio."""
    bruto = (iso or "").strip()
    if len(bruto) < 10:
        return ""
    ano, mes, dia = bruto[0:4], bruto[5:7], bruto[8:10]
    return f"{dia}{mes}{ano}"


def _num(valor) -> Decimal:
    try:
        return Decimal(str(valor or 0)).quantize(Decimal("0.01"))
    except Exception:                                    # noqa: BLE001
        return ZERO


def _moeda(valor) -> str:
    return decimal_para_texto(_num(valor), original="0")


def _quantidade(valor) -> str:
    """Quantidade com 5 casas — é o que o C170 usa (campo 05)."""
    try:
        d = Decimal(str(valor or 0)).quantize(Decimal("0.00001"))
    except Exception:                                    # noqa: BLE001
        d = Decimal("0.00000")
    return f"{d:.5f}".replace(".", ",")


def _aliquota(valor) -> str:
    return decimal_para_texto(_num(valor), original="0")


def _ordem_numerica(texto) -> int:
    digitos = _so_digitos(texto)
    return int(digitos) if digitos else 0


def versao_do_leiaute(ano: int) -> tuple[str, bool]:
    """(COD_VER, o ano está na tabela?). Fora dela vale a última conhecida."""
    if ano in COD_VER_POR_ANO:
        return COD_VER_POR_ANO[ano], True
    return COD_VER_POR_ANO[max(COD_VER_POR_ANO)], False


def cfop_de_entrada(cfop_xml: str, mapeado: str = "") -> tuple[str, bool]:
    """(CFOP de entrada, veio de regra aprovada?).

    A regra da empresa manda quando existe. Na falta dela, a conversão do
    primeiro dígito — 5→1, 6→2, 7→3 — que é como todo escritório começa uma
    escrituração: mesma natureza de operação, do outro lado. **É presunção, e
    sai marcada**: quem confere no Domínio precisa saber quais CFOPs alguém
    aprovou e quais o sistema deduziu.
    """
    regra = (mapeado or "").strip()
    if len(regra) == 4 and regra.isdigit():
        return regra, True

    origem = (cfop_xml or "").strip()
    equivalente = {"5": "1", "6": "2", "7": "3"}.get(origem[:1])
    if len(origem) == 4 and equivalente:
        return equivalente + origem[1:], False
    return origem, False


# ------------------------------------------------------------------ leitura

def _classificar(xml: dict, cnpj_empresa: str) -> str:
    """"entrada", "saida" ou "outra empresa" — pelo CNPJ, nunca pelo nome."""
    alvo = _so_digitos(cnpj_empresa)
    if _so_digitos(xml.get("dest_id")) == alvo:
        return "entrada"
    if _so_digitos(xml.get("emit_id")) == alvo:
        return "saida"
    return "outra"


def _ler_xmls(caminhos: list, cnpj_empresa: str, resultado: Resultado):
    """Lê tudo e separa o que é entrada da empresa. Devolve (nfes, ctes).

    As duas listas saem em ordem cronológica (data de emissão, depois número):
    o leiaute não obriga, mas quem abre o arquivo no Domínio procura a nota pela
    data, e a ordem dos nomes de arquivo do ZIP não diz nada a ninguém.
    """
    from ..audit_engine.xml_parser import parse_cte_xml, parse_nfe_xml

    nfes, ctes = [], []
    for caminho in sorted(caminhos):
        nome = os.path.basename(caminho)
        dados = parse_nfe_xml(caminho)
        # `items` não serve de crivo: o parser de NF-e devolve lista vazia e
        # nenhum erro para QUALQUER XML — inclusive um CT-e, que tem <dest> e
        # passaria por nota de entrada com zero itens. O número da nota é o que
        # só existe em NF-e (`nNF`).
        if "error" not in dados and dados.get("num") and dados.get("items"):
            c_stat = str(dados.get("c_stat") or "").strip()
            if c_stat and c_stat not in AUTORIZADA:
                # Denegada ou cancelada continua tendo <dest> igual à empresa;
                # escriturá-la como entrada normal (COD_SIT 00) lançaria
                # crédito de uma operação que não aconteceu.
                resultado.ignorados.append((nome, (
                    f"NF-e com cStat {c_stat} no protocolo: não está autorizada "
                    "(denegada ou cancelada) e não é escriturada como entrada")))
                continue
            situacao = _classificar(dados, cnpj_empresa)
            if situacao == "entrada":
                nfes.append(dados)
            else:
                resultado.ignorados.append((nome, {
                    "saida": "nota emitida pela própria empresa (saída)",
                    "outra": "nota de outra empresa (nem emitente nem destinatária)",
                }[situacao]))
            continue

        cte = parse_cte_xml(caminho)
        if "error" not in cte and cte.get("num"):
            c_stat = str(cte.get("c_stat") or "").strip()
            if c_stat and c_stat not in AUTORIZADA:
                resultado.ignorados.append((nome, (
                    f"CT-e com cStat {c_stat} no protocolo: não está autorizado "
                    "e não é escriturado")))
                continue
            # No CT-e o tomador é quem escritura o frete.
            if _so_digitos(cte.get("tomador_id")) == _so_digitos(cnpj_empresa):
                ctes.append(cte)
            else:
                resultado.ignorados.append(
                    (nome, "CT-e em que a empresa não é a tomadora"))
            continue

        resultado.ignorados.append(
            (nome, "não é XML de NF-e nem de CT-e legível"))

    nfes.sort(key=lambda n: (str(n.get("dh_emi") or ""), _ordem_numerica(n.get("num"))))
    ctes.sort(key=lambda c: (str(c.get("data") or ""), _ordem_numerica(c.get("num"))))
    return nfes, ctes


def _competencia(nfes: list, ctes: list, resultado: Resultado) -> tuple:
    """(DT_INI, DT_FIN) do mês. Pacote com dois meses é recusado.

    O SPED é MENSAL. Um ZIP com abril e maio juntos geraria um arquivo que
    nenhum dos dois meses aceita — e o erro só apareceria na importação.
    """
    datas = [d.get("dh_emi", "")[:7] for d in nfes if d.get("dh_emi")]
    datas += [d.get("data", "")[:7] for d in ctes if d.get("data")]
    meses = sorted({m for m in datas if len(m) == 7})
    if not meses:
        return "", ""
    if len(meses) > 1:
        resultado.avisos.append(
            "O pacote tem documentos de mais de um mês (" + ", ".join(meses)
            + "). O SPED é mensal: separe os XMLs por competência e gere um "
            "arquivo para cada.")
        return "", ""

    ano, mes = meses[0].split("-")
    ultimo = calendar.monthrange(int(ano), int(mes))[1]
    resultado.competencia = meses[0]
    return f"01{mes}{ano}", f"{ultimo:02d}{mes}{ano}"


# ------------------------------------------------------------------ Bloco 0

def _bloco_0(cadastro: Cadastro, nfes: list, ctes: list, dt_ini: str,
             dt_fin: str, resultado: Resultado) -> tuple:
    """(linhas do bloco 0, índice de COD_ITEM por (fornecedor, cProd))."""
    primeiro = nfes[0] if nfes else {}

    nome = cadastro.nome or primeiro.get("dest_name", "")
    ie = cadastro.ie or primeiro.get("dest_ie", "")
    uf = cadastro.uf or primeiro.get("dest_uf", "")
    cod_mun = cadastro.cod_mun or primeiro.get("dest_cod_mun", "")
    if not cadastro.ie and ie:
        resultado.presuncoes.append(Presuncao(
            "0000 · IE", ie, "lida do destinatário no XML, porque o cadastro da "
            "empresa não tem inscrição estadual"))
    if not cadastro.cod_mun and cod_mun:
        resultado.presuncoes.append(Presuncao(
            "0000 · COD_MUN", cod_mun,
            "código IBGE lido do destinatário no XML"))
    resultado.presuncoes.append(Presuncao(
        "0000 · IND_PERFIL / IND_ATIV", f"{cadastro.perfil} / {cadastro.ind_ativ}",
        "não existe no XML: veio do que foi informado na tela"))

    cod_ver = (cadastro.cod_ver or "").strip()
    if not cod_ver:
        ano = int(resultado.competencia[:4]) if resultado.competencia else 0
        cod_ver, na_tabela = versao_do_leiaute(ano)
        if not na_tabela:
            resultado.avisos.append(
                f"Não há versão de leiaute cadastrada para {ano}: o 0000 saiu "
                f"com COD_VER {cod_ver}, a última conhecida. Confira no PVA "
                "antes de importar.")

    linhas = [
        nova_linha("0000", {2: cod_ver, 3: COD_FIN_ORIGINAL, 4: dt_ini,
                            5: dt_fin, 6: nome, 7: _so_digitos(cadastro.cnpj),
                            9: uf, 10: ie, 11: cod_mun, 14: cadastro.perfil,
                            15: cadastro.ind_ativ}, TAMANHOS["0000"]),
        nova_linha("0001", {2: IND_MOV_COM_DADOS}, 2),
        # 0005 e 0100 são obrigatórios no leiaute e não vêm de nota nenhuma.
        # Saem vazios de propósito: o Domínio preenche do cadastro dele, e um
        # endereço inventado seria pior que um campo em branco.
        nova_linha("0005", {2: nome}, TAMANHOS["0005"]),
        nova_linha("0100", {}, TAMANHOS["0100"]),
    ]
    resultado.presuncoes.append(Presuncao(
        "0005 / 0100", "(em branco)",
        "endereço da empresa e dados do contabilista não estão no XML — "
        "complete no Domínio"))

    # --- participantes (0150), um por emitente e por transportador. O COD_MUN
    # é obrigatório para participante do Brasil (COD_PAIS 1058), e o CT-e traz
    # o do transportador em <enderEmit>, como a NF-e traz o do fornecedor.
    participantes = {}
    for nota in nfes:
        cod = _so_digitos(nota.get("emit_id"))
        if cod and cod not in participantes:
            participantes[cod] = nova_linha("0150", {
                2: cod, 3: nota.get("emit_name", ""), 4: "1058",
                5: cod if len(cod) == 14 else "",
                6: cod if len(cod) == 11 else "",
                7: nota.get("emit_ie", ""), 8: nota.get("emit_cod_mun", ""),
            }, TAMANHOS["0150"])
    for cte in ctes:
        cod = _so_digitos(cte.get("emit_id"))
        if cod and cod not in participantes:
            participantes[cod] = nova_linha("0150", {
                2: cod, 3: cte.get("emit_name", ""), 4: "1058",
                5: cod if len(cod) == 14 else "",
                6: cod if len(cod) == 11 else "",
                7: cte.get("emit_ie", ""), 8: cte.get("emit_cod_mun", ""),
            }, TAMANHOS["0150"])
    linhas.extend(participantes.values())

    # --- unidades (0190)
    unidades = {}
    for nota in nfes:
        for item in nota.get("items", []):
            unid = (item.get("unid") or "UN").strip().upper()[:6]
            unidades.setdefault(unid, nova_linha(
                "0190", {2: unid, 3: unid}, TAMANHOS["0190"]))
    linhas.extend(unidades.values())

    # --- produtos (0200). O COD_ITEM é o código do FORNECEDOR: a empresa não
    # tem código interno — é justamente por isso que ela não escritura. O
    # Domínio cadastra o produto a partir daqui.
    produtos, indice_item, repetidos = {}, {}, set()
    for nota in nfes:
        forn = _so_digitos(nota.get("emit_id"))
        for item in nota.get("items", []):
            cod = (item.get("cod_item") or "").strip()
            if not cod:
                continue
            chave = (forn, cod)
            if chave in indice_item:
                continue
            # Dois fornecedores podem usar o MESMO código para produtos
            # diferentes. Sem desempate, o 0200 juntaria os dois num produto só.
            # O sufixo cabe nos 60 caracteres do campo: cProd de 60 mais o
            # sufixo daria 69, e o importador recusaria a linha.
            cod_final = cod[:COD_ITEM_MAX]
            if any(c == cod for _f, c in indice_item):
                sufixo = f"-{forn[:8]}"
                cod_final = cod[:COD_ITEM_MAX - len(sufixo)] + sufixo
                repetidos.add(cod)
            indice_item[chave] = cod_final
            produtos[cod_final] = nova_linha("0200", {
                2: cod_final, 3: (item.get("descr") or "").strip()[:255],
                6: (item.get("unid") or "UN").strip().upper()[:6],
                7: "00", 8: _so_digitos(item.get("ncm")),
                13: _so_digitos(item.get("cest")),
            }, TAMANHOS["0200"])
    linhas.extend(produtos.values())

    if produtos:
        resultado.presuncoes.append(Presuncao(
            "0200 · TIPO_ITEM", "00 (mercadoria para revenda)",
            "o XML não diz o tipo do item; revise no Domínio se houver "
            "matéria-prima, uso e consumo ou ativo", quantidade=len(produtos)))
    if repetidos:
        resultado.presuncoes.append(Presuncao(
            "0200 · COD_ITEM", ", ".join(sorted(repetidos)[:5]),
            "o mesmo código foi usado por fornecedores diferentes; o do segundo "
            "recebeu sufixo com o CNPJ para não virar um produto só",
            quantidade=len(repetidos)))

    linhas.append(nova_linha("0990", {2: str(len(linhas) + 1)}, 2))
    return linhas, indice_item


# ------------------------------------------------------------------ Bloco C

def _cst_icms_do_item(item: dict) -> tuple[str, bool]:
    """(CST_ICMS de três dígitos, veio de CSOSN?).

    A origem (0 a 8) é campo separado no XML e vem colada ao CST no SPED. O
    fornecedor do Simples informa CSOSN (três dígitos, 101/102/...), que NÃO é
    CST: colar a origem nele dava "0102", quatro dígitos, campo inválido. O
    comprador escritura essa entrada como "outras" (x90), sem crédito.
    """
    orig = (item.get("orig") or "0").strip()[:1] or "0"
    cst = (item.get("cst") or "").strip()
    if len(cst) == 2:
        return f"{orig}{cst}", False
    return f"{orig}{CST_ICMS_SEM_CREDITO}", True


def _bloco_c(nfes: list, indice_item: dict, regras_cfop: dict,
             resultado: Resultado) -> list:
    if not nfes:
        # Bloco C sem dados existe do mesmo jeito: bloco ausente é erro de
        # estrutura, e só CT-e no pacote é um caso real (frete do mês).
        return [nova_linha("C001", {2: IND_MOV_SEM_DADOS}, 2),
                nova_linha("C990", {2: "2"}, 2)]

    linhas = [nova_linha("C001", {2: IND_MOV_COM_DADOS}, 2)]
    presumidos = 0
    contagem: Counter = Counter()
    totais: defaultdict = defaultdict(lambda: ZERO)

    for nota in nfes:
        forn = _so_digitos(nota.get("emit_id"))

        ind_pag = (nota.get("ind_pag") or "").strip()
        if ind_pag not in IND_PGTO_VALIDOS:
            ind_pag = IND_PGTO_OUTROS
            contagem["ind_pgto"] += 1
        mod_frete = (nota.get("mod_frete") or "").strip()
        if mod_frete not in MOD_FRETE_VALIDOS:
            mod_frete = IND_FRT_SEM_FRETE
            contagem["ind_frt"] += 1

        # Os itens primeiro: o VL_MERC do C100 recebe o que migrou para o
        # custo, e só se sabe quanto depois de percorrê-los.
        itens: list[Registro] = []
        migrado = ZERO
        for item in nota.get("items", []):
            cod_xml = (item.get("cod_item") or "").strip()
            cod = indice_item.get((forn, cod_xml), cod_xml)
            chave_regra = (forn, (item.get("cfop") or "").strip(), cod_xml)
            cfop, da_regra = cfop_de_entrada(item.get("cfop"),
                                             regras_cfop.get(chave_regra, ""))
            if not da_regra:
                presumidos += 1

            cst, do_simples = _cst_icms_do_item(item)
            if do_simples:
                contagem["csosn"] += 1
                totais["cred_sn"] += _num(item.get("vl_icms_sn"))
                bc, aliq, icms = ZERO, ZERO, ZERO
            else:
                bc = _num(item.get("vl_bc"))
                aliq = _num(item.get("p_icms"))
                icms = _num(item.get("vl_icms_normal", item.get("vl_icms")))

            # ICMS ST, FCP ST e IPI (inclusive o devolvido) vão para o custo:
            # VL_ITEM cresce, os campos do imposto ficam zerados.
            st = _num(item.get("vl_icms_st")) + _num(item.get("fcp_st"))
            ipi = _num(item.get("v_ipi")) + _num(item.get("v_ipi_devol"))
            if st:
                contagem["st"] += 1
                totais["st"] += st
            if ipi:
                contagem["ipi"] += 1
                totais["ipi"] += ipi
            migrado += st + ipi
            vl_item = _num(item.get("vl_prod")) + st + ipi

            itens.append(nova_linha("C170", {
                2: item.get("num_item", ""), 3: cod,
                5: _quantidade(item.get("qtd")),
                6: (item.get("unid") or "UN").strip().upper()[:6],
                7: _moeda(vl_item), 8: _moeda(item.get("vl_desc")),
                9: "0",
                10: cst, 11: cfop,
                13: _moeda(bc), 14: _aliquota(aliq), 15: _moeda(icms),
                16: _moeda(0), 18: _moeda(0),
                25: CST_PIS_COFINS_OUTRAS, 31: CST_PIS_COFINS_OUTRAS,
            }, TAMANHOS["C170"]))

        if not _data(nota.get("dh_saida")):
            contagem["dt_e_s_emissao"] += 1

        c100 = nova_linha("C100", {
            2: "0", 3: "1", 4: forn, 5: nota.get("modelo", "55"), 6: "00",
            7: nota.get("serie", ""), 8: nota.get("num", ""),
            9: nota.get("chave", ""), 10: _data(nota.get("dh_emi")),
            11: _data(nota.get("dh_saida")) or _data(nota.get("dh_emi")),
            12: _moeda(nota.get("vl_doc")), 13: ind_pag,
            14: _moeda(nota.get("vl_desc_total")),
            16: _moeda(_num(nota.get("vl_merc")) + migrado),
            17: mod_frete,
            18: _moeda(nota.get("vl_frete_total")),
            19: _moeda(nota.get("vl_seg_total")),
            20: _moeda(nota.get("vl_outro_total")),
            # 21 a 25 são refeitos pela cascata a partir dos C190; entram aqui
            # já coerentes com os itens para o arquivo nunca ter dois números.
            21: _moeda(nota.get("vl_bc_icms_total")),
            22: _moeda(nota.get("vl_icms_total")),
            23: _moeda(0), 24: _moeda(0), 25: _moeda(0),
            26: _moeda(0), 27: _moeda(0),
        }, TAMANHOS["C100"])
        linhas.append(c100)
        resultado.notas += 1
        for c170 in itens:
            c170.pai = c100
            c100.filhos.append(c170)
            linhas.append(c170)
            resultado.itens += 1

    if presumidos:
        resultado.presuncoes.append(Presuncao(
            "C170 · CFOP", "conversão do primeiro dígito (5→1, 6→2, 7→3)",
            "não havia regra de CFOP de entrada aprovada para estes itens; "
            "confira no Domínio antes de fechar a escrituração",
            quantidade=presumidos))
    if contagem["csosn"]:
        resultado.presuncoes.append(Presuncao(
            "C170 · CST_ICMS (fornecedor do Simples)",
            f"x{CST_ICMS_SEM_CREDITO} (outras), sem crédito",
            "o XML traz CSOSN, não CST. O crédito de ICMS do Simples informado "
            f"pelo fornecedor (vCredICMSSN) soma R$ {_moeda(totais['cred_sn'])} "
            "e não foi escriturado — decida no Domínio",
            quantidade=contagem["csosn"]))
    if contagem["st"]:
        resultado.presuncoes.append(Presuncao(
            "C170 · ICMS ST / FCP ST", f"R$ {_moeda(totais['st'])} no custo",
            "adquirente substituído não tem crédito: o Guia manda somar ao "
            "VL_ITEM e ao VL_MERC, com base, alíquota e valor de ST zerados",
            quantidade=contagem["st"]))
    if contagem["ipi"]:
        resultado.presuncoes.append(Presuncao(
            "C170 · IPI", f"R$ {_moeda(totais['ipi'])} no custo",
            "todo item sai como revenda (TIPO_ITEM 00), e revenda não credita "
            "IPI; se a empresa for industrial e o item for insumo, ajuste no "
            "Domínio", quantidade=contagem["ipi"]))
    resultado.presuncoes.append(Presuncao(
        "C170 · CST_PIS / CST_COFINS", f"{CST_PIS_COFINS_OUTRAS} (outras), sem valores",
        "o crédito de PIS/COFINS depende do regime da empresa, que o XML não "
        "diz; o Domínio apura pelo cadastro dele", quantidade=resultado.itens))
    resultado.presuncoes.append(Presuncao(
        "C100 · DT_E_S", "data de saída do fornecedor (ou de emissão)",
        "a data de ENTRADA na empresa não está no XML, e é ela que define em "
        "que mês a nota é escriturada" + (
            f"; em {contagem['dt_e_s_emissao']} nota(s) nem a saída havia, "
            "e valeu a emissão" if contagem["dt_e_s_emissao"] else ""),
        quantidade=resultado.notas))
    if contagem["ind_pgto"]:
        resultado.presuncoes.append(Presuncao(
            "C100 · IND_PGTO", f"{IND_PGTO_OUTROS} (outros)",
            "o XML não trouxe a forma de pagamento (indPag)",
            quantidade=contagem["ind_pgto"]))
    if contagem["ind_frt"]:
        resultado.presuncoes.append(Presuncao(
            "C100 · IND_FRT", f"{IND_FRT_SEM_FRETE} (sem ocorrência de transporte)",
            "o XML não trouxe a modalidade do frete (modFrete)",
            quantidade=contagem["ind_frt"]))

    linhas.append(nova_linha("C990", {2: str(len(linhas) + 1)}, 2))
    return linhas


# ------------------------------------------------------------------ Bloco D

def _bloco_d(ctes: list, resultado: Resultado) -> list:
    if not ctes:
        # Mês sem frete ainda tem Bloco D: o leiaute exige todo bloco, e o
        # arquivo saía sem D001/D990 quando o pacote não trazia CT-e.
        return [nova_linha("D001", {2: IND_MOV_SEM_DADOS}, 2),
                nova_linha("D990", {2: "2"}, 2)]

    linhas = [nova_linha("D001", {2: IND_MOV_COM_DADOS}, 2)]
    sem_cst = 0
    for cte in ctes:
        forn = _so_digitos(cte.get("emit_id"))
        cfop, _ = cfop_de_entrada(cte.get("cfop"))
        vl_doc = _num(cte.get("vl_doc"))
        bc = _num(cte.get("vl_bc"))
        icms = _num(cte.get("vl_icms"))
        aliq = _num(cte.get("p_icms"))

        # O CST vem do próprio grupo de ICMS do CT-e (00, 20, 40, 51, 90...),
        # com origem 0 na frente. Só sem ele vale a dedução pelo valor.
        cst_xml = (cte.get("cst") or "").strip()
        if len(cst_xml) == 2 and cst_xml.isdigit():
            cst = f"0{cst_xml}"
        else:
            cst = "000" if icms else "090"
            sem_cst += 1
        # VL_RED_BC (D190 campo 08) só existe com CST 20/70: é a parcela da
        # prestação que ficou fora da base reduzida.
        reducao = max(ZERO, vl_doc - bc) if cst[-2:] in CST_COM_REDUCAO else ZERO

        ind_frt = IND_FRT_POR_TOMADOR.get(str(cte.get("tomador_role") or ""),
                                          IND_FRT_TERCEIROS)
        d100 = nova_linha("D100", {
            2: "0", 3: "1", 4: forn, 5: "57", 6: "00",
            7: cte.get("serie", ""), 9: cte.get("num", ""),
            10: cte.get("chave", ""), 11: _data(cte.get("data")),
            12: _data(cte.get("data")),
            13: (cte.get("tp_cte") or "0").strip() or "0",
            15: _moeda(vl_doc), 16: _moeda(0), 17: ind_frt,
            18: _moeda(vl_doc), 19: _moeda(bc), 20: _moeda(icms),
            21: _moeda(0),
            # Município de origem e destino do serviço: obrigatórios no D100
            # do CT-e, e o XML os traz em <ide> (cMunIni / cMunFim).
            24: _so_digitos(cte.get("cod_mun_ini")),
            25: _so_digitos(cte.get("cod_mun_fim")),
        }, TAMANHOS["D100"])
        d190 = nova_linha("D190", {
            2: cst, 3: cfop, 4: _aliquota(aliq), 5: _moeda(vl_doc),
            6: _moeda(bc), 7: _moeda(icms), 8: _moeda(reducao),
        }, TAMANHOS["D190"])
        d190.pai = d100
        d100.filhos.append(d190)
        linhas.extend([d100, d190])
        resultado.fretes += 1

    resultado.presuncoes.append(Presuncao(
        "D100 · DT_A_P", "data de emissão do CT-e",
        "a data de aquisição do serviço não está no XML", quantidade=len(ctes)))
    if sem_cst:
        resultado.presuncoes.append(Presuncao(
            "D190 · CST", "deduzido do valor do ICMS do CT-e",
            "o XML não trouxe o CST do ICMS; sem ICMS destacado o gerador usa "
            "090 (outras), com ICMS usa 000", quantidade=sem_cst))
    linhas.append(nova_linha("D990", {2: str(len(linhas) + 1)}, 2))
    return linhas


def _blocos_vazios() -> list:
    """Um par X001/X990 para cada bloco que não temos como preencher.

    IND_MOV = 1 é "bloco sem dados informados". O leiaute exige que todos
    existam: bloco ausente é erro de estrutura no validador.
    """
    linhas = []
    for bloco in BLOCOS_VAZIOS:
        linhas.append(nova_linha(f"{bloco}001", {2: IND_MOV_SEM_DADOS}, 2))
        linhas.append(nova_linha(f"{bloco}990", {2: "2"}, 2))
    return linhas


# ------------------------------------------------------------------ fachada

def _fechar_documentos(doc: Documento, resultado: Resultado) -> None:
    """C190, totais do C100 e componentes — pela cascata da correção.

    O VL_OPR de cada C190 é a base dos itens mais o rateio do que está no
    documento e não neles (frete, seguro, outras despesas). O alvo do rateio é
    o VL_DOC, porque é a conta que o PVA faz: soma dos VL_OPR contra o total da
    nota. Passar zero de extra deixava toda nota com frete fora dessa conta.

    O que sobra dos componentes do C100 (IPI devolvido, imposto de importação,
    desoneração deduzida do total) vai para VL_OUT_DA, como a correção faz — e
    sai como presunção, com o total.
    """
    residuo = ZERO
    documentos_com_residuo = 0
    for c100 in doc.por_reg("C100"):
        alvo = texto_para_decimal(c100.campo(12))
        extra = alvo - soma_base_dos_itens(doc, c100) if alvo else ZERO
        reconstruir_c190(doc, c100, extra)
        _, avisos = recalcular_c100(doc, c100)
        resultado.avisos.extend(avisos)
        mudou, avisos = fechar_componentes(doc, c100)
        resultado.avisos.extend(avisos)
        for alteracao in mudou:
            residuo += (texto_para_decimal(alteracao.para)
                        - texto_para_decimal(alteracao.de))
            documentos_com_residuo += 1
    if documentos_com_residuo:
        resultado.presuncoes.append(Presuncao(
            "C100 · VL_OUT_DA", f"R$ {_moeda(residuo)}",
            "parcela do total da nota que não é mercadoria, frete, seguro, "
            "desconto, ST nem IPI (IPI devolvido, imposto de importação, "
            "desoneração) declarada como outras despesas acessórias para o "
            "documento fechar", quantidade=documentos_com_residuo))


def gerar(caminhos_xml: list, cadastro: Cadastro,
          regras_cfop: dict | None = None) -> Resultado:
    """Lê os XMLs e devolve o SPED montado, com tudo que foi presumido."""
    resultado = Resultado()
    if not _so_digitos(cadastro.cnpj):
        resultado.avisos.append(
            "Sem o CNPJ da empresa não dá para saber quais documentos do pacote "
            "são entrada dela. Nada foi gerado.")
        return resultado

    nfes, ctes = _ler_xmls(list(caminhos_xml), cadastro.cnpj, resultado)
    if not nfes and not ctes:
        resultado.avisos.append(
            "Nenhum documento de entrada desta empresa no pacote: nada a gerar.")
        return resultado

    dt_ini, dt_fin = _competencia(nfes, ctes, resultado)
    if not dt_ini:
        return resultado

    linhas_0, indice_item = _bloco_0(cadastro, nfes, ctes, dt_ini, dt_fin,
                                     resultado)
    linhas_c = _bloco_c(nfes, indice_item, regras_cfop or {}, resultado)
    linhas_d = _bloco_d(ctes, resultado)

    doc = Documento(registros=linhas_0 + linhas_c + linhas_d + _blocos_vazios(),
                    caminho="", fim_padrao="\r\n")

    # Os analíticos, os totais e os contadores saem do motor de reescrita:
    # mesma conta, mesmas regras do Guia, um lugar só.
    _fechar_documentos(doc, resultado)
    # Os X990 saem da contagem POSICIONAL do motor, não das minhas somas: é a
    # mesma função que fecha o arquivo na correção, e ela já conhece a exceção
    # do bloco 0 (o 0000 conta, "mesmo não pertencendo ao bloco").
    reconstruir_totais_de_blocos(doc)
    doc.registros.append(nova_linha("9001", {2: IND_MOV_COM_DADOS}, 2))
    doc.registros.append(nova_linha("9990", {2: "0"}, 2))
    doc.registros.append(nova_linha("9999", {2: "0"}, 2))
    reconstruir_bloco_9(doc)

    resultado.documento = doc
    return resultado


def gerar_de_pasta(pasta: str, cadastro: Cadastro,
                   regras_cfop: dict | None = None) -> Resultado:
    """Mesma coisa, varrendo uma pasta de XMLs (o ZIP já extraído)."""
    caminhos = []
    for raiz, _dirs, arquivos in os.walk(pasta):
        for nome in arquivos:
            if nome.lower().endswith(".xml"):
                caminhos.append(os.path.join(raiz, nome))
    return gerar(caminhos, cadastro, regras_cfop)
