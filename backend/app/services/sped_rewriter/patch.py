"""O que o usuário aceitou corrigir, e as travas antes de encostar no arquivo.

A auditoria (`cross_checker`) aponta a divergência; a curadoria aprova a regra;
a tela mostra a sugestão e a pessoa marca o que aceita. Este módulo é o quarto
passo: transforma o que ela marcou num conjunto de alterações **admissíveis**.

Admissível é a palavra: nem toda correção que faz sentido fiscalmente pode ser
aplicada isolada num arquivo SPED. A trava que mais dói está no campo 11 do
C170, e é fácil de passar despercebida porque as regras do Synax são POR ITEM:

    "O primeiro caractere deve ser o mesmo para todos os itens de um documento
     fiscal."  — Guia Prático 3.2.2, C170 campo 11

Ou seja: aceitar 1102 -> 2102 em um item de uma nota cujos outros itens estão
em 1xxx **rejeita o arquivo inteiro no PVA**. Por isso a validação é por
documento e a recusa é do documento todo — aplicar metade deixaria a nota
inconsistente, que é pior do que não aplicar.

Recusa nunca é silenciosa: sai em `Recusa`, com motivo, e a tela mostra. Vale
aqui a mesma regra do resto do sistema — operação em lote não devolve sucesso
verde com zero aplicado.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .documento import Documento, Registro

# Campos do C170 que uma correção pode tocar, pelo número do Guia.
CAMPOS_C170 = {
    "CST_ICMS": 10,
    "CFOP": 11,
    "VL_BC_ICMS": 13,
    "ALIQ_ICMS": 14,
    "VL_ICMS": 15,
    "VL_BC_ICMS_ST": 16,
    "VL_ICMS_ST": 18,
    "CST_IPI": 20,
    "VL_BC_IPI": 22,
    "ALIQ_IPI": 23,
    "VL_IPI": 24,
    # PIS/COFINS — corrigíveis só no leiaute do EFD Contribuições, que é
    # onde esses campos existem preenchidos. Estar no mapa não os oferece na
    # tela: quem decide isso é `leiaute` na opção, logo abaixo.
    "CST_PIS": 25,
    "VL_BC_PIS": 26,
    "ALIQ_PIS": 27,
    "VL_PIS": 30,
    "CST_COFINS": 31,
    "VL_BC_COFINS": 32,
    "ALIQ_COFINS": 33,
    "VL_COFINS": 36,
}

# Campos do D190 (frete) que uma correção pode tocar. O D100 NÃO tem registro
# de item — não existe "D170" —, então a correção de frete age direto no
# analítico, e não sobre itens que seriam re-somados.
CAMPOS_D190 = {
    "CST_ICMS": 2,
    "CFOP": 3,
    "ALIQ_ICMS": 4,
    "VL_OPR": 5,
    "VL_BC_ICMS": 6,
    "VL_ICMS": 7,
    "VL_RED_BC": 8,
}

# Campos do C100 e do D100 que uma correção pode tocar. Aqui o alvo é o
# DOCUMENTO, não o item nem o analítico — é o terceiro nível, e existe por um
# motivo só: o valor total escriturado divergir do valor do documento.
#
# A numeração é diferente nos dois, e confundi-las grava dinheiro no campo
# errado: no C100 o VL_DOC é o campo 12; no D100 é o 15 (lá o 12 é DT_A_P).
CAMPOS_C100 = {"VL_DOC": 12}
CAMPOS_D100 = {"VL_DOC": 15}

# Campos de dinheiro: entram como Decimal e saem com vírgula e duas casas.
CAMPOS_MONETARIOS = {"VL_BC_ICMS", "VL_ICMS", "VL_BC_ICMS_ST", "VL_ICMS_ST",
                     "VL_BC_IPI", "VL_IPI", "VL_OPR", "VL_RED_BC", "VL_DOC",
                     "VL_BC_PIS", "VL_PIS", "VL_BC_COFINS", "VL_COFINS"}

# VL_RED_BC do D190 "só pode ser preenchido se o campo CST_ICMS for igual a 20
# ou 70" (Guia 3.2.2, D190 campo 08).
CST_COM_REDUCAO = ("20", "70")

# ---------------------------------------------------------------- as opções
#
# As CINCO correções que a tela de curadoria oferece (definidas com o Sandro em
# 09/09/2026). Ficam aqui, e não no template, para a tela e o motor não
# divergirem: a suíte confere que todo campo declarado abaixo é de fato
# corrigível.
#
# "Valor contábil" NÃO é opção, de propósito. Não existe campo com esse nome no
# Bloco C — os candidatos seriam cinco (VL_DOC, VL_MERC, VL_ITEM, VL_OPR,
# VL_SERV) e três deles são derivados. O caso legítimo de mexer no valor da
# mercadoria é consequência da glosa, e o motor faz sozinho: ver
# `aplicar_glosas` em cascata.py.
#
# O CST também não é opção manual: a glosa o ajusta junto, para o arquivo não
# dizer "tributada integralmente" num item sem imposto nenhum.


# Os dois leiautes de SPED, repetidos aqui para o motor de reescrita não
# importar o da auditoria: são módulos de propósitos diferentes, e a
# dependência só existiria por causa de duas strings.
LEIAUTE_FISCAL = "fiscal"
LEIAUTE_CONTRIBUICOES = "contribuicoes"
# A correção que vale nos DOIS arquivos. Os campos 25 a 36 do C170 são os
# mesmos nos dois leiautes, e glosar neles não encosta em C190 nem no
# Bloco E — é zerar o imposto do item e trocar o CST.
LEIAUTE_AMBOS = "ambos"


@dataclass(frozen=True)
class Opcao:
    codigo: str
    rotulo: str
    registro: str
    campos: tuple
    # Em qual arquivo esta correção faz sentido. As cinco de sempre são do
    # EFD ICMS/IPI e continuam iguais; o EFD Contribuições tem as suas.
    leiaute: str = LEIAUTE_FISCAL


OPCOES_DE_CORRECAO = (
    Opcao("cfop_produto", "CFOP de produtos", "C170", ("CFOP",)),
    Opcao("cfop_frete", "CFOP de fretes", "D190", ("CFOP",)),
    Opcao("credito_icms_mercadoria", "Crédito de ICMS — mercadoria", "C170",
          ("VL_BC_ICMS", "ALIQ_ICMS", "VL_ICMS")),
    Opcao("credito_icms_frete", "Crédito de ICMS — frete", "D190",
          ("VL_BC_ICMS", "VL_ICMS")),
    Opcao("credito_ipi", "Crédito de IPI", "C170",
          ("VL_BC_IPI", "ALIQ_IPI", "VL_IPI")),
    # Acrescentadas em 10/09/2026, a pedido do Sandro. O comentário acima dizia
    # que "valor contábil" não era opção porque os candidatos eram cinco campos
    # e três deles derivados — continua verdade, e é por isso que a opção é
    # **um campo só**: o VL_DOC, o valor de face do documento. Os derivados
    # (VL_MERC, VL_OPR) seguem saindo da soma dos itens, e a ponte recusa a
    # correção quando os itens não fecham com o valor pedido, em vez de gravar
    # um total que os analíticos contradizem.
    Opcao("valor_documento", "Valor total da nota", "C100", ("VL_DOC",)),
    Opcao("valor_frete", "Valor total do frete", "D100", ("VL_DOC",)),
    # --- EFD Contribuições (17/09/2026) --------------------------------
    # Uma opção só para os dois tributos porque a auditoria emite UMA linha
    # de divergência para o par: o confronto é da BASE, que é a mesma para
    # PIS e COFINS (item menos ICMS, ICMS ST, FCP ST e IPI). Separar em duas
    # caixas faria a pessoa marcar metade de um cálculo.
    Opcao("credito_pis_cofins", "Crédito de PIS/COFINS — mercadoria", "C170",
          ("VL_BC_PIS", "ALIQ_PIS", "VL_PIS",
           "VL_BC_COFINS", "ALIQ_COFINS", "VL_COFINS"),
          # Nos dois leiautes desde 18/09/2026, a pedido do Sandro. No EFD
          # ICMS/IPI a tela só recebe as propostas de GLOSA — ver `propor`
          # em ponte.py —, porque ali o C170 quase nunca traz base de
          # PIS/COFINS e escrever base nova num arquivo que não escritura
          # esse tributo seria inventar dado.
          leiaute=LEIAUTE_AMBOS),
)


def opcoes_do_leiaute(leiaute: str) -> tuple:
    """As correções que a tela oferece para o arquivo que foi auditado.

    O EFD Contribuições não tem C190 nem Bloco E, então as sete opções de
    ICMS/IPI não têm o que reescrever ali. `LEIAUTE_AMBOS` sai nas duas
    listas: é o caso do PIS/COFINS, cujos campos do C170 existem nos dois
    arquivos. O que muda entre eles não é a caixa da tela, é o que a ponte
    oferece dentro dela — no Fiscal, só a glosa.
    """
    alvo = leiaute or LEIAUTE_FISCAL
    return tuple(o for o in OPCOES_DE_CORRECAO
                 if o.leiaute in (alvo, LEIAUTE_AMBOS))

# Primeiro dígito do CFOP conforme o sentido da operação (C170 campo 11 e
# C190 campo 03, ambos amarrados ao IND_OPER do C100).
PRIMEIRO_DIGITO = {"0": {"1", "2", "3"}, "1": {"5", "6", "7"}}


# ------------------------------------------------------------------- números

def texto_para_decimal(texto: str) -> Decimal:
    """"1.234,50" -> Decimal("1234.50"). Vazio e lixo viram zero.

    Campo vazio é zero para efeito de SOMA, mas não para efeito de gravação —
    ver `decimal_para_texto`, que preserva o vazio.
    """
    limpo = (texto or "").strip()
    if not limpo:
        return Decimal("0")
    if "," in limpo:
        limpo = limpo.replace(".", "").replace(",", ".")
    try:
        return Decimal(limpo)
    except InvalidOperation:
        return Decimal("0")


def decimal_para_texto(valor: Decimal, original: str = "") -> str:
    """Decimal -> "1234,50". Campo que estava VAZIO e continua zero segue vazio.

    Sem essa preservação, corrigir um item encheria o documento de "0,00" em
    campos que o emitente deixou em branco de propósito — mudança que ninguém
    pediu, num arquivo que precisa diferir do original só no que foi aceito.
    """
    arredondado = Decimal(valor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if not (original or "").strip() and arredondado == 0:
        return ""
    return f"{arredondado:.2f}".replace(".", ",")


def valor_para_texto(nome_campo: str, valor) -> str:
    """Normaliza o valor que veio da tela para o texto que vai no arquivo."""
    if isinstance(valor, str):
        return valor.strip()
    if nome_campo in CAMPOS_MONETARIOS:
        return decimal_para_texto(Decimal(str(valor)), original="0")
    return str(valor).strip()


# -------------------------------------------------------------------- modelo

@dataclass
class CorrecaoItem:
    """Uma correção aceita, no nível do item do documento."""

    documento: str                    # chave da NF-e ou "modelo/série/número"
    num_item: str
    campos: dict                      # {"CFOP": "1102", "VL_ICMS": Decimal(...)}
    motivo: str = ""                  # a regra que decidiu, em texto
    origem: str = ""                  # "regra aprovada #412", "ajuste manual"...
    aprovada_por: str = ""            # maker-checker: quem marcou na tela


@dataclass
class CorrecaoFrete:
    """Uma correção aceita no analítico de um documento de transporte.

    O alvo é o D190, identificado pelo CFOP que ele tem HOJE — é assim que a
    pessoa o enxerga na tela de auditoria ("o frete está em 2353 e deveria ser
    1353"). CT-e com mais de um D190 no mesmo CFOP é ambíguo e recusado, em vez
    de o motor escolher um por conta própria.
    """

    documento: str                    # chave do CT-e ou "modelo/série/número"
    cfop_atual: str
    campos: dict
    motivo: str = ""
    origem: str = ""
    aprovada_por: str = ""


@dataclass
class CorrecaoDocumento:
    """Uma correção no próprio C100/D100 — o valor de face do documento.

    Diferente das outras duas: não há item nem analítico a re-somar. O que se
    corrige aqui é o número que o cliente escriturou como total do documento
    quando ele não bate com o documento de verdade.
    """

    documento: str                    # chave da NF-e ou do CT-e
    campos: dict                      # {"VL_DOC": Decimal(...)}
    motivo: str = ""
    origem: str = ""
    aprovada_por: str = ""


@dataclass
class Alteracao:
    """Uma mudança atômica no arquivo, rastreável até a linha de origem.

    `origem` separa o que a pessoa pediu do que veio junto: ela aceitou UMA
    troca de CFOP, e essa troca gerou meia dúzia de alterações em cascata que
    ela não pediu e precisa poder auditar.
    """

    documento: str
    registro: str                     # "C170", "C190", "C100", "9900"...
    linha_origem: int | None
    campo: str
    de: str
    para: str
    num_item: str = ""
    motivo: str = ""
    origem: str = "correção aceita"   # ou "cascata" / "bloco 9"
    aprovada_por: str = ""


@dataclass
class Recusa:
    """Correção que não pode ser aplicada, com o porquê em português."""

    documento: str
    motivo: str
    correcoes: list = field(default_factory=list)


@dataclass
class Patch:
    """O conjunto de correções aceitas para um arquivo.

    `correcoes` são os itens de mercadoria (C170); `fretes` são os analíticos
    de transporte (D190). Duas listas porque os dois caminhos são estruturalmente
    diferentes — ver `CorrecaoFrete`.
    """

    correcoes: list[CorrecaoItem] = field(default_factory=list)
    fretes: list[CorrecaoFrete] = field(default_factory=list)
    documentos: list[CorrecaoDocumento] = field(default_factory=list)

    # Reescrever a APURAÇÃO (E110) é decisão separada da de corrigir documento,
    # e continua sendo: recalcular documento é aritmética conferível, mexer no
    # Bloco E muda imposto a recolher. O motor só encosta no E110 quando este
    # campo vem True — e quem o liga é uma caixa própria na tela, não o mesmo
    # clique que aceita as correções.
    atualizar_apuracao: bool = False

    @property
    def vazio(self) -> bool:
        return not (self.correcoes or self.fretes or self.documentos)

    def documentos_por_chave(self) -> dict[str, list[CorrecaoDocumento]]:
        agrupado: dict[str, list[CorrecaoDocumento]] = {}
        for c in self.documentos:
            agrupado.setdefault(c.documento, []).append(c)
        return agrupado

    def por_documento(self) -> dict[str, list[CorrecaoItem]]:
        agrupado: dict[str, list[CorrecaoItem]] = {}
        for c in self.correcoes:
            agrupado.setdefault(c.documento, []).append(c)
        return agrupado

    def fretes_por_documento(self) -> dict[str, list[CorrecaoFrete]]:
        agrupado: dict[str, list[CorrecaoFrete]] = {}
        for c in self.fretes:
            agrupado.setdefault(c.documento, []).append(c)
        return agrupado


# ---------------------------------------------------------------- validação

def _cfop_valido(cfop: str) -> bool:
    return len(cfop) == 4 and cfop.isdigit()


def _validar_documento(c100: Registro, itens: list[Registro],
                       correcoes: list[CorrecaoItem]) -> str | None:
    """Simula o resultado e devolve o motivo da recusa, ou None se pode aplicar.

    Valida ANTES de escrever qualquer campo — assim não existe meia-aplicação
    para desfazer, e o documento nunca fica num estado que o PVA rejeita.
    """
    ind_oper = c100.campo(2)

    # Os campos de TODAS as correções do mesmo item, juntos. Um `dict` por
    # correção perderia a primeira: a mesma linha costuma receber duas — o CFOP
    # que a curadoria mapeou e o crédito que a auditoria apontou —, e a que
    # ficasse de fora seria justamente a do CFOP, que é o que esta validação
    # existe para conferir.
    # NUM_ITEM comparado sem espaços dos dois lados: a ponte já normaliza o
    # lado da auditoria, e um " 1" no arquivo recusava o item inteiro.
    por_item: dict[str, dict] = {}
    for c in correcoes:
        por_item.setdefault(c.num_item.strip(), {}).update(c.campos)

    numeros = {i.campo(2).strip() for i in itens}
    for correcao in correcoes:
        if correcao.num_item.strip() not in numeros:
            return (f"item {correcao.num_item} não existe neste documento "
                    f"(a nota tem {len(itens)} item(ns))")
        for nome in correcao.campos:
            if nome not in CAMPOS_C170:
                return f"campo '{nome}' não é corrigível no C170"

    # Como ficaria o CFOP de cada item depois de aplicar tudo.
    resultado = []
    for item in itens:
        campos = por_item.get(item.campo(2).strip(), {})
        novo = (valor_para_texto("CFOP", campos["CFOP"])
                if "CFOP" in campos else item.campo(11))
        resultado.append((item.campo(2), novo))

    for num_item, cfop in resultado:
        if not _cfop_valido(cfop):
            return f"CFOP '{cfop}' do item {num_item} não tem 4 dígitos"

    permitidos = PRIMEIRO_DIGITO.get(ind_oper)
    if permitidos:
        fora = [(n, c) for n, c in resultado if c[0] not in permitidos]
        if fora:
            sentido = "entrada" if ind_oper == "0" else "saída"
            n, c = fora[0]
            return (f"CFOP {c} (item {n}) não é de {sentido} — com IND_OPER="
                    f"{ind_oper} o primeiro dígito tem de ser "
                    f"{' , '.join(sorted(permitidos))}")

    primeiros = {c[0] for _, c in resultado}
    if len(primeiros) > 1:
        # A trava do campo 11 do C170. É a que a chave por item do Synax mais
        # ameaça violar, e o PVA rejeita o arquivo inteiro por causa dela.
        detalhe = ", ".join(f"item {n}: {c}" for n, c in resultado)
        return ("todos os itens de um documento têm de começar com o mesmo "
                f"dígito de CFOP; ficaria {detalhe}")

    return None


# ---------------------------------------------------------------- aplicação

def aplicar_itens(doc: Documento, patch: Patch) -> tuple[list[Alteracao],
                                                         list[Recusa],
                                                         list[Registro]]:
    """Grava as correções nos C170. Devolve (alterações, recusas, C100 tocados).

    Só mexe no item. A reorganização do C190, os totais do C100 e o Bloco 9
    são a cascata — ver `cascata.py` —, e rodam depois porque dependem do que
    ficou aqui.
    """
    indice = doc.indice_de_documentos()
    alteracoes: list[Alteracao] = []
    recusas: list[Recusa] = []
    tocados: list[Registro] = []

    for chave, correcoes in patch.por_documento().items():
        c100 = indice.get(chave)
        if c100 is None:
            recusas.append(Recusa(documento=chave,
                                  motivo="documento não encontrado no arquivo",
                                  correcoes=correcoes))
            continue

        itens = doc.filhos_reg(c100, "C170")
        if not itens:
            recusas.append(Recusa(documento=chave,
                                  motivo="documento sem registros C170 (nada a corrigir no item)",
                                  correcoes=correcoes))
            continue

        motivo = _validar_documento(c100, itens, correcoes)
        if motivo:
            recusas.append(Recusa(documento=chave, motivo=motivo,
                                  correcoes=correcoes))
            continue

        por_num = {i.campo(2).strip(): i for i in itens}
        mexeu = False
        for correcao in correcoes:
            item = por_num[correcao.num_item.strip()]
            for nome, valor in correcao.campos.items():
                numero = CAMPOS_C170[nome]
                antes = item.campo(numero)
                depois = (decimal_para_texto(Decimal(str(valor)), antes)
                          if nome in CAMPOS_MONETARIOS and not isinstance(valor, str)
                          else valor_para_texto(nome, valor))
                if item.set(numero, depois):
                    mexeu = True
                    alteracoes.append(Alteracao(
                        documento=chave, registro="C170", linha_origem=item.linha,
                        num_item=correcao.num_item, campo=nome, de=antes,
                        para=depois, motivo=correcao.motivo,
                        origem=correcao.origem or "correção aceita",
                        aprovada_por=correcao.aprovada_por))

        if mexeu:
            tocados.append(c100)

    return alteracoes, recusas, tocados


def aplicar_documentos(doc: Documento, patch: Patch) -> tuple[list[Alteracao],
                                                              list[Recusa],
                                                              list[Registro],
                                                              list[Registro]]:
    """Grava o valor de face do documento. Devolve (alterações, recusas, C100, D100).

    Os dois tipos saem juntos porque a correção é a mesma — muda só a numeração
    do campo e o índice onde procurar o documento.
    """
    idx_nf = doc.indice_de_documentos()
    idx_cte = doc.indice_de_fretes()
    alteracoes: list[Alteracao] = []
    recusas: list[Recusa] = []
    c100s: list[Registro] = []
    d100s: list[Registro] = []

    for chave, correcoes in patch.documentos_por_chave().items():
        registro = idx_nf.get(chave)
        campos_validos = CAMPOS_C100
        if registro is None:
            registro = idx_cte.get(chave)
            campos_validos = CAMPOS_D100
        if registro is None:
            recusas.append(Recusa(documento=chave,
                                  motivo="documento não encontrado no arquivo",
                                  correcoes=correcoes))
            continue

        erro = ""
        for correcao in correcoes:
            for nome, valor in correcao.campos.items():
                if nome not in campos_validos:
                    erro = (f"campo '{nome}' não é corrigível no "
                            f"{registro.reg}")
                    break
                if texto_para_decimal(str(valor)) <= 0 and not isinstance(valor, str):
                    # Zerar o valor de face de um documento não é correção de
                    # escrituração: é apagar a nota pela metade.
                    erro = f"o valor do documento não pode ser zero ou negativo"
                    break
            if erro:
                break
        if erro:
            recusas.append(Recusa(documento=chave, motivo=erro,
                                  correcoes=correcoes))
            continue

        mexeu = False
        for correcao in correcoes:
            for nome, valor in correcao.campos.items():
                numero = campos_validos[nome]
                antes = registro.campo(numero)
                depois = (decimal_para_texto(Decimal(str(valor)), antes)
                          if not isinstance(valor, str)
                          else valor_para_texto(nome, valor))
                if registro.set(numero, depois):
                    mexeu = True
                    alteracoes.append(Alteracao(
                        documento=chave, registro=registro.reg,
                        linha_origem=registro.linha, campo=nome, de=antes,
                        para=depois, motivo=correcao.motivo,
                        origem=correcao.origem or "correção aceita",
                        aprovada_por=correcao.aprovada_por))

        if mexeu:
            (c100s if registro.reg == "C100" else d100s).append(registro)

    return alteracoes, recusas, c100s, d100s


def _validar_frete(d100: Registro, analiticos: list[Registro],
                   correcoes: list[CorrecaoFrete]) -> str | None:
    """Simula a correção do frete e devolve o motivo da recusa, ou None."""
    ind_oper = d100.campo(2)
    por_cfop: dict[str, list[Registro]] = {}
    for a in analiticos:
        por_cfop.setdefault(a.campo(3).strip(), []).append(a)

    for correcao in correcoes:
        alvos = por_cfop.get(correcao.cfop_atual.strip(), [])
        if not alvos:
            return (f"nenhum D190 com CFOP {correcao.cfop_atual} neste "
                    f"documento (há {', '.join(sorted(por_cfop)) or 'nenhum'})")
        if len(alvos) > 1:
            return (f"há {len(alvos)} registros D190 com CFOP "
                    f"{correcao.cfop_atual}: a correção não diz qual corrigir")
        for nome in correcao.campos:
            if nome not in CAMPOS_D190:
                return f"campo '{nome}' não é corrigível no D190"

    # Como ficariam as chaves (CST, CFOP, ALIQ) depois de aplicar tudo.
    novas: dict[str, tuple] = {}
    for a in analiticos:
        # Mesma razão do C170: o CT-e recebe com frequência duas correções no
        # mesmo analítico (o CFOP e o crédito), e ver só a primeira validaria
        # um estado que não é o que vai para o arquivo.
        campos = {}
        for c in correcoes:
            if c.cfop_atual.strip() == a.campo(3).strip():
                campos.update(c.campos)
        cst = valor_para_texto("CST_ICMS", campos.get("CST_ICMS", a.campo(2)))
        cfop = valor_para_texto("CFOP", campos.get("CFOP", a.campo(3)))
        aliq = a.campo(4) if "ALIQ_ICMS" not in campos else \
            valor_para_texto("ALIQ_ICMS", campos["ALIQ_ICMS"])
        red = campos.get("VL_RED_BC", a.campo(8))

        if not _cfop_valido(cfop):
            return f"CFOP '{cfop}' não tem 4 dígitos"
        permitidos = PRIMEIRO_DIGITO.get(ind_oper)
        if permitidos and cfop[0] not in permitidos:
            sentido = "entrada" if ind_oper == "0" else "saída"
            return (f"CFOP {cfop} não é de {sentido} — com IND_OPER={ind_oper} "
                    f"o primeiro dígito tem de ser {' , '.join(sorted(permitidos))}")
        # Guia 3.2.2, D190 campo 08.
        if texto_para_decimal(str(red)) != 0 and cst[-2:] not in CST_COM_REDUCAO:
            return (f"VL_RED_BC só pode ser preenchido com CST 20 ou 70; "
                    f"o CST ficaria {cst}")

        chave = (cst, cfop, f"{texto_para_decimal(aliq):.2f}")
        if chave in novas:
            # Cada D190 tem de ser igual ao D100 pai nos campos de valor; somar
            # dois grupos dobraria o crédito. Recusar é a resposta honesta.
            return (f"a correção faria dois registros D190 com a mesma "
                    f"combinação CST {chave[0]} / CFOP {chave[1]} / alíquota "
                    f"{chave[2]} — juntá-los mudaria o valor do crédito")
        novas[chave] = chave

    return None


def aplicar_fretes(doc: Documento, patch: Patch) -> tuple[list[Alteracao],
                                                          list[Recusa],
                                                          list[Registro]]:
    """Grava as correções nos D190. Devolve (alterações, recusas, D100 tocados).

    Diferente do C170/C190: aqui não há item para re-somar. O Guia amarra o
    D190 ao pai pela IGUALDADE ("o valor informado deve ser igual ao valor do
    campo VL_BC_ICMS do registro D100, pai deste registro D190"), e é o
    espelhamento no D100 — feito em `cascata.py` — que fecha a conta.
    """
    indice = doc.indice_de_fretes()
    alteracoes: list[Alteracao] = []
    recusas: list[Recusa] = []
    tocados: list[Registro] = []

    for chave, correcoes in patch.fretes_por_documento().items():
        d100 = indice.get(chave)
        if d100 is None:
            recusas.append(Recusa(documento=chave,
                                  motivo="documento de transporte não encontrado no arquivo",
                                  correcoes=correcoes))
            continue

        analiticos = doc.filhos_reg(d100, "D190")
        if not analiticos:
            recusas.append(Recusa(documento=chave,
                                  motivo="documento de transporte sem registro D190",
                                  correcoes=correcoes))
            continue

        motivo = _validar_frete(d100, analiticos, correcoes)
        if motivo:
            recusas.append(Recusa(documento=chave, motivo=motivo,
                                  correcoes=correcoes))
            continue

        # Os alvos são resolvidos ANTES de gravar qualquer campo. Quem procura
        # o D190 pelo CFOP no meio do laço não acha mais nada depois que a
        # correção do CFOP passou por ele: as duas correções do mesmo analítico
        # apontam para o CFOP ANTIGO, e a segunda estouraria — em produção, um
        # 500 no meio do download do arquivo corrigido.
        por_cfop_original = {a.campo(3).strip(): a for a in analiticos}

        mexeu = False
        for correcao in correcoes:
            alvo = por_cfop_original.get(correcao.cfop_atual.strip())
            if alvo is None:
                continue  # a validação acima já garantiu que existe
            for nome, valor in correcao.campos.items():
                numero = CAMPOS_D190[nome]
                antes = alvo.campo(numero)
                depois = (decimal_para_texto(Decimal(str(valor)), antes)
                          if nome in CAMPOS_MONETARIOS and not isinstance(valor, str)
                          else valor_para_texto(nome, valor))
                if alvo.set(numero, depois):
                    mexeu = True
                    alteracoes.append(Alteracao(
                        documento=chave, registro="D190", linha_origem=alvo.linha,
                        campo=nome, de=antes, para=depois, motivo=correcao.motivo,
                        origem=correcao.origem or "correção aceita",
                        aprovada_por=correcao.aprovada_por))

        if mexeu:
            tocados.append(d100)

    return alteracoes, recusas, tocados
