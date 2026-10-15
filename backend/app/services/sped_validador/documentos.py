"""Regras do DOCUMENTO: chave de acesso, operação/emitente/CFOP e CST × C190.

A chave de 44 dígitos repete o que o registro declara — UF (2), AAMM (4),
CNPJ do emitente (14), modelo (2), série (3), número (9), forma de emissão (1),
código (8) e DV (1) —, e o PVA confere cada pedaço contra o campo
correspondente. Chave com DV errado já é acusada por REGRA_VALIDA_CHV_NFE_CTE;
aqui ela é ignorada, para não acusar o mesmo defeito duas vezes.

Como em `contas.py`, cada regra roda só onde o descritor do PVA a aplica; o
texto vem do `validador.prop` e a lógica do Guia. Onde a exceção não é certa,
a leitura escolhida é a que NÃO acusa — por exemplo, o regime especial
(COD_SIT 08), em que existe saída emitida por terceiros (a própria regra
REGRA_CHAVE_REGIME_ESPECIAL_NFE diz isso), fica fora de "terceiros é entrada".
"""
from __future__ import annotations

from decimal import Decimal

from . import campos as cp

ZERO = Decimal("0")
ADVERTENCIA = "advertencia"

# Modelos com chave de acesso (MSG_CHVDFE_MODELO).
ELETRONICOS = frozenset({"55", "57", "63", "65", "67"})
CAMPOS_DE_CHAVE = ("CHV_NFE", "CHV_CTE", "CHV_DOCE", "CHV_CFE")

# Código IBGE da UF (os dois primeiros dígitos da chave).
UF_IBGE = {
    "11": "RO", "12": "AC", "13": "AM", "14": "RR", "15": "PA", "16": "AP",
    "17": "TO", "21": "MA", "22": "PI", "23": "CE", "24": "RN", "25": "PB",
    "26": "PE", "27": "AL", "28": "SE", "29": "BA", "31": "MG", "32": "ES",
    "33": "RJ", "35": "SP", "41": "PR", "42": "SC", "43": "RS", "50": "MS",
    "51": "MT", "52": "GO", "53": "DF",
}


def _chave(no, regra=None) -> tuple[str, str] | None:
    """(campo, chave) do registro, só se a chave tiver 44 dígitos e DV certo."""
    nomes = ([regra.campo] if regra is not None and regra.campo else []) \
        + list(CAMPOS_DE_CHAVE)
    for nome in nomes:
        if nome in no.d.indice_campo:
            valor = no.valor(nome).strip()
            if valor and cp.chave_valida(valor):
                return nome, valor
            return None
    return None


def _inteiro(texto: str) -> int | None:
    texto = texto.strip()
    if not texto:
        return 0
    return int(texto) if texto.isdigit() else None


def _cnpj_0000(v_) -> tuple[str, str]:
    zero = v_.nos[0] if v_.nos and v_.nos[0].d.reg == "0000" else None
    if zero is None:
        return "", ""
    return zero.valor("CNPJ").strip(), zero.valor("CPF").strip()


def _participantes(v_) -> dict:
    cache = getattr(v_, "_participantes", None)
    if cache is None:
        cache = {n.valor("COD_PART").strip(): (n.valor("CNPJ").strip(),
                                               n.valor("CPF").strip())
                 for n in v_.nos if n.d.reg == "0150"}
        v_._participantes = cache
    return cache


def _emitente_confere(chave: str, cnpj: str, cpf: str) -> bool | None:
    """A chave traz CNPJ (14) ou, para pessoa física, "000" + CPF (11)."""
    na_chave = chave[6:20]
    if cnpj:
        return na_chave == cnpj
    if cpf:
        return na_chave[3:] == cpf
    return None


# ----------------------------------------------- chave contra o documento

def _modelo_da_chave(v_, no, regra):
    achado = _chave(no, regra)
    if not achado or "COD_MOD" not in no.d.indice_campo:
        return
    campo, chave = achado
    modelo = no.valor("COD_MOD").strip()
    if modelo and chave[20:22] != modelo:
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  f"{v_.msg('MSG_COMPARA_CODIGO_MODELO')} Chave: modelo "
                  f"{chave[20:22]}; registro: {modelo}.", valor=chave)


def _serie_da_chave(v_, no, regra):
    achado = _chave(no, regra)
    if not achado or "SER" not in no.d.indice_campo:
        return
    campo, chave = achado
    serie = no.valor("SER").strip()
    informada = _inteiro(serie)
    if informada is None or informada != int(chave[22:25]):
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  f"{v_.msg('MSG_VALIDA_SERIE_CHAVE')} Chave: série "
                  f"{int(chave[22:25])}; registro: {serie or 'vazia'}.",
                  valor=chave)


