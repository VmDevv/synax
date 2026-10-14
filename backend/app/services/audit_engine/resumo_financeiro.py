"""Quanto dinheiro está em jogo na auditoria, e de que lado.

A tela de resultado mostrava contagem e só contagem: "12 Inconformidades de
ICMS". Isso não responde a única pergunta que decide se vale mexer — doze itens
podem ser R$ 40 ou R$ 40.000. Para descobrir, a pessoa abria as 25 abas, uma a
uma, e somava na calculadora.

Os números sempre estiveram lá: cada divergência já carrega o valor dos dois
lados. Ninguém somava. Este módulo soma, e classifica pela AÇÃO que cada uma
pede — que é o que o contador precisa saber:

  A ESTORNAR   o SPED creditou MAIS do que o XML autoriza. É risco fiscal:
               crédito tomado a maior, sujeito a glosa.
  A APROVEITAR o XML dá direito a mais do que o SPED creditou. É dinheiro
               deixado na mesa.
  A RECOLHER   ICMS ST e antecipação apurados como devidos.
  A CONFERIR   valor de documento que não pôde ser cruzado (XML faltando, nota
               não lançada). Não é imposto — é exposição a esclarecer.

TRÊS DECISÕES QUE NÃO DÃO PARA DEDUZIR DO CÓDIGO
------------------------------------------------
1. **A conta é feita a partir do `result_data` na hora de exibir, não gravada
   pelo motor.** Assim ela vale para as 633 auditorias que já existem, sem
   backfill e sem coluna nova. O relatório já está aberto na memória da rota —
   somar algumas listas custa microssegundos.

2. **O PIS/COFINS sobre frete fica FORA do total.** A aba calcula 1,65% e 7,6%
   sobre cada CT-e, mas o SPED Fiscal não traz D101/D105 para confrontar —
   medimos: zero em todos os arquivos reais. Somar isso ao "a aproveitar" criaria
   um número que ninguém consegue defender ("cadê essa divergência?"), e é
   exatamente a confusão que custou o vai-e-volta de 27 e 28/08. A aba continua
   mostrando o cálculo; o titular financeiro não o inclui.

3. **Sem valor não vira zero: vira "sem valor apurado".** Regras pendentes,
   CFOPs não mapeados e itens não conferidos são trabalho real, mas não têm
   cifra. Aparecem na triagem pela contagem, e o texto diz que não têm valor —
   somar zero faria parecer que já foram avaliados e não custam nada.
"""
from __future__ import annotations

# (chave no result_data, rótulo, id da aba, campo do SPED, campo do XML)
#
# O par (SPED, XML) é o que define o SENTIDO: creditou a mais = estornar,
# creditou a menos = aproveitar. Por isso estas listas ficam separadas das de
# valor único, embaixo.
CONFRONTOS = (
    ("icms_issues", "Inconformidades de ICMS", "tab-icms-issues",
     "sped_icms", "xml_icms"),
    ("ipi_issues", "Conferência de IPI", "tab-ipi-issues",
     "sped_val", "xml_val"),
    ("pis_cofins_issues", "Conferência de PIS/COFINS", "tab-pis-cofins-issues",
     "sped_val", "xml_val"),
)

# (chave, rótulo, aba, campo do valor, natureza)
VALORES_UNICOS = (
    ("icms_st_notes", "ICMS ST", "tab-icms-st", "vl_icms_st", "recolher"),
    ("anticipation_results", "Cálculo de antecipação", "tab-anticipation",
     "imposto_devido", "recolher"),
    ("missing_xmls", "XMLs faltantes", "tab-missing-xmls", "vl_doc", "conferir"),
    ("unrecorded_invoices", "Notas não lançadas", "tab-missing-invoices-log",
     "vl_total", "conferir"),
    # `sped_val`/`xml_val` daqui são o TOTAL DO DOCUMENTO, não imposto: a linha
    # diz que o valor da nota no SPED não bate com o do XML. Tratar a diferença
    # como crédito a tomar seria inventar imposto a partir de um erro de
    # digitação. Vale o `diff`, e a natureza é conferir.
    ("value_discrepancies", "Divergências de valor", "tab-divergences",
     "diff", "conferir"),
)

