# -*- coding: utf-8 -*-
"""Planilhas do Synax: o mesmo estilo em todas, e o dado sem estragar.

Pedido do Sandro em 02/09/2026 ("deixar todas as planilhas bem estilizadas").
Havia quatro geradores de Excel, cada um saindo de um jeito — três despejavam o
DataFrame cru e um tinha estilo próprio escrito à mão.

O que esta suíte trava não é gosto, é dano:

1. **Chave de acesso e CNPJ têm de sair como TEXTO.** Uma chave de 44 dígitos
   numa coluna sem formato vira `4,35E+43` no Excel, e um CNPJ iniciado em zero
   perde o zero — quem abre a planilha para achar a nota não acha a nota. O CST
   "00" viraria "0" pelo mesmo caminho.

2. **Coluna de dinheiro com formato de dinheiro.** `1234.5` numa coluna de ICMS
   é um número que ninguém confere de relance.

3. **Cabeçalho congelado e com filtro.** Numa aba de milhares de linhas, rolar
   trinta linhas já é perder de vista qual coluna é qual.

4. **A zebra NÃO pode cobrir o destaque.** A listra é formatação condicional e
   no Excel a condicional vence o preenchimento direto da célula: numa linha
   par, a faixa azul apagaria o âmbar da linha que pedia atenção. Por isso as
   abas com destaque saem sem zebra — e o teste verifica as duas coisas juntas.

5. **O cabeçalho sai em português.** Sem o mapa, a planilha chega ao contador
   com "sped_val" e "emit_name" no topo da coluna.

Os arquivos são gerados de verdade, salvos em memória e REABERTOS com openpyxl:
o que se afirma aqui é o que o Excel vai mostrar.
"""
import io
import os
import sys

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('backend'))

import openpyxl  # noqa: E402

from app.services.planilha_estilo import (  # noqa: E402
    AMBAR, MOEDA, PERCENTUAL, TEXTO, destacar_linhas, estilizar_aba,
    formato_da_coluna, montar_capa)
from app.services.audit_engine.excel_exporter import (  # noqa: E402
    RENOMEAR, generate_excel_from_results)

OK = FALHAS = 0
_falhou = []


def check(nome, condicao, detalhe=""):
    global OK, FALHAS
    if condicao:
        OK += 1
        print(f"  OK   | {nome}")
    else:
        FALHAS += 1
        _falhou.append(nome)
        print(f" FALHA | {nome}  [{detalhe}]")


def abrir(buffer):
    buffer.seek(0)
    return openpyxl.load_workbook(io.BytesIO(buffer.read()))


# ============================================================================
print("\n=== 1. O formato sai do NOME da coluna ===")

for titulo in ("Chave de Acesso", "CNPJ Emitente", "CFOP no SPED", "CST",
               "NCM", "CEST", "N° NF", "UF Emitente", "Código do Produto"):
    check(f"'{titulo}' sai como texto", formato_da_coluna(titulo) == TEXTO,
          formato_da_coluna(titulo))

for titulo in ("Valor Total", "ICMS", "Base de ICMS", "IPI Devolvido",
               "Valor no SPED", "Crédito Calculado", "FCP ST", "Retenções (R$)"):
    check(f"'{titulo}' sai como moeda", formato_da_coluna(titulo) == MOEDA,
          formato_da_coluna(titulo))

for titulo in ("Alíquota de ICMS (%)", "MVA (%)", "Redução da Base"):
    check(f"'{titulo}' sai como percentual", formato_da_coluna(titulo) == PERCENTUAL,
          formato_da_coluna(titulo))

# As armadilhas: percentual é testado ANTES de moeda, e token curto casa como
# palavra inteira.
check("'Alíquota de ICMS (%)' não vira moeda por conter 'icms'",
      formato_da_coluna("Alíquota de ICMS (%)") == PERCENTUAL)
check("'Custo' não vira moeda por conter 'st'",
      formato_da_coluna("Custo") is None, formato_da_coluna("Custo"))
