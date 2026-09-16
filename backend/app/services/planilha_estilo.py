"""Estilo único de todas as planilhas do Synax.

Existiam quatro geradores de Excel no sistema e cada um saía de um jeito: três
despejavam o DataFrame cru (cabeçalho preto sobre branco, coluna estreita
cortando o texto, número sem formato) e um tinha estilo próprio, escrito à mão.
Aqui fica o padrão, e todos passam por ele.

Não é só enfeite. Três coisas que este módulo conserta estragavam o trabalho:

1. **Chave de acesso e CNPJ viravam número.** Uma chave de 44 dígitos numa
   coluna sem formato de texto aparece como `4,35E+43` no Excel, e um CNPJ que
   começa com zero perde o zero. Quem abria a planilha para conferir a nota não
   achava a nota. Toda coluna de chave, CNPJ, CFOP, CST, NCM e CEST sai como
   TEXTO — inclusive para o CST "00" não virar "0".

2. **Valor sem formato de moeda.** `1234.5` numa coluna de ICMS é um número que
   ninguém soma de cabeça; `R$ 1.234,50` é. Colunas de dinheiro, de percentual e
   de contagem ganham o formato certo pelo NOME da coluna.

3. **Cabeçalho rolava junto com os dados.** Numa aba de 4.000 linhas, rolar
   trinta linhas já era perder de vista qual coluna é qual. Agora o cabeçalho é
   fixo e tem filtro.

A paleta é a mesma da tela (frontend/templates/layout/base.html): quem recebe a
planilha por e-mail reconhece que veio do mesmo lugar.
"""
import re

from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ------------------------------------------------------------------ paleta
AZUL_ESCURO = "026092"   # --syn-900: títulos
AZUL = "0377AA"          # --syn-700: faixa do cabeçalho
AZUL_CLARO = "DBF1F9"    # fundo da capa
ZEBRA = "F1F8FC"         # listra das linhas pares
CINZA_BORDA = "BFDDEC"
AMBAR = "FEF3C7"         # linha que precisa de atenção
VERMELHO = "B91C1C"      # aba de divergência
VERDE = "047857"         # aba informativa/ok

# ------------------------------------------------------- formatos numéricos
MOEDA = 'R$ #,##0.00'
PERCENTUAL = '0.00"%"'
TEXTO = "@"
INTEIRO = "#,##0"

# Tokens curtos precisam casar como PALAVRA: "st" não pode casar dentro de
# "custo", nem "uf" dentro de qualquer coisa.
_CURTOS = {"uf", "st", "bc", "vl", "ipi", "pis", "cst", "ncm", "nf", "ir", "csll"}

# A ordem das listas importa: percentual é testado antes de moeda porque
# "Alíquota de ICMS" contém "icms" e não é dinheiro.
_COLUNAS_TEXTO = (
    "cnpj", "cpf", "chave", "codigo", "código", "cfop", "cst", "ncm", "cest",
    "nº", "n°", "uf", "serie", "série", "inscri", "municipio", "município",
)
_COLUNAS_PERCENTUAL = (
    "aliq", "alíq", "%", "mva", "reduc", "reduç", "percent",
)
_COLUNAS_MOEDA = (
    "valor", "vl", "icms", "ipi", "pis", "cofins", "st", "fcp", "base", "bc",
    "credito", "crédito", "total", "frete", "diferenc", "diferenç", "r$",
    "retenc", "retenç", "ir", "csll", "mono", "difal", "antecip",
)
_COLUNAS_INTEIRO = ("quantidade", "qtd", "notas processadas", "inconsist", "avisos")


def _casa(titulo: str, tokens) -> bool:
    for token in tokens:
        if token in _CURTOS:
            if re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", titulo):
                return True
        elif token in titulo:
            return True
    return False


