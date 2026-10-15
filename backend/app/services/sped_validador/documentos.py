"""Regras do DOCUMENTO: chave de acesso, operação/emitente/CFOP, CST × C190,
datas × situação, documento sem efeito e duplicidade.

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

A situação do documento manda nas datas (Guia, Seção 7): o regular (00, 02,
06) é do período do 0000, e o extemporâneo (01, 03, 07) é o que deveria ter
sido escriturado antes — nem a emissão nem a entrada/saída dele caem no
período. A data que decide é a de entrada/saída (DT_E_S) ou de
aquisição/prestação (DT_A_P), e o DT_DOC quando ela vem vazia — a mesma
troca que o Guia manda fazer ao somar os documentos no E110.

Os MODIFICADORES do descritor (`DE_MODIFICADOR`) são o outro lado da
obrigatoriedade reduzida: o motor já deixa de exigir campo no documento
cancelado (`motor._isencoes`); aqui o campo que NÃO pode vir preenchido é
acusado.
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
    """Emissão de terceiros: o emitente da chave é o participante (0150).
    Vale para a NF-e (C100) e para o CT-e (D100), cada um com o seu texto."""
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
        texto = v_.msg("MSG_" + regra.id[len("REGRA_"):],
                       v_.msg("MSG_COMPARA_CNPJ_NFE_PARTICIPANTE_12001_V1"))
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  f"{texto} Chave: {chave[6:20]}; participante {cod_part}: "
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


# ------------------------------------------------------- CST × campos (C170)
#
# Duas famílias no leiaute 017 em diante. As de ENTRADA (VALIDA_CST_*_C170)
# tratam só os CST do ICMS monofásico; as de SAÍDA regular (os "_V3" e "_V2",
# que citam IND_OPER 1 e COD_SIT 00/01) tratam todos. As versões antigas de
# fase 6 (CST_ICMS_CAMPOS_ICMS_C170, CST_ICMS_CAMPOS_ST_COM_COD_SIT_C170) e a
# REGRA_ALIQ_ICMS_C170 ("CST 00, 10, 20 ou 70 pede alíquota") continuam não
# conferidas: aplicadas à entrada, acusariam o item sem direito a crédito —
# que o Guia manda escriturar com alíquota e imposto zerados — e acusariam
# centenas de itens em SPEDs transmitidos (476 só no da MEGA, jun/2026).


def _monofasico_na_entrada(campo_id: str, chave_msg: str, regra_id: str,
                           zero=(), positivo=()):
    """Entrada (IND_OPER 0) com CST monofásico: o campo é zero (`zero`) ou
    maior que zero (`positivo`), conforme os dois últimos dígitos do CST.

    Roda também com o campo VAZIO, que vale zero — "deve ser maior que zero"
    é justamente o campo que não veio. No complementar e no regime especial
    o campo vazio é facultativo, e fica de fora.
    """
    def verificar(v_, no, c, v):
        if no.operacao != "0":
            return
        cst = no.valor("CST_ICMS").strip()
        final = cst[-2:]
        if final not in zero and final not in positivo:
            return
        if not v and no.doc is not None and no.doc.sit in ("06", "07", "08"):
            return
        valor = cp.numero(v) if v else ZERO
        if valor is None:
            return
        if (final in zero and valor != 0) or (final in positivo and valor <= 0):
            sufixo = "_CST_FINAL_2_PEDIDO_DESCONSIDERAR" if final == "02" else ""
            v_.acusar(no.r.linha, no.d.reg, campo_id, regra_id,
                      f"{v_.msg(chave_msg + sufixo, v_.msg(chave_msg))} CST {cst}.",
                      valor=v, gravidade=_gravidade_cst(cst))
    return verificar


def _saida_regular(no) -> bool:
    return (no.operacao == "1" and no.doc is not None
            and no.doc.sit in ("00", "01"))


def _cst_campos_icms_saida(v_, no, regra):
    """MSG_CST_ICMS_CAMPOS_ICMS_COM_COD_SIT_C170_V2, item a item."""
    if not _saida_regular(no):
        return
    cst = no.valor("CST_ICMS").strip()
    final = cst[-2:]
    bc, aliq, icms = (_valor(no, c) for c in ("VL_BC_ICMS", "ALIQ_ICMS", "VL_ICMS"))
    if None in (bc, aliq, icms) or final in ("20", "51", "90") or not final:
        return
    if final in ("30", "40", "41", "50", "60", "61"):
        errado = bool(bc or aliq or icms)
    elif final in ("02", "15"):
        errado = bc != 0 or icms != 0 or aliq <= 0
    elif final == "53":
        errado = bc != 0 or icms != 0
    else:
        errado = bc > 1 and (aliq <= 0 or icms <= 0)
    if errado:
        chave = "MSG_CST_ICMS_CAMPOS_ICMS_COM_COD_SIT_C170_V2"
        sufixo = "_CST_FINAL_2_PEDIDO_DESCONSIDERAR" if final == "02" else ""
        v_.acusar(no.r.linha, no.d.reg, "CST_ICMS", regra.id,
                  f"{v_.msg(chave + sufixo, v_.msg(chave))} CST {cst}, base "
                  f"{no.valor('VL_BC_ICMS') or '0'}, alíquota "
                  f"{no.valor('ALIQ_ICMS') or '0'}, ICMS {no.valor('VL_ICMS') or '0'}.",
                  valor=cst, gravidade=_gravidade_cst(cst))


def _cst_campos_st_saida(v_, no, regra):
    """MSG_CST_ICMS_CAMPOS_ST_V2: o "maior ou igual a zero" o formato já
    garante (número sem sinal); sobra o que tem de ser zero."""
    if not _saida_regular(no):
        return
    cst = no.valor("CST_ICMS").strip()
    final = cst[-2:]
    bc_st, icms_st = _valor(no, "VL_BC_ICMS_ST"), _valor(no, "VL_ICMS_ST")
    if bc_st is None or icms_st is None:
        return
    if final in ("02", "15", "53"):
        errado = bc_st != 0
    elif final == "61":
        errado = bc_st != 0 or icms_st != 0
    else:
        return
    if errado:
        chave = "MSG_CST_ICMS_CAMPOS_ST_V2"
        sufixo = "_CST_FINAL_2_PEDIDO_DESCONSIDERAR" if final == "02" else ""
        v_.acusar(no.r.linha, no.d.reg, "CST_ICMS", regra.id,
                  f"{v_.msg(chave + sufixo, v_.msg(chave))} CST {cst}.",
                  valor=cst, gravidade=_gravidade_cst(cst))


def _cst_ipi_da_operacao(v_, no, regra):
    campo = regra.campo or "CST_IPI"
    cst, cfop = no.valor(campo).strip(), no.valor("CFOP").strip()
    if not cst.isdigit() or not cfop:
        return
    if (cfop[0] in "123" and int(cst) >= 50) or (cfop[0] in "567" and int(cst) < 50):
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  f"{v_.msg('MSG_VALIDA_CST_IPI')} CFOP {cfop}, CST_IPI {cst}.",
                  valor=cst)


def _quantidade_positiva(v_, no, regra):
    """QTD maior que zero, menos no complementar (06/07) — que pode só
    complementar valor ou imposto, sem mercadoria nova."""
    campo = regra.campo or "QTD"
    texto = no.valor(campo).strip()
    if not texto or (no.doc is not None and no.doc.sit in ("06", "07")):
        return
    valor = cp.numero(texto)
    if valor is not None and valor <= 0:
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  v_.msg("MSG_REGRA_MAIOR_ZERO_CONDICAO_COD_SIT"), valor=texto)


# ------------------------------------------------------- transporte (D100)

BPE = "63"
# Modelos de transporte em papel que deixaram de existir em 2019.
TRANSPORTE_EM_PAPEL = frozenset({"07", "09", "10", "11", "26", "27"})


def _bpe(regra_id: str, aceitos: tuple, chave_msg: str):
    """Bilhete de passagem eletrônico (63): saída, emissão própria e tipo
    normal (0), substituto (3) ou de embarque (4)."""
    def verificar(v_, no, c, v):
        if no.valor("COD_MOD").strip() == BPE and v not in aceitos:
            v_.acusar(no.r.linha, no.d.reg, c.id, regra_id, v_.msg(chave_msg),
                      valor=v)
    return verificar


def _referencia_do_bpe(v_, no, regra):
    campo = regra.campo or "CHV_CTE_REF"
    if (no.valor("COD_MOD").strip() == BPE and no.valor("TP_CT_E").strip() == "3"
            and not no.valor(campo).strip()):
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  v_.msg("MSG_OBRIGATORIO_BPE_REF"))


def _origem_do_bpe_na_uf(v_, no, c, v):
    """BP-e: o município de origem é da UF do informante (0000)."""
    if no.valor("COD_MOD").strip() != BPE or not v_.uf or len(v) != 7:
        return
    uf = UF_IBGE.get(v[:2])
    if uf and uf != v_.uf:
        v_.acusar(no.r.linha, no.d.reg, c.id, "REGRA_COD_MUN_ORIG_UF_0000_CAMPO",
                  f"{v_.msg('MSG_REGRA_COD_MUN_ORIG_UF_0000')} Município da UF "
                  f"{uf}; informante da UF {v_.uf}.", valor=v)


def _modelo_em_papel_ate_2018(v_, no, c, v):
    data = cp.data(v)
    modelo = no.valor("COD_MOD").strip()
    if data and modelo in TRANSPORTE_EM_PAPEL and data >= "20190101":
        v_.acusar(no.r.linha, no.d.reg, c.id, "REGRA_DATA_MAXIMA_MODELO_DOC_A13E1V2",
                  f"{v_.msg('MSG_DATA_MAXIMA_MODELO_DOC_A13E1V2')} Modelo {modelo}.",
                  valor=v)


def _municipio_do_transporte_na_entrada(v_, no, regra):
    """CT-e, CT-e OS e BP-e de ENTRADA trazem os municípios de origem e de
    destino (MSG_OBRIGATORIO_COD_MUN_DOCE_V3)."""
    campo = regra.campo
    if (campo and no.valor("IND_OPER").strip() == "0"
            and no.valor("COD_MOD").strip() in ("57", "63", "67")
            and no.sit not in SEM_EFEITO and not no.valor(campo).strip()):
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  v_.msg("MSG_OBRIGATORIO_COD_MUN_DOCE_V3"))


def _referencia_diferente(v_, no, c, v):
    if v == no.valor("CHV_CTE").strip():
        v_.acusar(no.r.linha, no.d.reg, c.id, "REGRA_DIFERENTE_CTE_REF",
                  v_.msg("MSG_REGRA_DIFERENTE_CTE_REF"), valor=v)


def _referencia_so_no_substituto(v_, no, c, v):
    """CHV_CTE_REF só no substituto (tipo 3 ou 6), e nele obrigatória. Sem o
    tipo informado (o de terceiros pode vir sem) não há o que comparar; o
    BP-e substituto já tem regra própria (REGRA_OBRIGATORIO_BPE_REF)."""
    tipo = no.valor("TP_CT_E").strip()
    if not tipo or no.sit in SEM_EFEITO:
        return
    substituto = tipo in ("3", "6")
    if v and not substituto:
        v_.acusar(no.r.linha, no.d.reg, c.id, "REGRA_VALIDA_CHV_CTE_REF",
                  f"{v_.msg('MSG_VALIDA_CHV_CTE_REF')} Tipo do CT-e: {tipo}.",
                  valor=v)
    elif not v and substituto and no.valor("COD_MOD").strip() != BPE:
        v_.acusar(no.r.linha, no.d.reg, c.id, "REGRA_VALIDA_CHV_CTE_REF",
                  f"{v_.msg('MSG_VALIDA_CHV_CTE_REF')} Tipo do CT-e: {tipo}, "
                  "e a chave do substituído não veio.")


def _cte_simplificado_x_fretes(v_, no, regra):
    if (no.valor("COD_MOD").strip() != "57" or no.valor("IND_OPER").strip() != "1"
            or no.valor("TP_CT_E").strip() not in ("5", "6")):
        return
    fretes = [f for f in no.filhos if f.d.reg == "D130"]
    if not fretes:
        return
    soma, servico = ZERO, _valor(no, "VL_SERV")
    for f in fretes:
        v = _valor(f, "VL_FRT")
        if v is None:
            return
        soma += v
    if servico is not None and servico != soma:
        v_.acusar(no.r.linha, no.d.reg, "VL_SERV", regra.id,
                  f"{v_.msg('MSG_VALIDA_VL_SERV_D100')} Informado "
                  f"{no.valor('VL_SERV')}; soma dos D130: {soma}.",
                  valor=no.valor("VL_SERV"))


def _cst_de_servico(v_, no, regra):
    campo = regra.campo or "CST_ICMS"
    cst = no.valor(campo).strip()
    if cst and cst[0] != "0":
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  v_.msg("MSG_CST_ICMS_SERVICO"), valor=cst)


# ------------------------------------------------------- datas × situação

# Tabela 4.1.2 e Guia, Seção 7: regular, extemporâneo e sem efeito.
REGULARES = frozenset({"00", "02", "06"})
EXTEMPORANEOS = frozenset({"01", "03", "07"})
SEM_EFEITO = frozenset({"02", "03", "04", "05"})
REGIME_ESPECIAL = "08"


def _data(no, campo: str | None) -> str | None:
    if not campo or campo not in no.d.indice_campo:
        return None
    return cp.data(no.valor(campo).strip())


def _data_de_referencia(no, regra) -> str | None:
    """DT_E_S/DT_A_P (o campo que a regra cita), ou DT_DOC quando vazio.

    Data preenchida e inválida devolve None — o formato já foi acusado, e
    trocar pela outra data acusaria o documento por um campo que está certo.
    """
    propria = next((c for c in (regra.campo, *regra.outros)
                    if c and c.startswith("DT_") and c != "DT_DOC"), None)
    for campo in (propria, "DT_DOC"):
        if campo and campo in no.d.indice_campo and no.valor(campo).strip():
            return _data(no, campo)
    return None


def _situacao_x_periodo(extemporaneo: bool, chave_msg: str):
    """Regular com data ANTERIOR ao período pede situação extemporânea; e
    extemporâneo com data DENTRO do período pede situação regular.

    Data depois do fim do período não entra aqui: o documento não é
    extemporâneo (é de um período que nem fechou), e a DT_FIN do 0000 já é
    acusada pelas regras de data de cada campo.
    """
    situacoes = EXTEMPORANEOS if extemporaneo else REGULARES

    def verificar(v_, no, regra):
        if no.sit not in situacoes or not v_.dt_ini or not v_.dt_fin:
            return
        data = _data_de_referencia(no, regra)
        if not data:
            return
        dentro = v_.dt_ini <= data <= v_.dt_fin
        if (extemporaneo and dentro) or (not extemporaneo and data < v_.dt_ini):
            v_.acusar(no.r.linha, no.d.reg, "COD_SIT", regra.id,
                      f"{v_.msg(chave_msg)} Situação {no.sit}, data "
                      f"{cp.br(data)}; período do 0000: {cp.br(v_.dt_ini)} a "
                      f"{cp.br(v_.dt_fin)}.", valor=no.sit)
    return verificar


def _emissao_ate_o_fim(v_, no, regra):
    """DT_DOC até a DT_FIN do 0000 — menos no regime especial (08), em que o
    Guia admite emissão depois da entrada/saída (C100, Exceções 5 e 11) e o
    PVA passa a exigir a DT_E_S da saída emitida depois do período."""
    if no.sit == REGIME_ESPECIAL or no.sit in SEM_EFEITO:
        return
    campo = regra.campo or "DT_DOC"
    data = _data(no, campo)
    if data and v_.dt_fin and data > v_.dt_fin:
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  f"{v_.msg('MSG_DATA_MENOR_IGUAL_DT_FIN_0000')} "
                  f"({cp.br(v_.dt_fin)})", valor=no.valor(campo))


def _entrada_saida_depois_da_emissao(v_, no, regra):
    """DT_E_S nunca antes da emissão. No regime especial (08) o Guia admite —
    aquisição de cana, derivados de petróleo — e avisa que o PVA emite
    ADVERTÊNCIA (C100, Exceção 5)."""
    campo = regra.campo or "DT_E_S"
    entrada_saida, emissao = _data(no, campo), _data(no, "DT_DOC")
    if entrada_saida and emissao and entrada_saida < emissao:
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  f"{v_.msg('MSG_DT_E_S_MAIOR_IGUAL_DT_DOC')} Emissão "
                  f"{cp.br(emissao)}; {campo} {cp.br(entrada_saida)}.",
                  valor=no.valor(campo),
                  gravidade=ADVERTENCIA if no.sit == REGIME_ESPECIAL else None)


def _saida_emitida_depois(v_, no, regra):
    campo = regra.campo or "DT_E_S"
    if no.operacao != "1" or no.sit in SEM_EFEITO or no.valor(campo).strip():
        return
    emissao = _data(no, "DT_DOC")
    if emissao and v_.dt_fin and emissao > v_.dt_fin:
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  f"{v_.msg('MSG_OBRIGATORIO_DT_E_S')} Emissão "
                  f"{cp.br(emissao)}; fim do período {cp.br(v_.dt_fin)}.")


def _cfe_desde_julho_2011(v_, no, regra):
    campo = regra.campo or "DT_DOC"
    data = _data(no, campo)
    if data and data < "20110701":
        v_.acusar(no.r.linha, no.d.reg, campo, regra.id,
                  v_.msg("MSG_EXTEMPORANEO_CFE"), valor=no.valor(campo))


# ------------------------------------------------- situação do documento

def _denegada_ou_inutilizada_so_eletronica(v_, no, regra):
    modelo = no.modelo
    if no.sit in ("04", "05") and modelo and modelo not in ("55", "65"):
        v_.acusar(no.r.linha, no.d.reg, "COD_SIT", regra.id,
                  f"{v_.msg('MSG_VALIDA_SITUACAO_PARA_NFE_A4E5V1')} Modelo "
                  f"{modelo}.", valor=no.sit)


def _norma_do_regime_especial(v_, no, regra):
    """COD_SIT 08 pede o C110 com a norma que autoriza o documento, menos na
    NF-e de emissão própria (Guia, C100 campo 06), desde julho/2012."""
    if no.sit != REGIME_ESPECIAL or (v_.dt_fin and v_.dt_fin < "20120701"):
        return
    modelo = no.modelo
    c110 = v_.lt.registros.get("C110")
    if not modelo or (c110 is not None and c110.modelos
                      and modelo not in c110.modelos):
        return
    if no.valor("IND_EMIT").strip() == "0" and modelo == "55":
        return
    if not any(f.d.reg == "C110" for f in no.filhos):
        v_.acusar(no.r.linha, no.d.reg, "", regra.id,
                  v_.msg("MSG_OBRIGATORIO_C110"), valor=no.sit)


# Filhos que justificam o C170 numa NF-e de emissão própria (Guia, C100,
# Exceções 2, 6 e 10): sem um deles, o item não é informado.
FILHOS_DO_ITEM_DA_NFE_PROPRIA = frozenset({"C176", "C177", "C180", "C181"})


def _item_da_nfe_propria(v_, no, regra):
    """C170 em NF-e de emissão própria só com C176, C177, C180 ou C181.

    Só no documento regular (00/01): no complementar e no regime especial o
    Guia manda informar os filhos "quando houver informação a ser prestada".
    """
    doc = no.pai
    if (doc is None or doc.d.reg != "C100" or doc.modelo != "55"
            or doc.valor("IND_EMIT").strip() != "0" or doc.sit not in ("00", "01")):
        return
    if not any(f.d.reg in FILHOS_DO_ITEM_DA_NFE_PROPRIA for f in no.filhos):
        v_.acusar(no.r.linha, no.d.reg, "", regra.id,
                  f"{v_.msg('MSG_NFE_EMITIDA_15001_V1')} NF-e do C100 da "
                  f"linha {doc.r.linha}.")


# ------------------------------------------------------------ duplicidade

def _valor_da_chave(no, campo: str) -> str:
    c = no.d.indice_campo.get(campo)
    texto = no.valor(campo).strip() if c else ""
    if c and c.tipo == "N" and texto:
        n = cp.numero(texto)
        if n is not None:
            return format(n.normalize(), "f")
    return texto


def _repetido(v_, no, regra, campos, gravidade=None) -> None:
    """Acusa o registro cuja combinação de `campos` já apareceu no arquivo."""
    vistos = v_.__dict__.setdefault("_chaves_de_documento", {})
    valores = tuple(_valor_da_chave(no, c) for c in campos)
    chave = (no.d.reg, regra.id, valores)
    primeira = vistos.get(chave)
    if primeira is None:
        vistos[chave] = no.r.linha
        return
    descricao = ", ".join(f"{c}={v or 'vazio'}" for c, v in zip(campos, valores))
    texto = v_.msg("MSG_DUPLICIDADE_NO_REGISTRO", "Duplicidade de ocorrência "
                   "da chave {0}.").replace("{0}", f"({descricao})")
    if regra.id.startswith("REGRA_MULTIPLAS_OCORRENCIAS"):
        texto = v_.msg("MSG_MULTIPLAS_OCORRENCIAS_C100", texto)
    v_.acusar(no.r.linha, no.d.reg, "", regra.id,
              f"{texto} A primeira está na linha {primeira}.",
              gravidade=gravidade)


def _duplicidade_do_documento(propria: bool):
    """C100 e D100: a chave do Guia é a do descritor, sem o COD_PART na
    emissão própria (o participante não identifica a nota que a própria
    empresa emitiu)."""
    def verificar(v_, no, regra):
        if no.valor("IND_EMIT").strip() != ("0" if propria else "1"):
            return
        campos = [c for c in no.d.chave if not (propria and c == "COD_PART")]
        _repetido(v_, no, regra, campos)
    return verificar


def _mesma_nota_repetida(v_, no, regra):
    """Advertência do Guia (C100, Validação do Registro): dois C100 com o
    mesmo emitente, situação, participante, série e número, fora a NF-e e a
    NFC-e — que têm a chave de acesso para se distinguir."""
    if no.modelo in ("55", "65"):
        return
    _repetido(v_, no, regra, ("IND_EMIT", "COD_SIT", "COD_PART", "SER", "NUM_DOC"))


# ------------------------------------------- modificadores de situação

# O que o documento sem efeito ainda informa: situação, operação, modelo, a
# "chave do documento" (texto do PVA; o descritor dá a de cada registro) e a
# chave de acesso. Bate, campo a campo, com o que o Guia lista nas Exceções
# do C100, C800, D400 e D500.
IDENTIFICACAO_SEM_EFEITO = frozenset({
    "REG", "COD_SIT", "IND_OPER", "IND_EMIT", "COD_MOD", "SER", "SUB",
    "NUM_DOC"}) | frozenset(CAMPOS_DE_CHAVE)
# Onde o Guia proíbe, com todas as letras, o filho do documento cancelado.
SEM_FILHO_QUANDO_SEM_EFEITO = frozenset({"C100", "C800"})
DESCRICAO_SITUACAO = {"02": "cancelado", "03": "cancelado extemporâneo",
                      "04": "denegado", "05": "numeração inutilizada"}


def _documento_sem_efeito(situacoes: frozenset):
    def verificar(v_, no, regra_id):
        if no.sit not in situacoes:
            return
        permitidos = IDENTIFICACAO_SEM_EFEITO | set(no.d.chave)
        lista = ", ".join(c.id for c in no.d.campos if c.id in permitidos)
        motivo = (f"Documento {DESCRICAO_SITUACAO.get(no.sit, no.sit)} "
                  f"(COD_SIT {no.sit}): informe só {lista}.")
        nao_preencher = v_.msg("MSG_CAMPO_NAO_DEVE_SER_PREENCHIDO",
                               "O Campo não deve ser preenchido.")
        for c in no.d.campos:
            valor = no.r.campo(c.n)
            if c.id not in permitidos and valor.strip():
                v_.acusar(no.r.linha, no.d.reg, c.id, regra_id,
                          f"{nao_preencher} {motivo}", valor=valor)
        if no.d.reg in SEM_FILHO_QUANDO_SEM_EFEITO:
            for f in no.filhos:
                v_.acusar(f.r.linha, f.d.reg, "", regra_id,
                          f"O {no.d.reg} da linha {no.r.linha} é documento "
                          f"{DESCRICAO_SITUACAO.get(no.sit, no.sit)} (COD_SIT "
                          f"{no.sit}): não se informam registros filhos.")
    return verificar


# Campos que a NFC-e não informa (Guia, C100, Exceção 9).
FORA_DA_NFCE = ("COD_PART", "VL_BC_ICMS_ST", "VL_ICMS_ST", "VL_IPI", "VL_PIS",
                "VL_COFINS", "VL_PIS_ST", "VL_COFINS_ST")


def _nfce_sem_destinatario_nem_st(v_, no, regra_id):
    """Os filhos fora do C190/C195/C185 já caem na existência por modelo."""
    if no.modelo != "65" or no.sit not in ("00", "01"):
        return
    for campo in FORA_DA_NFCE:
        if campo in no.d.indice_campo and no.valor(campo).strip():
            v_.acusar(no.r.linha, no.d.reg, campo, regra_id,
                      f"{v_.msg('MSG_CAMPO_NAO_DEVE_SER_PREENCHIDO')} "
                      f"{v_.msg('MSG_NFE_CONSUMIDOR_A7E2V1')}",
                      valor=no.valor(campo))


# ------------------------------------------------------------------ mapas

DE_MODIFICADOR = {
    "REGRA_NOTA_FISCAL_CANCELADA_14001_V1":
        _documento_sem_efeito(frozenset({"02", "03", "04"})),
    "REGRA_NOTA_FISCAL_INUTILIZADA_A12E1V2": _documento_sem_efeito(frozenset({"05"})),
    "REGRA_NFE_CONSUMIDOR_14001V1": _nfce_sem_destinatario_nem_st,
}

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
    "REGRA_VALIDA_CST_VL_BC_ICMS_C170": _monofasico_na_entrada(
        "VL_BC_ICMS", "MSG_VALIDA_CST_VL_BC_ICMS_C170",
        "REGRA_VALIDA_CST_VL_BC_ICMS_C170", zero=MONOFASICO),
    "REGRA_VALIDA_CST_VL_BC_ICMS_ST_C170": _monofasico_na_entrada(
        "VL_BC_ICMS_ST", "MSG_VALIDA_CST_VL_BC_ICMS_ST_C170",
        "REGRA_VALIDA_CST_VL_BC_ICMS_ST_C170", zero=MONOFASICO),
    "REGRA_VALIDA_CST_ALIQ_ICMS_C170": _monofasico_na_entrada(
        "ALIQ_ICMS", "MSG_VALIDA_CST_ALIQ_ICMS_C170",
        "REGRA_VALIDA_CST_ALIQ_ICMS_C170", zero=("61",), positivo=("02", "15")),
    "REGRA_VALIDA_CST_ALIQ_ST_C170": _monofasico_na_entrada(
        "ALIQ_ST", "MSG_VALIDA_CST_ALIQ_ST_C170",
        "REGRA_VALIDA_CST_ALIQ_ST_C170", zero=("02", "61"), positivo=("15",)),
    "REGRA_BPE_SAIDA": _bpe("REGRA_BPE_SAIDA", ("1",), "MSG_BPE_SAIDA"),
    "REGRA_BPE_EMISSAO_PROPRIA":
        _bpe("REGRA_BPE_EMISSAO_PROPRIA", ("0",), "MSG_BPE_EMISSAO_PROPRIA"),
    "REGRA_TIPO_BPE_V2": _bpe("REGRA_TIPO_BPE_V2", ("0", "3", "4"), "MSG_TIPO_BPE_V2"),
    "REGRA_COD_MUN_ORIG_UF_0000_CAMPO": _origem_do_bpe_na_uf,
    "REGRA_DATA_MAXIMA_MODELO_DOC_A13E1V2": _modelo_em_papel_ate_2018,
    "REGRA_DIFERENTE_CTE_REF": _referencia_diferente,
    "REGRA_VALIDA_CHV_CTE_REF": _referencia_so_no_substituto,
}

# Regras de campo que também olham o campo VAZIO: "deve ser maior que zero"
# (ou "obrigatório nesta situação") é exatamente o campo que não veio.
DE_CAMPO_VAZIO = frozenset({"REGRA_VALIDA_CST_ALIQ_ICMS_C170",
                            "REGRA_VALIDA_CST_ALIQ_ST_C170",
                            "REGRA_VALIDA_CHV_CTE_REF"})

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
    "REGRA_CHAVE_REGIME_ESPECIAL_CTE": _chave_regime_especial,
    "REGRA_COMPARA_CNPJ_CTE_PARTICIPANTE_14003_V5": _cnpj_participante_da_chave,
    "REGRA_OBRIGATORIO_BPE_REF": _referencia_do_bpe,
    "REGRA_OBRIGATORIO_COD_MUN_DOCE_ORIG_V3": _municipio_do_transporte_na_entrada,
    "REGRA_OBRIGATORIO_COD_MUN_DOCE_DEST_V3": _municipio_do_transporte_na_entrada,
    "REGRA_VALIDA_VL_SERV_D100": _cte_simplificado_x_fretes,
    "REGRA_CST_ICMS_SERVICO": _cst_de_servico,
    "REGRA_IND_EMIT": _terceiros_e_entrada,
    "REGRA_IND_EMIT_NFCE": _nfce_emitente,
    "REGRA_CFOP_C100": _cfop_da_operacao("MSG_CFOP_C100"),
    "REGRA_CFOP_D190": _cfop_da_operacao("MSG_CFOP_D190"),
    "REGRA_CFOP_NFCE": _cfop_nfce,
    "REGRA_CFOP_SAIDA": _cfop_de_saida(("5",), "MSG_CFOP_SAIDA"),
    "REGRA_CFOP_SAIDA_AMPLO": _cfop_de_saida(("5", "6", "7"), "MSG_CFOP_SAIDA_AMPLO"),
    "REGRA_COERENCIA_CFOP_C170": _coerencia_cfop("C170"),
    "REGRA_COERENCIA_CFOP_C185": _coerencia_cfop("C185"),
    "REGRA_CST_ICMS_CAMPOS_ICMS_C190": _cst_campos_icms,
    "REGRA_CST_ICMS_CAMPOS_ICMS_C170_V3": _cst_campos_icms_saida,
    "REGRA_CST_ICMS_CAMPOS_ST_COM_COD_SIT_C170_V2": _cst_campos_st_saida,
    "REGRA_VALIDA_CST_IPI": _cst_ipi_da_operacao,
    "REGRA_MAIOR_ZERO_CONDICAO_COD_SIT": _quantidade_positiva,
    # Datas × situação
    "REGRA_DATA_MENOR_IGUAL_DT_FIN_0000_DT_DOC_C100": _emissao_ate_o_fim,
    "REGRA_DT_E_S_MAIOR_IGUAL_DT_DOC": _entrada_saida_depois_da_emissao,
    "REGRA_OBRIGATORIO_DT_E_S": _saida_emitida_depois,
    "REGRA_DOCUMENTO_NORMAL_DT_ES":
        _situacao_x_periodo(False, "MSG_DOCUMENTO_NORMAL"),
    "REGRA_DOCUMENTO_NORMAL_DT_AP":
        _situacao_x_periodo(False, "MSG_DOCUMENTO_NORMAL"),
    "REGRA_DOCUMENTO_NORMAL_DT_DOC_A9E2V3":
        _situacao_x_periodo(False, "MSG_DOCUMENTO_NORMAL_DT_DOC"),
    "REGRA_DOCUMENTO_EXTEMPORANEO_DT_E_S":
        _situacao_x_periodo(True, "MSG_DOCUMENTO_EXTEMPORANEO"),
    "REGRA_DOCUMENTO_EXTEMPORANEO_DT_A_P":
        _situacao_x_periodo(True, "MSG_DOCUMENTO_EXTEMPORANEO"),
    "REGRA_EXTEMPORANEO_CFE": _cfe_desde_julho_2011,
    # Situação do documento
    "REGRA_VALIDA_SITUACAO_PARA_NFE_A7E2V1": _denegada_ou_inutilizada_so_eletronica,
    "REGRA_OBRIGATORIO_C110_14001_V1": _norma_do_regime_especial,
    "REGRA_COMPLEMENTO_NFE_EMITIDA_15001_V1_C176_C177_C180_C181":
        _item_da_nfe_propria,
    # Duplicidade
    "REGRA_DUPLICIDADE_NO_REGISTRO_C100_EMISSAO_PROPRIA":
        _duplicidade_do_documento(propria=True),
    "REGRA_DUPLICIDADE_NO_REGISTRO_C100_EMISSAO_TERCEIROS":
        _duplicidade_do_documento(propria=False),
    "REGRA_DUPLICIDADE_NO_REGISTRO_D100_EMISSAO_PROPRIA_A9E2V1":
        _duplicidade_do_documento(propria=True),
    "REGRA_DUPLICIDADE_NO_REGISTRO_D100_EMISSAO_TERCEIROS_A9E2V1":
        _duplicidade_do_documento(propria=False),
    "REGRA_MULTIPLAS_OCORRENCIAS_C100": _mesma_nota_repetida,
}
