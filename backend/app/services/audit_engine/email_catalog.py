"""Catálogo dos apontamentos que podem entrar no e-mail ao cliente.

FONTE ÚNICA DA VERDADE: a tela de geração de e-mail e o montador do corpo do
e-mail leem ESTA lista. Antes, a tela tinha 5 checkboxes escritos à mão no
HTML e o backend tinha 5 parâmetros `include_*` booleanos — as duas listas
divergiam do que o motor realmente produz, e categorias inteiras da auditoria
(notas não lançadas, duplicadas, DIFAL, ST, antecipação, CFOP, CT-e) não
tinham como ser comunicadas ao cliente.

Para adicionar um apontamento novo: acrescente uma entrada em CATEGORIAS. A
tela passa a exibir o checkbox e o e-mail passa a montar a seção, sem tocar
em HTML nem em rota.

Campos de cada categoria:
    chave         id usado no formulário (`incluir=<chave>`) e no HTML
    rotulo        título da seção no e-mail e do checkbox na tela
    emoji, cor    identidade visual na tela (cor = paleta Tailwind já usada)
    grupo         agrupamento visual da tela
    secao         "nfe" | "cte" — em qual bloco do e-mail as linhas entram
    fontes        chaves de result_data que alimentam a categoria
    informativo   True = não é erro, é ponto de atenção (muda o texto padrão)
    intro         TEXTO PADRÃO da categoria: o parágrafo que explica ao cliente
                  o que aquele bloco significa e o que se espera dele
    formatador    função (item) -> dict com num/emit/descr/detalhe
"""

# ---------------------------------------------------------------------------
# Helpers de leitura tolerante
#
# Cada trecho do motor grava o item com um nome de campo diferente (`num`,
# `num_nf`, `nf_num`...). Em vez de espalhar `.get()` encadeado por todo lado,
# os helpers abaixo tentam os apelidos conhecidos em ordem.
# ---------------------------------------------------------------------------

def _primeiro(item: dict, *chaves, padrao: str = "") -> str:
    for c in chaves:
        v = item.get(c)
        if v not in (None, "", "-"):
            return str(v).strip()
    return padrao


def _num_da_chave(chave: str) -> str:
    """Número da NF embutido na chave de acesso de 44 dígitos (posições 25–34)."""
    chave = str(chave or "")
    if len(chave) == 44:
        try:
            return str(int(chave[25:34]))
        except (ValueError, TypeError):
            return "-"
    return "-"


def _num(item: dict) -> str:
    n = _primeiro(item, "num", "num_nf", "nf_num", "nfe_num", "numero")
    if n and n != "0":
        return n
    return _num_da_chave(_primeiro(item, "chave", "key"))


def _emit(item: dict) -> str:
    return _primeiro(item, "emit_name", "fornecedor", "emit", "razao_social",
                     "cnpj_emitente", "emit_cnpj", padrao="-")


def _descr(item: dict) -> str:
    return _primeiro(item, "descr", "descricao", "product_description", "produto",
                     "descr_item", "cfop_desc")


def _moeda(valor) -> str:
    try:
        return f"R$ {float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "R$ 0,00"


# ---------------------------------------------------------------------------
# Formatadores — devolvem os pedaços da linha; o montador do e-mail decide o
# layout final (texto puro x HTML). Nenhum deles devolve HTML.
# ---------------------------------------------------------------------------

def _fmt_padrao(item: dict) -> dict:
    """Serve para tudo que já traz `reason` pronto do motor (ICMS, IPI, PIS...)."""
    return {
        "num": _num(item),
        "emit": _emit(item),
        "descr": _descr(item),
        "detalhe": _primeiro(item, "reason", "motivo", "details", "status",
                             padrao="Inconsistência identificada."),
    }


def _fmt_xml_faltante(item: dict) -> dict:
    return {
        "num": _num(item),
        "emit": _emit(item),
        "descr": "",
        "detalhe": ("XML não localizado para o documento escriturado no SPED. "
                    "Favor encaminhar o arquivo."),
    }


def _fmt_nao_lancada(item: dict) -> dict:
    tipo = _primeiro(item, "tipo", padrao="NF-e").upper()
    return {
        "num": _num(item),
        "emit": _emit(item),
        "descr": "",
        "detalhe": (f"{tipo} encontrada no arquivo/cofre fiscal mas SEM escrituração "
                    f"no SPED — valor {_moeda(item.get('vl_total'))}."),
    }