def formato_da_coluna(titulo) -> str | None:
    """Formato de célula deduzido do NOME da coluna. None = deixa como está.

    Pelo nome, e não pelo tipo do dado, de propósito: uma coluna de CNPJ chega
    como texto às vezes e como número outras, dependendo de quem preencheu — e é
    justamente a que vira notação científica quando o Excel decide sozinho.
    """
    t = str(titulo or "").strip().lower()
    if not t:
        return None
    if _casa(t, _COLUNAS_TEXTO):
        return TEXTO
    if _casa(t, _COLUNAS_PERCENTUAL):
        return PERCENTUAL
    if _casa(t, _COLUNAS_INTEIRO):
        return INTEIRO
    if _casa(t, _COLUNAS_MOEDA):
        return MOEDA
    return None


# ------------------------------------------------------------------ estilo

def estilizar_aba(ws, cor_aba: str = None, largura_minima: int = 10,
                  largura_maxima: int = 55, amostra: int = 400,
                  zebra: bool = True) -> None:
    """Aplica o padrão do Synax numa aba já preenchida (cabeçalho na linha 1).

    `amostra` limita quantas linhas são lidas para calcular a largura das
    colunas: numa aba de 4.000 linhas, medir todas custa mais do que o ganho —
    as primeiras centenas já dão a régua.

    `zebra=False` é OBRIGATÓRIO nas abas que usam `destacar_linhas`. A listra é
    formatação condicional, e no Excel a condicional vence o preenchimento
    direto da célula: numa linha par, a faixa azul cobriria o âmbar do destaque
    e o aviso sumiria justamente na linha que precisava dele.
    """
    if ws.max_row < 1 or ws.max_column < 1:
        return

    ultima_coluna = ws.max_column
    ultima_linha = ws.max_row

    fonte_cabecalho = Font(color="FFFFFF", bold=True, size=11)
    fundo_cabecalho = PatternFill("solid", start_color=AZUL, end_color=AZUL)
    centro = Alignment(horizontal="center", vertical="center", wrap_text=True)
    linha_fina = Side(style="thin", color=CINZA_BORDA)

    for coluna in range(1, ultima_coluna + 1):
        celula = ws.cell(row=1, column=coluna)
        celula.font = fonte_cabecalho
        celula.fill = fundo_cabecalho
        celula.alignment = centro
        celula.border = Border(bottom=linha_fina)
    ws.row_dimensions[1].height = 30

    # Cabeçalho fixo e filtro: sem isto, uma aba de milhares de linhas obriga a
    # rolar de volta ao topo toda vez que se perde a coluna.
    ws.freeze_panes = "A2"
    if ultima_linha >= 1:
        ws.auto_filter.ref = (f"A1:{get_column_letter(ultima_coluna)}"
                              f"{max(ultima_linha, 2)}")

    limite = min(ultima_linha, amostra + 1)
    for coluna in range(1, ultima_coluna + 1):
        letra = get_column_letter(coluna)
        titulo = ws.cell(row=1, column=coluna).value
        formato = formato_da_coluna(titulo)

        maior = len(str(titulo or ""))
        for linha in range(2, limite + 1):
            valor = ws.cell(row=linha, column=coluna).value
            if valor is not None:
                maior = max(maior, len(str(valor)))
        # +4 para a seta do filtro não cobrir a última letra do cabeçalho.
        ws.column_dimensions[letra].width = max(
            largura_minima, min(largura_maxima, maior + 4))

        if not formato:
            continue
        alinhamento = (Alignment(horizontal="left") if formato == TEXTO
                       else Alignment(horizontal="right"))
        for linha in range(2, ultima_linha + 1):
            celula = ws.cell(row=linha, column=coluna)
            celula.number_format = formato
            celula.alignment = alinhamento

    # Listra por regra condicional, não célula a célula: numa aba grande são
    # dezenas de milhares de células, e a regra custa uma linha de XML.
    if zebra and ultima_linha > 1:
        faixa = f"A2:{get_column_letter(ultima_coluna)}{ultima_linha}"
        ws.conditional_formatting.add(faixa, FormulaRule(
            formula=["MOD(ROW(),2)=0"],
            fill=PatternFill("solid", start_color=ZEBRA, end_color=ZEBRA),
            stopIfTrue=False))

    if cor_aba:
        ws.sheet_properties.tabColor = cor_aba