def _numero_da_chave(v_, no, regra):
    achado = _chave(no, regra)
    campo_num = regra.outros[0] if regra.outros else "NUM_DOC"
    if not achado or campo_num not in no.d.indice_campo:
        return
    campo, chave = achado
    numero = _inteiro(no.valor(campo_num))
    if numero is not None and numero != int(chave[25:34]):
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  f"O número do documento na chave ({int(chave[25:34])}) não "
                  f"confere com o {campo_num} informado ({numero}).",
                  valor=chave)


def _uf_da_chave(v_, no, regra):
    """Só em EMISSÃO PRÓPRIA: em documento de terceiro, a UF da chave é a do
    emitente, que não precisa ser a do informante."""
    achado = _chave(no, regra)
    if not achado or no.valor("IND_EMIT").strip() != "0" or not v_.uf:
        return
    campo, chave = achado
    uf = UF_IBGE.get(chave[:2])
    if uf != v_.uf:
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  f"A UF informada na chave ({uf or chave[:2]}) não confere com "
                  f"a UF do informante do arquivo ({v_.uf}).", valor=chave)


def _cnpj_proprio_da_chave(v_, no, regra):
    """Emissão própria: o emitente da chave é o informante (0000)."""
    achado = _chave(no, regra)
    if (not achado or no.valor("IND_EMIT").strip() != "0"
            or no.sit == "08"):
        return
    campo, chave = achado
    cnpj, cpf = _cnpj_0000(v_)
    if _emitente_confere(chave, cnpj, cpf) is False:
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  "O CNPJ/CPF da chave não é o do informante (registro 0000), "
                  "e o documento é de emissão própria.", valor=chave)


def _cnpj_participante_da_chave(v_, no, regra):
    """Emissão de terceiros: o emitente da chave é o participante (0150)."""
    achado = _chave(no, regra)
    if (not achado or no.valor("IND_EMIT").strip() != "1"
            or no.sit == "08"):
        return
    campo, chave = achado
    cod_part = no.valor("COD_PART").strip()
    dados_part = _participantes(v_).get(cod_part)
    if not dados_part:
        return          # participante ausente: acusado por REGRA_EXISTE_COD_PART
    if _emitente_confere(chave, *dados_part) is False:
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  f"{v_.msg('MSG_COMPARA_CNPJ_NFE_PARTICIPANTE_12001_V1')} "
                  f"Chave: {chave[6:20]}; participante {cod_part}: "
                  f"{dados_part[0] or dados_part[1]}.", valor=chave)


def _chave_so_em_eletronico(v_, no, c, v):
    modelo = no.valor("COD_MOD").strip()
    if v and modelo and modelo not in ELETRONICOS:
        v_.acusar(no.r.linha, no.d.reg, c.id, "REGRA_CHVDFE_MODELO_12001_V1",
                  f"{v_.msg('MSG_CHVDFE_MODELO')} Modelo {modelo}.", valor=v)


def _chave_obrigatoria(terceiros: bool):
    def verificar(v_, no, regra):
        campo = regra.campo or "CHV_NFE"
        if campo not in no.d.indice_campo or no.valor(campo).strip():
            return
        modelo = no.valor("COD_MOD").strip()
        emit = no.valor("IND_EMIT").strip()
        # NFC-e tem regra própria (REGRA_OBRIGATORIO_CHVE_NFCE_A7E2V1).
        if (modelo not in ELETRONICOS or modelo == "65"
                or emit != ("1" if terceiros else "0") or no.sit == "05"):
            return
        chave_msg = ("MSG_OBRIGATORIO_CHV_NFE_TERCEIROS" if terceiros
                     else "MSG_OBRIGATORIO_CHV_NFE_CTE")
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  f"{v_.msg(chave_msg)} Modelo {modelo}, "
                  f"{'terceiros' if terceiros else 'emissão própria'}.")
    return verificar


def _chave_nfce(v_, no, regra):
    if (no.valor("COD_MOD").strip() == "65" and not no.valor("CHV_NFE").strip()
            and no.sit != "05"):
        v_.acusar(no.r.linha, no.d.reg, "CHV_NFE", regra.id,
                  v_.msg("MSG_OBRIGATORIO_CHVE_NFCE_A7E2V1"))


def _chave_regime_especial(v_, no, regra):
    campo = regra.campo or "CHV_NFE"
    if (no.sit == "08" and no.valor("IND_OPER").strip() == "1"
            and no.valor("IND_EMIT").strip() == "1"
            and no.valor("COD_MOD").strip() in ELETRONICOS
            and campo in no.d.indice_campo and not no.valor(campo).strip()):
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  v_.msg("MSG_CHAVE_REGIME_ESPECIAL"))


