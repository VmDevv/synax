"""Regras dos CADASTROS do Bloco 0: participante (0150) e item (0200).

Duas famílias. A primeira é o cadastro contra o resto do arquivo: participante
e item só entram se algum registro os usar (Guia, 0150 campo 02 e 0200 campo
02) — e "usar" é o que o descritor do PVA diz, pela fonte de valores de cada
campo que aponta para o 0150 ou o 0200, sem lista à mão que esqueça um. A
segunda é o próprio cadastro: participante do exterior não tem CNPJ, CPF, IE
nem SUFRAMA; o do Brasil tem CNPJ ou CPF e município; item de industrial tem
NCM.

Duas regras do 0200 ficam de fora DE PROPÓSITO, por medição: o GTIN do
COD_BARRA (REGRA_VALIDA_COD_BARRA) e a IE por UF (REGRA_VALIDA_IE_COD_MUN_V2,
que pede um algoritmo por estado). Código de barras fora do padrão GTIN volta
mês a mês no SPED da Biancardi (junho e setembro/2026, os mesmos quatro itens):
se o PVA o tratasse como erro, o arquivo não teria sido transmitido.
Continuam contadas como não conferidas até um gabarito dizer o que o PVA faz.
"""
from __future__ import annotations

import re

BRASIL = "1058"

# Filho do 0200 que justifica o item sem movimento, e desde quando (Guia,
# 0200, "Validação do registro").
FILHOS_QUE_JUSTIFICAM_O_ITEM = {"0220": "20120701", "0205": "20210101",
                                "0221": "20230101"}

# Registros em que o item tem direito a ressarcimento/complemento do ICMS ST
# (MSG_REGRA_OBRIGATORIO_ALIQ_RESSARC_ST).
REGISTROS_DE_RESSARCIMENTO = ("C180", "C185", "C330", "C380", "C430", "C480",
                              "C810", "C870")

# Tipos de item dispensados do NCM no industrial (MSG_OBRIGATORIO_NCM).
TIPOS_SEM_NCM = frozenset({"07", "08", "09", "10", "99"})


def _do_brasil(no) -> bool | None:
    """COD_PAIS 1058 ou 01058; None se vazio ou inválido (já acusado)."""
    pais = no.valor("COD_PAIS").strip()
    if not pais.isdigit():
        return None
    return pais.lstrip("0") == BRASIL


def _referencias(v_) -> dict[str, set]:
    """{registro do cadastro: códigos usados fora dele}, lido uma vez.

    Conta todo campo cuja fonte de valores aponta para o cadastro, em
    qualquer registro que não seja o próprio — inclusive o 0221 de outro item
    (COD_ITEM_ATOMICO): na dúvida sobre o que o PVA chama de "demais blocos",
    a leitura que conta mais referências é a que não acusa.
    """
    cache = getattr(v_, "_referencias", None)
    if cache is not None:
        return cache
    cache = {"0150": set(), "0200": set()}
    campos_por_registro: dict[str, list] = {}
    for d in v_.lt.registros.values():
        for c in d.campos:
            alvo = v_.fontes.get(c.fonte or "", {}).get("parametros", {}).get("registro")
            if alvo in cache and alvo != d.reg:
                campos_por_registro.setdefault(d.reg, []).append((c, alvo))
    for no in v_.nos:
        for c, alvo in campos_por_registro.get(no.d.reg, ()):
            if c.n <= len(no.r.campos):
                codigo = no.r.campo(c.n).strip()
                if codigo:
                    cache[alvo].add(codigo)
    v_._referencias = cache
    return cache


# ------------------------------------------------------------- participante

def _so_no_brasil(v_, no, regra):
    campo = regra.campo
    if campo and _do_brasil(no) is False and no.valor(campo).strip():
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  f"{v_.msg('MSG_NAO_PREENCHER_BRA')} COD_PAIS "
                  f"{no.valor('COD_PAIS').strip()}.", valor=no.valor(campo))


def _cnpj_ou_cpf(v_, no, regra):
    if no.valor("CNPJ").strip() and no.valor("CPF").strip():
        v_.acusar(no.r.linha, no.d.reg, "CNPJ", regra.id,
                  v_.msg("MSG_CNPJ_EXCLUDENTE"), valor=no.valor("CNPJ"))


