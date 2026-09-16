import io
from datetime import datetime

import pandas as pd

from ..planilha_estilo import (
    AMBAR, AZUL, VERDE, VERMELHO, destacar_linhas, estilizar_planilha, montar_capa)
from ...core.fuso import hora_local

# Cor da guia por aba: quem abre o arquivo acha a aba de divergência sem ler o
# nome de todas. Vermelho aponta erro, azul é conferência, verde é informativo.
CORES_DAS_ABAS = {
    "Divergências ICMS": VERMELHO,
    "Divergências IPI": VERMELHO,
    "PIS e COFINS": VERMELHO,
    "Divergências de Valor": VERMELHO,
    "XMLs Faltantes": VERMELHO,
    "CNPJ do Emitente": VERMELHO,
    "Malha Fina (SPED)": VERMELHO,
    "Itens Não Conferidos": AMBAR,
    "Novos CFOPs": AZUL,
    "Novos CFOPs (CT-e)": AZUL,
    "Antecipação ICMS": AZUL,
    "PIS-COFINS Frete": AZUL,
    "Relatório de NFs": VERDE,
    "Planilha CT-e": VERDE,
    "ICMS Mono": VERDE,
    "DIFAL Interestadual": VERDE,
    "DIFAL Interno": VERDE,
}


# Cabeçalho legível para as chaves internas do motor. Sem isto a planilha sai
# com "sped_val" e "emit_name" no topo da coluna — o nome que o programa usa,
# não o que o contador lê. O que faltar aqui cai em `_humanizar`, que ao menos
# tira o underscore e capitaliza; a lista existe para dar o termo FISCAL certo
# ("Motivo", "Valor no SPED"), que nenhuma regra automática adivinha.
RENOMEAR = {
    "key": "Chave de Acesso", "chave": "Chave de Acesso",
    "num": "N° Documento", "num_nf": "N° NF", "num_item": "Item",
    "type": "Tipo", "tipo": "Tipo", "tipo_operacao": "Operação",
    "emit": "Emitente", "emit_name": "Fornecedor", "fornecedor": "Fornecedor",
    "razao_social": "Razão Social",
    "emit_cnpj": "CNPJ Emitente", "cnpj_emitente": "CNPJ Emitente",
    "emit_uf": "UF Emitente", "uf_emitente": "UF Emitente",
    "company_cnpj": "CNPJ da Empresa", "supplier_cnpj": "CNPJ do Fornecedor",
    "cod_part": "Código do Participante (SPED)",
    "sped_cnpj": "CNPJ no SPED", "xml_cnpj": "CNPJ no XML",
    "sped_name": "Participante no SPED",
    "tipo_divergencia": "Tipo da Divergência",
    "descr": "Descrição", "descr_item": "Descrição do Item",
    "product_description": "Descrição do Produto", "product_code": "Código do Produto",
    "cod_item": "Código no SPED", "cod": "Código",
    "cfop": "CFOP", "cfop_sped": "CFOP no SPED", "cfop_xml": "CFOP no XML",
    "sped_cfop": "CFOP no SPED", "supplier_cfop": "CFOP do Fornecedor",
    "expected_cfop": "CFOP Esperado", "current_sped_cfop": "CFOP Atual no SPED",
    "cst": "CST", "cst_expected": "CST Esperado", "cst_ipi": "CST do IPI",
    "ncm": "NCM", "cest": "CEST", "mva": "MVA (%)", "c_benef": "Código do Benefício",
    "reason": "Motivo", "motivo": "Motivo", "diagnostic": "Diagnóstico",
    "status": "Situação", "situacao": "Situação", "obs": "Observação",
    "sped_val": "Valor no SPED", "xml_val": "Valor no XML", "diff": "Diferença",
    "sped_icms": "ICMS no SPED", "xml_icms": "ICMS no XML",
    "vl_doc": "Valor do Documento", "vl_item": "Valor do Item",
    "vl_total": "Valor Total", "valor": "Valor",
    "vl_bc": "Base de Cálculo", "vl_bc_icms": "Base de ICMS", "bc_icms": "Base de ICMS",
    "vl_icms": "ICMS", "icms": "ICMS", "icms_sn": "ICMS do Simples",
    "vl_icms_st": "ICMS ST", "fcp_st": "FCP ST", "icms_mono": "ICMS Monofásico",
    "ipi": "IPI", "v_ipi": "IPI", "ipi_devolvido": "IPI Devolvido",
    "xml_ipi_proprio": "IPI Destacado no XML", "xml_ipi_devol": "IPI Devolvido no XML",
    "p_icms": "Alíquota de ICMS (%)",
    "n_sped": "Linhas no SPED", "n_xml": "Linhas no XML",
    "casado_por_ia": "Relacionado por IA", "ia_confianca": "Confiança da IA",
    "ia_justificativa": "Justificativa da IA",
    "ia_sped_descr": "Descrição no SPED (IA)", "ia_xml_descr": "Descrição no XML (IA)",
    "ind_emit": "Emissão", "ind_oper": "Entrada/Saída", "mod": "Modelo",
    "toma_name": "Tomador", "idx": "Ordem",
}