# ------------------------------------------- operação, emitente e CFOP

def _terceiros_e_entrada(v_, no, regra):
    if (no.valor("IND_EMIT").strip() == "1" and no.valor("IND_OPER").strip() == "1"
            and no.sit != "08"):
        v_.acusar(no.r.linha, no.d.reg, "IND_EMIT", regra.id,
                  v_.msg("MSG_IND_EMIT"), valor="1")


def _nfce_emitente(v_, no, regra):
    if no.valor("COD_MOD").strip() == "65" and no.valor("IND_EMIT").strip() != "0":
        v_.acusar(no.r.linha, no.d.reg, "IND_EMIT", regra.id,
                  v_.msg("MSG_IND_EMIT_NFCE"), valor=no.valor("IND_EMIT"))


def _nfce_operacao(v_, no, c, v):
    if no.valor("COD_MOD").strip() == "65" and v != "1":
        v_.acusar(no.r.linha, no.d.reg, c.id, "REGRA_IND_OPER_NFCE_12001_V1",
                  v_.msg("MSG_IND_OPER_NFCE"), valor=v)


def _cfop_da_operacao(chave_msg: str):
    """1º dígito do CFOP do item/analítico × IND_OPER do documento."""
    def verificar(v_, no, regra):
        cfop = no.valor("CFOP").strip()
        operacao = no.operacao
        if not cfop or operacao not in ("0", "1"):
            return
        esperado = ("1", "2", "3") if operacao == "0" else ("5", "6", "7")
        if cfop[0] not in esperado:
            v_.acusar(no.r.linha, no.d.reg, "CFOP", regra.id,
                      f"{v_.msg(chave_msg)} Documento de "
                      f"{'entrada' if operacao == '0' else 'saída'}; CFOP {cfop}.",
                      valor=cfop)
    return verificar


def _cfop_nfce(v_, no, regra):
    cfop = no.valor("CFOP").strip()
    if no.modelo == "65" and cfop and cfop[0] != "5":
        v_.acusar(no.r.linha, no.d.reg, "CFOP", regra.id,
                  v_.msg("MSG_CFOP_NFCE"), valor=cfop)


def _cfop_de_saida(primeiros: tuple, chave_msg: str):
    def verificar(v_, no, regra):
        cfop = no.valor("CFOP").strip()
        if cfop and cfop[0] not in primeiros:
            v_.acusar(no.r.linha, no.d.reg, "CFOP", regra.id,
                      v_.msg(chave_msg), valor=cfop)
    return verificar


def _coerencia_cfop(itens_reg: str):
    """Todos os itens do documento com o mesmo 1º dígito de CFOP (Guia,
    C170 campo 11) — a trava que a correção do SPED também respeita."""
    def verificar(v_, no, regra):
        itens = [f for f in no.filhos if f.d.reg == itens_reg]
        primeiros = {f.valor("CFOP").strip()[:1] for f in itens
                     if f.valor("CFOP").strip()}
        if len(primeiros) > 1:
            v_.acusar(no.r.linha, no.d.reg, "CFOP", regra.id,
                      f"{v_.msg('MSG_COERENCIA_CFOP')} Os {itens_reg} deste "
                      f"documento começam com {', '.join(sorted(primeiros))}.",
                      valor="/".join(sorted(primeiros)))
    return verificar


# ------------------------------------------------------- CST × campos (C190)

MONOFASICO = ("02", "15", "53", "61")


def _gravidade_cst(cst: str) -> str | None:
    """Os textos do PVA para o CST final 02 pedem ao Simples Nacional que
    "desconsidere essa advertência": ali é advertência; nos demais, erro."""
    return ADVERTENCIA if cst[-2:] == "02" else None


def _valor(no, campo: str) -> Decimal | None:
    texto = no.valor(campo).strip()
    return ZERO if not texto else cp.numero(texto)


def _zero_no_monofasico(campo_id: str, chave_msg: str, regra_id: str,
                        so_saida_61: bool = False):
    def verificar(v_, no, c, v):
        cst = no.valor("CST_ICMS").strip()
        cfop = no.valor("CFOP").strip()
        if so_saida_61:
            if cst[-2:] != "61" or cfop[:1] not in ("5", "6", "7"):
                return
        elif cst[-2:] not in MONOFASICO:
            return
        valor = cp.numero(v)
        if valor is not None and valor != 0:
            v_.acusar(no.r.linha, no.d.reg, campo_id, regra_id,
                      f"{v_.msg(chave_msg)} CST {cst}.", valor=v,
                      gravidade=_gravidade_cst(cst))
    return verificar