def _documento_no_brasil(v_, no, regra):
    if (_do_brasil(no) and not no.valor("CNPJ").strip()
            and not no.valor("CPF").strip()):
        v_.acusar(no.r.linha, no.d.reg, regra.campo or "CNPJ", regra.id,
                  f"{v_.msg('MSG_OBRIGATORIO_CPF_BRA')} Informe o CNPJ ou o "
                  "CPF do participante.")


def _municipio_no_brasil(v_, no, regra):
    campo = regra.campo or "COD_MUN"
    municipio = no.valor(campo).strip()
    if _do_brasil(no) and municipio in ("", "9999999"):
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  v_.msg("MSG_OBRIGATORIO_PARA_BRASIL_A10E3V4"), valor=municipio)


def _cadastro_usado(registro: str, campo: str, chave_msg: str):
    def verificar(v_, no, regra):
        codigo = no.valor(campo).strip()
        if not codigo or codigo in _referencias(v_)[registro]:
            return
        if registro == "0200":
            filhos = {f.d.reg for f in no.filhos}
            if any(reg in filhos and v_.dt_ini >= desde
                   for reg, desde in FILHOS_QUE_JUSTIFICAM_O_ITEM.items()):
                return
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  f"{v_.msg(chave_msg)} O código {codigo} não aparece em "
                  "nenhum outro registro do arquivo.", valor=codigo)
    return verificar


def _digito_suframa(v_, no, c, v):
    """Módulo 11 com pesos 9 a 2 nos oito primeiros dígitos; resto que daria
    10 ou 11 vira 0. Conferido nas três inscrições reais do SPED da Rio Doce
    (set/2026), uma delas com o dígito zero."""
    if len(v) != 9 or not v.isdigit():
        valido = False
    else:
        soma = sum(int(d) * p for d, p in zip(v[:8], range(9, 1, -1)))
        dv = 11 - soma % 11
        valido = int(v[8]) == (0 if dv >= 10 else dv)
    if not valido:
        v_.acusar(no.r.linha, no.d.reg, c.id, "REGRA_DIGITO_SUFRAMA",
                  v_.msg("MSG_VALIDA_SUFRAMA", "Inscrição SUFRAMA inválida."),
                  valor=v)


# --------------------------------------------------------------------- item

def _ncm_do_industrial(v_, no, regra):
    campo = regra.campo or "COD_NCM"
    if no.valor(campo).strip() or no.valor("TIPO_ITEM").strip() in TIPOS_SEM_NCM:
        return
    zero = v_.nos[0] if v_.nos and v_.nos[0].d.reg == "0000" else None
    if zero is not None and zero.valor("IND_ATIV").strip() == "0":
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  f"{v_.msg('MSG_OBRIGATORIO_NCM')} Tipo do item "
                  f"{no.valor('TIPO_ITEM').strip() or 'vazio'}.")


def _ncm_generico(v_, no, c, v):
    if v == "99999999":
        v_.acusar(no.r.linha, no.d.reg, c.id, "REGRA_VALIDA_COD_NCM",
                  v_.msg("MSG_VALIDA_COD_NCM"), valor=v)


def _codigo_anterior_no_0205(v_, no, c, v):
    v_.acusar(no.r.linha, no.d.reg, c.id, "REGRA_NAO_INFORMAR_COD_ANT_ITEM_0200",
              v_.msg("MSG_NAO_INFORMAR_COD_ANT_ITEM_0200"), valor=v)


def _servico_da_lc_116(v_, no, c, v):
    if not re.fullmatch(r"\d\d\.\d\d", v):
        v_.acusar(no.r.linha, no.d.reg, c.id, "REGRA_VALIDA_COD_LST",
                  v_.msg("MSG_VALIDA_COD_LST"), valor=v)


def _barra_do_fator_de_conversao(v_, no, regra):
    campo = regra.campo or "COD_BARRA"
    if no.valor(campo).strip():
        return
    com_barra = [f for f in no.filhos
                 if f.d.reg == "0220" and f.valor("COD_BARRA").strip()]
    if com_barra:
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  f"{v_.msg('MSG_OBRIGATORIO_COD_BARRA')} 0220 da linha "
                  f"{com_barra[0].r.linha}.")


