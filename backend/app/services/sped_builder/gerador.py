"""A montagem do arquivo, registro a registro.

A ordem aqui não é estilo: o SPED é lido sequencialmente e cada bloco tem de
aparecer uma vez, na ordem, com o contador certo. O que este módulo faz é
percorrer os XMLs uma vez juntando o que precisa (participantes, unidades,
produtos) e só então escrever — porque o Bloco 0 vem ANTES das notas no arquivo
e só fica pronto depois de ler todas elas.

O escritor e os contadores são os do `sped_rewriter`: `Registro`, `Documento`,
`reconstruir_c190` e `reconstruir_bloco_9` já sabem montar linha, refazer
analítico e contar bloco. Reimplementar isso aqui criaria duas verdades sobre o
mesmo leiaute — e a segunda estaria errada em algum lugar.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal

from ..sped_rewriter.cascata import (reconstruir_bloco_9, reconstruir_c190,
                                     reconstruir_totais_de_blocos)
from ..sped_rewriter.documento import NIVEL, Documento, Registro
from ..sped_rewriter.patch import decimal_para_texto

ZERO = Decimal("0")

# Versão do leiaute e finalidade do arquivo (0000, campos 02 e 03).
COD_VER_PADRAO = "018"
COD_FIN_ORIGINAL = "0"

# Blocos que este gerador NÃO tem como preencher a partir de XML de entrada.
# Vão declarados como "sem dados" (IND_MOV = 1) porque o leiaute exige que todo
# bloco exista: bloco ausente é erro de estrutura, e bloco inventado é pior.
BLOCOS_VAZIOS = ("E", "G", "H", "K", "1")

# Tamanho de cada registro, em número de campos (incluindo o REG).
TAMANHOS = {"0000": 15, "0005": 10, "0100": 13, "0150": 13, "0190": 3,
            "0200": 13, "C100": 29, "C170": 38, "C190": 12, "D100": 25,
            "D190": 9}


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
    """

    cnpj: str
    nome: str = ""
    ie: str = ""
    uf: str = ""
    cod_mun: str = ""
    perfil: str = "A"          # 0000 campo 14 — A, B ou C
    ind_ativ: str = "1"        # 0000 campo 15 — 0 industrial, 1 outros
    cod_ver: str = COD_VER_PADRAO


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