# Trabalho real que não tem cifra. Entra na triagem pela contagem.
SEM_VALOR = (
    ("unmapped_items", "Novos CFOPs a mapear", "tab-new-cfops"),
    ("unmapped_cte_items", "Novos CFOPs de CT-e", "tab-cte-cfop-disc"),
    ("cfop_classifications_issues", "Divergências de CFOP", "tab-cfop-issues"),
    ("cte_cfop_classifications_issues", "Divergências de CFOP no CT-e",
     "tab-cte-cfop-disc"),
    ("unmapped_icms_cfops", "Regras de ICMS pendentes", "tab-icms-pending"),
    ("unmapped_ipi_rules", "Regras de IPI pendentes", "tab-ipi-pending"),
    ("unmapped_pis_cofins_rules", "Regras de PIS/COFINS pendentes",
     "tab-pis-cofins-pending"),
    ("itens_nao_conferidos", "Itens não conferidos", "tab-nao-conferidos"),
    ("cnpj_emitente_issues", "CNPJ do emitente divergente", "tab-cnpj-emitente"),
    ("cte_tomador_issues", "CT-e com tomador divergente", "tab-cte-issues"),
    ("notas_duplicadas", "Notas duplicadas", "tab-duplicadas"),
)

# Rótulo e ícone de cada natureza. O ícone existe porque a cor NUNCA pode ser o
# único sinal: vermelho e verde ficam a ΔE 4,1 para quem tem deuteranopia — foi
# medido com o validador de paleta, não estimado.
NATUREZAS = {
    "estornar": ("A estornar", "fa-arrow-trend-down",
                 "crédito tomado a maior — sujeito a glosa"),
    "aproveitar": ("A aproveitar", "fa-arrow-trend-up",
                   "crédito devido que o SPED não tomou"),
    "recolher": ("A recolher", "fa-file-invoice-dollar",
                 "ICMS ST e antecipação apurados"),
    "conferir": ("A conferir", "fa-magnifying-glass",
                 "documento sem cruzamento — valor exposto, não imposto"),
}

TOLERANCIA = 0.01   # centavo de arredondamento não é divergência


def _num(valor) -> float:
    try:
        return float(valor or 0.0)
    except (TypeError, ValueError):
        return 0.0


# Prefixo da mensagem das linhas que comparam BASE, e não crédito. As
# auditorias gravadas a partir de 08/09/2026 trazem `comparacao="base"` e nem
# chegam aqui; este texto é o caminho para as 633 que já existiam. Ele é estável:
# está travado em tests/test_pis_cofins_valor_e_cte_outra_uf.py, seção 3c.
PREFIXO_LINHA_DE_BASE = "base de pis/cofins divergente"


def _compara_credito(item) -> bool:
    """A linha compara CRÉDITO (soma no financeiro) ou BASE (não soma)?

    `pis_cofins_issues` mistura as duas coisas: umas linhas dizem "o SPED
    creditou R$ X sem direito" — isso é imposto; outras dizem "a base do SPED é
    R$ 0,00 contra R$ 820,00 calculada" — isso é base de cálculo, e o SPED
    Fiscal quase nunca preenche esse campo, então quase todo item gera uma.

    Somar as duas juntas deu R$ 111.273,57 de "crédito a aproveitar" numa
    auditoria real — número que não existe e que ninguém conseguiria defender.
    """
    if not isinstance(item, dict):
        return False
    if item.get("comparacao") == "base":
        return False
    return not str(item.get("reason", "")).strip().lower().startswith(
        PREFIXO_LINHA_DE_BASE)


def _e_erro(item) -> bool:
    """Linha informativa não entra na conta — mesmo critério do resumo do motor."""
    if not isinstance(item, dict):
        return False
    if str(item.get("status", "")).lower() in ("informativo", "ok", "correto", "info"):
        return False
    return item.get("is_error") is not False