def _fmt_duplicada(item: dict) -> dict:
    return {
        "num": _num(item),
        "emit": _emit(item),
        "descr": "",
        "detalhe": (f"{_primeiro(item, 'tipo', padrao='Documento')} escriturado MAIS DE UMA VEZ "
                    f"no SPED ({_primeiro(item, 'tipo_operacao', padrao='Entrada')}) — "
                    f"valor {_moeda(item.get('vl_doc'))}."),
    }


def _fmt_divergencia_valor(item: dict) -> dict:
    detalhe = _primeiro(item, "reason", "motivo")
    if not detalhe:
        detalhe = (f"Valor divergente: SPED {_moeda(item.get('sped_val', item.get('valor_sped')))} "
                   f"x XML {_moeda(item.get('xml_val', item.get('valor_xml')))}.")
    return {"num": _num(item), "emit": _emit(item), "descr": _descr(item), "detalhe": detalhe}


def _fmt_cfop_divergente(item: dict) -> dict:
    return {
        "num": _num(item),
        "emit": _emit(item),
        "descr": _descr(item),
        "detalhe": (f"CFOP de entrada escriturado {_primeiro(item, 'sped_cfop', padrao='-')} "
                    f"diverge do esperado {_primeiro(item, 'expected_cfop', padrao='-')} "
                    f"(CFOP de origem {_primeiro(item, 'supplier_cfop', padrao='-')})."),
    }


def _fmt_cfop_nao_mapeado(item: dict) -> dict:
    """`unmapped_items` / `cfops_desconhecidos` — não têm número de nota."""
    cfop_origem = _primeiro(item, "supplier_cfop", "cfop", padrao="-")
    return {
        "num": "-",
        "emit": _primeiro(item, "cnpj_emitente", "supplier_cnpj", padrao="-"),
        "descr": _descr(item),
        "detalhe": (f"CFOP de origem {cfop_origem} ainda sem regra de entrada definida "
                    f"(escriturado como {_primeiro(item, 'current_sped_cfop', 'cfop_sugerido', padrao='-')})."),
    }


def _fmt_cfop_sem_regra_icms(item) -> dict:
    """`unmapped_icms_cfops` é lista de STRINGS (só o código do CFOP).

    Esta era a origem de um erro 500: o montador antigo chamava `.get()` em
    cada item, e string não tem `.get()` — qualquer auditoria com CFOP de ICMS
    não mapeado derrubava a geração do e-mail.
    """
    cfop = item if isinstance(item, str) else _primeiro(item, "cfop", padrao="-")
    return {
        "num": "-",
        "emit": "-",
        "descr": "",
        "detalhe": (f"CFOP {cfop} sem regra de crédito de ICMS cadastrada — "
                    f"o direito ao crédito precisa ser confirmado."),
    }


def _fmt_regra_tributo_ausente(item: dict) -> dict:
    """`unmapped_ipi_rules` / `unmapped_pis_cofins_rules` — dicts sem nº de nota."""
    cst = _primeiro(item, "cst_ipi", "cst_expected", "cst", padrao="*")
    return {
        "num": "-",
        "emit": "-",
        "descr": _descr(item),
        "detalhe": (f"CFOP {_primeiro(item, 'cfop', padrao='-')} / CST {cst} sem regra de "
                    f"crédito cadastrada — tratamento precisa ser confirmado."),
    }


def _fmt_icms_st(item: dict) -> dict:
    itens = item.get("items") or []
    total = sum(float(i.get("vl_icms_st", 0) or 0) for i in itens)
    return {
        "num": _num(item),
        "emit": _emit(item),
        "descr": "",
        "detalhe": (f"Nota com ICMS-ST destacado em {len(itens)} item(ns) — "
                    f"total {_moeda(total)}. Confira se o produto é de fato substituído."),
    }


def _fmt_st_calculo(item: dict) -> dict:
    return {
        "num": _primeiro(item, "num_nf", "num", padrao="-"),
        "emit": _primeiro(item, "fornecedor", "emit_name", padrao="-"),
        "descr": _primeiro(item, "descricao_item", "descr"),
        "detalhe": (f"ICMS-ST devido por antecipação estimado em {_moeda(item.get('icms_st'))} "
                    f"(MVA {_primeiro(item, 'mva_str', padrao='-')}, "
                    f"CEST {_primeiro(item, 'cest', padrao='-')})."),
    }