def _linha(reg: str, campos: dict, total: int, fim: str = "\r\n") -> Registro:
    """Monta o Registro com o número exato de campos do leiaute."""
    valores = [""] * total
    valores[0] = reg
    for numero, valor in campos.items():
        if numero <= total:
            valores[numero - 1] = "" if valor is None else str(valor)
    return Registro(reg=reg, campos=valores, linha=0, crua="", fim=fim,
                    criado=True)


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
    """Lê tudo e separa o que é entrada da empresa. Devolve (nfes, ctes)."""
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
            # No CT-e o tomador é quem escritura o frete.
            if _so_digitos(cte.get("tomador_id")) == _so_digitos(cnpj_empresa):
                ctes.append(cte)
            else:
                resultado.ignorados.append(
                    (nome, "CT-e em que a empresa não é a tomadora"))
            continue

        resultado.ignorados.append(
            (nome, "não é XML de NF-e nem de CT-e legível"))
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
    import calendar
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

    linhas = [
        _linha("0000", {2: cadastro.cod_ver, 3: COD_FIN_ORIGINAL, 4: dt_ini,
                        5: dt_fin, 6: nome, 7: _so_digitos(cadastro.cnpj),
                        9: uf, 10: ie, 11: cod_mun, 14: cadastro.perfil,
                        15: cadastro.ind_ativ}, TAMANHOS["0000"]),
        _linha("0001", {2: "0"}, 2),
        # 0005 e 0100 são obrigatórios no leiaute e não vêm de nota nenhuma.
        # Saem vazios de propósito: o Domínio preenche do cadastro dele, e um
        # endereço inventado seria pior que um campo em branco.
        _linha("0005", {2: nome}, TAMANHOS["0005"]),
        _linha("0100", {}, TAMANHOS["0100"]),
    ]
    resultado.presuncoes.append(Presuncao(
        "0005 / 0100", "(em branco)",
        "endereço da empresa e dados do contabilista não estão no XML — "
        "complete no Domínio"))

    # --- participantes (0150), um por emitente e por transportador
    participantes = {}
    for nota in nfes:
        cod = _so_digitos(nota.get("emit_id"))
        if cod and cod not in participantes:
            participantes[cod] = _linha("0150", {
                2: cod, 3: nota.get("emit_name", ""), 4: "1058",
                5: cod if len(cod) == 14 else "",
                6: cod if len(cod) == 11 else "",
                7: nota.get("emit_ie", ""), 8: nota.get("emit_cod_mun", ""),
            }, TAMANHOS["0150"])
    for cte in ctes:
        cod = _so_digitos(cte.get("emit_id"))
        if cod and cod not in participantes:
            participantes[cod] = _linha("0150", {
                2: cod, 3: cte.get("emit_name", ""), 4: "1058",
                5: cod if len(cod) == 14 else "",
                6: cod if len(cod) == 11 else "",
            }, TAMANHOS["0150"])
    linhas.extend(participantes.values())

    # --- unidades (0190)
    unidades = {}
    for nota in nfes:
        for item in nota.get("items", []):
            unid = (item.get("unid") or "UN").strip().upper()[:6]
            unidades.setdefault(unid, _linha(
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
            cod_final = cod
            if any(c == cod for _f, c in indice_item):
                cod_final = f"{cod}-{forn[:8]}"
                repetidos.add(cod)
            indice_item[chave] = cod_final
            produtos[cod_final] = _linha("0200", {
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

    linhas.append(_linha("0990", {2: str(len(linhas) + 1)}, 2))
    return linhas, indice_item


# ------------------------------------------------------------------ Bloco C

def _bloco_c(nfes: list, indice_item: dict, regras_cfop: dict,
             resultado: Resultado) -> list:
    linhas = [_linha("C001", {2: "0"}, 2)]
    presumidos = 0

    for nota in nfes:
        forn = _so_digitos(nota.get("emit_id"))
        c100 = _linha("C100", {
            2: "0", 3: "1", 4: forn, 5: nota.get("modelo", "55"), 6: "00",
            7: nota.get("serie", ""), 8: nota.get("num", ""),
            9: nota.get("chave", ""), 10: _data(nota.get("dh_emi")),
            11: _data(nota.get("dh_saida")) or _data(nota.get("dh_emi")),
            12: _moeda(nota.get("vl_doc")), 13: "0",
            14: _moeda(nota.get("vl_desc_total")),
            16: _moeda(nota.get("vl_merc")),
            17: "0" if _num(nota.get("vl_frete_total")) else "9",
            18: _moeda(nota.get("vl_frete_total")),
            19: _moeda(nota.get("vl_seg_total")),
            20: _moeda(nota.get("vl_outro_total")),
            21: _moeda(nota.get("vl_bc_icms_total")),
            22: _moeda(nota.get("vl_icms_total")),
            23: _moeda(nota.get("vl_bc_st_total")),
            24: _moeda(nota.get("vl_st")),
            25: _moeda(nota.get("vl_ipi_total")),
            26: _moeda(nota.get("vl_pis_total")),
            27: _moeda(nota.get("vl_cofins_total")),
        }, TAMANHOS["C100"])
        linhas.append(c100)
        resultado.notas += 1

        for item in nota.get("items", []):
            cod = indice_item.get((forn, (item.get("cod_item") or "").strip()),
                                  (item.get("cod_item") or "").strip())
            chave_regra = (forn, (item.get("cfop") or "").strip(),
                           (item.get("cod_item") or "").strip())
            cfop, da_regra = cfop_de_entrada(item.get("cfop"),
                                             regras_cfop.get(chave_regra, ""))
            if not da_regra:
                presumidos += 1

            c170 = _linha("C170", {
                2: item.get("num_item", ""), 3: cod,
                5: _quantidade(item.get("qtd")),
                6: (item.get("unid") or "UN").strip().upper()[:6],
                7: _moeda(item.get("vl_prod")), 8: _moeda(item.get("vl_desc")),
                9: "0",
                10: f"{item.get('orig', '0')}{item.get('cst', '')}",
                11: cfop,
                13: _moeda(item.get("vl_bc")), 14: _aliquota(item.get("p_icms")),
                15: _moeda(item.get("vl_icms_normal", item.get("vl_icms"))),
                16: _moeda(0), 18: _moeda(item.get("vl_icms_st")),
                20: item.get("cst_ipi", ""),
                22: _moeda(item.get("vl_bc_ipi")), 24: _moeda(item.get("v_ipi")),
                25: item.get("cst_pis", ""), 26: _moeda(item.get("vl_bc_pis")),
                27: _aliquota(item.get("p_pis")), 30: _moeda(item.get("vl_pis")),
                31: item.get("cst_cofins", ""),
                32: _moeda(item.get("vl_bc_cofins")),
                33: _aliquota(item.get("p_cofins")),
                36: _moeda(item.get("vl_cofins")),
            }, TAMANHOS["C170"])
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

    linhas.append(_linha("C990", {2: str(len(linhas) + 1)}, 2))
    return linhas


# ------------------------------------------------------------------ Bloco D

def _bloco_d(ctes: list, resultado: Resultado) -> list:
    if not ctes:
        return []

    linhas = [_linha("D001", {2: "0"}, 2)]
    for cte in ctes:
        forn = _so_digitos(cte.get("emit_id"))
        cfop, _ = cfop_de_entrada(cte.get("cfop"))
        d100 = _linha("D100", {
            2: "0", 3: "1", 4: forn, 5: "57", 6: "00",
            7: cte.get("serie", ""), 9: cte.get("num", ""),
            10: cte.get("chave", ""), 11: _data(cte.get("data")),
            12: _data(cte.get("data")), 13: "0",
            15: _moeda(cte.get("vl_doc")), 16: _moeda(0), 17: "1",
            18: _moeda(cte.get("vl_doc")),
            19: _moeda(cte.get("vl_bc")), 20: _moeda(cte.get("vl_icms")),
            21: _moeda(0),
        }, TAMANHOS["D100"])
        d190 = _linha("D190", {
            2: "090" if not _num(cte.get("vl_icms")) else "000",
            3: cfop, 4: _aliquota(0), 5: _moeda(cte.get("vl_doc")),
            6: _moeda(cte.get("vl_bc")), 7: _moeda(cte.get("vl_icms")),
            8: _moeda(0),
        }, TAMANHOS["D190"])
        d190.pai = d100
        d100.filhos.append(d190)
        linhas.extend([d100, d190])
        resultado.fretes += 1

    resultado.presuncoes.append(Presuncao(
        "D190 · CST e alíquota", "deduzidos do valor do ICMS do CT-e",
        "o CT-e não escritura CST; sem ICMS destacado o gerador usa 090 "
        "(outras), com ICMS usa 000", quantidade=len(ctes)))
    linhas.append(_linha("D990", {2: str(len(linhas) + 1)}, 2))
    return linhas


def _blocos_vazios() -> list:
    """Um par X001/X990 para cada bloco que não temos como preencher.

    IND_MOV = 1 é "bloco sem dados informados". O leiaute exige que todos
    existam: bloco ausente é erro de estrutura no validador.
    """
    linhas = []
    for bloco in BLOCOS_VAZIOS:
        linhas.append(_linha(f"{bloco}001", {2: "1"}, 2))
        linhas.append(_linha(f"{bloco}990", {2: "2"}, 2))
    return linhas


# ------------------------------------------------------------------ fachada

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

    # Os analíticos e os contadores saem do motor de reescrita: mesma conta,
    # mesmas regras do Guia, um lugar só.
    for c100 in doc.por_reg("C100"):
        reconstruir_c190(doc, c100, ZERO)
    # Os X990 saem da contagem POSICIONAL do motor, não das minhas somas: é a
    # mesma função que fecha o arquivo na correção, e ela já conhece a exceção
    # do bloco 0 (o 0000 conta, "mesmo não pertencendo ao bloco").
    reconstruir_totais_de_blocos(doc)
    doc.registros.append(_linha("9001", {2: "0"}, 2))
    doc.registros.append(_linha("9990", {2: "0"}, 2))
    doc.registros.append(_linha("9999", {2: "0"}, 2))
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
