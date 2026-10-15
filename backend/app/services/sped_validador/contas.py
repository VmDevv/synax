"""Regras de CONTA: o valor de um registro tem de bater com a soma de outros.

São as regras do PVA que cruzam registros — o analítico (C190) contra os itens
(C170), o documento (C100) contra os analíticos e contra os itens (mercadoria,
desconto, abatimento, ajustes), e a apuração (E110) contra os ajustes (E111),
as guias (E116) e os próprios campos dela. Rodam DEPOIS que o
arquivo inteiro virou árvore, e só nos registros em que o descritor do PVA as
aplica: a regra `REGRA_VL_ICMS_ANALIT` está no C100, C500, C600 e D100, e é
nesses que ela roda — mapear "à mão" quem soma o quê esqueceria um.

O texto de cada regra vem do `validador.prop` do PVA; a aritmética, do Guia
Prático. Comparação EXATA, em Decimal, sem tolerância — o PVA compara os
valores com duas casas, e "quase igual" num total de documento é exatamente o
erro de arredondamento que reprova o arquivo. Se o gabarito mostrar que o PVA
tolera centavos, a tolerância entra aqui, num lugar só.

O que NÃO está aqui de propósito: as variantes `_14001_V1` (C590) e as somas
que dependem de classificar o código de ajuste do documento (C197 no
VL_AJ_DEBITOS/VL_AJ_CREDITOS/VL_TOT_DED do E110) — essas continuam contadas
como "não conferidas".
"""
from __future__ import annotations

from decimal import Decimal

from . import campos as cp

ZERO = Decimal("0")

# COD_SIT de documento extemporâneo: o débito dele vai para DEB_ESP (E110 campo
# 15), não para o total de débitos do período (campo 02).
EXTEMPORANEOS = frozenset({"01", "07"})

# Analítico -> registro de itens do mesmo documento.
ITENS_DO_ANALITICO = {"C190": "C170", "C690": "C610", "D590": "D510",
                      "D690": "D610"}
CHAVE_ANALITICA = ("CST_ICMS", "CFOP", "ALIQ_ICMS")

# Registros analíticos que entram no total de débitos/créditos do E110
# (mensagens MSG_VL_TOT_DEBITOS_E110_17002_V1 e MSG_VL_TOT_CREDITOS_E110_17002_V1).
ANALITICOS_DEBITO = ("C190", "C320", "C390", "C490", "C590", "C690", "C790",
                     "C890", "D190", "D300", "D390", "D410", "D590", "D690",
                     "D696", "D730", "D760")
ANALITICOS_CREDITO = ("C190", "C590", "D190", "D590", "D730")


def moeda(valor: Decimal) -> str:
    """Decimal -> "1.234,56", como o contador lê."""
    texto = f"{valor:,.2f}"
    return texto.replace(",", "_").replace(".", ",").replace("_", ".")


def _valor(no, campo_id: str) -> Decimal | None:
    """Valor numérico do campo; vazio é zero; inválido é None (já acusado)."""
    if campo_id not in no.d.indice_campo:
        return None
    texto = no.valor(campo_id).strip()
    if not texto:
        return ZERO
    return cp.numero(texto)


def _chave(no) -> tuple | None:
    partes = []
    for campo_id in CHAVE_ANALITICA:
        if campo_id not in no.d.indice_campo:
            return None
        texto = no.valor(campo_id).strip()
        if campo_id == "ALIQ_ICMS":
            n = cp.numero(texto) if texto else ZERO
            texto = format(n.normalize(), "f") if n is not None else texto
        partes.append(texto)
    return tuple(partes)


def _em_vigor(no) -> bool:
    """Documento cancelado/denegado/inutilizado não tem filho a somar."""
    return no.sit not in ("02", "03", "04", "05")


def _soma(nos, campo_id: str) -> Decimal | None:
    total = ZERO
    for n in nos:
        v = _valor(n, campo_id)
        if v is None:
            return None
        total += v
    return total


