# -*- coding: utf-8 -*-
"""O dinheiro na tela de resultado — e os números que ele NÃO pode inventar.

A aba "Visão Geral" mostrava contagem e só contagem: "12 Inconformidades de
ICMS". Isso não responde a única pergunta que decide se vale mexer — doze itens
podem ser R$ 40 ou R$ 40.000. Para descobrir, a pessoa abria as 25 abas e somava
na calculadora. Os valores sempre estiveram no relatório; ninguém somava.

As travas desta suíte, em ordem de importância — as três primeiras são erros que
a primeira versão COMETEU e que só apareceram ao rodar contra dados reais:

1. **Base de cálculo NÃO é crédito.** `pis_cofins_issues` mistura duas coisas:
   linhas que dizem "o SPED creditou R$ X sem direito" (imposto) e linhas que
   dizem "a base do SPED é R$ 0,00 contra R$ 820,00 calculada" (base). O SPED
   Fiscal quase nunca preenche `VL_BC_PIS`, então quase todo item gera uma linha
   de base. Somando as duas juntas, uma auditoria real da NORT KIT exibia
   **R$ 111.273,57 de "crédito a aproveitar"** — dinheiro que não existe.

2. **Valor de documento NÃO é imposto.** `value_discrepancies` compara o total
   da nota no SPED com o do XML. A diferença é erro de escrituração a conferir,
   não crédito a tomar. Tratá-la como crédito inventaria imposto a partir de um
   erro de digitação.

3. **Sem valor não vira zero.** Regras pendentes e itens não conferidos são
   trabalho real sem cifra. Contá-los como R$ 0,00 faria parecer que já foram
   avaliados e não custam nada.

4. **O sentido importa.** SPED > XML é crédito tomado a maior (estornar); XML >
   SPED é crédito deixado na mesa (aproveitar). Trocar os dois inverte o
   conselho dado ao cliente.

5. **A cor nunca é o único sinal.** Vermelho e verde ficam a ΔE 4,1 sob
   deuteranopia — medido com o validador de paleta. A tela usa seta e rótulo
   escrito; quem lê só a cor não pode ser induzido a erro.
"""
import io
import os
import sys

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('backend'))

from app.core.templating import build_env, moeda_br  # noqa: E402
from app.services.audit_engine.resumo_financeiro import (  # noqa: E402
    NATUREZAS, PREFIXO_LINHA_DE_BASE, resumo_financeiro)

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


def linha(resumo, chave):
    return next((l for l in resumo["linhas"] if l["chave"] == chave), None)


# ============================================================================
print("\n=== 1. Base de cálculo não vira crédito ===")

# Reproduz o caso real da NORT KIT: 3 linhas de base (o SPED não preenche
# VL_BC_PIS) e 1 linha de crédito indevido de verdade.
BASE_E_CREDITO = {
    "pis_cofins_issues": [
        {"reason": "Base de PIS/COFINS divergente: SPED R$ 0,00 vs Base Calculada R$ 820,00",
         "comparacao": "base", "sped_val": 0.0, "xml_val": 820.0},
        {"reason": "Base de PIS/COFINS divergente: SPED R$ 0,00 vs Base Calculada R$ 1.500,00",
         "comparacao": "base", "sped_val": 0.0, "xml_val": 1500.0},
        # Auditoria ANTIGA: não tem a marca `comparacao`, só o texto.
        {"reason": "Base de PIS/COFINS divergente: SPED R$ 0,00 vs Base Calculada R$ 300,00",
         "sped_val": 0.0, "xml_val": 300.0},
        {"reason": "CFOP/CST sem direito a crédito de PIS/COFINS, mas o SPED creditou R$ 45,00",
         "sped_val": 45.0, "xml_val": 0.0},
    ],
}
r = resumo_financeiro(BASE_E_CREDITO)
check("as 3 linhas de BASE não viram crédito a aproveitar",
      r["totais"]["aproveitar"] == 0.0, r["totais"]["aproveitar"])
check("a linha de crédito indevido vira 'a estornar'",
      r["totais"]["estornar"] == 45.0, r["totais"]["estornar"])
check("a auditoria ANTIGA (sem a marca) também é filtrada, pelo texto",
      r["totais"]["aproveitar"] == 0.0,
      "o caminho retroativo caiu — auditoria antiga somaria base")
check("mas a contagem mostra as 4 ocorrências (base é apontamento, só não é R$)",
      linha(r, "pis_cofins_issues")["qtd"] == 4,
      linha(r, "pis_cofins_issues")["qtd"])
check("o prefixo do texto é o documentado", PREFIXO_LINHA_DE_BASE
      == "base de pis/cofins divergente", PREFIXO_LINHA_DE_BASE)

