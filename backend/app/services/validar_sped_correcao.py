"""Correção pelo Synax do que o PVA apontou — só a parte que é formato e contagem.

Pedido do Sandro em 23/10/2026: "conseguimos colocar a correção pelo sistema também?", e o escopo que ele escolheu:
"correção automática: formato e contagens, com prévia antes/depois, revalidação no PVA e os outros erros listados com
o que fazer".

O que é automático, e por quê só isso:

- **Espaço no início ou no fim do campo** (`MSG_ESPACOS_INICIO_FINAL`): tirar o espaço não muda valor fiscal nenhum.
- **Falta a quebra de linha no fim do arquivo** (`MSG_QUEBRA_ULTIMA_LINHA_INVALIDA`): o PVA nem importa o arquivo.
- **Contagens** (linhas de cada bloco, Bloco 9, total do arquivo): refeitas pelo MESMO código da Correção do SPED
  (`sped_rewriter.cascata.reconstruir_totais_de_blocos` / `reconstruir_bloco_9`), que conta o arquivo como ele é.

Todo o resto — valores, apuração, cadastros, perfil — muda imposto ou depende de informação que não está no arquivo, e
a decisão é de gente: sai como pendência, com o texto do PVA, o valor que ele espera e o que fazer (quase sempre:
corrigir no Domínio e gerar de novo). Nada aqui inventa valor.

Regras:

- **Só mexe onde o PVA apontou**, e só se a linha do arquivo for mesmo o registro que ele disse: linha que não confere
  vira pendência, nunca palpite.
- **Campo não tocado sai byte a byte** (`sped_rewriter.documento`): a cópia corrigida difere do original só no que a
  prévia mostra. O que a leitura não conseguiu manter (BOM, linha que não é registro) também aparece na prévia.
- **O original não muda**: a correção é uma cópia, que volta ao PVA como validação nova.
"""
import logging
import re
from dataclasses import asdict, dataclass, field

from . import pva_motor
from .sped_rewriter import cascata, documento

logger = logging.getLogger(__name__)

ESPACOS = "MSG_ESPACOS_INICIO_FINAL"
QUEBRA_FINAL = "MSG_QUEBRA_ULTIMA_LINHA_INVALIDA"
CONTAGENS = frozenset({
    "MSG_QTD_LIN_BLOCO0", "MSG_QTD_LIN_BLOCOB", "MSG_QTD_LIN_BLOCOC", "MSG_QTD_LIN_BLOCOD", "MSG_QTD_LIN_BLOCOE",
    "MSG_QTD_LIN_BLOCOG", "MSG_QTD_LIN_BLOCOH", "MSG_QTD_LIN_BLOCOK", "MSG_QTD_LIN_BLOCO1", "MSG_QTD_LIN_BLOCO9",
    "MSG_QTD_LIN_ARQUIVO", "MSG_QTD_REG_BLC", "MSG_QTD_REG_BLC_OBRIGATORIO",
})
AUTOMATICAS = CONTAGENS | {ESPACOS, QUEBRA_FINAL}
# O que o PVA chama de espaço e o que chega do Domínio no lugar dele (o espaço não separável, 0xA0).
BRANCOS = " \t "

DOMINIO = "Corrija no Domínio e gere o arquivo de novo."