def _itens_de_ressarcimento(v_) -> set:
    """COD_ITEM dos registros de ressarcimento: o do próprio registro ou, no
    C180 (filho do C170) e nos demais sem o campo, o do pai mais próximo."""
    cache = getattr(v_, "_itens_ressarcimento", None)
    if cache is None:
        cache = set()
        for no in v_.nos:
            if no.d.reg not in REGISTROS_DE_RESSARCIMENTO:
                continue
            atual = no
            while atual is not None and "COD_ITEM" not in atual.d.indice_campo:
                atual = atual.pai
            if atual is not None and atual.valor("COD_ITEM").strip():
                cache.add(atual.valor("COD_ITEM").strip())
        v_._itens_ressarcimento = cache
    return cache


def _unidade_com_fator(v_, no, regra):
    """Item escriturado (C170) em unidade diferente da do estoque (0200
    UNID_INV) pede o fator de conversão (0220). O texto do PVA pede "um
    filho 0220", sem dizer de qual unidade — e é o que se confere; nos SPEDs
    reais medidos, todo item nessa situação tinha o 0220 da própria unidade.
    """
    cache = getattr(v_, "_unidades_do_item", None)
    if cache is None:
        cache = {n.valor("COD_ITEM").strip(): (
                     n.valor("UNID_INV").strip(),
                     any(f.d.reg == "0220" for f in n.filhos))
                 for n in v_.nos if n.d.reg == "0200"}
        v_._unidades_do_item = cache
    unidade, item = no.valor("UNID").strip(), no.valor("COD_ITEM").strip()
    if not unidade or item not in cache:
        return
    unidade_estoque, tem_fator = cache[item]
    if unidade != unidade_estoque and not tem_fator:
        v_.acusar(no.r.linha, no.d.reg, "UNID", regra.id,
                  f"{v_.msg('MSG_VALIDA_UNID_CONVERSAO')} Item {item}: unidade "
                  f"{unidade} no {no.d.reg} e {unidade_estoque} no 0200.",
                  valor=unidade)


def _aliquota_do_ressarcimento(v_, no, regra):
    codigo = no.valor("COD_ITEM").strip()
    if (codigo and not no.valor("ALIQ_ICMS").strip()
            and codigo in _itens_de_ressarcimento(v_)):
        v_.acusar(no.r.linha, no.d.reg, "ALIQ_ICMS", regra.id,
                  v_.msg("MSG_REGRA_OBRIGATORIO_ALIQ_RESSARC_ST"), valor=codigo)


# ------------------------------------------------------------------ mapas

DE_CAMPO = {
    "REGRA_DIGITO_SUFRAMA": _digito_suframa,
    "REGRA_VALIDA_COD_NCM": _ncm_generico,
    "REGRA_NAO_INFORMAR_COD_ANT_ITEM_0200": _codigo_anterior_no_0205,
    "REGRA_VALIDA_COD_LST": _servico_da_lc_116,
}

DE_REGISTRO = {
    "REGRA_NAO_PREENCHER_BRA_CNPJ": _so_no_brasil,
    "REGRA_NAO_PREENCHER_BRA_CPF": _so_no_brasil,
    "REGRA_NAO_PREENCHER_BRA_IE": _so_no_brasil,
    "REGRA_NAO_PREENCHER_BRA_SUFRAMA": _so_no_brasil,
    "REGRA_CNPJ_EXCLUDENTE": _cnpj_ou_cpf,
    "REGRA_OBRIGATORIO_CPF_BRA_V2": _documento_no_brasil,
    "REGRA_OBRIGATORIO_PARA_BRASIL_A10E3V4": _municipio_no_brasil,
    "REGRA_REFERENCIADO_COD_PART_17002_V1":
        _cadastro_usado("0150", "COD_PART", "MSG_REFERENCIADO_COD_PART"),
    "REGRA_REFERENCIADO_COD_ITEM_17002_V1":
        _cadastro_usado("0200", "COD_ITEM", "MSG_REFERENCIADO_COD_ITEM"),
    "REGRA_OBRIGATORIO_NCM": _ncm_do_industrial,
    "REGRA_OBRIGATORIO_COD_BARRA": _barra_do_fator_de_conversao,
    "REGRA_OBRIGATORIO_ALIQ_RESSARC_ST": _aliquota_do_ressarcimento,
    "REGRA_VALIDA_UNID_CONVERSAO_17001_V1": _unidade_com_fator,
}
