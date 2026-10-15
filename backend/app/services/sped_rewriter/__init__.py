"""Motor de reescrita do SPED: aplica as correções aceitas e refaz a cascata.

Uso típico:

    from backend.app.services.sped_rewriter import (
        CorrecaoItem, Patch, aplicar, ler)

    doc = ler("SPED_042026.txt")
    resultado = aplicar(doc, Patch([
        CorrecaoItem(documento=chave_nfe, num_item="3",
                     campos={"CFOP": "1102", "VL_ICMS": Decimal("224.64")},
                     motivo="revenda, não consumo", origem="regra aprovada #412",
                     aprovada_por="joao@vm.com.br"),
    ]))
    doc.gravar("SPED_042026_corrigido.txt")

O original NUNCA é sobrescrito — `gravar` recebe outro caminho de propósito.
"""
from .cascata import (Resultado, ajustar_cst_do_credito, aplicar,
                      aplicar_contribuicoes, espelhar_d100,
                      reconstruir_bloco_9, reconstruir_c190,
                      reconstruir_totais_de_blocos)
from .documento import (Documento, Registro, ler, ler_texto,
                        nome_sugerido, nova_linha)
from .ponte import Proposta, montar_patch, propor, propor_contribuicoes
from .previa import Previa, prever
from .previa import texto as texto_da_previa
from .relatorio import Relatorio, linhas_para_planilha, montar, para_json, texto
from .patch import Alteracao, CorrecaoFrete, CorrecaoItem, Patch, Recusa

__all__ = [
    "Alteracao", "CorrecaoFrete", "CorrecaoItem", "Documento", "Patch",
    "Recusa", "Registro", "Relatorio", "Resultado", "ajustar_cst_do_credito",
    "aplicar", "aplicar_contribuicoes", "espelhar_d100",
    "ler", "ler_texto", "linhas_para_planilha", "montar", "para_json",
    "Previa", "Proposta", "montar_patch", "nome_sugerido", "nova_linha", "prever",
    "propor", "propor_contribuicoes", "reconstruir_bloco_9",
    "reconstruir_c190", "texto_da_previa",
    "reconstruir_totais_de_blocos",
    "texto",
]
