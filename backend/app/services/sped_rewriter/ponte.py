"""Da divergência apontada à correção que o motor de reescrita aceita.

A auditoria diz "o item 3 desta nota está com CFOP 1556 e deveria ser 1102".
O motor quer um `CorrecaoItem` com documento, número do item e os campos a
gravar. Este módulo é a tradução — e é onde ficam as recusas que só aparecem
quando se olha o ARQUIVO, e não o relatório da auditoria.

**A ponte lê o SPED, não só o `result_data`.** Dava para montar o patch direto
do JSON da auditoria, e seria mais rápido. Mas aí o "de → para" que a pessoa lê
na tela viria da memória da auditoria, enquanto o que vai ser reescrito é o
arquivo. Quando os dois discordam — auditoria de três semanas atrás, arquivo
substituído no meio —, quem manda é o arquivo: a proposta sai impedida, com o
motivo, em vez de gravar por cima de um número que ninguém conferiu.

Três coisas que só o arquivo responde:

1. **Qual D190 corrigir.** A divergência de ICMS do frete é do DOCUMENTO (o
   D100 tem um valor de ICMS só); o registro que se corrige é o analítico. Se
   o CT-e tem três D190, a correção precisa dizer qual — e isso não está no
   relatório.
2. **Se o valor apontado ainda é o que está lá.**
3. **O "de" de cada campo**, que é o que a tela mostra e o relatório registra.

O que NÃO é automatizável, e por quê — cada caso vira `impedimento` visível,
nunca um item que some da lista:

- **Divergência apurada na soma de várias linhas** (`itens_agrupados > 1`). O
  ICMS de um produto que aparece em cinco linhas da nota é conferido somado
  (ver `agrupar_icms_por_item_identico` no cross_checker). Repartir a diferença
  entre as linhas seria invenção.
- **Crédito a apropriar sem base e alíquota.** O Guia manda informar valor,
  base e alíquota em conjunto: crédito com base zerada é tão inconsistente
  quanto base com crédito zerado. Auditoria rodada antes de 09/09/2026 não
  guardou a base do XML, e os XMLs já foram apagados de `temp_uploads` —
  a proposta aparece impedida, pedindo para rodar a auditoria de novo.
- **Documento que não está no arquivo**, ou item que não está no documento.

O caminho inverso — glosar — precisa de UM campo só: zerar o valor do imposto.
Base, alíquota, CST e a migração para o custo da mercadoria são obrigação do
leiaute e a cascata faz sozinha (`aplicar_glosas`). Pedir esses campos aqui
duplicaria a regra em dois lugares.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .documento import Documento, Registro, ler_texto
from .patch import (OPCOES_DE_CORRECAO, CorrecaoDocumento, CorrecaoFrete,
                    CorrecaoItem, Patch, decimal_para_texto,
                    texto_para_decimal)

ZERO = Decimal("0")

# Como a auditoria identifica a linha de frete. O CT-e não tem registro de
# item, então o "item" de uma divergência de frete é o documento inteiro.
ALVO_CTE = "CT-e"

ROTULOS = {o.codigo: o.rotulo for o in OPCOES_DE_CORRECAO}

# A correção de crédito precisa dos três campos juntos; estas são as chaves que
# a auditoria passou a gravar em 09/09/2026 para que isso fosse possível.
FALTA_BASE = ("esta auditoria é anterior a 09/09/2026 e não guardou a base do "
              "XML — rode a auditoria de novo para corrigir o crédito por aqui")


@dataclass
class Proposta:
    """Uma correção oferecida na tela, já conferida contra o arquivo."""

    id: str                       # estável entre a montagem da tela e o POST
    opcao: str                    # código de OPCOES_DE_CORRECAO
    documento: str                # chave da NF-e / CT-e
    num_doc: str = ""             # número da nota, para a pessoa reconhecer
    fornecedor: str = ""
    alvo: str = ""                # "Item 3" | "Frete CFOP 2353"
    num_item: str = ""            # vazio no frete
    cfop_atual: str = ""          # preenchido no frete: identifica o D190
    descricao: str = ""
    campos: dict = field(default_factory=dict)
    mudancas: list = field(default_factory=list)   # [(campo, de, para)]
    motivo: str = ""              # a razão apontada pela auditoria
    delta: Decimal = ZERO         # quanto de imposto a correção move
    impedimento: str = ""

    @property
    def rotulo(self) -> str:
        return ROTULOS.get(self.opcao, self.opcao)

    @property
    def aplicavel(self) -> bool:
        return not self.impedimento and bool(self.campos)


# --------------------------------------------------------------------- números

def _num(valor) -> Decimal:
    """Número vindo do `result_data` (JSON: float) para Decimal com 2 casas."""
    if valor in (None, ""):
        return ZERO
    try:
        return Decimal(str(valor)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return ZERO


# ---------------------------------------------------------------- localização

def _item_do_documento(doc: Documento, c100: Registro, num_item: str):
    for item in doc.filhos_reg(c100, "C170"):
        if item.campo(2).strip() == str(num_item).strip():
            return item
    return None


def _analitico_do_frete(doc: Documento, d100: Registro, cfop: str):
    """(D190 alvo, impedimento). Sem CFOP, só resolve se houver um analítico só."""
    analiticos = doc.filhos_reg(d100, "D190")
    if not analiticos:
        return None, ("o CT-e não tem registro D190 no arquivo — não há "
                      "analítico para corrigir")

    alvo = (cfop or "").strip()
    if not alvo:
        # Auditoria antiga não gravava o CFOP escriturado na divergência de
        # frete. Com um analítico só não há ambiguidade; com mais de um, quem
        # escolhe não pode ser o sistema.
        if len(analiticos) == 1:
            return analiticos[0], ""
        return None, (f"o CT-e tem {len(analiticos)} registros D190 e a "
                      "auditoria não guardou em qual o ICMS foi escriturado — "
                      "rode a auditoria de novo")

    casados = [a for a in analiticos if a.campo(3).strip() == alvo]
    if not casados:
        return None, (f"o arquivo não tem D190 com CFOP {alvo} neste CT-e "
                      f"(tem {', '.join(sorted({a.campo(3) for a in analiticos}))})")
    if len(casados) > 1:
        return None, (f"o CT-e tem {len(casados)} registros D190 com CFOP "
                      f"{alvo}: a correção não diz qual corrigir")
    return casados[0], ""


# ------------------------------------------------------------------ montagem

def _mudanca(registro: Registro, numero: int, nome: str, novo: str):
    """(campo, de, para) lido do arquivo, ou None se já está assim."""
    atual = registro.campo(numero)
    if atual.strip() == novo.strip():
        return None
    return (nome, atual, novo)


def _proposta(opcao: str, documento: str, **kw) -> Proposta:
    alvo = kw.get("num_item") or kw.get("cfop_atual") or ""
    return Proposta(id=f"{opcao}|{documento}|{alvo}", opcao=opcao,
                    documento=documento, **kw)


def _cfops_de_produto(doc: Documento, dados: dict) -> list[Proposta]:
    indice = doc.indice_de_documentos()
    propostas = []
    for issue in dados.get("cfop_classifications_issues", []) or []:
        chave = str(issue.get("key") or "").strip()
        num_item = str(issue.get("num_item") or "").strip()
        novo = str(issue.get("expected_cfop") or "").strip()
        p = _proposta("cfop_produto", chave, num_item=num_item,
                      num_doc=str(issue.get("num") or ""),
                      fornecedor=str(issue.get("emit_name") or ""),
                      descricao=str(issue.get("product_description") or ""),
                      alvo=f"Item {num_item}",
                      motivo=(f"CFOP escriturado {issue.get('sped_cfop')} "
                              f"contra {novo} na regra aprovada para o produto"))

        c100 = indice.get(chave)
        item = _item_do_documento(doc, c100, num_item) if c100 else None
        if c100 is None:
            p.impedimento = "documento não encontrado no arquivo SPED"
        elif item is None:
            p.impedimento = f"o item {num_item} não existe neste documento"
        elif not novo:
            p.impedimento = "a auditoria não indicou o CFOP correto"
        else:
            mudanca = _mudanca(item, 11, "CFOP", novo)
            if mudanca is None:
                p.impedimento = "o arquivo já está com este CFOP"
            else:
                p.campos = {"CFOP": novo}
                p.mudancas = [mudanca]
        propostas.append(p)
    return propostas


def _cfops_de_frete(doc: Documento, dados: dict) -> list[Proposta]:
    indice = doc.indice_de_fretes()
    propostas = []
    for issue in dados.get("cte_cfop_classifications_issues", []) or []:
        chave = str(issue.get("key") or "").strip()
        atual = str(issue.get("sped_cfop") or "").strip()
        novo = str(issue.get("expected_cfop") or "").strip()
        p = _proposta("cfop_frete", chave, cfop_atual=atual,
                      num_doc=str(issue.get("num") or ""),
                      fornecedor=str(issue.get("emit_name") or ""),
                      descricao="Conhecimento de Transporte (Frete)",
                      alvo=f"Frete CFOP {atual}",
                      motivo=(f"CFOP escriturado {atual} contra {novo} na regra "
                              "aprovada para o frete"))

        d100 = indice.get(chave)
        if d100 is None:
            p.impedimento = "documento de transporte não encontrado no arquivo SPED"
            propostas.append(p)
            continue

        alvo, impedimento = _analitico_do_frete(doc, d100, atual)
        if impedimento:
            p.impedimento = impedimento
        elif not novo:
            p.impedimento = "a auditoria não indicou o CFOP correto"
        else:
            mudanca = _mudanca(alvo, 3, "CFOP", novo)
            if mudanca is None:
                p.impedimento = "o arquivo já está com este CFOP"
            else:
                p.cfop_atual = alvo.campo(3).strip()
                p.id = f"cfop_frete|{chave}|{p.cfop_atual}"
                p.campos = {"CFOP": novo}
                p.mudancas = [mudanca]
        propostas.append(p)
    return propostas


def _credito_de_icms(doc: Documento, dados: dict) -> list[Proposta]:
    """Mercadoria (C170) e frete (D190) saem da MESMA lista da auditoria.

    O que separa os dois é `num_item == "CT-e"` — foi assim que o motor gravou
    a divergência de frete desde o início, para o relatório mostrar as duas
    juntas na aba de ICMS.
    """
    idx_nf = doc.indice_de_documentos()
    idx_cte = doc.indice_de_fretes()
    propostas = []

    # A alíquota viaja como TEXTO já formatado ("12,00"): `valor_para_texto` só
    # converte campo monetário, e um float 12.0 chegaria ao arquivo como "12.0",
    # com ponto, que não é o separador do SPED.
    for issue in dados.get("icms_issues", []) or []:
        chave = str(issue.get("key") or "").strip()
        num_item = str(issue.get("num_item") or "").strip()
        glosa = str(issue.get("acao") or "") == "glosar"
        sped = _num(issue.get("sped_icms"))
        xml = _num(issue.get("xml_icms"))
        alvo_valor = ZERO if glosa else xml

        # ---------------------------------------------------------- frete
        if num_item == ALVO_CTE:
            atual = str(issue.get("sped_cfop") or "").strip()
            p = _proposta("credito_icms_frete", chave, cfop_atual=atual,
                          num_doc=str(issue.get("num") or ""),
                          fornecedor=str(issue.get("emit_name") or ""),
                          descricao="Conhecimento de Transporte (Frete)",
                          alvo=f"Frete CFOP {atual}" if atual else "Frete",
                          motivo=str(issue.get("reason") or ""))
            d100 = idx_cte.get(chave)
            if d100 is None:
                p.impedimento = "documento de transporte não encontrado no arquivo SPED"
                propostas.append(p)
                continue

            d190, impedimento = _analitico_do_frete(doc, d100, atual)
            if impedimento:
                p.impedimento = impedimento
                propostas.append(p)
                continue

            base = _num(issue.get("xml_bc_icms"))
            if alvo_valor > 0 and base <= 0:
                p.impedimento = FALTA_BASE
                propostas.append(p)
                continue

            # Sem crédito, a base vai a zero junto: base preenchida com imposto
            # zerado é meia glosa, e o D190 não tem cascata que complete isso.
            base_alvo = base if alvo_valor > 0 else ZERO
            campos = {"VL_BC_ICMS": base_alvo, "VL_ICMS": alvo_valor}
            mudancas = [m for m in (
                _mudanca(d190, 6, "VL_BC_ICMS",
                         decimal_para_texto(base_alvo, d190.campo(6))),
                _mudanca(d190, 7, "VL_ICMS",
                         decimal_para_texto(alvo_valor, d190.campo(7))),
            ) if m]
            if not mudancas:
                p.impedimento = "o arquivo já está com esse valor de ICMS"
            else:
                p.cfop_atual = d190.campo(3).strip()
                p.id = f"credito_icms_frete|{chave}|{p.cfop_atual}"
                p.campos = campos
                p.mudancas = mudancas
                p.delta = alvo_valor - texto_para_decimal(d190.campo(7))
            propostas.append(p)
            continue

        # ------------------------------------------------------ mercadoria
        p = _proposta("credito_icms_mercadoria", chave, num_item=num_item,
                      num_doc=str(issue.get("num") or ""),
                      fornecedor=str(issue.get("emit_name") or ""),
                      descricao=str(issue.get("descr") or ""),
                      alvo=f"Item {num_item}",
                      motivo=str(issue.get("reason") or ""))

        c100 = idx_nf.get(chave)
        item = _item_do_documento(doc, c100, num_item) if c100 else None
        agrupados = int(issue.get("itens_agrupados") or 1)

        if c100 is None:
            p.impedimento = "documento não encontrado no arquivo SPED"
        elif item is None:
            p.impedimento = f"o item {num_item} não existe neste documento"
        elif agrupados > 1:
            # A conferência somou as N linhas do mesmo produto na nota; a
            # diferença é do conjunto, e não há como saber de qual linha ela
            # veio. Repartir seria inventar.
            p.impedimento = (f"a divergência foi apurada na soma de {agrupados} "
                             "linhas do mesmo produto nesta nota — corrija à mão, "
                             "linha a linha")
        elif glosa:
            # Zerar o valor basta: base, alíquota, CST e a migração do ICMS ST /
            # IPI para o custo são obrigação do leiaute e a cascata faz sozinha.
            mudanca = _mudanca(item, 15, "VL_ICMS",
                               decimal_para_texto(ZERO, item.campo(15)))
            if mudanca is None:
                p.impedimento = "o item já está sem crédito de ICMS no arquivo"
            else:
                p.campos = {"VL_ICMS": ZERO}
                p.mudancas = [mudanca]
                p.delta = -texto_para_decimal(item.campo(15))
        else:
            base = _num(issue.get("xml_bc_icms"))
            aliq = _num(issue.get("xml_aliq_icms"))
            if xml > 0 and base <= 0:
                p.impedimento = FALTA_BASE
            else:
                aliq_texto = decimal_para_texto(aliq, item.campo(14))
                campos = {"VL_BC_ICMS": base, "ALIQ_ICMS": aliq_texto,
                          "VL_ICMS": xml}
                mudancas = [m for m in (
                    _mudanca(item, 13, "VL_BC_ICMS",
                             decimal_para_texto(base, item.campo(13))),
                    _mudanca(item, 14, "ALIQ_ICMS", aliq_texto),
                    _mudanca(item, 15, "VL_ICMS",
                             decimal_para_texto(xml, item.campo(15))),
                ) if m]
                if not mudancas:
                    p.impedimento = "o arquivo já está com esses valores"
                else:
                    p.campos = campos
                    p.mudancas = mudancas
                    p.delta = xml - texto_para_decimal(item.campo(15))
        propostas.append(p)

    return propostas


def _credito_de_ipi(doc: Documento, dados: dict) -> list[Proposta]:
    indice = doc.indice_de_documentos()
    propostas = []
    for issue in dados.get("ipi_issues", []) or []:
        chave = str(issue.get("key") or "").strip()
        num_item = str(issue.get("num_item") or "").strip()
        glosa = str(issue.get("acao") or "") == "glosar"
        xml = _num(issue.get("xml_val"))

        p = _proposta("credito_ipi", chave, num_item=num_item,
                      num_doc=str(issue.get("num") or ""),
                      fornecedor=str(issue.get("emit_name") or ""),
                      descricao=str(issue.get("descr") or ""),
                      alvo=f"Item {num_item}",
                      motivo=str(issue.get("reason") or ""))

        c100 = indice.get(chave)
        item = _item_do_documento(doc, c100, num_item) if c100 else None
        if c100 is None:
            p.impedimento = "documento não encontrado no arquivo SPED"
        elif item is None:
            p.impedimento = f"o item {num_item} não existe neste documento"
        elif glosa or xml <= 0:
            mudanca = _mudanca(item, 24, "VL_IPI",
                               decimal_para_texto(ZERO, item.campo(24)))
            if mudanca is None:
                p.impedimento = "o item já está sem crédito de IPI no arquivo"
            else:
                p.campos = {"VL_IPI": ZERO}
                p.mudancas = [mudanca]
                p.delta = -texto_para_decimal(item.campo(24))
        else:
            base = _num(issue.get("xml_bc_ipi"))
            if base <= 0:
                p.impedimento = FALTA_BASE
            else:
                # A alíquota do IPI é DERIVADA, não copiada: o XML traz valor e
                # base do imposto, não o percentual (`vl_bc_ipi` e `v_ipi` em
                # xml_parser). Dividir um pelo outro é aritmética conferível
                # sobre dois números do próprio documento — não é presunção.
                aliq = (xml / base * 100).quantize(Decimal("0.01"))
                aliq_texto = decimal_para_texto(aliq, item.campo(23))
                campos = {"VL_BC_IPI": base, "ALIQ_IPI": aliq_texto,
                          "VL_IPI": xml}
                mudancas = [m for m in (
                    _mudanca(item, 22, "VL_BC_IPI",
                             decimal_para_texto(base, item.campo(22))),
                    _mudanca(item, 23, "ALIQ_IPI", aliq_texto),
                    _mudanca(item, 24, "VL_IPI",
                             decimal_para_texto(xml, item.campo(24))),
                ) if m]
                if not mudancas:
                    p.impedimento = "o arquivo já está com esses valores"
                else:
                    p.campos = campos
                    p.mudancas = mudancas
                    p.delta = xml - texto_para_decimal(item.campo(24))
        propostas.append(p)
    return propostas


# Os três tipos de divergência de valor que a auditoria produz. Os dois
# primeiros comparam o SPED com o documento de verdade; o terceiro é uma
# contradição interna do próprio arquivo, e não tem alvo.
TIPO_NFE = "NF-e"
TIPO_CTE = "CT-e"
TIPO_INTERNO = "NF-e (SPED C100 vs C190)"


def _soma_opr(doc: Documento, c100: Registro) -> Decimal:
    return sum((texto_para_decimal(a.campo(5))
                for a in doc.filhos_reg(c100, "C190")), ZERO)


def _valores(doc: Documento, dados: dict) -> list[Proposta]:
    """Divergências de valor total — do documento, não do item.

    Corrigir o total é corrigir o DOCUMENTO inteiro, não um campo: o VL_DOC vai
    para o valor do documento de verdade, o rateio dos analíticos passa a mirar
    esse valor e o resíduo que sobra dos componentes do C100 é declarado como
    despesa acessória (ver `cascata.fechar_componentes`). A nota fecha nas duas
    contas que o PVA faz — soma dos VL_OPR contra VL_DOC, e componentes contra
    VL_DOC.

    **Os itens não são tocados**, e essa é a linha que este módulo não cruza:
    distribuir a diferença entre eles faria a nota fechar e cada linha passar a
    mentir sobre o valor da mercadoria. Quando a diferença for de outra natureza
    — item escriturado a menos, ST não lançado —, o conserto continua sendo no
    item, e o relatório mostra o resíduo justamente para essa conferência ser
    possível.

    O caso que ainda é recusado é o inverso: itens que somam MAIS que o total do
    documento. Aí não há resíduo a declarar, e fechar exigiria diminuir
    mercadoria ou imposto — o que é inventar, não corrigir.
    """
    idx_nf = doc.indice_de_documentos()
    idx_cte = doc.indice_de_fretes()
    propostas = []
    resolvidos = set()

    divergencias = dados.get("value_discrepancies") or []
    # Confronto com o documento primeiro: é o único que traz um valor ALVO.
    ordenadas = ([d for d in divergencias if d.get("type") != TIPO_INTERNO]
                 + [d for d in divergencias if d.get("type") == TIPO_INTERNO])

    for issue in ordenadas:
        chave = str(issue.get("key") or "").strip()
        tipo = str(issue.get("type") or "")
        alvo = _num(issue.get("xml_val"))
        escriturado = _num(issue.get("sped_val"))
        if chave in resolvidos:
            continue

        base = dict(num_doc=str(issue.get("num") or ""),
                    fornecedor=str(issue.get("emit_name") or ""),
                    motivo=str(issue.get("reason") or "")
                    or f"total escriturado divergente do documento ({tipo})")

        # ---------------------------------------------------------- frete
        if tipo == TIPO_CTE:
            p = _proposta("valor_frete", chave, alvo="Documento (CT-e)",
                          descricao="Conhecimento de Transporte (Frete)", **base)
            d100 = idx_cte.get(chave)
            if d100 is None:
                p.impedimento = "documento de transporte não encontrado no arquivo SPED"
            elif alvo <= 0:
                p.impedimento = "a auditoria não trouxe o valor do CT-e"
            else:
                mudanca = _mudanca(d100, 15, "VL_DOC",
                                   decimal_para_texto(alvo, d100.campo(15)))
                if mudanca is None:
                    p.impedimento = "o arquivo já está com esse valor"
                else:
                    p.campos = {"VL_DOC": alvo}
                    p.mudancas = [mudanca]
                    resolvidos.add(chave)
            propostas.append(p)
            continue

        # ----------------------------------------------------- mercadoria
        p = _proposta("valor_documento", chave, alvo="Documento (NF-e)",
                      descricao="Valor total da nota", **base)
        c100 = idx_nf.get(chave)
        if c100 is None:
            p.impedimento = "documento não encontrado no arquivo SPED"
            propostas.append(p)
            continue

        soma = _soma_opr(doc, c100)
        if tipo == TIPO_INTERNO:
            # Sem o valor do documento real, os dois lados são só dois números
            # diferentes: o total pode estar errado, ou os itens.
            p.impedimento = (
                f"divergência interna do SPED: os analíticos somam "
                f"{decimal_para_texto(soma)} e o total declara "
                f"{c100.campo(12)}. Sem o XML não dá para saber qual dos dois "
                "está certo — corrija o item ou o total à mão")
            propostas.append(p)
            continue

        base = sum((texto_para_decimal(i.campo(7))
                    - texto_para_decimal(i.campo(8))
                    + texto_para_decimal(i.campo(18))
                    + texto_para_decimal(i.campo(24))
                    for i in doc.filhos_reg(c100, "C170")), ZERO)

        if alvo <= 0:
            p.impedimento = "a auditoria não trouxe o valor do documento"
        elif base > alvo + Decimal("0.05"):
            # Itens valendo mais que o documento: fechar exigiria REDUZIR
            # mercadoria ou imposto, e nenhum resíduo resolve isso.
            p.impedimento = (
                f"os itens somam {decimal_para_texto(base)}, acima do total do "
                f"documento ({decimal_para_texto(alvo)}). Fechar a nota exigiria "
                "diminuir mercadoria ou imposto — o conserto é no item")
        else:
            mudanca = _mudanca(c100, 12, "VL_DOC",
                               decimal_para_texto(alvo, c100.campo(12)))
            if mudanca is None:
                p.impedimento = "o arquivo já está com esse valor"
            else:
                p.campos = {"VL_DOC": alvo}
                p.mudancas = [mudanca]
                if abs(soma - alvo) > Decimal("0.05"):
                    # A pessoa precisa saber ANTES de marcar que isto mexe em
                    # mais que um campo — e a prévia mostra cada linha.
                    p.motivo += (
                        f" · os analíticos somam {decimal_para_texto(soma)}: a "
                        "nota será fechada no valor do documento, com o rateio "
                        "refeito e o resíduo declarado em outras despesas "
                        "acessórias. Os itens não são alterados.")
                resolvidos.add(chave)
        propostas.append(p)

    return propostas


def propor(dados: dict, texto_sped: str) -> list[Proposta]:
    """As correções que a auditoria autoriza oferecer, conferidas no arquivo.

    Devolve TODAS — inclusive as impedidas, com o motivo. Sumir com a proposta
    que não deu para montar deixaria a tela dizendo que a nota está resolvida
    quando ninguém resolveu nada; é a mesma razão da aba "Itens Não Conferidos".
    """
    doc = ler_texto(texto_sped or "")
    propostas = (_cfops_de_produto(doc, dados)
                 + _credito_de_icms(doc, dados)
                 + _credito_de_ipi(doc, dados)
                 + _cfops_de_frete(doc, dados)
                 + _valores(doc, dados)
                 # Só a glosa: ver `_credito_de_pis_cofins`.
                 + _credito_de_pis_cofins(doc, dados, so_glosa=True))

    # Id repetido faria a tela marcar duas linhas com um clique só e o POST
    # aplicar a errada. Acontece de verdade: a mesma nota pode ter divergência
    # de CFOP e de crédito no mesmo item, e as duas nascem com alvo igual — o
    # código da opção é que as separa. Se ainda assim colidir, o segundo
    # ganha sufixo em vez de silenciosamente sumir.
    vistos: dict[str, int] = {}
    for p in propostas:
        if p.id in vistos:
            vistos[p.id] += 1
            p.id = f"{p.id}#{vistos[p.id]}"
        else:
            vistos[p.id] = 1
    return propostas


def montar_patch(propostas: list[Proposta], aceitos, aprovada_por: str = "",
                 origem: str = "divergência apontada na auditoria",
                 atualizar_apuracao: bool = False) -> Patch:
    """Só o que a pessoa marcou E o que dá para aplicar vira patch.

    A conferência do `aplicavel` é repetida aqui de propósito: o id vem do
    formulário, e formulário é entrada do usuário. Marcar uma proposta impedida
    — por HTML editado ou por tela velha aberta desde antes de o arquivo mudar —
    não pode virar gravação.
    """
    escolhidos = set(aceitos or [])
    patch = Patch(atualizar_apuracao=bool(atualizar_apuracao))
    for p in propostas:
        if p.id not in escolhidos or not p.aplicavel:
            continue
        if p.opcao in ("valor_documento", "valor_frete"):
            patch.documentos.append(CorrecaoDocumento(
                documento=p.documento, campos=dict(p.campos), motivo=p.motivo,
                origem=origem, aprovada_por=aprovada_por))
        elif p.opcao in ("cfop_frete", "credito_icms_frete"):
            patch.fretes.append(CorrecaoFrete(
                documento=p.documento, cfop_atual=p.cfop_atual, campos=dict(p.campos),
                motivo=p.motivo, origem=origem, aprovada_por=aprovada_por))
        else:
            patch.correcoes.append(CorrecaoItem(
                documento=p.documento, num_item=p.num_item, campos=dict(p.campos),
                motivo=p.motivo, origem=origem, aprovada_por=aprovada_por))
    return patch


# ==================================================== EFD Contribuições
#
# A auditoria emite UMA linha de divergência para o par PIS/COFINS, porque o
# confronto é da BASE e ela é a mesma para os dois (item menos ICMS, ICMS ST,
# FCP ST e IPI, feita no `xml_parser`). Por isso existe uma proposta só, com os
# seis campos juntos: marcar metade de um cálculo não é opção.
#
# O valor é DERIVADO da base, não copiado do XML: o `vPIS` do fornecedor foi
# calculado sobre a base DELE, e o que se escritura aqui é o crédito do
# comprador sobre a base da tese do STF.
#
# A alíquota é FIXA — 1,65% de PIS e 7,60% de COFINS —, pedido do Sandro em
# 23/09/2026. Até aqui ela vinha do próprio XML (pPIS/pCOFINS), e o XML traz a
# alíquota do FORNECEDOR, não o crédito de quem compra: fornecedor no
# cumulativo destaca 0,65/3,00 e a correção gravava crédito a menor; fornecedor
# do Simples não destaca nada e a proposta saía impedida. No não-cumulativo o
# crédito do comprador segue a alíquota dele, qualquer que seja o regime de
# quem vendeu. O que não dá crédito nenhum (monofásico, CST 70 a 75) continua
# saindo pela GLOSA, que olha a regra cadastrada e não passa por aqui.
ALIQ_PIS_CONTRIBUICOES = Decimal("1.65")
ALIQ_COFINS_CONTRIBUICOES = Decimal("7.60")

FRETE_NAO_AUTOMATIZAVEL = (
    "o PIS/COFINS do frete é escriturado nos registros D101 e D105, que esta "
    "correção ainda não reescreve — ajuste à mão")


def _aliquota_para_texto(aliquota: Decimal, original: str) -> str:
    """Alíquota no formato que o arquivo já usa: "1,6500" continua com quatro
    casas. Reescrever para "1,65" mudaria um campo que já está certo — e o
    arquivo corrigido só pode diferir do original no que foi aceito."""
    atual = (original or "").strip()
    casas = max(2, len(atual.split(",", 1)[1])) if "," in atual else 2
    return f"{aliquota:.{casas}f}".replace(".", ",")


def _credito_de_pis_cofins(doc: Documento, dados: dict,
                           so_glosa: bool = False) -> list[Proposta]:
    """As correções de PIS/COFINS do C170, conferidas no arquivo.

    `so_glosa` é o modo do EFD ICMS/IPI (18/09/2026). Ali a única correção
    que faz sentido é APAGAR o crédito indevido: o confronto de BASE contra
    base gera linha em quase todo item daquele arquivo — é o efeito medido e
    aceito em 28/08/2026, porque o Fiscal quase não preenche VL_BC_PIS —, e
    transformar isso em proposta de reescrita mandaria gravar base e alíquota
    de um tributo que aquele arquivo não escritura. O que a glosa precisa,
    ao contrário, está todo dentro do próprio SPED: o CST do item e a regra
    cadastrada. Por isso ela vale nos dois, com XML ou sem.
    """
    indice = doc.indice_de_documentos()
    propostas = []
    for issue in dados.get("pis_cofins_issues", []) or []:
        chave = str(issue.get("key") or "").strip()
        num_item = str(issue.get("num_item") or "").strip()
        glosa = str(issue.get("acao") or "") == "glosar"
        if so_glosa and not glosa:
            continue

        p = _proposta("credito_pis_cofins", chave, num_item=num_item,
                      num_doc=str(issue.get("num") or ""),
                      fornecedor=str(issue.get("emit_name") or ""),
                      descricao=str(issue.get("descr") or ""),
                      alvo=f"Item {num_item}" if num_item != ALVO_CTE else "Frete",
                      motivo=str(issue.get("reason") or ""))

        if num_item == ALVO_CTE:
            p.impedimento = FRETE_NAO_AUTOMATIZAVEL
            propostas.append(p)
            continue

        c100 = indice.get(chave)
        item = _item_do_documento(doc, c100, num_item) if c100 else None
        if c100 is None:
            p.impedimento = "documento não encontrado no arquivo SPED"
            propostas.append(p)
            continue
        if item is None:
            p.impedimento = f"o item {num_item} não existe neste documento"
            propostas.append(p)
            continue

        if glosa:
            # Zerar o valor basta: base, alíquota e o CST 70 são obrigação do
            # leiaute e a cascata faz sozinha, como no ICMS.
            campos = {"VL_PIS": ZERO, "VL_COFINS": ZERO}
            mudancas = [m for m in (
                _mudanca(item, 30, "VL_PIS", decimal_para_texto(ZERO, item.campo(30))),
                _mudanca(item, 36, "VL_COFINS", decimal_para_texto(ZERO, item.campo(36))),
            ) if m]
            if not mudancas:
                p.impedimento = "o item já está sem crédito de PIS/COFINS no arquivo"
            else:
                p.campos = campos
                p.mudancas = mudancas
                p.delta = -(texto_para_decimal(item.campo(30))
                            + texto_para_decimal(item.campo(36)))
            propostas.append(p)
            continue

        base = _num(issue.get("base_calculada"))
        if base <= 0:
            base = _num(issue.get("xml_val"))
        # Fixas, e não as do XML (ver ALIQ_PIS_CONTRIBUICOES). Por isso a
        # auditoria antiga, que não guardou `xml_aliq_pis`, também é corrigida.
        aliq_pis = ALIQ_PIS_CONTRIBUICOES
        aliq_cofins = ALIQ_COFINS_CONTRIBUICOES
        if base <= 0:
            p.impedimento = "a auditoria não trouxe a base de cálculo do XML"
        else:
            vl_pis = (base * aliq_pis / 100).quantize(Decimal("0.01"))
            vl_cofins = (base * aliq_cofins / 100).quantize(Decimal("0.01"))
            texto_aliq_pis = _aliquota_para_texto(aliq_pis, item.campo(27))
            texto_aliq_cofins = _aliquota_para_texto(aliq_cofins, item.campo(33))
            campos = {"VL_BC_PIS": base, "ALIQ_PIS": texto_aliq_pis,
                      "VL_PIS": vl_pis, "VL_BC_COFINS": base,
                      "ALIQ_COFINS": texto_aliq_cofins, "VL_COFINS": vl_cofins}
            mudancas = [m for m in (
                _mudanca(item, 26, "VL_BC_PIS", decimal_para_texto(base, item.campo(26))),
                _mudanca(item, 27, "ALIQ_PIS", texto_aliq_pis),
                _mudanca(item, 30, "VL_PIS", decimal_para_texto(vl_pis, item.campo(30))),
                _mudanca(item, 32, "VL_BC_COFINS", decimal_para_texto(base, item.campo(32))),
                _mudanca(item, 33, "ALIQ_COFINS", texto_aliq_cofins),
                _mudanca(item, 36, "VL_COFINS", decimal_para_texto(vl_cofins, item.campo(36))),
            ) if m]
            if not mudancas:
                p.impedimento = "o arquivo já está com esses valores"
            else:
                p.campos = campos
                p.mudancas = mudancas
                p.delta = ((vl_pis + vl_cofins)
                           - texto_para_decimal(item.campo(30))
                           - texto_para_decimal(item.campo(36)))
        propostas.append(p)
    return propostas


def propor_contribuicoes(dados: dict, texto_sped: str) -> list[Proposta]:
    """As correções que o arquivo de EFD Contribuições autoriza oferecer.

    Separada de `propor` de propósito: as cinco opções do Fiscal reescrevem
    C190, Bloco E e campos de ICMS/IPI, que não existem neste arquivo.
    """
    doc = ler_texto(texto_sped or "")
    propostas = _credito_de_pis_cofins(doc, dados)

    vistos: dict[str, int] = {}
    for p in propostas:
        if p.id in vistos:
            vistos[p.id] += 1
            p.id = f"{p.id}#{vistos[p.id]}"
        else:
            vistos[p.id] = 1
    return propostas