def _cst_campos_icms(v_, no, regra):
    if no.operacao != "1":
        return
    cst = no.valor("CST_ICMS").strip()
    final = cst[-2:]
    if final not in MONOFASICO:
        return
    aliq, icms = _valor(no, "ALIQ_ICMS"), _valor(no, "VL_ICMS")
    if aliq is None or icms is None:
        return
    errado = ((final in ("02", "15") and (icms != 0 or aliq <= 0))
              or (final == "53" and aliq <= 0)
              or (final == "61" and (aliq != 0 or icms != 0)))
    if errado:
        v_.acusar(no.r.linha, no.d.reg, "CST_ICMS", regra.id,
                  f"{v_.msg('MSG_CST_ICMS_CAMPOS_ICMS_C190')} CST {cst}, "
                  f"alíquota {no.valor('ALIQ_ICMS') or '0'}, ICMS "
                  f"{no.valor('VL_ICMS') or '0'}.", valor=cst,
                  gravidade=_gravidade_cst(cst))


# ------------------------------------------------------------------ mapas

DE_CAMPO = {
    "REGRA_CHVDFE_MODELO_12001_V1": _chave_so_em_eletronico,
    "REGRA_IND_OPER_NFCE_12001_V1": _nfce_operacao,
    "REGRA_VALIDA_CST_VL_BC_ICMS_C190": _zero_no_monofasico(
        "VL_BC_ICMS", "MSG_VALIDA_CST_VL_BC_ICMS_C190",
        "REGRA_VALIDA_CST_VL_BC_ICMS_C190"),
    "REGRA_VALIDA_CST_VL_BC_ICMS_ST_C190": _zero_no_monofasico(
        "VL_BC_ICMS_ST", "MSG_VALIDA_CST_VL_BC_ICMS_ST_C190",
        "REGRA_VALIDA_CST_VL_BC_ICMS_ST_C190"),
    "REGRA_VALIDA_CST_VL_ICMS_ST_C190": _zero_no_monofasico(
        "VL_ICMS_ST", "MSG_VALIDA_CST_VL_ICMS_ST_C190",
        "REGRA_VALIDA_CST_VL_ICMS_ST_C190", so_saida_61=True),
}

DE_REGISTRO = {
    "REGRA_COMPARA_CODIGO_MODELO": _modelo_da_chave,
    "REGRA_VALIDA_SERIE_CHAVE": _serie_da_chave,
    "REGRA_COMPARA_NUM_DOC_NFE_CTE": _numero_da_chave,
    "REGRA_COMPARA_NUM_DOC_NFE_CTE_C100_V4": _numero_da_chave,
    "REGRA_COMPARA_UF_NFE_CTE_13001_V1": _uf_da_chave,
    "REGRA_COMPARA_CNPJ_NFE_CTE_12001_V1": _cnpj_proprio_da_chave,
    "REGRA_COMPARA_CNPJ_NFE_CTE_14003_V5": _cnpj_proprio_da_chave,
    "REGRA_COMPARA_CNPJ_NFE_PARTICIPANTE_12001_V1": _cnpj_participante_da_chave,
    "REGRA_OBRIGATORIO_CHV_NFE_CTE_A13E1V2": _chave_obrigatoria(terceiros=False),
    "REGRA_OBRIGATORIO_CHV_NFE_CTE_TERCEIROS_12001_V1": _chave_obrigatoria(terceiros=True),
    "REGRA_OBRIGATORIO_CHVE_NFCE_A7E2V1": _chave_nfce,
    "REGRA_CHAVE_REGIME_ESPECIAL_NFE": _chave_regime_especial,
    "REGRA_IND_EMIT": _terceiros_e_entrada,
    "REGRA_IND_EMIT_NFCE": _nfce_emitente,
    "REGRA_CFOP_C100": _cfop_da_operacao("MSG_CFOP_C100"),
    "REGRA_CFOP_D190": _cfop_da_operacao("MSG_CFOP_D190"),
    "REGRA_CFOP_NFCE": _cfop_nfce,
    "REGRA_CFOP_SAIDA": _cfop_de_saida(("5",), "MSG_CFOP_SAIDA"),
    "REGRA_CFOP_SAIDA_AMPLO": _cfop_de_saida(("5", "6", "7"), "MSG_CFOP_SAIDA_AMPLO"),
    "REGRA_COERENCIA_CFOP_C170": _coerencia_cfop("C170"),
    "REGRA_CST_ICMS_CAMPOS_ICMS_C190": _cst_campos_icms,
}