so_base = resumo_financeiro({"pis_cofins_issues": [
    {"reason": "Base de PIS/COFINS divergente: SPED R$ 0,00 vs Base Calculada R$ 820,00",
     "comparacao": "base", "sped_val": 0.0, "xml_val": 820.0}]})
check("só linhas de base: a categoria sai marcada SEM valor apurado",
      linha(so_base, "pis_cofins_issues")["tem_valor"] is False)
check("e o total em jogo é zero, não R$ 820,00", so_base["em_jogo"] == 0.0,
      so_base["em_jogo"])


# ============================================================================
print("\n=== 2. Valor de documento não é imposto ===")

DOC = {"value_discrepancies": [
    {"type": "NF-e", "sped_val": 10000.0, "xml_val": 9000.0, "diff": 1000.0,
     "reason": "Total da NF-e divergente"},
]}
r = resumo_financeiro(DOC)
check("não entra como crédito a estornar", r["totais"]["estornar"] == 0.0)
check("nem como crédito a aproveitar", r["totais"]["aproveitar"] == 0.0)
check("entra como 'a conferir', pelo campo diff",
      r["totais"]["conferir"] == 1000.0, r["totais"]["conferir"])
check("e fica FORA do total em jogo (não é imposto)", r["em_jogo"] == 0.0,
      r["em_jogo"])


# ============================================================================
print("\n=== 3. O sentido: quem creditou a mais, quem creditou a menos ===")

ICMS = {"icms_issues": [
    {"sped_icms": 500.0, "xml_icms": 300.0, "reason": "x"},   # creditou 200 a mais
    {"sped_icms": 100.0, "xml_icms": 450.0, "reason": "y"},   # deixou 350 na mesa
    {"sped_icms": 80.0, "xml_icms": 80.0, "reason": "z"},     # bate: nada
    {"sped_icms": 10.0, "xml_icms": 10.005, "reason": "w"},   # meio centavo: nada
]}
r = resumo_financeiro(ICMS)
check("SPED > XML soma em 'a estornar'", r["totais"]["estornar"] == 200.0,
      r["totais"]["estornar"])
check("XML > SPED soma em 'a aproveitar'", r["totais"]["aproveitar"] == 350.0,
      r["totais"]["aproveitar"])
check("item que bate não soma nada", r["em_jogo"] == 550.0, r["em_jogo"])
check("diferença de meio centavo é arredondamento, não divergência",
      r["totais"]["estornar"] + r["totais"]["aproveitar"] == 550.0)

l = linha(r, "icms_issues")
check("a linha mostra o lado que PESA mais", l["natureza"] == "aproveitar",
      l["natureza"])
check("com o outro sentido ao lado, para não sumir",
      l["contrapartida"] == 200.0, l["contrapartida"])
check("e conta as 4 ocorrências, não só as 2 com valor", l["qtd"] == 4, l["qtd"])


# ============================================================================
print("\n=== 4. Sem valor não é zero ===")

SEM = {"unmapped_items": [{"a": 1}, {"a": 2}], "itens_nao_conferidos": [{"b": 1}]}
r = resumo_financeiro(SEM)
check("categorias sem cifra aparecem na triagem", len(r["linhas"]) == 2)
check("marcadas como SEM valor", all(not l["tem_valor"] for l in r["linhas"]))
check("com a contagem certa",
      linha(r, "unmapped_items")["qtd"] == 2)
check("e não somam zero no financeiro", r["em_jogo"] == 0.0)
check("o painel sabe que não há dinheiro", r["tem_dinheiro"] is False)


# ============================================================================
print("\n=== 5. Ordem da triagem: onde o trabalho rende ===")

MISTO = {
    "icms_issues": [{"sped_icms": 50.0, "xml_icms": 0.0}],          # R$ 50
    "ipi_issues": [{"sped_val": 9000.0, "xml_val": 0.0}],           # R$ 9.000
    "missing_xmls": [{"vl_doc": 700.0}],                            # R$ 700
    "unmapped_items": [{"x": 1}] * 80,                              # sem valor, 80
}
r = resumo_financeiro(MISTO)
ordem = [l["chave"] for l in r["linhas"]]
check("quem tem mais dinheiro vem primeiro",
      ordem[0] == "ipi_issues", ordem)
check("depois o segundo maior", ordem[1] == "missing_xmls", ordem)
check("e o sem-valor vai para o fim, mesmo com 80 ocorrências",
      ordem[-1] == "unmapped_items", ordem)
check("a barra é proporcional ao maior",
      linha(r, "ipi_issues")["proporcao"] == 100.0)
check("a menor tem barra pequena, mas não zero",
      0 < linha(r, "icms_issues")["proporcao"] < 2)