def _fmt_antecipacao(item: dict) -> dict:
    return {
        "num": _primeiro(item, "num_nf", "num", padrao="-"),
        "emit": _primeiro(item, "cnpj_emitente", "emit_name", padrao="-"),
        "descr": _descr(item),
        "detalhe": (f"Produto sujeito a antecipação de ICMS "
                    f"({_primeiro(item, 'categoria_ncm', padrao='NCM listado')}) — "
                    f"NCM {_primeiro(item, 'ncm', padrao='-')}, "
                    f"valor {_moeda(item.get('vl_item'))}."),
    }


def _fmt_difal(item: dict) -> dict:
    # DIFAL interno grava a base como `vl_bc`; o interestadual, como `vl_bc_icms`.
    base = item.get("vl_bc")
    if base in (None, "", 0, 0.0):
        base = item.get("vl_bc_icms")
    return {
        "num": _primeiro(item, "num_nf", "num", padrao="-"),
        "emit": _primeiro(item, "emit_cnpj", "emit_name", padrao="-"),
        "descr": _descr(item),
        "detalhe": (f"Operação sujeita a DIFAL (CFOP {_primeiro(item, 'cfop_sped', padrao='-')}) — "
                    f"base {_moeda(base)}, "
                    f"ICMS destacado {_moeda(item.get('vl_icms'))}."),
    }


def _fmt_cnpj_emitente(item: dict) -> dict:
    """A linha tem que dizer QUAL fornecedor está no lugar de qual.

    "CNPJ divergente" sozinho manda o cliente abrir a nota para descobrir o que
    trocar. Os dois CNPJs lado a lado já respondem: ou o participante está
    errado, ou é a filial.
    """
    return {
        "num": _num(item),
        "emit": _emit(item),
        "descr": _primeiro(item, "type"),
        "detalhe": _primeiro(
            item, "reason",
            padrao=(f"CNPJ do emitente no SPED ({_primeiro(item, 'sped_cnpj', padrao='não informado')}) "
                    f"diferente do XML ({_primeiro(item, 'xml_cnpj', padrao='não informado')}).")),
    }


def _fmt_cte_tomador(item: dict) -> dict:
    return {
        "num": _num(item),
        "emit": _emit(item),
        "descr": "",
        "detalhe": (f"CT-e escriturado, porém o tomador do serviço no XML é "
                    f"{_primeiro(item, 'tomador_role', padrao='outro participante')} — "
                    f"o crédito só cabe a quem consta como tomador."),
    }


def _fmt_cte_cfop(item: dict) -> dict:
    return {
        "num": _num(item),
        "emit": _emit(item),
        "descr": "",
        "detalhe": (f"CFOP do CT-e escriturado {_primeiro(item, 'sped_cfop', padrao='-')} "
                    f"diverge do esperado {_primeiro(item, 'expected_cfop', padrao='-')} "
                    f"(UF do emitente {_primeiro(item, 'emit_uf', padrao='-')})."),
    }


def _fmt_cte_nao_mapeado(item: dict) -> dict:
    return {
        "num": "-",
        "emit": "-",
        "descr": "",
        "detalhe": (f"CFOP de CT-e {_primeiro(item, 'supplier_cfop', padrao='-')} / UF "
                    f"{_primeiro(item, 'emitter_uf', padrao='-')} ainda sem regra de entrada "
                    f"definida (escriturado como "
                    f"{_primeiro(item, 'current_sped_cfop', padrao='-')})."),
    }


# ---------------------------------------------------------------------------
# O catálogo
# ---------------------------------------------------------------------------