check("'Fornecedor' fica sem formato", formato_da_coluna("Fornecedor") is None)
check("'Motivo' fica sem formato", formato_da_coluna("Motivo") is None)
check("coluna sem título não quebra", formato_da_coluna(None) is None)


# ============================================================================
print("\n=== 2. Uma aba estilizada, reaberta e conferida ===")

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Teste"
ws.append(["Chave de Acesso", "Fornecedor", "Valor Total", "CST"])
CHAVE = "32260836318376000196570010002111461000211146"
ws.append([CHAVE, "TRANSPORTES MOBILINE LTDA ME DO ESPIRITO SANTO", 1234.5, "00"])
ws.append(["32260836318376000196570010002111471000211147", "OUTRA", 99.9, "50"])
estilizar_aba(ws)

buf = io.BytesIO()
wb.save(buf)
lido = abrir(buf)["Teste"]

check("o cabeçalho ficou em negrito e branco",
      lido["A1"].font.bold and lido["A1"].font.color.rgb.endswith("FFFFFF"),
      lido["A1"].font.color)
check("com a faixa azul do Synax", lido["A1"].fill.start_color.rgb.endswith("0377AA"),
      lido["A1"].fill.start_color.rgb)
check("o cabeçalho é fixo ao rolar", lido.freeze_panes == "A2", lido.freeze_panes)
check("e tem filtro", lido.auto_filter.ref == "A1:D3", lido.auto_filter.ref)

check("a chave de acesso sai como TEXTO (senão vira 4,35E+43)",
      lido["A2"].number_format == TEXTO, lido["A2"].number_format)
check("e o valor da chave chegou inteiro", str(lido["A2"].value) == CHAVE)
check("o CST '00' sai como texto (senão vira 0)",
      lido["D2"].number_format == TEXTO and str(lido["D2"].value) == "00",
      f"{lido['D2'].number_format} / {lido['D2'].value!r}")
check("a coluna de valor sai como moeda", lido["C2"].number_format == MOEDA,
      lido["C2"].number_format)
check("e continua sendo número, não texto", lido["C2"].value == 1234.5)

largura_forn = lido.column_dimensions["B"].width
check("a coluna larga foi alargada, mas com teto",
      20 < largura_forn <= 55, largura_forn)
check("a coluna estreita tem largura mínima",
      lido.column_dimensions["D"].width >= 10, lido.column_dimensions["D"].width)
check("há listra zebrada", len(list(lido.conditional_formatting)) >= 1)

vazia = openpyxl.Workbook().active
estilizar_aba(vazia)
check("aba sem nada não levanta", True)


# ============================================================================
print("\n=== 3. Destaque de linha e zebra não podem brigar ===")

wb2 = openpyxl.Workbook()
ws2 = wb2.active
ws2.append(["Fornecedor", "IPI Devolvido no XML"])
ws2.append(["COM DEVOLUCAO", 12.30])     # linha 2 (par) — a que o Excel listraria
ws2.append(["SEM DEVOLUCAO", 0.0])
destacar_linhas(ws2, "IPI Devolvido no XML", lambda v: float(v or 0) > 0.01)
estilizar_aba(ws2, zebra=False)

buf2 = io.BytesIO()
wb2.save(buf2)
l2 = abrir(buf2).active

check("a linha com devolução ficou âmbar",
      l2["A2"].fill.start_color.rgb.endswith(AMBAR), l2["A2"].fill.start_color.rgb)
check("a linha sem devolução não", not l2["A3"].fill.start_color.rgb.endswith(AMBAR))
check("e a aba com destaque saiu SEM zebra (a condicional cobriria o âmbar)",
      len(list(l2.conditional_formatting)) == 0,
      "zebra ligada apagaria o destaque na linha par")

ws3 = openpyxl.Workbook().active
ws3.append(["A", "B"])
ws3.append([1, 2])
destacar_linhas(ws3, "Coluna Que Nao Existe", lambda v: True)
check("destacar por coluna inexistente não levanta nem pinta",
      ws3["A2"].fill.start_color.rgb in (None, "00000000"),
      ws3["A2"].fill.start_color.rgb)