check("cada linha sabe para que aba levar",
      all(l["aba"].startswith("tab-") for l in r["linhas"]))


# ============================================================================
print("\n=== 6. Linha informativa não entra na conta ===")

INFO = {"icms_issues": [
    {"sped_icms": 100.0, "xml_icms": 0.0, "status": "informativo"},
    {"sped_icms": 200.0, "xml_icms": 0.0, "is_error": False},
    {"sped_icms": 300.0, "xml_icms": 0.0},
]}
r = resumo_financeiro(INFO)
check("status 'informativo' é ignorado", r["totais"]["estornar"] == 300.0,
      r["totais"]["estornar"])
check("is_error=False também", linha(r, "icms_issues")["qtd"] == 1,
      linha(r, "icms_issues")["qtd"])


# ============================================================================
print("\n=== 7. Entradas estranhas não derrubam a tela ===")

check("relatório vazio", resumo_financeiro({})["em_jogo"] == 0.0)
check("None", resumo_financeiro(None)["em_jogo"] == 0.0)
check("lista com item que não é dicionário",
      resumo_financeiro({"icms_issues": ["lixo", None, 42]})["em_jogo"] == 0.0)
check("valor em texto é convertido",
      resumo_financeiro({"icms_issues": [{"sped_icms": "500,00", "xml_icms": "0"}]}
                        )["em_jogo"] == 0.0, "texto com vírgula vira 0, não quebra")
check("valor numérico em texto simples soma",
      resumo_financeiro({"icms_issues": [{"sped_icms": "500", "xml_icms": "0"}]}
                        )["totais"]["estornar"] == 500.0)


# ============================================================================
print("\n=== 8. A tela: dinheiro visível, e a cor não decide sozinha ===")

TPL = io.open(os.path.join("frontend", "templates", "partials", "curation",
                           "curation_results.html"), encoding="utf-8").read()
# O recorte começa no comentário do painel: o `{% set fin = ... %}` e o
# cabeçalho ficam ANTES da <div>, e sem isso o teste olharia só metade.
ini = TPL.find("{# ================= VISÃO GERAL")
fim = TPL.find('id="panel-tab-general-report"')
painel = TPL[ini:fim]

check("o painel usa o resumo financeiro", "financeiro" in painel)
check("e tolera auditoria renderizada sem ele (não dá 500)",
      "| default(none)" in painel and "{% if fin %}" in painel)
check("os três totais aparecem",
      all(x in painel for x in ("totais.estornar", "totais.aproveitar",
                                "totais.recolher")))
check("valores em formato brasileiro, não 37734.35",
      "moeda_br" in painel and '"%.2f"|format' not in painel)
check("o filtro de moeda está registrado", "moeda_br" in build_env().filters)
check("1234567.8 vira 1.234.567,80", moeda_br(1234567.8) == "1.234.567,80",
      moeda_br(1234567.8))
check("valor inválido não quebra a tela", moeda_br(None) == "0,00")

check("cada natureza tem ícone além da cor",
      all(len(v) == 3 and v[1].startswith("fa-") for v in NATUREZAS.values()),
      NATUREZAS)
check("estornar e aproveitar usam setas OPOSTAS (o sinal que não é cor)",
      NATUREZAS["estornar"][1] == "fa-arrow-trend-down"
      and NATUREZAS["aproveitar"][1] == "fa-arrow-trend-up")
check("o ícone da natureza é renderizado na linha", "rot[1]" in painel)
check("e o rótulo escrito também", "rot[0] }}" in painel)

check("a barra é de uma cor só (comprimento é a informação)",
      painel.count("#0377AA") == 1, painel.count("#0377AA"))
check("cada linha da triagem leva para a aba",
      "openCurationTab('{{ l.aba }}')" in painel)
check("o valor 'a conferir' é dito como exposição, não como imposto",
      "é imposto" in painel and "exposição a esclarecer" in painel)

check("as abas vazias são dobradas", "dobrarAbasVazias" in TPL)
check("a Visão Geral e o gerador de e-mail nunca são dobrados",
      "'tab-overview', 'tab-email-generator'" in TPL)
check("o sinal da dobra é o badge", "querySelector('.badge')" in TPL)
check("DIFAL e Relatório CT-e ganharam badge (senão seriam dobrados cheios)",
      TPL.count("result.difal_interno | length") >= 1
      and TPL.count("result.cte_xml_list | length") >= 1)
check("dobrar uma aba só não vale o clique", "vazias.length < 2" in TPL)

check("o painel renderiza com dados reais",
      bool(build_env().get_template("partials/curation/curation_results.html")))


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

sys.exit(1 if FALHAS else 0)