def destacar_linhas(ws, coluna_titulo: str, condicao, cor: str = AMBAR) -> None:
    """Pinta a linha inteira quando `condicao(valor da coluna)` é verdadeira.

    Serve para o que não pode passar batido numa lista longa — a nota de
    devolução na aba de IPI, o CFOP presumido na de NFS-e. A tela já destaca
    essas linhas; a planilha precisa destacar também, senão a informação se
    perde justamente no formato em que o trabalho é feito.

    A coluna é achada pelo TÍTULO, não pela posição: as abas são montadas a
    partir de dicionários e a ordem das colunas muda quando um campo novo entra.
    Título que não existe simplesmente não destaca nada — nunca levanta.
    """
    if ws.max_row < 2:
        return

    indice = None
    alvo = coluna_titulo.strip().lower()
    for coluna in range(1, ws.max_column + 1):
        if str(ws.cell(row=1, column=coluna).value or "").strip().lower() == alvo:
            indice = coluna
            break
    if indice is None:
        return

    fundo = PatternFill("solid", start_color=cor, end_color=cor)
    for linha in range(2, ws.max_row + 1):
        try:
            marcar = bool(condicao(ws.cell(row=linha, column=indice).value))
        except Exception:  # noqa: BLE001 — destaque é enfeite, não pode derrubar
            marcar = False
        if marcar:
            for coluna in range(1, ws.max_column + 1):
                ws.cell(row=linha, column=coluna).fill = fundo


def montar_capa(wb, titulo: str, linhas, posicao: int = 0, subtitulo: str = ""):
    """Primeira aba: quem é a empresa, de quando é o arquivo e o que tem dentro.

    `linhas` é uma lista de (rótulo, valor). Números saem à direita, com
    formato; o resto à esquerda.
    """
    ws = wb.create_sheet(title="Visão Geral", index=posicao)
    ws.sheet_properties.tabColor = AZUL_ESCURO
    ws.sheet_view.showGridLines = False

    ws.merge_cells("A1:C1")
    c = ws["A1"]
    c.value = titulo
    c.font = Font(color=AZUL_ESCURO, bold=True, size=18)
    c.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 34

    linha_atual = 2
    if subtitulo:
        ws.merge_cells("A2:C2")
        s = ws["A2"]
        s.value = subtitulo
        s.font = Font(color="64748B", size=11)
        linha_atual = 3

    linha_atual += 1
    fundo = PatternFill("solid", start_color=AZUL_CLARO, end_color=AZUL_CLARO)
    for rotulo, valor in linhas:
        rot = ws.cell(row=linha_atual, column=1, value=rotulo)
        rot.font = Font(bold=True, color=AZUL_ESCURO, size=11)
        rot.fill = fundo
        rot.alignment = Alignment(horizontal="left", vertical="center")

        val = ws.cell(row=linha_atual, column=2, value=valor)
        val.font = Font(size=11)
        if isinstance(valor, (int, float)) and not isinstance(valor, bool):
            val.number_format = INTEIRO if float(valor).is_integer() else MOEDA
            val.alignment = Alignment(horizontal="right", vertical="center")
        else:
            val.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[linha_atual].height = 20
        linha_atual += 1

    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 46
    return ws


def estilizar_planilha(wb, cores: dict = None, pular=("Visão Geral",),
                       sem_zebra=()) -> None:
    """Passa `estilizar_aba` em todas as abas do workbook.

    `cores` mapeia nome da aba -> cor da guia, para a pessoa achar a aba de
    divergência sem ler o nome de todas. `sem_zebra` lista as abas em que
    alguma linha é destacada com preenchimento próprio — ver `estilizar_aba`.
    """
    cores = cores or {}
    for ws in wb.worksheets:
        if ws.title in pular:
            continue
        estilizar_aba(ws, cor_aba=cores.get(ws.title),
                      zebra=ws.title not in sem_zebra)