# ------------------------------------------------- analítico contra itens

def _analitico_x_itens(campo_id: str, chave_msg: str):
    def verificar(v_, no, regra):
        itens_reg = ITENS_DO_ANALITICO.get(no.d.reg)
        if itens_reg is None or no.pai is None or not _em_vigor(no):
            v_.nao_conferidas[regra.id] += 1
            return
        itens = [f for f in no.pai.filhos if f.d.reg == itens_reg]
        if not itens or campo_id not in itens[0].d.indice_campo:
            # Documento sem itens (NF-e de emissão própria dispensa o C170):
            # não há o que somar.
            return
        chave = _chave(no)
        do_grupo = [i for i in itens if _chave(i) == chave]
        if not do_grupo:
            return          # combinação sem item: acusada pela regra da chave
        informado, soma = _valor(no, campo_id), _soma(do_grupo, campo_id)
        if informado is None or soma is None or informado == soma:
            return
        v_.acusar(no.r.linha, no.d.reg, campo_id, regra.id,
                  f"{v_.msg(chave_msg)} Informado {moeda(informado)}; soma dos "
                  f"{itens_reg} com CST {chave[0]}, CFOP {chave[1]} e alíquota "
                  f"{chave[2]}: {moeda(soma)}.", valor=no.valor(campo_id))
    return verificar


def _chave_existe_nos_itens(v_, no, regra):
    itens_reg = ITENS_DO_ANALITICO.get(no.d.reg)
    if itens_reg is None or no.pai is None or not _em_vigor(no):
        v_.nao_conferidas[regra.id] += 1
        return
    itens = [f for f in no.pai.filhos if f.d.reg == itens_reg]
    if not itens:
        return
    chave = _chave(no)
    if chave is not None and all(_chave(i) != chave for i in itens):
        v_.acusar(no.r.linha, no.d.reg, "CST_ICMS", regra.id,
                  f"{v_.msg('MSG_CHAVE_EXISTE_NO_REGISTRO_ITEM')} Nenhum "
                  f"{itens_reg} deste documento tem CST {chave[0]}, CFOP "
                  f"{chave[1]} e alíquota {chave[2]}.",
                  valor="/".join(chave))


# ---------------------------------------------- documento contra analíticos

def _documento_x_analiticos(campo_id: str, chave_msg: str):
    def verificar(v_, no, regra):
        if not _em_vigor(no):
            return
        analitico = no.d.reg[:2] + "90"
        filhos = [f for f in no.filhos if f.d.reg == analitico]
        if not filhos or campo_id not in filhos[0].d.indice_campo:
            return
        informado, soma = _valor(no, campo_id), _soma(filhos, campo_id)
        if informado is None or soma is None or informado == soma:
            return
        v_.acusar(no.r.linha, no.d.reg, campo_id, regra.id,
                  f"{v_.msg(chave_msg)} Informado no {no.d.reg}: "
                  f"{moeda(informado)}; soma dos {analitico}: {moeda(soma)}.",
                  valor=no.valor(campo_id))
    return verificar


# ------------------------------------------------- documento contra itens

def _nfe_propria(no) -> bool:
    """NF-e de emissão própria só traz o C170 do item com C176/C177/C180/C181
    (Guia, C100, Exceção 2): a soma dos itens ali é PARCIAL por desenho."""
    return no.valor("IND_EMIT").strip() == "0" and no.modelo == "55"