CATEGORIAS = [
    # ---------------- Escrituração ----------------
    {
        "chave": "xmls_faltantes",
        "rotulo": "Cobrança de XMLs faltantes",
        "emoji": "🟠", "cor": "orange", "grupo": "Escrituração", "secao": "nfe",
        "fontes": ["xmls_faltantes"],
        "informativo": False,
        "formatador": _fmt_xml_faltante,
        "descricao": "Documentos escriturados no SPED sem o arquivo XML correspondente.",
        "intro": ("Os documentos abaixo constam escriturados no SPED, porém não localizamos "
                  "o arquivo XML correspondente. Solicitamos o envio dos arquivos para "
                  "concluirmos a validação:"),
    },
    {
        "chave": "notas_nao_lancadas",
        "rotulo": "Notas não lançadas (malha fina)",
        "emoji": "🔴", "cor": "rose", "grupo": "Escrituração", "secao": "nfe",
        "fontes": ["unrecorded_invoices"],
        "informativo": False,
        "formatador": _fmt_nao_lancada,
        "descricao": "Documentos existentes no XML/cofre fiscal e ausentes no SPED.",
        "intro": ("Identificamos documentos fiscais emitidos contra a empresa que NÃO foram "
                  "escriturados no SPED do período. A omissão de entrada é apontamento de "
                  "malha fina e deve ser regularizada:"),
    },
    {
        "chave": "notas_duplicadas",
        "rotulo": "Notas duplicadas no SPED",
        "emoji": "🟤", "cor": "amber", "grupo": "Escrituração", "secao": "nfe",
        "fontes": ["notas_duplicadas"],
        "informativo": False,
        "formatador": _fmt_duplicada,
        "descricao": "Mesma chave de acesso escriturada mais de uma vez.",
        "intro": ("Os documentos a seguir foram escriturados em duplicidade no SPED. "
                  "A duplicidade infla créditos e precisa ser eliminada na retificação:"),
    },
    {
        "chave": "divergencias_valor",
        "rotulo": "Divergências de valor (SPED x XML)",
        "emoji": "🟡", "cor": "yellow", "grupo": "Escrituração", "secao": "nfe",
        "fontes": ["value_discrepancies"],
        "informativo": False,
        "formatador": _fmt_divergencia_valor,
        "descricao": "Valor total escriturado diferente do valor do documento.",
        "intro": ("Nos documentos abaixo, o valor escriturado no SPED não confere com o valor "
                  "do XML ou com o somatório do próprio registro analítico:"),
    },

    {
        "chave": "cnpj_emitente",
        "rotulo": "CNPJ do emitente divergente",
        "emoji": "🟥", "cor": "rose", "grupo": "Escrituração", "secao": "nfe",
        "fontes": ["cnpj_emitente_issues"],
        "informativo": False,
        "formatador": _fmt_cnpj_emitente,
        "descricao": "Nota escriturada em fornecedor diferente do emitente do XML.",
        "intro": ("Nos documentos abaixo, o participante informado na escrituração não é "
                  "quem emitiu o XML. Valores e impostos conferem, porque vieram do XML "
                  "correto — o que está trocado é o cadastro do fornecedor, e ele define "
                  "de quem o crédito foi tomado:"),
    },

    # ---------------- Tributos ----------------
    {
        "chave": "icms",
        "rotulo": "Divergências de ICMS",
        "emoji": "🔴", "cor": "red", "grupo": "Tributos", "secao": "nfe",
        "fontes": ["icms_issues"],
        "informativo": False,
        "formatador": _fmt_padrao,
        "descricao": "Crédito indevido ou valor de ICMS divergente do documento.",
        "intro": ("Apuramos as seguintes inconsistências no crédito de ICMS — glosa de crédito "
                  "indevido ou diferença entre o valor escriturado e o destacado no documento:"),
    },
    {
        "chave": "icms_sem_regra",
        "rotulo": "CFOPs de ICMS sem regra de crédito",
        "emoji": "🔴", "cor": "red", "grupo": "Tributos", "secao": "nfe",
        "fontes": ["unmapped_icms_cfops"],
        "informativo": True,
        "formatador": _fmt_cfop_sem_regra_icms,
        "descricao": "CFOPs cujo direito a crédito de ICMS ainda não foi definido.",
        "intro": ("Os CFOPs abaixo não possuem regra de crédito de ICMS definida em nosso "
                  "cadastro. Pedimos confirmação do tratamento aplicável a cada um:"),
    },
    {
        "chave": "ipi",
        "rotulo": "Divergências de IPI (CST / crédito)",
        "emoji": "🟣", "cor": "purple", "grupo": "Tributos", "secao": "nfe",
        "fontes": ["ipi_issues"],
        "informativo": False,
        "formatador": _fmt_padrao,
        "descricao": "Crédito de IPI indevido ou divergente do documento.",
        "intro": ("Verificamos as seguintes inconsistências na apuração do IPI, entre crédito "
                  "indevido e divergência de valor em relação ao documento fiscal:"),
    },
    {
        "chave": "ipi_sem_regra",
        "rotulo": "CFOPs de IPI sem regra de crédito",
        "emoji": "🟣", "cor": "purple", "grupo": "Tributos", "secao": "nfe",
        "fontes": ["unmapped_ipi_rules"],
        "informativo": True,
        "formatador": _fmt_regra_tributo_ausente,
        "descricao": "Combinações CFOP/CST de IPI ainda sem tratamento definido.",
        "intro": ("As combinações de CFOP e CST abaixo não possuem regra de crédito de IPI "
                  "cadastrada. Solicitamos confirmação do tratamento correto:"),
    },
    {
        "chave": "pis_cofins",
        "rotulo": "Divergências de PIS e COFINS",
        "emoji": "🔵", "cor": "blue", "grupo": "Tributos", "secao": "nfe",
        "fontes": ["pis_cofins_issues"],
        "informativo": False,
        "formatador": _fmt_padrao,
        "descricao": "Crédito indevido ou base de cálculo divergente (exclusão do ICMS).",
        "intro": ("Apontamos as seguintes inconsistências na apuração de PIS e COFINS, "
                  "incluindo divergências de base de cálculo pela exclusão do ICMS "
                  "(Tema 69 / STF):"),
    },
    {
        "chave": "pis_cofins_sem_regra",
        "rotulo": "CFOPs de PIS/COFINS sem regra",
        "emoji": "🔵", "cor": "blue", "grupo": "Tributos", "secao": "nfe",
        "fontes": ["unmapped_pis_cofins_rules"],
        "informativo": True,
        "formatador": _fmt_regra_tributo_ausente,
        "descricao": "Combinações CFOP/CST de PIS/COFINS sem tratamento definido.",
        "intro": ("As combinações de CFOP e CST abaixo não possuem regra de crédito de "
                  "PIS/COFINS cadastrada. Pedimos confirmação do tratamento aplicável:"),
    },
    {
        "chave": "icms_st",
        "rotulo": "Notas com ICMS-ST destacado",
        "emoji": "🟠", "cor": "orange", "grupo": "Tributos", "secao": "nfe",
        "fontes": ["icms_st_notes"],
        "informativo": True,
        "formatador": _fmt_icms_st,
        "descricao": "Documentos com substituição tributária destacada pelo emitente.",
        "intro": ("Os documentos abaixo vieram com ICMS-ST destacado pelo emitente. Não há "
                  "crédito de ICMS sobre operações já substituídas — vale conferir o "
                  "enquadramento dos produtos:"),
    },
    {
        "chave": "icms_st_calculo",
        "rotulo": "ICMS-ST a recolher (cálculo)",
        "emoji": "🟠", "cor": "orange", "grupo": "Tributos", "secao": "nfe",
        "fontes": ["icms_st_calculation"],
        "informativo": True,
        "formatador": _fmt_st_calculo,
        "descricao": "Estimativa de ST devida por antecipação, com MVA aplicada.",
        "intro": ("Com base na MVA aplicável a cada CEST, estimamos ICMS-ST devido por "
                  "antecipação nas operações abaixo. Os valores são estimativa para "
                  "conferência, não guia de recolhimento:"),
    },
    {
        "chave": "antecipacao",
        "rotulo": "Antecipação de ICMS (NCM)",
        "emoji": "🟢", "cor": "emerald", "grupo": "Tributos", "secao": "nfe",
        "fontes": ["antecipacao_icms", "anticipation_results"],
        "informativo": True,
        "formatador": _fmt_antecipacao,
        "descricao": "Produtos de revenda com NCM sujeito a antecipação.",
        "intro": ("Identificamos produtos adquiridos para revenda cujo NCM está sujeito à "
                  "antecipação de ICMS. Recomendamos conferir a apuração do período:"),
    },
    {
        "chave": "difal",
        "rotulo": "DIFAL (interno e interestadual)",
        "emoji": "🟦", "cor": "sky", "grupo": "Tributos", "secao": "nfe",
        "fontes": ["difal_interno", "difal_interestadual"],
        "informativo": True,
        "formatador": _fmt_difal,
        "descricao": "Operações de uso/consumo e ativo sujeitas a diferencial de alíquota.",
        "intro": ("As operações abaixo são de uso, consumo ou ativo imobilizado e estão "
                  "sujeitas ao diferencial de alíquota (DIFAL). Listagem para conferência "
                  "da apuração:"),
    },

    # ---------------- CFOP ----------------
    {
        "chave": "cfop_divergente",
        "rotulo": "CFOP de entrada divergente",
        "emoji": "🟪", "cor": "violet", "grupo": "CFOP", "secao": "nfe",
        "fontes": ["cfop_classifications_issues"],
        "informativo": False,
        "formatador": _fmt_cfop_divergente,
        "descricao": "CFOP escriturado diferente da regra aprovada para o produto.",
        "intro": ("Nos itens abaixo, o CFOP de entrada escriturado diverge do CFOP definido "
                  "para aquele fornecedor e produto. A classificação incorreta afeta "
                  "diretamente o direito a crédito:"),
    },
    {
        "chave": "cfop_nao_mapeado",
        "rotulo": "CFOPs de entrada sem regra",
        "emoji": "⚪", "cor": "slate", "grupo": "CFOP", "secao": "nfe",
        "fontes": ["cfops_desconhecidos"],
        "informativo": True,
        "formatador": _fmt_cfop_nao_mapeado,
        "descricao": "Combinações fornecedor/produto ainda sem CFOP de entrada definido.",
        "intro": ("As combinações de fornecedor, CFOP de origem e produto listadas abaixo "
                  "ainda não possuem CFOP de entrada definido em nosso cadastro. Pedimos "
                  "orientação sobre a destinação de cada item:"),
    },

    # ---------------- CT-e ----------------
    {
        "chave": "cte_tomador",
        "rotulo": "CT-e com tomador divergente",
        "emoji": "🚚", "cor": "indigo", "grupo": "CT-e", "secao": "cte",
        "fontes": ["cte_tomador_issues"],
        "informativo": False,
        "formatador": _fmt_cte_tomador,
        "descricao": "Frete escriturado em que a empresa não é a tomadora do serviço.",
        "intro": ("Nos conhecimentos de transporte abaixo, a empresa não figura como tomadora "
                  "do serviço no XML. O crédito de ICMS sobre o frete cabe apenas ao tomador:"),
    },
    {
        "chave": "cte_cfop",
        "rotulo": "CFOP de CT-e divergente",
        "emoji": "🚚", "cor": "indigo", "grupo": "CT-e", "secao": "cte",
        "fontes": ["cte_cfop_classifications_issues"],
        "informativo": False,
        "formatador": _fmt_cte_cfop,
        "descricao": "CFOP do frete escriturado diferente da regra aprovada.",
        "intro": ("O CFOP escriturado nos conhecimentos de transporte abaixo diverge do "
                  "definido para a UF do emitente:"),
    },
    {
        "chave": "cte_nao_mapeado",
        "rotulo": "CFOPs de CT-e sem regra",
        "emoji": "🚚", "cor": "indigo", "grupo": "CT-e", "secao": "cte",
        "fontes": ["unmapped_cte_items"],
        "informativo": True,
        "formatador": _fmt_cte_nao_mapeado,
        "descricao": "Combinações CFOP/UF de frete ainda sem regra de entrada.",
        "intro": ("As combinações de CFOP e UF de frete abaixo ainda não possuem regra de "
                  "entrada cadastrada. Pedimos confirmação do tratamento:"),
    },
]

