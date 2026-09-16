"""O recálculo em cascata: C170 -> C190 -> C100 -> Bloco 9.

A ordem não é preferência, é dependência — cada nível soma o de baixo, e o
Bloco 9 vem por último porque os passos anteriores podem CRIAR e APAGAR linhas.

As regras do Guia Prático 3.2.2 que obrigam cada passo:

- **C190 é reconstruído, nunca editado no lugar.** A chave do registro é
  (CST_ICMS, CFOP, ALIQ_ICMS), "não podem ser informados dois ou mais registros
  com a mesma combinação", e "a combinação deve existir nos respectivos
  registros de itens do C170". Trocar o CFOP de um item o MOVE de grupo: pode
  nascer um C190 que não existia e sobrar outro sem item nenhum. Editar no
  lugar deixaria o grupo antigo com a soma velha e nenhum item apontando para
  ele — rejeição imediata no PVA.

- **VL_BC_ICMS, VL_ICMS, VL_BC_ICMS_ST e VL_ICMS_ST do C190 são somas do
  C170**, campo a campo, para a mesma combinação (validação explícita dos
  campos 06 a 09). VL_IPI idem (C170 campo 24: "esses valores serão
  totalizados para o registro C190").

- **VL_OPR não é soma de VL_ITEM.** O Guia define como mercadorias + frete +
  seguro + outras despesas acessórias + ICMS_ST + FCP_ST + IPI, menos desconto
  incondicional. Frete, seguro e despesas vivem no C100 e NÃO são rateados por
  item, então a parcela deles é medida no arquivo original e redistribuída —
  ver `_extra_de_opr`.

- **C100 campos 21 e 22**: "a soma dos valores do campo VL_BC_ICMS dos
  registros analíticos (C190) deve" corresponder ao C100.

- **Bloco 9**: o 9990 conta o próprio 9001, o próprio 9990 **e o 9999** — o
  Guia é explícito ("o registro 9999, apesar de não pertencer ao Bloco 9,
  também deve ser contabilizado nesta soma"). Errar por um aqui rejeita o
  arquivo inteiro.

O que este módulo deliberadamente NÃO grava:

- **VL_DOC (C100 campo 12).** É o valor de face da nota; correção de tributo
  não muda o que o fornecedor cobrou. Se a cascata fizer a soma dos VL_OPR
  divergir dele, isso vira AVISO para decisão humana, não gravação silenciosa.
- **E110 e E520 (a apuração).** Recalcular documento é aritmética conferível;
  mexer no Bloco E muda imposto a recolher, e a apuração real tem ajustes
  (E111/E530) e saldo anterior que não saem do documento. O impacto é
  calculado e mostrado; gravar é um segundo aceite, à parte.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from .documento import Documento, Registro
from .patch import (Alteracao, Patch, Recusa, aplicar_documentos,
                    aplicar_fretes, aplicar_itens, decimal_para_texto,
                    texto_para_decimal)

ZERO = Decimal("0")

# C190: número do campo no Guia -> de onde vem a soma no C170.
SOMAS_C190 = {
    6: 13,    # VL_BC_ICMS     <- C170 VL_BC_ICMS
    7: 15,    # VL_ICMS        <- C170 VL_ICMS
    8: 16,    # VL_BC_ICMS_ST  <- C170 VL_BC_ICMS_ST
    9: 18,    # VL_ICMS_ST     <- C170 VL_ICMS_ST
    11: 24,   # VL_IPI         <- C170 VL_IPI
}

# C100: número do campo -> campo correspondente no C190 que o totaliza.
SOMAS_C100 = {
    21: 6,    # VL_BC_ICMS
    22: 7,    # VL_ICMS
    23: 8,    # VL_BC_ICMS_ST
    24: 9,    # VL_ICMS_ST
    25: 11,   # VL_IPI
}

NOMES_C100 = {12: "VL_DOC", 14: "VL_DESC", 16: "VL_MERC", 20: "VL_OUT_DA",
              21: "VL_BC_ICMS", 22: "VL_ICMS", 23: "VL_BC_ICMS_ST",
              24: "VL_ICMS_ST", 25: "VL_IPI"}

# O que compõe o valor do documento, pelo próprio C100: mercadoria + frete +
# seguro + outras despesas + ICMS ST + IPI, menos o desconto. É a segunda conta
# que o PVA confere na nota (a primeira é a soma dos VL_OPR contra o VL_DOC), e
# é ela que decide onde entra o resíduo quando a nota é fechada pelo documento.
COMPONENTES_C100 = (16, 18, 19, 20, 24, 25)
C100_DESCONTO = 14
C100_OUTRAS_DESPESAS = 20
NOMES_C190 = {5: "VL_OPR", 6: "VL_BC_ICMS", 7: "VL_ICMS", 8: "VL_BC_ICMS_ST",
              9: "VL_ICMS_ST", 10: "VL_RED_BC", 11: "VL_IPI"}


@dataclass
class Resultado:
    alteracoes: list[Alteracao] = field(default_factory=list)
    recusas: list[Recusa] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    @property
    def houve_mudanca(self) -> bool:
        return bool(self.alteracoes)


# ------------------------------------------------------------------ auxiliares

# CFOP de entrada: é o que gera crédito. Guia 3.2.2, E110 campo 06 (soma dos
# VL_ICMS dos C190/D190 dos documentos de entrada) e E520 campo 04 (VL_IPI dos
# C190 "quando o CFOP iniciar por 1, 2 ou 3").
INICIAIS_DE_ENTRADA = ("1", "2", "3")


def creditos_de_icms(doc: Documento) -> Decimal:
    """ICMS creditado nas entradas — a soma que o campo 06 do E110 representa.

    Mora aqui, e o relatório a importa, porque os dois PRECISAM devolver o mesmo
    número: a tela mostra "o crédito muda R$ X" e a apuração grava esse mesmo X.
    Duas implementações da mesma soma divergem em algum momento, e o dia em que
    isso acontecer a tela vai prometer um valor e o arquivo entregar outro.
    """
    total = ZERO
    for reg in ("C190", "D190"):
        for analitico in doc.por_reg(reg):
            cfop = analitico.campo(3).strip()
            if cfop and cfop[0] in INICIAIS_DE_ENTRADA:
                total += texto_para_decimal(analitico.campo(7))
    return total


def _aliq_chave(texto: str) -> str:
    """Normaliza a alíquota só para AGRUPAR: "12", "12,00" e "12.00" são o mesmo.

    O texto original é preservado na hora de gravar — normalizar aqui e gravar
    normalizado reescreveria a alíquota de todo C190 do arquivo.
    """
    return f"{texto_para_decimal(texto):.2f}"


def _chave_grupo(reg: Registro, campo_cst: int, campo_cfop: int,
                 campo_aliq: int) -> tuple[str, str, str]:
    return (reg.campo(campo_cst).strip(),
            reg.campo(campo_cfop).strip(),
            _aliq_chave(reg.campo(campo_aliq)))


def _base_do_item(item: Registro) -> Decimal:
    """Parcela do VL_OPR que o próprio item carrega.

    Mercadoria menos desconto, mais ICMS ST e IPI — as parcelas do VL_OPR que
    existem linha a linha. Frete, seguro e outras despesas ficam de fora porque
    o C170 não as tem; elas entram pelo rateio de `_extra_de_opr`.
    """
    return (texto_para_decimal(item.campo(7))     # VL_ITEM
            - texto_para_decimal(item.campo(8))   # VL_DESC
            + texto_para_decimal(item.campo(18))  # VL_ICMS_ST
            + texto_para_decimal(item.campo(24)))  # VL_IPI


def _extra_de_opr(c100: Registro) -> Decimal:
    """Quanto do VL_OPR do documento NÃO vem dos itens, medido no original.

    É o frete + seguro + outras despesas que o emitente distribuiu entre os
    grupos e que o C170 não traz. Precisa ser medido ANTES de aplicar qualquer
    correção — por isso `aplicar` chama esta função primeiro e guarda o número.
    """
    itens = [f for f in c100.filhos if f.reg == "C170"]
    analiticos = [f for f in c100.filhos if f.reg == "C190"]
    if not itens or not analiticos:
        return ZERO
    soma_opr = sum((texto_para_decimal(a.campo(5)) for a in analiticos), ZERO)
    soma_base = sum((_base_do_item(i) for i in itens), ZERO)
    return soma_opr - soma_base


def _ratear(extra: Decimal, bases: list[Decimal]) -> list[Decimal]:
    """Distribui `extra` proporcionalmente às bases, sem perder centavo.

    O resíduo do arredondamento vai para o maior grupo: sem isso a soma dos
    VL_OPR não fecha com o VL_DOC por um centavo, que é exatamente o tipo de
    diferença que faz o PVA reclamar.
    """
    total = sum(bases, ZERO)
    if extra == 0 or total == 0:
        return [ZERO for _ in bases]
    parcelas = [(extra * b / total).quantize(Decimal("0.01")) for b in bases]
    residuo = extra - sum(parcelas, ZERO)
    if residuo != 0:
        maior = bases.index(max(bases))
        parcelas[maior] += residuo
    return parcelas


# ----------------------------------------------------------------- C190

def _novo_c190(chave, aliq_texto: str, modelo: Registro | None,
               c100: Registro, fim: str) -> Registro:
    """Cria um C190 do zero para um grupo que passou a existir.

    VL_RED_BC (campo 10) e COD_OBS (campo 12) NÃO são deriváveis do C170 —
    redução de base não existe no item. O grupo novo nasce com redução zero, no
    mesmo formato dos irmãos, e a divergência (se houver) é reportada como
    aviso em vez de ser inventada aqui.
    """
    cst, cfop, _ = chave
    red_bc = "0,00"
    if modelo is not None and not modelo.campo(10).strip():
        red_bc = ""
    campos = ["C190", cst, cfop, aliq_texto, "0,00", "0,00", "0,00",
              "0,00", "0,00", red_bc, "0,00", ""]
    return Registro(reg="C190", campos=campos, linha=0, crua="", fim=fim,
                    pai=c100, criado=True)


def reconstruir_c190(doc: Documento, c100: Registro,
                     extra_opr: Decimal) -> tuple[list[Alteracao], list[str]]:
    """Regrava os analíticos do documento a partir dos itens."""
    alteracoes: list[Alteracao] = []
    avisos: list[str] = []
    chave_doc = c100.chave_documento()

    itens = doc.filhos_reg(c100, "C170")
    if not itens:
        return alteracoes, avisos
    analiticos = doc.filhos_reg(c100, "C190")

    # Agrupa os itens pela chave do C190.
    grupos: dict[tuple, dict] = {}
    for item in itens:
        chave = _chave_grupo(item, 10, 11, 14)
        g = grupos.setdefault(chave, {"aliq_texto": item.campo(14).strip(),
                                      "base": ZERO,
                                      "somas": {n: ZERO for n in SOMAS_C190}})
        g["base"] += _base_do_item(item)
        for campo_190, campo_170 in SOMAS_C190.items():
            g["somas"][campo_190] += texto_para_decimal(item.campo(campo_170))

    existentes = {_chave_grupo(a, 2, 3, 4): a for a in analiticos}

    # Ordem: os grupos que já existiam ficam onde estavam; os novos entram no
    # fim. Assim o diff do arquivo mostra só o que mudou de verdade.
    ordenadas = ([c for c in existentes if c in grupos]
                 + [c for c in grupos if c not in existentes])

    bases = [grupos[c]["base"] for c in ordenadas]
    rateios = _ratear(extra_opr, bases)

    ultimo = analiticos[-1] if analiticos else itens[-1]
    for chave, rateio in zip(ordenadas, rateios):
        dados = grupos[chave]
        registro = existentes.get(chave)
        if registro is None:
            modelo = analiticos[0] if analiticos else None
            registro = _novo_c190(chave, dados["aliq_texto"], modelo, c100,
                                  doc.fim_padrao)
            doc.inserir_apos(ultimo, registro)
            alteracoes.append(Alteracao(
                documento=chave_doc, registro="C190", linha_origem=None,
                campo="registro", de="", para=f"CST {chave[0]} / CFOP {chave[1]}"
                f" / alíq. {dados['aliq_texto'] or '0,00'}",
                motivo="itens passaram a existir nesta combinação",
                origem="cascata"))
        ultimo = registro

        valores = {5: dados["base"] + rateio}
        valores.update({n: dados["somas"][n] for n in SOMAS_C190})
        for numero, valor in valores.items():
            antes = registro.campo(numero)
            depois = decimal_para_texto(valor, antes)
            if registro.set(numero, depois) and not registro.criado:
                alteracoes.append(Alteracao(
                    documento=chave_doc, registro="C190",
                    linha_origem=registro.linha or None,
                    campo=NOMES_C190.get(numero, f"campo {numero}"),
                    de=antes, para=depois,
                    motivo=f"soma dos itens CST {chave[0]} / CFOP {chave[1]}",
                    origem="cascata"))

    # Grupos que perderam todos os itens saem do arquivo.
    for chave, registro in existentes.items():
        if chave in grupos:
            continue
        if texto_para_decimal(registro.campo(10)) != 0:
            avisos.append(
                f"{chave_doc}: o C190 CST {chave[0]} / CFOP {chave[1]} tinha "
                f"redução de base (VL_RED_BC {registro.campo(10)}) e foi "
                "removido — a redução não é rateável por item e precisa ser "
                "conferida à mão.")
        doc.remover(registro)
        alteracoes.append(Alteracao(
            documento=chave_doc, registro="C190", linha_origem=registro.linha,
            campo="registro", de=f"CST {chave[0]} / CFOP {chave[1]}", para="",
            motivo="nenhum item restou nesta combinação", origem="cascata"))

    return alteracoes, avisos


# ----------------------------------------------------------------- C100

def recalcular_c100(doc: Documento, c100: Registro) -> tuple[list[Alteracao],
                                                             list[str]]:
    """Fecha os totais do documento com a soma dos analíticos."""
    alteracoes: list[Alteracao] = []
    avisos: list[str] = []
    chave_doc = c100.chave_documento()
    analiticos = doc.filhos_reg(c100, "C190")
    if not analiticos:
        return alteracoes, avisos

    for campo_100, campo_190 in SOMAS_C100.items():
        total = sum((texto_para_decimal(a.campo(campo_190)) for a in analiticos),
                    ZERO)
        antes = c100.campo(campo_100)
        depois = decimal_para_texto(total, antes)
        if c100.set(campo_100, depois):
            alteracoes.append(Alteracao(
                documento=chave_doc, registro="C100", linha_origem=c100.linha,
                campo=NOMES_C100[campo_100], de=antes, para=depois,
                motivo="soma dos registros analíticos C190", origem="cascata"))

    # VL_DOC não é gravado: é o valor de face da nota. Divergir dele depois da
    # correção é informação para a pessoa, não algo para o motor "consertar".
    soma_opr = sum((texto_para_decimal(a.campo(5)) for a in analiticos), ZERO)
    vl_doc = texto_para_decimal(c100.campo(12))
    if vl_doc and abs(soma_opr - vl_doc) >= Decimal("0.01"):
        avisos.append(
            f"{chave_doc}: a soma dos VL_OPR dos C190 ficou "
            f"{decimal_para_texto(soma_opr)} contra VL_DOC {c100.campo(12)}. "
            "O valor de face da nota não foi alterado — confira se a correção "
            "deveria migrar valor de imposto para o valor da mercadoria.")

    return alteracoes, avisos


# --------------------------------------------------------------- Bloco 9

# Situações de CST_ICMS (dois últimos dígitos) que JÁ dizem "sem crédito".
# Nessas a glosa não mexe: o arquivo já está coerente.
TRIBUTACAO_SEM_CREDITO = {"30", "40", "41", "50", "51", "60", "90"}
# Para onde vai o CST quando o crédito é glosado e a operação foi tributada:
# 90 = "Outras". É o código que o próprio arquivo do escritório usa no CT-e sem
# crédito, e o único neutro o bastante para não afirmar isenção ou imunidade
# que ninguém apurou.
TRIBUTACAO_GLOSADA = "90"

# CST_IPI (Guia 3.2.2, C170 campo 20): 00 é "entrada com recuperação de
# crédito"; 49 é "outras entradas". Os demais de entrada (01 a 05) já dizem
# alíquota zero, isenta, não tributada, imune ou suspensa — e ficam como estão.
CST_IPI_SEM_CREDITO = {"01", "02", "03", "04", "05", "49"}
CST_IPI_GLOSADO = "49"

# Para onde vão os CSTs quando a correção faz o caminho INVERSO — o item passa
# a ter crédito que não tinha. Decisão do Sandro em 09/09/2026: o motor ajusta
# sozinho, porque um item com crédito e CST 060 ("ICMS cobrado anteriormente
# por ST") é contradição dentro do próprio arquivo, e resolver isso a mão, item
# a item, é microgerência que o PVA cobra e a tela não deveria repassar.
#
# 00 = "tributada integralmente" é o único destino honesto: 20 afirmaria uma
# redução de base que ninguém apurou, e 70/90 voltariam a dizer que não há
# imposto a apropriar. Para o IPI, 00 é "entrada com recuperação de crédito" —
# exatamente o que a correção acabou de afirmar ao gravar o valor.
TRIBUTACAO_COM_CREDITO = "00"
CST_IPI_COM_CREDITO = "00"

# Campos do C170 fotografados antes da correção, para a glosa saber o que mudou.
CAMPOS_FOTOGRAFADOS = {
    "VL_ITEM": 7, "CST_ICMS": 10, "VL_BC_ICMS": 13, "ALIQ_ICMS": 14,
    "VL_ICMS": 15, "VL_BC_ICMS_ST": 16, "VL_ICMS_ST": 18, "CST_IPI": 20,
    "VL_BC_IPI": 22, "ALIQ_IPI": 23, "VL_IPI": 24,
}


def fotografar_itens(doc: Documento) -> dict:
    """Guarda o estado dos C170 ANTES da correção.

    A glosa é detectada por comparação — "tinha crédito, ficou zero" —, e
    depois de gravar não há mais como saber o que havia. Fotografar o arquivo
    inteiro custa alguns milhares de strings e evita passar estado escondido
    entre as funções.
    """
    return {id(item): {nome: item.campo(numero)
                       for nome, numero in CAMPOS_FOTOGRAFADOS.items()}
            for item in doc.por_reg("C170")}


def _glosar_cst_icms(cst: str) -> str:
    """Mantém a ORIGEM e troca só a tributação. "000" -> "090", "100" -> "190".

    O primeiro dígito diz se a mercadoria é nacional ou importada — não tem
    nada a ver com o direito ao crédito, e mexer nele mudaria uma informação
    que ninguém auditou.
    """
    limpo = (cst or "").strip()
    if len(limpo) < 2:
        return limpo
    origem = limpo[:-2] or "0"
    return f"{origem}{TRIBUTACAO_GLOSADA}"


def aplicar_glosas(doc: Documento, c100: Registro,
                   antes: dict) -> tuple[list[Alteracao], list[str]]:
    """Completa a glosa: zera o resto do imposto, ajusta o CST e migra o valor.

    Duas regras do Guia, e elas NÃO são a mesma:

    1. **Zerar o imposto é zerar os três campos.** "Para documentos de entrada,
       os campos de valor de imposto, base de cálculo e alíquota só devem ser
       informados se o adquirente tiver direito à apropriação do crédito"
       (C170). Deixar a base preenchida com imposto zero é meia glosa.

    2. **ICMS ST e IPI glosados MIGRAM para a mercadoria; o ICMS próprio não.**
       O Guia responde na seção de perguntas do C100: sem direito ao crédito,
       "os valores do ICMS ST e/ou IPI destacados devem ser adicionados ao
       valor das mercadorias [...] VL_MERC [...] VL_ITEM [...] uma vez que
       compõem o custo". O ICMS próprio fica fora porque é imposto POR DENTRO —
       já está no preço da mercadoria, e somá-lo de novo dobraria o custo.

    Efeito colateral bom da migração: `_base_do_item` soma VL_ITEM + ICMS ST +
    IPI, então tirar de um e pôr no outro mantém a base — VL_OPR e VL_DOC
    continuam fechando sozinhos, que é justamente por que o Guia manda fazer
    assim.

    O CST acompanha para o arquivo não dizer "tributada integralmente" num item
    que ficou sem imposto nenhum. Decisão do Sandro em 09/09/2026: automático,
    para não virar mais uma caixa na tela.
    """
    alteracoes: list[Alteracao] = []
    avisos: list[str] = []
    chave_doc = c100.chave_documento()
    migrado = ZERO

    def registrar(item, numero, nome, valor):
        anterior = item.campo(numero)
        if item.set(numero, valor):
            alteracoes.append(Alteracao(
                documento=chave_doc, registro="C170", linha_origem=item.linha,
                num_item=item.campo(2), campo=nome, de=anterior, para=valor,
                motivo="glosa do crédito: o Guia manda não informar imposto, "
                       "base e alíquota quando não há direito à apropriação",
                origem="cascata"))

    for item in doc.filhos_reg(c100, "C170"):
        foto = antes.get(id(item))
        if not foto:
            continue

        # --- ICMS próprio: zera o conjunto e ajusta o CST, sem migrar valor.
        if (texto_para_decimal(foto["VL_ICMS"]) > 0
                and texto_para_decimal(item.campo(15)) == 0):
            registrar(item, 13, "VL_BC_ICMS",
                      decimal_para_texto(ZERO, item.campo(13)))
            registrar(item, 14, "ALIQ_ICMS",
                      decimal_para_texto(ZERO, item.campo(14)))
            cst = item.campo(10).strip()
            if cst[-2:] not in TRIBUTACAO_SEM_CREDITO:
                registrar(item, 10, "CST_ICMS", _glosar_cst_icms(cst))

        # --- ICMS ST e IPI: zeram, ajustam o CST e MIGRAM para a mercadoria.
        for nome, campo_valor, campos_zerar, campo_cst in (
                ("ICMS ST", 18, (16, 17), None),
                ("IPI", 24, (22, 23), 20)):
            valor_antes = texto_para_decimal(
                foto["VL_ICMS_ST" if nome == "ICMS ST" else "VL_IPI"])
            if valor_antes <= 0 or texto_para_decimal(item.campo(campo_valor)) != 0:
                continue

            for numero in campos_zerar:
                registrar(item, numero, f"campo {numero}",
                          decimal_para_texto(ZERO, item.campo(numero)))

            if campo_cst is not None:
                cst_ipi = item.campo(campo_cst).strip()
                if cst_ipi and cst_ipi not in CST_IPI_SEM_CREDITO:
                    registrar(item, campo_cst, "CST_IPI", CST_IPI_GLOSADO)

            novo_item = texto_para_decimal(item.campo(7)) + valor_antes
            anterior = item.campo(7)
            depois = decimal_para_texto(novo_item, anterior)
            if item.set(7, depois):
                migrado += valor_antes
                alteracoes.append(Alteracao(
                    documento=chave_doc, registro="C170",
                    linha_origem=item.linha, num_item=item.campo(2),
                    campo="VL_ITEM", de=anterior, para=depois,
                    motivo=f"{nome} sem direito a crédito entra no custo da "
                           "mercadoria (Guia, perguntas do C100)",
                    origem="cascata"))

    if migrado:
        anterior = c100.campo(16)
        depois = decimal_para_texto(
            texto_para_decimal(anterior) + migrado, anterior)
        if c100.set(16, depois):
            alteracoes.append(Alteracao(
                documento=chave_doc, registro="C100", linha_origem=c100.linha,
                campo="VL_MERC", de=anterior, para=depois,
                motivo="imposto glosado incorporado ao valor das mercadorias",
                origem="cascata"))

    return alteracoes, avisos


def ajustar_cst_do_credito(doc: Documento, c100: Registro,
                           antes: dict) -> tuple[list[Alteracao], list[str]]:
    """O inverso da glosa: item que GANHOU crédito não pode manter CST de isento.

    Até 09/09/2026 este caso só levantava aviso, e a decisão do que fazer ficava
    com quem estava na tela. Não é um bom lugar para essa decisão: o arquivo
    sairia dizendo, na mesma linha, que há R$ 36,00 de ICMS a apropriar e que o
    imposto foi cobrado anteriormente por substituição (CST 060). Quem recebe
    isso é o PVA, e o erro só aparece na entrega.

    A ORIGEM da mercadoria (primeiro dígito) não é tocada — nacional não vira
    importada porque alguém corrigiu um crédito.
    """
    alteracoes: list[Alteracao] = []
    avisos: list[str] = []
    chave_doc = c100.chave_documento()

    for item in doc.filhos_reg(c100, "C170"):
        foto = antes.get(id(item))
        if not foto:
            continue

        # --- ICMS: ganhou crédito e o CST dizia que não havia imposto.
        if (texto_para_decimal(foto["VL_ICMS"]) == 0
                and texto_para_decimal(item.campo(15)) > 0):
            cst = item.campo(10).strip()
            if cst[-2:] in TRIBUTACAO_SEM_CREDITO:
                origem = cst[:-2] or "0"
                anterior = item.campo(10)
                novo = f"{origem}{TRIBUTACAO_COM_CREDITO}"
                if item.set(10, novo):
                    alteracoes.append(Alteracao(
                        documento=chave_doc, registro="C170",
                        linha_origem=item.linha, num_item=item.campo(2),
                        campo="CST_ICMS", de=anterior, para=novo,
                        motivo="o item passou a ter crédito de ICMS; o CST "
                               "anterior dizia não haver imposto a apropriar",
                        origem="cascata"))

        # --- IPI: mesma coisa, com os códigos do campo 20.
        if (texto_para_decimal(foto["VL_IPI"]) == 0
                and texto_para_decimal(item.campo(24)) > 0):
            cst_ipi = item.campo(20).strip()
            if cst_ipi in CST_IPI_SEM_CREDITO:
                anterior = item.campo(20)
                if item.set(20, CST_IPI_COM_CREDITO):
                    alteracoes.append(Alteracao(
                        documento=chave_doc, registro="C170",
                        linha_origem=item.linha, num_item=item.campo(2),
                        campo="CST_IPI", de=anterior, para=CST_IPI_COM_CREDITO,
                        motivo="o item passou a ter crédito de IPI; 00 é "
                               "'entrada com recuperação de crédito'",
                        origem="cascata"))

    return alteracoes, avisos


def espelhar_d100(doc: Documento, d100: Registro) -> tuple[list[Alteracao],
                                                           list[str]]:
    """Fecha o documento de transporte com o seu analítico.

    Aqui a regra do Guia é o INVERSO da do C190, e é fácil aplicar a errada: o
    C100 é a SOMA dos seus C190, mas o D190 tem de ser IGUAL ao pai —

        "o valor informado deve ser igual ao valor do campo VL_BC_ICMS do
         registro D100, pai deste registro D190"   (D190 campos 06 e 07)

    Faz sentido: o D100 não tem registro de item para somar. Por isso a
    correção age no D190 e o D100 recebe o mesmo valor de volta.
    """
    alteracoes: list[Alteracao] = []
    avisos: list[str] = []
    analiticos = doc.filhos_reg(d100, "D190")
    if not analiticos:
        return alteracoes, avisos

    chave_doc = d100.chave_documento()
    if len(analiticos) > 1:
        distintos = {(a.campo(6), a.campo(7)) for a in analiticos}
        if len(distintos) > 1:
            avisos.append(
                f"{chave_doc}: o documento tem {len(analiticos)} registros D190 "
                "com valores diferentes, e o Guia manda cada um ser igual ao "
                "D100. O espelhamento usou o primeiro — confira à mão.")

    # O valor de face do CT-e também vive no analítico (D190 campo 05, VL_OPR).
    # Corrigir o VL_DOC do D100 e deixar o VL_OPR velho é a mesma incoerência
    # que o Guia proíbe nos campos de imposto — e o único caso em que dá para
    # espelhar sem inventar é o do analítico único: com dois ou mais, não há
    # como saber quanto do valor novo é de cada um.
    vl_doc = texto_para_decimal(d100.campo(15))
    if vl_doc:
        if len(analiticos) == 1:
            alvo = analiticos[0]
            antes = alvo.campo(5)
            depois = decimal_para_texto(vl_doc, antes)
            if alvo.set(5, depois):
                alteracoes.append(Alteracao(
                    documento=chave_doc, registro="D190",
                    linha_origem=alvo.linha, campo="VL_OPR", de=antes,
                    para=depois,
                    motivo="valor da operação igual ao valor do documento",
                    origem="cascata"))
        else:
            soma = sum((texto_para_decimal(a.campo(5)) for a in analiticos), ZERO)
            if abs(soma - vl_doc) >= Decimal("0.01"):
                avisos.append(
                    f"{chave_doc}: a soma dos VL_OPR dos {len(analiticos)} "
                    f"registros D190 ficou {decimal_para_texto(soma)} contra "
                    f"VL_DOC {d100.campo(15)}. Com mais de um analítico não dá "
                    "para saber quanto do valor é de cada um — ajuste à mão.")

    fonte = analiticos[0]
    for campo_100, campo_190 in ((19, 6), (20, 7)):
        antes = d100.campo(campo_100)
        depois = decimal_para_texto(texto_para_decimal(fonte.campo(campo_190)),
                                    antes)
        if d100.set(campo_100, depois):
            alteracoes.append(Alteracao(
                documento=chave_doc, registro="D100", linha_origem=d100.linha,
                campo="VL_BC_ICMS" if campo_100 == 19 else "VL_ICMS",
                de=antes, para=depois,
                motivo="o D190 tem de ser igual ao D100 pai", origem="cascata"))

    return alteracoes, avisos


def reconstruir_totais_de_blocos(doc: Documento) -> list[Alteracao]:
    """Refaz o X990 de cada bloco (0990, C990, D990, E990...).

    Criar ou apagar um C190 muda a quantidade de linhas do bloco C, e o C990
    ficaria mentindo — o PVA rejeita o arquivo por isso, mesmo com todos os
    valores fiscais certos.

    A contagem é POSICIONAL, da abertura ao encerramento, inclusive. E o bloco
    0 tem a exceção que o Guia escreve com todas as letras: "para este cálculo,
    o registro 0000, mesmo não pertencendo ao bloco 0, deve ser somado" — a
    mesma armadilha do 9990 com o 9999, na outra ponta do arquivo.
    """
    alteracoes: list[Alteracao] = []
    posicao = {id(r): i for i, r in enumerate(doc.registros)}

    for fechamento in doc.registros:
        if len(fechamento.reg) != 4 or not fechamento.reg.endswith("990"):
            continue
        bloco = fechamento.reg[0]
        if bloco == "9":
            continue  # o 9990 é do Bloco 9, refeito em reconstruir_bloco_9

        abertura_reg = "0000" if bloco == "0" else f"{bloco}001"
        aberturas = [r for r in doc.registros if r.reg == abertura_reg]
        if not aberturas:
            continue

        inicio = posicao[id(aberturas[0])]
        fim = posicao[id(fechamento)]
        quantidade = fim - inicio + 1

        antes = fechamento.campo(2)
        if fechamento.set(2, str(quantidade)):
            alteracoes.append(Alteracao(
                documento="(arquivo)", registro=fechamento.reg,
                linha_origem=fechamento.linha, campo=f"QTD_LIN_{bloco}",
                de=antes, para=str(quantidade),
                motivo="linhas do bloco depois da correção", origem="bloco 9"))

    return alteracoes


def reconstruir_bloco_9(doc: Documento) -> list[Alteracao]:
    """Regrava 9900, 9990 e 9999. Sempre por último.

    Ovo-e-galinha: cada 9900 é uma linha do arquivo, e as linhas 9900 se
    contam. Por isso a lista de tipos é fechada antes de escrever a contagem —
    e existe uma linha 9900 contando as próprias linhas 9900.
    """
    alteracoes: list[Alteracao] = []
    abertura = doc.por_reg("9001")
    if not abertura:
        return alteracoes
    abertura = abertura[0]

    fixos = {"9900", "9990", "9999"}
    contagem = Counter(r.reg for r in doc.registros if r.reg not in fixos)

    # O arquivo final terá uma linha 9900 para cada tipo de registro, mais as
    # três dos próprios registros de fechamento.
    total_9900 = len(contagem) + 3
    contagem["9900"] = total_9900
    contagem["9990"] = 1
    contagem["9999"] = 1

    antigos = doc.por_reg("9900")
    anterior = {r.campo(2): r.campo(3) for r in antigos}
    for velho in antigos:
        doc.remover(velho)

    # A ORDEM das linhas 9900 é preservada. O Guia não a impõe, e cada gerador
    # usa a sua — os arquivos reais trazem os registros na ordem em que
    # aparecem (os 9xxx no fim), não em ordem alfabética. Reordenar reescreve
    # dezenas de linhas que ninguém pediu para mexer, e some com a garantia de
    # que o arquivo corrigido difere do original só no que foi aceito.
    # Tipo que passou a existir entra no fim; arquivo sem 9900 nenhum usa a
    # ordem alfabética, que ao menos é determinística.
    ordenados = [t for t in anterior if t in contagem]
    ordenados += [t for t in sorted(contagem) if t not in anterior]

    ponteiro = abertura
    for reg_blc in ordenados:
        novo = Registro(reg="9900", campos=["9900", reg_blc, str(contagem[reg_blc])],
                        linha=0, crua="", fim=doc.fim_padrao, pai=abertura,
                        criado=True)
        doc.inserir_apos(ponteiro, novo)
        ponteiro = novo
        antes = anterior.get(reg_blc, "")
        if antes != str(contagem[reg_blc]):
            alteracoes.append(Alteracao(
                documento="(arquivo)", registro="9900", linha_origem=None,
                campo=f"QTD_REG_BLC {reg_blc}", de=antes,
                para=str(contagem[reg_blc]),
                motivo="contagem de linhas do arquivo", origem="bloco 9"))

    # QTD_LIN_9: abertura + as 9900 + o próprio 9990 + o 9999, que o Guia manda
    # contar aqui apesar de não pertencer ao bloco.
    qtd_bloco_9 = 1 + total_9900 + 1 + 1
    for registro, numero, valor, nome in (
            (doc.por_reg("9990"), 2, qtd_bloco_9, "QTD_LIN_9"),
            (doc.por_reg("9999"), 2, len(doc.registros), "QTD_LIN")):
        if not registro:
            continue
        alvo = registro[0]
        antes = alvo.campo(numero)
        if alvo.set(numero, str(valor)):
            alteracoes.append(Alteracao(
                documento="(arquivo)", registro=alvo.reg, linha_origem=alvo.linha,
                campo=nome, de=antes, para=str(valor),
                motivo="total de linhas depois da correção", origem="bloco 9"))

    return alteracoes


def soma_base_dos_itens(doc: Documento, c100: Registro) -> Decimal:
    """A parcela do valor do documento que os ITENS carregam.

    Mercadoria menos desconto, mais ICMS ST e IPI — o que existe linha a linha.
    Frete, seguro e despesas acessórias vivem no C100 e entram pelo rateio.
    """
    return sum((_base_do_item(i) for i in doc.filhos_reg(c100, "C170")), ZERO)


def fechar_componentes(doc: Documento, c100: Registro) -> tuple[list[Alteracao],
                                                                list[str]]:
    """Faz os componentes do C100 somarem o VL_DOC, declarando o resíduo.

    Chamada só para o documento cujo VALOR foi corrigido, e depois de o
    `recalcular_c100` ter refeito ICMS ST e IPI a partir dos analíticos — antes
    disso os componentes estariam medindo um estado intermediário.

    **O resíduo vai para VL_OUT_DA (outras despesas acessórias), e essa é a
    escolha que precisa ficar registrada.** A alternativa seria distribuir a
    diferença entre os itens, e ela foi recusada: o total fecharia e cada linha
    passaria a mentir sobre o valor da mercadoria. "Outras despesas acessórias"
    é o campo que o próprio Guia reserva para o que não é mercadoria, frete,
    seguro, ST nem IPI — se a diferença for de OUTRA natureza (item escriturado
    a menos, ST não lançado), o conserto é no item, e o relatório mostra o
    resíduo justamente para essa conferência ser possível.
    """
    alteracoes: list[Alteracao] = []
    avisos: list[str] = []
    chave_doc = c100.chave_documento()

    vl_doc = texto_para_decimal(c100.campo(12))
    if not vl_doc:
        return alteracoes, avisos

    componentes = sum((texto_para_decimal(c100.campo(n))
                       for n in COMPONENTES_C100), ZERO)
    componentes -= texto_para_decimal(c100.campo(C100_DESCONTO))
    residuo = vl_doc - componentes
    if abs(residuo) < Decimal("0.01"):
        return alteracoes, avisos

    if residuo < 0:
        # Os componentes declaram MAIS do que o total. Tirar de "outras
        # despesas" só resolveria se houvesse o que tirar; mexer em mercadoria
        # ou imposto para fechar seria inventar.
        avisos.append(
            f"{chave_doc}: os componentes do C100 somam "
            f"{decimal_para_texto(componentes)}, acima do VL_DOC "
            f"{c100.campo(12)}. O valor não foi fechado — confira mercadoria, "
            "frete e desconto do documento.")
        return alteracoes, avisos

    anterior = c100.campo(C100_OUTRAS_DESPESAS)
    depois = decimal_para_texto(texto_para_decimal(anterior) + residuo, anterior)
    if c100.set(C100_OUTRAS_DESPESAS, depois):
        alteracoes.append(Alteracao(
            documento=chave_doc, registro="C100", linha_origem=c100.linha,
            campo="VL_OUT_DA", de=anterior, para=depois,
            motivo=f"resíduo de {decimal_para_texto(residuo)} declarado como "
                   "outras despesas acessórias, para os componentes do C100 "
                   "somarem o valor do documento",
            origem="cascata"))
    return alteracoes, avisos


# ------------------------------------------------------------- apuração (E110)

# E110 (Guia 3.2.2). O saldo do período é a conta dos campos de débito menos os
# de crédito; os números abaixo são as posições que entram em cada lado.
E110_DEBITOS = (2, 3, 4, 5)     # totais, ajustes e estornos de crédito
E110_CREDITOS = (6, 7, 8, 9, 10)  # totais, ajustes, estornos de débito, saldo anterior
E110_TOT_CREDITOS = 6
E110_AJ_CREDITOS = 7
E110_SLD_APURADO = 11
E110_TOT_DED = 12
E110_ICMS_RECOLHER = 13
E110_SLD_CREDOR_TRANSPORTAR = 14

NOMES_E110 = {6: "VL_TOT_CREDITOS", 7: "VL_AJ_CREDITOS", 11: "VL_SLD_APURADO",
              13: "VL_ICMS_RECOLHER", 14: "VL_SLD_CREDOR_TRANSPORTAR"}

MOTIVOS_E110 = {
    E110_TOT_CREDITOS: "crédito de ICMS das entradas, com a diferença apurada "
                       "na correção",
    E110_AJ_CREDITOS: "estorno de débito transferido para a sub-apuração do "
                      "ICMS (Guia, registro C197)",
}


def _saldo_e110(e110: Registro, credito_extra: Decimal = ZERO) -> Decimal:
    """(débitos) - (créditos). Positivo é imposto a recolher; negativo, saldo credor."""
    debitos = sum((texto_para_decimal(e110.campo(n)) for n in E110_DEBITOS), ZERO)
    creditos = sum((texto_para_decimal(e110.campo(n)) for n in E110_CREDITOS), ZERO)
    return debitos - creditos - credito_extra


def atualizar_e110(doc: Documento, delta: Decimal,
                   campo_credito: int = E110_TOT_CREDITOS
                   ) -> tuple[list[Alteracao], list[str]]:
    """Leva a diferença de crédito para a apuração do ICMS e refaz o saldo.

    Esta função é a exceção conquistada, não o comportamento padrão: até
    09/09/2026 o motor não encostava no Bloco E de propósito, porque recalcular
    documento é aritmética conferível e mexer na apuração muda imposto a
    recolher. O Sandro pediu em 10/09/2026, e ela só roda com
    `Patch.atualizar_apuracao` ligado — uma caixa própria na tela.

    Três decisões que a mantêm honesta:

    1. **Aplica a DIFERENÇA, não substitui o total.** O VL_TOT_CREDITOS do
       arquivo foi produzido pelo sistema do cliente e pode conter o que o
       nosso somatório não enxerga. Somar o delta preserva isso e deixa a
       alteração legível no relatório: "de X para X + Δ".

    2. **Confere a fórmula do Guia no arquivo ANTES de reescrever.** Se o saldo
       declarado não puder ser reproduzido a partir dos próprios campos do
       E110, o arquivo segue uma conta que este código não conhece — regime
       especial, ajuste fora do leiaute, arredondamento diferente. Aí ele NÃO
       toca em nada e avisa. Reescrever ali seria trocar o número certo de
       alguém pelo número errado do sistema.

    3. **Um E110 só.** Mais de um significa mais de uma apuração no arquivo, e
       decidir em qual delas o crédito entra não é conta: é informação que o
       documento não tem.

    `campo_credito` escolhe ONDE o delta entra, e os dois casos NÃO são
    intercambiáveis: 06 (VL_TOT_CREDITOS) é o crédito das entradas, que é o que
    a correção de item muda; 07 (VL_AJ_CREDITOS) é "ajustes a crédito
    decorrentes do documento fiscal", que é onde o Guia manda lançar o estorno
    de débito transferido para a sub-apuração (ver
    `services/sped_debito_subapuracao.py`). Somar um no outro deixaria o
    arquivo com o saldo certo e os componentes errados — e é justamente por
    componente que o PVA confere.

    E111/E116 (ajustes e obrigações a recolher) não são tocados — quem os
    escriturou tinha um motivo que não está no arquivo.
    """
    alteracoes: list[Alteracao] = []
    avisos: list[str] = []
    if delta == 0:
        return alteracoes, avisos

    registros = doc.por_reg("E110")
    if not registros:
        avisos.append(
            "O arquivo não tem registro E110: não há apuração de ICMS para "
            f"atualizar, e a diferença de {decimal_para_texto(delta)} em "
            "crédito ficou só nos documentos.")
        return alteracoes, avisos
    if len(registros) > 1:
        avisos.append(
            f"O arquivo tem {len(registros)} registros E110. A apuração NÃO foi "
            "atualizada: decidir em qual delas o crédito entra não é conta que "
            "o documento responda.")
        return alteracoes, avisos

    e110 = registros[0]

    # A conferência da fórmula, feita sobre os valores ORIGINAIS.
    saldo_declarado = (texto_para_decimal(e110.campo(E110_SLD_APURADO))
                       - texto_para_decimal(e110.campo(E110_SLD_CREDOR_TRANSPORTAR)))
    if abs(_saldo_e110(e110) - saldo_declarado) >= Decimal("0.01"):
        avisos.append(
            "A apuração do arquivo não fecha pela fórmula do Guia "
            f"(campos 02+03+04+05 - 06+07+08+09+10 = "
            f"{decimal_para_texto(_saldo_e110(e110))}, mas o E110 declara "
            f"{decimal_para_texto(saldo_declarado)}). O E110 não foi alterado — "
            "ajuste a apuração no PVA depois de importar o arquivo.")
        return alteracoes, avisos

    def gravar(numero: int, valor: Decimal, motivo: str) -> None:
        antes = e110.campo(numero)
        depois = decimal_para_texto(valor, antes or "0")
        if e110.set(numero, depois):
            alteracoes.append(Alteracao(
                documento="(apuração)", registro="E110",
                linha_origem=e110.linha, campo=NOMES_E110[numero], de=antes,
                para=depois, motivo=motivo, origem="apuração"))

    creditos = texto_para_decimal(e110.campo(campo_credito)) + delta
    if creditos < 0:
        avisos.append(
            f"A correção tiraria {decimal_para_texto(-delta)} de "
            f"{NOMES_E110[campo_credito]}, mais do que os "
            f"{e110.campo(campo_credito)} declarados no E110. A apuração não "
            "foi alterada.")
        return alteracoes, avisos

    gravar(campo_credito, creditos,
           f"{MOTIVOS_E110[campo_credito]} ({decimal_para_texto(delta)})")

    saldo = _saldo_e110(e110)
    if saldo > 0:
        gravar(E110_SLD_APURADO, saldo, "saldo devedor refeito com o crédito novo")
        gravar(E110_SLD_CREDOR_TRANSPORTAR, ZERO,
               "apuração fechou devedora: não há saldo credor a transportar")
        deducoes = texto_para_decimal(e110.campo(E110_TOT_DED))
        gravar(E110_ICMS_RECOLHER, max(ZERO, saldo - deducoes),
               "ICMS a recolher = saldo apurado menos deduções (campo 11 - 12)")
    else:
        gravar(E110_SLD_APURADO, ZERO,
               "apuração fechou credora: não há saldo devedor")
        gravar(E110_ICMS_RECOLHER, ZERO, "sem saldo devedor, nada a recolher")
        gravar(E110_SLD_CREDOR_TRANSPORTAR, -saldo,
               "saldo credor a transportar para o período seguinte")

    return alteracoes, avisos


# ------------------------------------------------------------------ fachada

def aplicar(doc: Documento, patch: Patch) -> Resultado:
    """Aplica as correções aceitas e refaz tudo que depende delas.

    Ordem: mede o VL_OPR extra de cada documento (antes de qualquer alteração),
    grava os itens, reconstrói os analíticos, fecha os totais do documento e só
    então refaz o Bloco 9.
    """
    resultado = Resultado()

    # Precisa ser medido ANTES: depois da correção não dá mais para separar o
    # que era frete/seguro do que era imposto.
    extras = {id(c): _extra_de_opr(c) for c in doc.por_reg("C100")}
    # E a foto dos itens, porque a glosa é detectada por comparação.
    antes_itens = fotografar_itens(doc)
    # O crédito ANTES de qualquer correção: é a base do delta que vai (ou não)
    # para a apuração no fim.
    creditos_antes = creditos_de_icms(doc)

    alteracoes, recusas, tocados = aplicar_itens(doc, patch)
    resultado.alteracoes.extend(alteracoes)
    resultado.recusas.extend(recusas)

    alteracoes, recusas, fretes = aplicar_fretes(doc, patch)
    resultado.alteracoes.extend(alteracoes)
    resultado.recusas.extend(recusas)

    # O valor de face do documento é o terceiro alvo. Vem depois dos outros dois
    # porque o aviso de "a soma dos VL_OPR não bate com o VL_DOC", lá no
    # recalcular_c100, tem de enxergar o valor NOVO.
    alteracoes, recusas, docs_c, docs_d = aplicar_documentos(doc, patch)
    resultado.alteracoes.extend(alteracoes)
    resultado.recusas.extend(recusas)
    # Documento cujo VALOR foi corrigido fecha NAQUELE valor: o rateio dos
    # analíticos passa a mirar o total corrigido em vez de medir o que estava
    # no arquivo — que é justamente o número errado que a correção veio arrumar.
    for c100 in docs_c:
        if c100 not in tocados:
            tocados.append(c100)
        alvo = texto_para_decimal(c100.campo(12))
        if alvo:
            extras[id(c100)] = alvo - soma_base_dos_itens(doc, c100)
    for d100 in docs_d:
        if d100 not in fretes:
            fretes.append(d100)

    if not tocados and not fretes:
        return resultado

    for d100 in fretes:
        mudou, avisos = espelhar_d100(doc, d100)
        resultado.alteracoes.extend(mudou)
        resultado.avisos.extend(avisos)

    for c100 in tocados:
        # Antes do C190: a glosa mexe no CST, que é parte da chave do grupo.
        mudou, avisos = aplicar_glosas(doc, c100, antes_itens)
        resultado.alteracoes.extend(mudou)
        resultado.avisos.extend(avisos)

        # E o caminho inverso, pelo mesmo motivo: o CST entra na chave do C190.
        mudou, avisos = ajustar_cst_do_credito(doc, c100, antes_itens)
        resultado.alteracoes.extend(mudou)
        resultado.avisos.extend(avisos)

        mudou, avisos = reconstruir_c190(doc, c100, extras.get(id(c100), ZERO))
        resultado.alteracoes.extend(mudou)
        resultado.avisos.extend(avisos)

        mudou, avisos = recalcular_c100(doc, c100)
        resultado.alteracoes.extend(mudou)
        resultado.avisos.extend(avisos)

        # Depois do recálculo: ICMS ST e IPI do C100 já vieram dos analíticos, e
        # é sobre eles que o resíduo é medido.
        if c100 in docs_c:
            mudou, avisos = fechar_componentes(doc, c100)
            resultado.alteracoes.extend(mudou)
            resultado.avisos.extend(avisos)

    # A apuração vem depois dos documentos: o delta de crédito é a diferença
    # entre o que os analíticos somavam antes e o que somam agora.
    if patch.atualizar_apuracao:
        mudou, avisos = atualizar_e110(doc, creditos_de_icms(doc) - creditos_antes)
        resultado.alteracoes.extend(mudou)
        resultado.avisos.extend(avisos)

    # Os contadores de linha vêm depois de tudo que cria ou apaga registro.
    resultado.alteracoes.extend(reconstruir_totais_de_blocos(doc))
    resultado.alteracoes.extend(reconstruir_bloco_9(doc))
    return resultado