def _vl_merc_x_itens(v_, no, regra):
    """VL_MERC do C100 contra a soma do VL_ITEM dos C170.

    As duas fontes não dizem a mesma coisa: o Guia (C100 campo 16) manda
    IGUALAR na nota que não é NF-e, de terceiros, regular ou extemporânea; o
    texto do PVA diz MAIOR OU IGUAL, exceto na NF-e de entrada. O motor
    acusa só onde as duas concordam: modelo diferente de 55, terceiros,
    COD_SIT 00/01, e mercadoria MENOR que a soma dos itens. Gabarito a buscar:
    nota assim com VL_MERC acima da soma — o PVA acusa ou não?
    """
    if (no.modelo in (None, "55") or no.valor("IND_EMIT").strip() != "1"
            or no.sit not in ("00", "01")):
        return
    itens = [f for f in no.filhos if f.d.reg == "C170"]
    if not itens:
        return
    informado, soma = _valor(no, "VL_MERC"), _soma(itens, "VL_ITEM")
    if informado is None or soma is None or informado >= soma:
        return
    v_.acusar(no.r.linha, no.d.reg, "VL_MERC", regra.id,
              f"{v_.msg('MSG_VL_MERC_C170')} Informado {moeda(informado)}; soma "
              f"dos VL_ITEM dos C170: {moeda(soma)}.", valor=no.valor("VL_MERC"))


def _documento_x_itens(campo_id: str, chave_msg: str):
    """Desconto (VL_DESC) e abatimento (VL_ABAT_NT) do C100 = soma dos C170.

    Fora: a NF-e de emissão própria (itens parciais) e, no complementar e no
    regime especial, o C100 que deixou o campo vazio — ali ele é facultativo
    (Guia, C100, Exceções 3 e 4).
    """
    def verificar(v_, no, regra):
        if not _em_vigor(no) or _nfe_propria(no):
            return
        if no.sit in ("06", "07", "08") and not no.valor(campo_id).strip():
            return
        itens = [f for f in no.filhos if f.d.reg == "C170"]
        if not itens or campo_id not in itens[0].d.indice_campo:
            return
        informado, soma = _valor(no, campo_id), _soma(itens, campo_id)
        if informado is None or soma is None or informado == soma:
            return
        v_.acusar(no.r.linha, no.d.reg, campo_id, regra.id,
                  f"{v_.msg(chave_msg)} Informado {moeda(informado)}; soma dos "
                  f"C170: {moeda(soma)}.", valor=no.valor(campo_id))
    return verificar


def _ajustes_abaixo_do_documento(v_, no, regra):
    """ICMS dos ajustes do documento (C197) abaixo do VL_DOC do C100.

    Fora o complementar (06/07): a nota que só complementa o ICMS tem VL_DOC
    igual ao imposto, e o ajuste dela pode ter o mesmo valor.
    """
    if not _em_vigor(no) or no.sit in ("06", "07"):
        return
    ajustes = [n for f in no.filhos if f.d.reg == "C195"
               for n in f.filhos if n.d.reg == "C197"]
    if not ajustes:
        return
    soma, vl_doc = _soma(ajustes, "VL_ICMS"), _valor(no, "VL_DOC")
    if soma is None or vl_doc is None or soma <= 0 or soma < vl_doc:
        return
    v_.acusar(no.r.linha, no.d.reg, "VL_DOC", regra.id,
              f"{v_.msg('MSG_VL_ICMS_MAIOR_VL_DOC_C100')} Soma do VL_ICMS dos "
              f"C197: {moeda(soma)}; VL_DOC: {moeda(vl_doc)}.",
              valor=no.valor("VL_DOC"))


# ------------------------------------------------------- apuração (E110)

def _e110(no) -> dict | None:
    """{campo: Decimal} do E110; None se algum campo da conta for inválido."""
    valores = {}
    for c in no.d.campos[1:]:
        v = _valor(no, c.id)
        if v is None:
            return None
        valores[c.id] = v
    return valores


DEBITOS_E110 = ("VL_TOT_DEBITOS", "VL_AJ_DEBITOS", "VL_TOT_AJ_DEBITOS",
                "VL_ESTORNOS_CRED")
CREDITOS_E110 = ("VL_TOT_CREDITOS", "VL_AJ_CREDITOS", "VL_TOT_AJ_CREDITOS",
                 "VL_ESTORNOS_DEB", "VL_SLD_CREDOR_ANT")