# Índice por chave, para o backend resolver rápido o que veio marcado na tela.
POR_CHAVE = {c["chave"]: c for c in CATEGORIAS}

# Paleta: classes do Tailwind escritas POR EXTENSO, e não montadas com o nome
# da cor (`text-{{ cor }}-700`). Mesma razão registrada em
# partials/admin/certificados_list.html: o Tailwind só gera o CSS das classes
# que encontra escritas literalmente. O hex acompanha porque cliente de e-mail
# não roda Tailwind — lá a cor precisa ir inline no style.
PALETA = {
    "red":     {"titulo": "text-red-700",     "contador": "text-red-600",     "borda": "hover:border-red-400",     "hex": "#dc2626"},
    "rose":    {"titulo": "text-rose-700",    "contador": "text-rose-600",    "borda": "hover:border-rose-400",    "hex": "#e11d48"},
    "orange":  {"titulo": "text-orange-700",  "contador": "text-orange-600",  "borda": "hover:border-orange-400",  "hex": "#ea580c"},
    "amber":   {"titulo": "text-amber-700",   "contador": "text-amber-600",   "borda": "hover:border-amber-400",   "hex": "#d97706"},
    "yellow":  {"titulo": "text-yellow-700",  "contador": "text-yellow-600",  "borda": "hover:border-yellow-400",  "hex": "#ca8a04"},
    "purple":  {"titulo": "text-purple-700",  "contador": "text-purple-600",  "borda": "hover:border-purple-400",  "hex": "#9333ea"},
    "violet":  {"titulo": "text-violet-700",  "contador": "text-violet-600",  "borda": "hover:border-violet-400",  "hex": "#7c3aed"},
    "blue":    {"titulo": "text-blue-700",    "contador": "text-blue-600",    "borda": "hover:border-blue-400",    "hex": "#2563eb"},
    "sky":     {"titulo": "text-sky-700",     "contador": "text-sky-600",     "borda": "hover:border-sky-400",     "hex": "#0284c7"},
    "indigo":  {"titulo": "text-indigo-700",  "contador": "text-indigo-600",  "borda": "hover:border-indigo-400",  "hex": "#4f46e5"},
    "emerald": {"titulo": "text-emerald-700", "contador": "text-emerald-600", "borda": "hover:border-emerald-400", "hex": "#059669"},
    "slate":   {"titulo": "text-slate-700",   "contador": "text-slate-600",   "borda": "hover:border-slate-400",   "hex": "#475569"},
}
_PALETA_PADRAO = PALETA["slate"]