# O que fazer, por mensagem do PVA. Orientação, não decisão: diz onde está o problema e quem resolve.
O_QUE_FAZER = {
    "MSG_EXISTE_APURACAO_IPI": (
        "O arquivo declara a empresa contribuinte de IPI, e aí o PVA exige a apuração do IPI (E500 e filhos). Ou "
        "falta apurar o IPI no Domínio, ou o cadastro da atividade está errado. " + DOMINIO),
    "MSG_VL_TOT_DEBITOS_E110_17002_V1": (
        "A apuração do ICMS (E110) não bate com a soma dos documentos. Refaça a apuração no Domínio e gere de novo: "
        "mudar o E110 muda o imposto a recolher e pede o E116 (código de receita e vencimento)."),
    "MSG_VL_TOT_CREDITOS_E110_17002_V1": (
        "A apuração do ICMS (E110) não bate com a soma dos documentos. Refaça a apuração no Domínio e gere de novo: "
        "mudar o E110 muda o imposto a recolher e pede o E116 (código de receita e vencimento)."),
    "MSG_ICMS_REC_ST_E250": (
        "A obrigação do ICMS-ST a recolher (E250) não bate com a apuração do ST. Refaça a apuração do ST no Domínio "
        "e gere de novo."),
    "MSG_DATA_MENOR_DT_FIN_0000": (
        "Data depois do fim do período. No 0205, DT_FIM é a data em que a descrição antiga do item deixou de valer: "
        "corrija no cadastro do item no Domínio (ou tire a alteração, se a descrição não mudou)."),
    "MSG_CST_ICMS_RED_BC": (
        "O CST é de redução de base (final 20 ou 70), mas o valor da redução está zerado. Informe a redução ou "
        "corrija o CST na nota. Advertência não impede a entrega."),
    "MSG_EXISTE_CEST": (
        "O CEST do item não existe na tabela. Corrija o cadastro do item no Domínio (sem CEST, deixe em branco)."),
    "MSG_REFERENCIADO_UNID_A6E1V2": (
        "Unidade no 0190 que nenhum item ou documento do arquivo usa: ela não deveria ir no arquivo. Confira no "
        "Domínio."),
    "MSG_VL_ICMS_ANALIT": (
        "O ICMS da nota (C100) não bate com a soma dos registros analíticos (C190). Confira a nota no Domínio."),
    "MSG_VL_BC_ICMS_ANALIT": (
        "A base do ICMS da nota (C100) não bate com a soma dos registros analíticos (C190). Confira a nota no Domínio."),
    "MSG_VL_MERC_C170": (
        "O valor das mercadorias da nota (C100) é menor que a soma dos itens (C170). Confira a nota no Domínio."),
    "MSG_BLOCO_COM_MOVIMENTO": (
        "O bloco diz que tem movimento, mas está vazio. Se a empresa não teve nada desse bloco no mês, gere como "
        "\"sem movimento\" no Domínio; se teve, faltam os registros."),
    "MSG_BLOCO_SEM_MOVIMENTO": (
        "O bloco diz que não tem movimento, mas tem registros. Confira no Domínio qual dos dois está certo."),
    "MSG_REGISTRO_OBRIGATORIO": (
        "Falta um registro que o perfil ou a atividade da empresa exige. " + DOMINIO),
    "MSG_CAMPO_OBRIGATORIO": "Campo obrigatório em branco. " + DOMINIO,
    "MSG_COMPARA_CNPJ_NFE_PARTICIPANTE_12001_V1": (
        "O CNPJ da chave da NF-e não é o do participante da nota. Confira o fornecedor/cliente da nota no Domínio."),
    "MSG_NAO_INFORMAR_FIN_DOCE": (
        "Este documento (C500) não leva a finalidade de NF3e. Confira o modelo e os campos da nota no Domínio."),
    "MSG_PREENCHER_CHV_DOCE": "Falta a chave do documento eletrônico (C500). Confira a nota no Domínio.",
    "MSG_COMPARA_CODIGO_MODELO": "O modelo informado não bate com o da chave do documento. Confira a nota no Domínio.",
}


@dataclass
class Mudanca:
    """Uma alteração que a correção faz: o "antes → depois" da prévia."""
    linha: int | None
    registro: str
    campo: str
    antes: str
    depois: str
    motivo: str


@dataclass
class Pendencia:
    """O que o Synax não corrige, com a mensagem do PVA e o que fazer."""
    tipo: str               # "E" ou "A"
    codigo: str
    registro: str
    campo: str
    linha: int | None
    valor: str
    esperado: str
    descricao: str
    o_que_fazer: str


@dataclass
class Proposta:
    mudancas: list[Mudanca] = field(default_factory=list)
    pendencias: list[Pendencia] = field(default_factory=list)
    resolvidos: int = 0                         # apontamentos do PVA que a correção cobre
    avisos: list[str] = field(default_factory=list)
    conteudo: bytes | None = None               # a cópia corrigida; None = nada a corrigir

    @property
    def corrige(self) -> bool:
        return bool(self.mudancas) and self.conteudo is not None

    def como_dict(self) -> dict:
        return {"mudancas": [asdict(m) for m in self.mudancas], "pendencias": [asdict(p) for p in self.pendencias],
                "resolvidos": self.resolvidos, "avisos": list(self.avisos)}


def mostrar(texto: str) -> str:
    """Para a prévia: marca só os espaços das pontas (␣ espaço, ⍽ não separável) — "CANIVETE " e
    "CANIVETE" não se distinguem a olho, e marcar os do meio poluiria o nome inteiro."""
    texto = texto or ""
    miolo = texto.strip(BRANCOS)
    if not miolo:
        return "".join("⍽" if ch == " " else "␣" for ch in texto)
    ini = texto.index(miolo)
    def marca(pedaco):
        return "".join("⍽" if ch == " " else "␣" for ch in pedaco)
    return marca(texto[:ini]) + miolo + marca(texto[ini + len(miolo):])


def numero_do_campo(campo: str) -> int | None:
    """"13 - BAIRRO" -> 13. O PVA manda o número do Guia na frente do nome."""
    m = re.match(r"\s*(\d+)\s*-", campo or "")
    return int(m.group(1)) if m else None


def o_que_fazer(item: pva_motor.Item, doc: "documento.Documento | None" = None) -> str:
    texto = O_QUE_FAZER.get(item.codigo) or DOMINIO
    if item.esperado:
        texto = f"O PVA espera {item.esperado}. " + texto
    if item.codigo == "MSG_CST_ICMS_RED_BC" and doc is not None:
        sugestao = _sugestao_red_bc(doc, item)
        if sugestao:
            texto += f" Conta de referência: valor da operação − base = {sugestao}."
    return texto