def _saldo(e):
    """(02+03+04+05) - (06+07+08+09+10): positivo deve, negativo transporta."""
    return (sum(e[c] for c in DEBITOS_E110)
            - sum(e[c] for c in CREDITOS_E110))


def _sld_devedor(v_, no, regra):
    e = _e110(no)
    if e is None:
        return
    esperado = max(_saldo(e), ZERO)
    if e["VL_SLD_APURADO"] != esperado:
        v_.acusar(no.r.linha, "E110", "VL_SLD_APURADO", regra.id,
                  f"{v_.msg('MSG_SLD_DEVEDOR_E110')} Pela fórmula do Guia "
                  f"((02+03+04+05) - (06+07+08+09+10)), o saldo devedor é "
                  f"{moeda(esperado)}; o E110 informa {moeda(e['VL_SLD_APURADO'])}.",
                  valor=no.valor("VL_SLD_APURADO"))


def _sld_credor(v_, no, regra):
    e = _e110(no)
    if e is None:
        return
    saldo = _saldo(e)
    esperado = -saldo if saldo < 0 else ZERO
    if e["VL_SLD_CREDOR_TRANSPORTAR"] != esperado:
        v_.acusar(no.r.linha, "E110", "VL_SLD_CREDOR_TRANSPORTAR", regra.id,
                  f"{v_.msg('MSG_SLD_CREDOR_E110')} Pela fórmula do Guia, o "
                  f"saldo credor a transportar é {moeda(esperado)}; o E110 "
                  f"informa {moeda(e['VL_SLD_CREDOR_TRANSPORTAR'])}.",
                  valor=no.valor("VL_SLD_CREDOR_TRANSPORTAR"))


def _icms_recolher(v_, no, regra):
    e = _e110(no)
    if e is None:
        return
    esperado = max(e["VL_SLD_APURADO"] - e["VL_TOT_DED"], ZERO)
    if e["VL_ICMS_RECOLHER"] != esperado:
        v_.acusar(no.r.linha, "E110", "VL_ICMS_RECOLHER", regra.id,
                  f"{v_.msg('MSG_ICMS_RECOLHER')} Esperado {moeda(esperado)}; "
                  f"informado {moeda(e['VL_ICMS_RECOLHER'])}.",
                  valor=no.valor("VL_ICMS_RECOLHER"))


def _ded_ate_saldo(v_, no, regra):
    e = _e110(no)
    if e is None:
        return
    if e["VL_TOT_DED"] > e["VL_SLD_APURADO"]:
        v_.acusar(no.r.linha, "E110", "VL_TOT_DED", regra.id,
                  f"{v_.msg('MSG_VALIDA_VL_TOT_DED_E110')} Deduções "
                  f"{moeda(e['VL_TOT_DED'])}; saldo devedor "
                  f"{moeda(e['VL_SLD_APURADO'])}.", valor=no.valor("VL_TOT_DED"))


def _ajustes_e111(campo_id: str, natureza: str, chave_msg: str):
    """Campo do E110 = soma dos E111 com 3º caractere 0 (ICMS próprio) e o 4º
    caractere da natureza (0 outros débitos, 1 estorno de créditos, 2 outros
    créditos, 3 estorno de débitos — Tabela 5.1.1)."""
    def verificar(v_, no, regra):
        e = _e110(no)
        if e is None:
            return
        soma = ZERO
        for f in no.filhos:
            if f.d.reg != "E111":
                continue
            codigo = f.valor("COD_AJ_APUR").strip()
            if len(codigo) >= 4 and codigo[2] == "0" and codigo[3] == natureza:
                v = _valor(f, "VL_AJ_APUR")
                if v is None:
                    return
                soma += v
        if e[campo_id] != soma:
            v_.acusar(no.r.linha, "E110", campo_id, regra.id,
                      f"{v_.msg(chave_msg)} Informado {moeda(e[campo_id])}; "
                      f"soma dos E111 desta natureza: {moeda(soma)}.",
                      valor=no.valor(campo_id))
    return verificar