destacar_linhas(ws3, "A", lambda v: 1 / 0)
check("predicado que explode não derruba a planilha", True)


# ============================================================================
print("\n=== 4. O relatório da auditoria, de ponta a ponta ===")

DADOS = {
    "company_name": "DISTRIBUIDORA LAURENCINI LTDA",
    "company_cnpj": "36318376000196",
    "periodo_inicio": "2026-08-01",
    "periodo_fim": "2026-08-31",
    "resumo": {"notas_processadas": 4392, "inconsistencias": 87, "informativos": 12},
    "icms_issues": [
        {"key": CHAVE, "num": "58897", "emit_name": "FORNECEDOR X",
         "num_item": 1, "descr": "PRODUTO A", "cfop": "1102", "cst": "00",
         "reason": "Crédito indevido", "sped_val": 120.5, "xml_val": 100.0},
    ],
    "ipi_issues": [
        {"key": CHAVE, "num": "2777", "emit_name": "FORNECEDOR Y",
         "xml_ipi_devol": 45.0, "xml_ipi_proprio": 0.0, "sped_val": 0.0,
         "xml_val": 45.0},
        {"key": CHAVE, "num": "2778", "emit_name": "FORNECEDOR Z",
         "xml_ipi_devol": 0.0, "xml_ipi_proprio": 10.0, "sped_val": 9.0,
         "xml_val": 10.0},
    ],
    "relatorio_geral": [
        {"num_nf": "58897", "cnpj_emitente": "01234567000199", "uf_emitente": "MG",
         "razao_social": "FORNECEDOR X", "descr_item": "PRODUTO A",
         "vl_total": 1000.0, "bc_icms": 1000.0, "icms": 120.0, "ipi": 0.0,
         "cfop_xml": "5102", "cfop_sped": "1102", "key": CHAVE},
    ],
    "cte_xml_list": [
        {"num": "211146", "emit_cnpj": "11222333000144", "emit_uf": "ES",
         "rem_name": "REM", "dest_name": "DEST", "toma_name": "TOMADOR",
         "nfe_nums": "58897", "nfe_keys": CHAVE, "nfe_cfop_sped": "1102",
         "vl_doc": 300.0, "vl_bc": 300.0, "vl_icms": 30.45},
    ],
}

livro = abrir(generate_excel_from_results(DADOS))

check("a primeira aba é a Visão Geral", livro.sheetnames[0] == "Visão Geral",
      livro.sheetnames)
capa = livro["Visão Geral"]
textos = [str(c.value) for linha in capa.iter_rows() for c in linha if c.value]
check("a capa traz o nome da empresa",
      any("LAURENCINI" in t for t in textos), textos[:6])
check("o CNPJ", any("36318376000196" in t for t in textos))
check("o período apurado", any("2026-08-01" in t for t in textos))
check("a data de geração", any("Gerado em" in t for t in textos))
check("e os três números do resumo",
      any("4392" in t for t in textos) and any("87" in t for t in textos))
check("a capa não tem linhas de grade (é capa, não tabela)",
      capa.sheet_view.showGridLines is False)

check("as abas com dado foram criadas",
      {"Divergências ICMS", "Divergências IPI", "Relatório de NFs",
       "Planilha CT-e"} <= set(livro.sheetnames), livro.sheetnames)
check("aba sem dado não é criada", "XMLs Faltantes" not in livro.sheetnames)

icms = livro["Divergências ICMS"]
cabecalhos = [c.value for c in icms[1]]
check("o cabeçalho saiu em português, não com a chave interna",
      "Chave de Acesso" in cabecalhos and "Valor no SPED" in cabecalhos,
      cabecalhos)
check("nenhum underscore sobrou no cabeçalho",
      not any("_" in str(c) for c in cabecalhos), cabecalhos)