def _sugestao_red_bc(doc, item) -> str:
    """No C190/C590 a redução costuma ser VL_OPR (5) − VL_BC_ICMS (6). Só sugere: quem decide é quem conhece a nota."""
    from decimal import Decimal, InvalidOperation

    r = next((x for x in doc.registros if x.linha == item.linha), None)
    if r is None or r.reg not in ("C190", "C590") or numero_do_campo(item.campo) != 10:
        return ""
    try:
        opr = Decimal(r.campo(5).replace(".", "").replace(",", "."))
        base = Decimal(r.campo(6).replace(".", "").replace(",", "."))
    except (InvalidOperation, ValueError):
        return ""
    diferenca = opr - base
    if diferenca <= 0:
        return ""
    return f"{diferenca:.2f}".replace(".", ",")


def _pendencia(item: pva_motor.Item, texto: str) -> Pendencia:
    return Pendencia(tipo=item.tipo, codigo=item.codigo, registro=item.registro, campo=item.campo, linha=item.linha,
                     valor=item.valor, esperado=item.esperado, descricao=item.descricao, o_que_fazer=texto)


def propor(caminho: str, resultado: pva_motor.Resultado) -> Proposta:
    """O que dá para corrigir no arquivo `caminho`, dado o que o PVA disse dele. Não grava nada."""
    doc = documento.ler(caminho)
    proposta = Proposta(avisos=list(doc.avisos))
    por_linha = {r.linha: r for r in doc.registros}
    contagens: list[pva_motor.Item] = []

    for item in resultado.itens:
        if item.codigo == ESPACOS:
            r = por_linha.get(item.linha) if item.linha else None
            n = numero_do_campo(item.campo)
            if r is None or n is None or r.reg != item.registro:
                proposta.pendencias.append(_pendencia(item, (
                    "A linha que o PVA apontou não confere com o arquivo (outro registro ou campo); tire o espaço "
                    "no Domínio.")))
                continue
            antes = r.campo(n)
            depois = antes.strip(BRANCOS)
            if depois != antes:
                r.set(n, depois)
                proposta.mudancas.append(Mudanca(
                    linha=item.linha, registro=r.reg, campo=item.campo, antes=antes, depois=depois,
                    motivo="espaço no início ou no fim do campo"))
            proposta.resolvidos += 1
        elif item.codigo in CONTAGENS:
            contagens.append(item)
        else:
            proposta.pendencias.append(_pendencia(item, o_que_fazer(item, doc)))

    if contagens:
        alteracoes = []
        if doc.por_reg("9001") and doc.por_reg("9999"):
            alteracoes = cascata.reconstruir_totais_de_blocos(doc) + cascata.reconstruir_bloco_9(doc)
        if alteracoes:
            for a in alteracoes:
                proposta.mudancas.append(Mudanca(
                    linha=a.linha_origem or None, registro=a.registro, campo=a.campo, antes=a.de, depois=a.para,
                    motivo=a.motivo or "contagem refeita"))
            proposta.resolvidos += len(contagens)
        else:
            for item in contagens:
                proposta.pendencias.append(_pendencia(item, (
                    "O Synax não conseguiu refazer a contagem: o arquivo não tem a abertura ou o encerramento do Bloco 9 "
                    "(9001/9999), ou a contagem já confere com o que ele lê. " + DOMINIO)))

    _quebra_final(resultado, doc, proposta)

    if proposta.mudancas:
        # `serializar` põe o fim de linha em todo registro, inclusive no último: é isso que cura a quebra final.
        proposta.conteudo = doc.serializar().encode(documento.ENCODING)
    return proposta


def _quebra_final(resultado: pva_motor.Resultado, doc, proposta: Proposta) -> None:
    """A quebra de linha que falta no fim do arquivo. O PVA recusa antes de importar (sem lista de erros) e diz o
    motivo em `falha`/`mensagens`; e, mesmo sem ele dizer, a cópia gravada sai com a quebra (`serializar` põe fim de
    linha em todo registro) — então ela aparece na prévia sempre que acontece."""
    textos = " ".join([resultado.falha or "", *resultado.mensagens])
    apontada = QUEBRA_FINAL in textos
    if doc.registros and not doc.registros[-1].fim and (apontada or proposta.mudancas):
        ultimo = doc.registros[-1]
        proposta.mudancas.append(Mudanca(
            linha=ultimo.linha, registro=ultimo.reg, campo="fim da linha", antes="(sem quebra de linha)",
            depois="quebra de linha", motivo="a última linha do arquivo não terminava com quebra de linha"))
        if apontada:
            proposta.resolvidos += 1
        return
    if resultado.falha and not resultado.itens and not apontada:
        # Recusado antes de importar por outro motivo: não há o que corrigir aqui, mas a pendência aparece.
        proposta.pendencias.append(Pendencia(
            tipo="E", codigo="", registro="", campo="", linha=None, valor="", esperado="",
            descricao=resultado.falha, o_que_fazer=DOMINIO))