def _humanizar(df):
    """Cabeçalho em português. O que não está no mapa perde o underscore."""
    return df.rename(columns={
        c: RENOMEAR.get(c, str(c).replace("_", " ").strip().capitalize())
        for c in df.columns
    })


def generate_excel_from_results(data: dict) -> io.BytesIO:
    """
    Recebe o dicionário com os resultados da auditoria (task.result_data)
    e retorna um objeto BytesIO contendo o arquivo Excel com as abas geradas dinamicamente.
    """
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine='openpyxl') as writer:

        # Helper para adicionar aba apenas se houver dados
        def add_sheet_if_exists(key, sheet_name):
            items = data.get(key, [])
            if items and len(items) > 0:
                _humanizar(pd.DataFrame(items)).to_excel(
                    writer, sheet_name=sheet_name, index=False)

        # Mapeamento dinâmico baseado no JSON
        add_sheet_if_exists("unmapped_items", "Novos CFOPs")
        add_sheet_if_exists("unmapped_cte_items", "Novos CFOPs (CT-e)")
        add_sheet_if_exists("icms_issues", "Divergências ICMS")
        add_sheet_if_exists("ipi_issues", "Divergências IPI")
        add_sheet_if_exists("pis_cofins_issues", "PIS e COFINS")
        # Crédito de PIS/COFINS sobre frete: contábil do CT-e menos o ICMS.
        add_sheet_if_exists("pis_cofins_frete", "PIS-COFINS Frete")
        add_sheet_if_exists("missing_xmls", "XMLs Faltantes")
        add_sheet_if_exists("unrecorded_invoices", "Malha Fina (SPED)")
        add_sheet_if_exists("value_discrepancies", "Divergências de Valor")
        # Emitente escriturado × emitente do XML — quem é o fornecedor da nota.
        add_sheet_if_exists("cnpj_emitente_issues", "CNPJ do Emitente")
        add_sheet_if_exists("anticipation_results", "Antecipação ICMS")
        
        # DIFAL (Informativo)
        add_sheet_if_exists("difal_interestadual", "DIFAL Interestadual")
        add_sheet_if_exists("difal_interno", "DIFAL Interno")

        # ICMS Monofásico de combustíveis. A aba sai com as quatro parcelas
        # separadas (próprio / retido / cobrado antes / diferido), como na tela.
        add_sheet_if_exists("icms_mono", "ICMS Mono")

        # Itens que o motor NÃO conseguiu cruzar com o XML. É a lista que o
        # contador confere à mão, então vai para o Excel com o MOTIVO junto —
        # sem ele a planilha diz que o item ficou de fora, mas não por quê, e
        # quem abre não sabe o que procurar na nota.
        nao_conferidos = data.get("itens_nao_conferidos", [])
        if nao_conferidos and len(nao_conferidos) > 0:
            df_nc = pd.DataFrame(nao_conferidos)
            rename_nc = {
                "num": "N° NF",
                "num_item": "Item",
                "emit_name": "Fornecedor",
                "cnpj_emitente": "CNPJ Fornecedor",
                "cod_item": "Código no SPED",
                "descr": "Descrição no SPED",
                "cfop_sped": "CFOP SPED",
                "vl_item": "Valor do Item",
                "vl_icms": "ICMS no SPED",
                "motivo": "Por que não foi conferido",
                "key": "Chave de Acesso",
            }
            cols_nc = [c for c in rename_nc.keys() if c in df_nc.columns]
            df_nc = df_nc[cols_nc].rename(columns=rename_nc)
            df_nc.to_excel(writer, sheet_name="Itens Não Conferidos", index=False)

        # Relatório Geral de NFs: uma linha por item de nota, com os impostos do
        # XML lado a lado com o CFOP escriturado. A tela pagina de 50 em 50; a
        # planilha sai INTEIRA — é justamente ela que o escritório abre para
        # conferir o mês todo de uma vez.
        relatorio_itens = data.get("relatorio_geral", [])
        if relatorio_itens and len(relatorio_itens) > 0:
            df_rel = pd.DataFrame(relatorio_itens)
            rename_rel = {
                "num_nf": "N° NF",
                "cnpj_emitente": "CNPJ Emitente",
                "uf_emitente": "UF",
                "razao_social": "Razão Social",
                "descr_item": "Descrição Item",
                "vl_total": "Valor Total",
                "bc_icms": "BC ICMS",
                "icms": "ICMS",
                "icms_sn": "ICMS SN",
                "ipi": "IPI",
                "ipi_devolvido": "IPI Devolvido",
                "st": "ST",
                "fcp_st": "FCP ST",
                "icms_mono": "ICMS Mono",
                "cfop_xml": "CFOP XML",
                "cfop_sped": "CFOP SPED",
                "key": "Chave de Acesso",
            }
            cols_rel = [c for c in rename_rel.keys() if c in df_rel.columns]
            df_rel = df_rel[cols_rel].rename(columns=rename_rel)
            df_rel.to_excel(writer, sheet_name="Relatório de NFs", index=False)

        # Planilha CT-e (12 colunas extraídas do XML)
        cte_items = data.get("cte_xml_list", [])
        if cte_items and len(cte_items) > 0:
            df_cte = pd.DataFrame(cte_items)
            rename_map = {
                "num": "N° CT-e",
                "emit_cnpj": "CNPJ Emitente",
                "emit_uf": "UF Emit.",
                "rem_name": "Remetente",
                "dest_name": "Destinatário",
                "toma_name": "Tomador",
                "nfe_nums": "NF-E Transporte",
                "nfe_keys": "Chave NF-e",
                "nfe_cfop_sped": "CFOP NF-e",
                "vl_doc": "Valor Total",
                "vl_bc": "Base ICMS",
                "vl_icms": "ICMS"
            }
            cols_present = [c for c in rename_map.keys() if c in df_cte.columns]
            df_cte = df_cte[cols_present].rename(columns=rename_map)
            df_cte.to_excel(writer, sheet_name="Planilha CT-e", index=False)

        # Auditoria sem nenhum apontamento ainda precisa gerar arquivo: planilha
        # que não abre parece erro do sistema, não resultado limpo.
        if not writer.sheets:
            pd.DataFrame({'Situação': ['Nenhuma divergência encontrada nesta auditoria.']}
                         ).to_excel(writer, sheet_name='Sem Divergências', index=False)

        wb = writer.book

        resumo = data.get("resumo", {}) or {}
        periodo = ""
        # `or ''` e não o default do get: o SPED sem 0000 legível grava None na
        # chave, e a chave EXISTE — sem isto a capa sairia com "None a None".
        inicio = data.get("periodo_inicio") or ""
        fim = data.get("periodo_fim") or ""
        if inicio or fim:
            periodo = f"{inicio} a {fim}"

        # `posicao=0` já insere a capa como primeira aba — é a que a pessoa vê
        # ao abrir o arquivo.
        montar_capa(
            wb,
            titulo="Auditoria de Entradas",
            subtitulo=data.get("company_name", "") or "",
            linhas=[
                ("Empresa", data.get("company_name", "")),
                ("CNPJ", data.get("company_cnpj", "")),
                ("Período apurado", periodo),
                ("Gerado em", hora_local(datetime.utcnow()).strftime("%d/%m/%Y às %H:%M")),
                ("", ""),
                ("Notas processadas", int(resumo.get("notas_processadas", 0) or 0)),
                ("Inconsistências encontradas", int(resumo.get("inconsistencias", 0) or 0)),
                ("Avisos informativos (DIFAL)", int(resumo.get("informativos", 0) or 0)),
            ],
        )

        estilizar_planilha(wb, cores=CORES_DAS_ABAS,
                           sem_zebra=("Divergências IPI",))

        # Nota de devolução vai destacada na aba de IPI, como na tela: o valor
        # do XML ali é a soma de vIPI + vIPIDevol e não bate com o que a pessoa
        # vê ao abrir o arquivo, então é a linha que precisa de olho humano.
        if "Divergências IPI" in wb.sheetnames:
            destacar_linhas(wb["Divergências IPI"], "IPI Devolvido no XML",
                            lambda v: float(v or 0) > 0.01)

    output.seek(0)
    return output