# Ordem dos grupos na tela — as categorias mais acionáveis primeiro.
GRUPOS = ["Escrituração", "Tributos", "CFOP", "CT-e"]

# Texto padrão das partes fixas do e-mail (também editável em um lugar só).
TEXTO_ABERTURA = (
    "Na auditoria fiscal dos lançamentos de entrada da empresa {empresa}"
    "{cnpj_txt}, referente a {periodo}, identificamos os pontos abaixo:"
)
TEXTO_FECHAMENTO = (
    "Permanecemos à disposição para esclarecimentos e solicitamos a análise e "
    "os devidos ajustes nos itens apontados."
)
ASSINATURA = "Equipe Synax | Auditoria e Compliance Tributário"


def contar(result_data: dict, categoria: dict) -> int:
    """Quantos apontamentos esta categoria tem nesta auditoria."""
    total = 0
    for fonte in categoria["fontes"]:
        valor = result_data.get(fonte) or []
        if isinstance(valor, list):
            total += len(valor)
    return total


def itens_de(result_data: dict, categoria: dict) -> list:
    """Todos os itens crus das fontes da categoria, na ordem declarada."""
    itens = []
    for fonte in categoria["fontes"]:
        valor = result_data.get(fonte) or []
        if isinstance(valor, list):
            itens.extend(valor)
    return itens


def resumo_para_tela(result_data: dict) -> list:
    """Catálogo enriquecido com a contagem desta auditoria, pronto para o Jinja.

    `disponivel` indica se há apontamento — a tela usa isso para pré-marcar o
    checkbox e para deixar em segundo plano o que está zerado.
    """
    linhas = []
    for categoria in CATEGORIAS:
        qtd = contar(result_data, categoria)
        paleta = PALETA.get(categoria["cor"], _PALETA_PADRAO)
        linhas.append({
            "chave": categoria["chave"],
            "rotulo": categoria["rotulo"],
            "emoji": categoria["emoji"],
            "grupo": categoria["grupo"],
            "descricao": categoria["descricao"],
            "informativo": categoria["informativo"],
            "quantidade": qtd,
            "disponivel": qtd > 0,
            # Classes prontas: o template só interpola, nunca monta nome de classe.
            "classe_titulo": paleta["titulo"],
            "classe_contador": paleta["contador"],
            "classe_borda": paleta["borda"],
        })
    return linhas


def cor_hex(categoria: dict) -> str:
    """Hex da categoria, para o style inline do corpo do e-mail."""
    return PALETA.get(categoria["cor"], _PALETA_PADRAO)["hex"]