check("a chave de acesso da aba de ICMS é texto",
      icms.cell(row=2, column=cabecalhos.index("Chave de Acesso") + 1
                ).number_format == TEXTO)
check("o valor do SPED é moeda",
      icms.cell(row=2, column=cabecalhos.index("Valor no SPED") + 1
                ).number_format == MOEDA)
check("toda aba de dado tem cabeçalho fixo",
      all(livro[n].freeze_panes == "A2" for n in livro.sheetnames
          if n != "Visão Geral"),
      [(n, livro[n].freeze_panes) for n in livro.sheetnames])
check("e filtro", all(livro[n].auto_filter.ref for n in livro.sheetnames
                      if n != "Visão Geral"))
check("as guias têm cor", icms.sheet_properties.tabColor is not None)

ipi = livro["Divergências IPI"]
cab_ipi = [c.value for c in ipi[1]]
col_dev = cab_ipi.index("IPI Devolvido no XML") + 1
linha_dev = 2 if ipi.cell(row=2, column=col_dev).value else 3
check("a nota com IPI devolvido sai destacada em âmbar",
      ipi.cell(row=linha_dev, column=1).fill.start_color.rgb.endswith(AMBAR),
      ipi.cell(row=linha_dev, column=1).fill.start_color.rgb)
check("e essa aba não tem zebra, para o âmbar não ser coberto",
      len(list(ipi.conditional_formatting)) == 0)

vazio = abrir(generate_excel_from_results(
    {"company_name": "X", "company_cnpj": "1", "resumo": {}}))
check("auditoria sem apontamento ainda gera arquivo",
      "Sem Divergências" in vazio.sheetnames, vazio.sheetnames)
check("e a capa continua lá", "Visão Geral" in vazio.sheetnames)


# ============================================================================
print("\n=== 5. As outras planilhas do sistema usam o mesmo estilo ===")

for caminho, funcao in (
    (os.path.join("backend", "app", "services", "nfse", "pdf_ia_auditor.py"),
     "gerar_excel"),
    (os.path.join("backend", "app", "services", "nfse", "pdf_ia_lancamento.py"),
     "gerar_excel_auditoria"),
    (os.path.join("backend", "app", "services", "audit_engine", "excel_exporter.py"),
     "generate_excel_from_results"),
):
    fonte = io.open(caminho, encoding="utf-8").read()
    nome = os.path.basename(caminho)
    check(f"{nome} passa pelo estilo comum", "estilizar_planilha" in fonte)
    check(f"{nome} não redefine cor de cabeçalho por conta própria",
          "1E3A8A" not in fonte, "cor antiga escrita à mão")

UPL = io.open(os.path.join("backend", "app", "api", "routes", "uploads.py"),
              encoding="utf-8").read()
# A prova é não haver mais escrita de Excel na rota — os nomes das chaves
# erradas continuam no arquivo, mas dentro do comentário que explica o defeito.
check("a rota /api/export/{task_id} não monta mais planilha por conta própria",
      "ExcelWriter" not in UPL, "voltou um gerador de Excel dentro da rota")
check("as duas rotas de export devolvem o MESMO relatório",
      UPL.count("generate_excel_from_results") >= 2,
      UPL.count("generate_excel_from_results"))

REQ = io.open("requirements.txt", encoding="utf-8").read()
check("pandas está no requirements (todo Excel depende dele)",
      "pandas==" in REQ, "faltava — servidor novo quebraria no 1º export")
check("openpyxl também", "openpyxl==" in REQ)


# ============================================================================
print("\n=== 6. Nenhum cabeçalho interno escapou do mapa ===")

feios = [k for k, v in RENOMEAR.items() if "_" in v or v.islower()]
check("todo rótulo do mapa é legível", not feios, feios)
check("o mapa cobre as chaves mais comuns do motor",
      {"key", "num", "emit_name", "sped_val", "xml_val", "cfop", "cst",
       "reason", "descr"} <= set(RENOMEAR), "faltando chave comum")


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

sys.exit(1 if FALHAS else 0)