def _guias_e116(v_, no, regra):
    e = _e110(no)
    if e is None:
        return
    soma = _soma([f for f in no.filhos if f.d.reg == "E116"], "VL_OR")
    if soma is None:
        return
    esperado = e["VL_ICMS_RECOLHER"] + e["DEB_ESP"]
    if soma != esperado:
        v_.acusar(no.r.linha, "E110", "VL_ICMS_RECOLHER", regra.id,
                  f"{v_.msg('MSG_ICMS_REC_E116')} Soma dos E116: {moeda(soma)}; "
                  f"ICMS a recolher + extra-apuração: {moeda(esperado)}.",
                  valor=no.valor("VL_ICMS_RECOLHER"))


def _total_por_cfop(debito: bool, chave_msg: str, campo_id: str):
    """VL_TOT_DEBITOS (02) e VL_TOT_CREDITOS (06) contra os analíticos.

    Débito: CFOP iniciado por 5, 6 ou 7, e o 1605; documento extemporâneo fica
    de fora (vai para o DEB_ESP). Crédito: CFOP iniciado por 1, 2 ou 3, exceto
    1605, e o 5605. Só com UM E110 no arquivo — com apuração decendial, saber
    qual documento cai em qual período é outra regra.
    """
    registros = ANALITICOS_DEBITO if debito else ANALITICOS_CREDITO

    def verificar(v_, no, regra):
        if sum(1 for n in v_.nos if n.d.reg == "E110") != 1:
            v_.nao_conferidas[regra.id] += 1
            return
        e = _e110(no)
        if e is None:
            return
        soma = ZERO
        for n in v_.nos:
            if n.d.reg not in registros or not _em_vigor(n):
                continue
            cfop = n.valor("CFOP").strip()
            if debito:
                entra = cfop[:1] in ("5", "6", "7") or cfop == "1605"
                if n.sit in EXTEMPORANEOS:
                    entra = False
            else:
                entra = ((cfop[:1] in ("1", "2", "3") and cfop != "1605")
                         or cfop == "5605")
            if not entra:
                continue
            v = _valor(n, "VL_ICMS")
            if v is None:
                return
            soma += v
        if e[campo_id] != soma:
            v_.acusar(no.r.linha, "E110", campo_id, regra.id,
                      f"{v_.msg(chave_msg)} Informado {moeda(e[campo_id])}; "
                      f"soma dos analíticos: {moeda(soma)}.",
                      valor=no.valor(campo_id))
    return verificar


# --------------------------------------------- existência ligada ao 0000

def _apuracao_ipi(deve_existir: bool, chave_msg: str):
    """IND_ATIV do 0000 decide o Bloco E500: industrial ou equiparado (0) é
    contribuinte do IPI e apresenta a apuração; os demais (1), não.

    Achado no gabarito (LIESA, jul/2026): IND_ATIV 0 sem E500 nenhum.
    """
    def verificar(v_, no, regra):
        ind = no.valor("IND_ATIV").strip()
        if ind != ("0" if deve_existir else "1"):
            return
        tem = any(n.d.reg == "E500" for n in v_.nos)
        if tem != deve_existir:
            v_.acusar(no.r.linha, "0000", "IND_ATIV", regra.id,
                      f"{v_.msg(chave_msg)} IND_ATIV = {ind}.", valor=ind)
    return verificar