def resumo_financeiro(resultado: dict) -> dict:
    """Totais por natureza e a lista de categorias, ordenada por valor."""
    resultado = resultado or {}
    totais = {"estornar": 0.0, "aproveitar": 0.0, "recolher": 0.0, "conferir": 0.0}
    linhas = []

    for chave, rotulo, aba, campo_sped, campo_xml in CONFRONTOS:
        # `_compara_credito` só descarta linha de BASE — a contagem da triagem
        # usa a lista cheia, porque a linha de base É uma inconsistência a
        # conferir; ela só não é dinheiro.
        todos = [i for i in (resultado.get(chave) or []) if _e_erro(i)]
        itens = [i for i in todos if _compara_credito(i)]
        if not todos:
            continue
        estornar = aproveitar = 0.0
        for item in itens:
            sped = _num(item.get(campo_sped))
            xml = _num(item.get(campo_xml))
            if sped - xml > TOLERANCIA:
                estornar += sped - xml
            elif xml - sped > TOLERANCIA:
                aproveitar += xml - sped
        totais["estornar"] += estornar
        totais["aproveitar"] += aproveitar

        # Uma categoria pode ter os dois sentidos. A linha da triagem mostra o
        # que pesa mais, com o outro ao lado — dividir em duas linhas faria a
        # mesma aba aparecer duas vezes na lista.
        natureza = "estornar" if estornar >= aproveitar else "aproveitar"
        linhas.append({
            "chave": chave, "rotulo": rotulo, "aba": aba,
            "qtd": len(todos), "valor": round(max(estornar, aproveitar), 2),
            "natureza": natureza if (estornar or aproveitar) else "sem_valor",
            "contrapartida": round(min(estornar, aproveitar), 2),
            "tem_valor": bool(estornar or aproveitar),
        })

    for chave, rotulo, aba, campo, natureza in VALORES_UNICOS:
        itens = [i for i in (resultado.get(chave) or []) if _e_erro(i)]
        if not itens:
            continue
        valor = round(sum(_num(i.get(campo)) for i in itens), 2)
        totais[natureza] += valor
        linhas.append({
            "chave": chave, "rotulo": rotulo, "aba": aba,
            "qtd": len(itens), "valor": valor, "natureza": natureza,
            "contrapartida": 0.0, "tem_valor": True,
        })

    for chave, rotulo, aba in SEM_VALOR:
        itens = resultado.get(chave) or []
        if not itens:
            continue
        linhas.append({
            "chave": chave, "rotulo": rotulo, "aba": aba,
            "qtd": len(itens), "valor": 0.0, "natureza": "sem_valor",
            "contrapartida": 0.0, "tem_valor": False,
        })

    # Quem tem cifra vem primeiro, do maior para o menor; depois o resto, pela
    # quantidade. É a ordem em que o trabalho rende.
    linhas.sort(key=lambda l: (not l["tem_valor"], -l["valor"], -l["qtd"]))

    pico = max([l["valor"] for l in linhas] + [0.0])
    for l in linhas:
        l["proporcao"] = round(l["valor"] * 100.0 / pico, 1) if pico else 0.0

    em_jogo = totais["estornar"] + totais["aproveitar"] + totais["recolher"]
    return {
        "totais": {k: round(v, 2) for k, v in totais.items()},
        "em_jogo": round(em_jogo, 2),
        "linhas": linhas,
        "com_valor": [l for l in linhas if l["tem_valor"]],
        "sem_valor": [l for l in linhas if not l["tem_valor"]],
        "tem_dinheiro": em_jogo > TOLERANCIA,
        "naturezas": NATUREZAS,
    }


def valores_para_colunas(resultado: dict) -> dict:
    """Os quatro totais, com o nome das colunas de `audit_tasks`.

    Um lugar só para o motor (ao concluir) e o resumo por e-mail (ao preencher
    auditoria antiga) gravarem a MESMA conta da Visão Geral.
    """
    totais = resumo_financeiro(resultado)["totais"]
    return {"vl_estornar": totais["estornar"], "vl_aproveitar": totais["aproveitar"],
            "vl_recolher": totais["recolher"], "vl_conferir": totais["conferir"]}


def abas_com_conteudo(resultado: dict) -> set:
    """Chaves do result_data que têm alguma linha — para a tela dobrar o resto."""
    resultado = resultado or {}
    return {c for c in resultado
            if isinstance(resultado.get(c), list) and resultado.get(c)}