REGRAS_DE_CONTA = {
    "REGRA_EXISTE_APURACAO_IPI":
        _apuracao_ipi(True, "MSG_EXISTE_APURACAO_IPI"),
    "REGRA_NAO_EXISTE_APURACAO_IPI":
        _apuracao_ipi(False, "MSG_NAO_EXISTE_APURACAO_IPI"),
    "REGRA_VL_BC_ICMS_NO_REGISTRO_ITEM":
        _analitico_x_itens("VL_BC_ICMS", "MSG_VL_BC_ICMS_NO_REGISTRO_ITEM"),
    "REGRA_VL_ICMS_NO_REGISTRO_ITEM":
        _analitico_x_itens("VL_ICMS", "MSG_VL_ICMS_NO_REGISTRO_ITEM"),
    "REGRA_VL_BC_ICMS_ST_NO_REGISTRO_ITEM":
        _analitico_x_itens("VL_BC_ICMS_ST", "MSG_VL_BC_ICMS_ST_NO_REGISTRO_ITEM"),
    "REGRA_VL_ICMS_ST_NO_REGISTRO_ITEM":
        _analitico_x_itens("VL_ICMS_ST", "MSG_VL_ICMS_ST_NO_REGISTRO_ITEM"),
    "REGRA_CHAVE_EXISTE_NO_REGISTRO_ITEM_A8E1V1": _chave_existe_nos_itens,
    "REGRA_VL_BC_ICMS_ANALIT":
        _documento_x_analiticos("VL_BC_ICMS", "MSG_VL_BC_ICMS_ANALIT"),
    "REGRA_VL_ICMS_ANALIT":
        _documento_x_analiticos("VL_ICMS", "MSG_VL_ICMS_ANALIT"),
    "REGRA_VL_BC_ICMS_ST_ANALIT":
        _documento_x_analiticos("VL_BC_ICMS_ST", "MSG_VL_BC_ICMS_ST_ANALIT"),
    "REGRA_VL_ICMS_ST_ANALIT":
        _documento_x_analiticos("VL_ICMS_ST", "MSG_VL_ICMS_ST_ANALIT"),
    "REGRA_VL_IPI_C190":
        _documento_x_analiticos("VL_IPI", "MSG_VL_IPI_ANALIT"),
    "REGRA_VL_MERC_C170": _vl_merc_x_itens,
    "REGRA_VALIDA_DESC_DOC_ITEM":
        _documento_x_itens("VL_DESC", "MSG_VALIDA_DESC_DOC_ITEM"),
    "REGRA_VALIDA_VL_ABAT_NT":
        _documento_x_itens("VL_ABAT_NT", "MSG_VALIDA_VL_ABAT_NT"),
    "REGRA_VL_ICMS_MAIOR_VL_DOC_C100": _ajustes_abaixo_do_documento,
    "REGRA_SLD_DEVEDOR_E110": _sld_devedor,
    "REGRA_SLD_CREDOR_E110": _sld_credor,
    "REGRA_ICMS_RECOLHER": _icms_recolher,
    "REGRA_VALIDA_VL_TOT_DED_E110": _ded_ate_saldo,
    "REGRA_VL_TOT_AJ_DEBITOS_E110":
        _ajustes_e111("VL_TOT_AJ_DEBITOS", "0", "MSG_VL_TOT_AJ_DEBITOS_E110"),
    "REGRA_VL_ESTORNOS_CRED_E110":
        _ajustes_e111("VL_ESTORNOS_CRED", "1", "MSG_VL_ESTORNOS_CRED_E110"),
    "REGRA_VL_TOT_AJ_CREDITOS_E110":
        _ajustes_e111("VL_TOT_AJ_CREDITOS", "2", "MSG_VL_TOT_AJ_CREDITOS_E110"),
    "REGRA_VL_ESTORNOS_DEB_E110":
        _ajustes_e111("VL_ESTORNOS_DEB", "3", "MSG_VL_ESTORNOS_DEB_E110"),
    "REGRA_ICMS_REC_E116": _guias_e116,
    "REGRA_VL_TOT_DEBITOS_E110_17002_V1":
        _total_por_cfop(True, "MSG_VL_TOT_DEBITOS_E110_17002_V1", "VL_TOT_DEBITOS"),
    "REGRA_VL_TOT_CREDITOS_E110_17002_V1":
        _total_por_cfop(False, "MSG_VL_TOT_CREDITOS_E110_17002_V1", "VL_TOT_CREDITOS"),
}
