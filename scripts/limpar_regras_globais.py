# -*- coding: utf-8 -*-
"""Apaga do banco toda regra global ou com curinga.

Decisão do escritório (25/08/2026): ICMS e IPI seguem a mesma linha do CFOP —
regra é do tenant, com chave exata, sem curinga. Duas coisas violavam isso e
este script limpa as duas:

1. Regras marcadas `is_global=True` (ou gravadas no tenant fantasma "GLOBAL").
   O motor buscava `tenant_id == meu OU is_global`, então uma regra que não era
   de ninguém decidia crédito de ICMS/IPI para todas as empresas do sistema.

2. Regras com "*" ou "TODOS" na chave. No IPI o "*" no CST fazia uma resposta
   valer para qualquer CST daquele CFOP; no CFOP o "TODOS" produzia regra que
   o motor nunca acha, porque a busca é por chave exata.

Roda sem gravar por padrão. Para apagar de verdade:

    venv/Scripts/python.exe scripts/limpar_regras_globais.py --aplicar

A tabela `cest_mva_rules` NÃO é tocada: ela é a Portaria 16 (dado legal), não
regra de crédito, e é lida direto, fora do escopo por tenant.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import or_

from backend.app.infrastructure.database.session import SessionLocal
from backend.app.domain.models.rules import (
    CfopCreditRule, CfopClassification, CteCfopClassification,
    IpiCreditRule, PisCofinsCreditRule, IcmsAnticipationRule,
)

CURINGAS = ("*", "TODOS")

# (modelo, rótulo, campos que compõem a chave e não podem ser curinga)
ALVOS = [
    (CfopCreditRule,        "ICMS  (cfop_credit_rules)",           []),
    (IpiCreditRule,         "IPI   (ipi_credit_rules)",            ["cst_expected"]),
    (PisCofinsCreditRule,   "PIS/COFINS (pis_cofins_credit_rules)", ["cst_expected"]),
    (IcmsAnticipationRule,  "Antecipação ICMS (icms_anticipation_rules)", ["uf_origem", "uf_destino", "cfop"]),
    (CfopClassification,    "CFOP  (cfop_classifications)",        ["company_cnpj", "supplier_cnpj", "product_code"]),
    (CteCfopClassification, "CT-e  (cte_cfop_classifications)",    ["company_cnpj"]),
]


def _condicao_global(model):
    return or_(model.is_global == True, model.tenant_id == "GLOBAL")  # noqa: E712


def _condicao_curinga(model, campos):
    if not campos:
        return None
    return or_(*[getattr(model, c).in_(CURINGAS) for c in campos])


def limpar(aplicar: bool) -> int:
    db = SessionLocal()
    total = 0
    try:
        for model, rotulo, campos_chave in ALVOS:
            globais = db.query(model).filter(_condicao_global(model)).all()

            cond_curinga = _condicao_curinga(model, campos_chave)
            curingas = db.query(model).filter(cond_curinga).all() if cond_curinga is not None else []

            # Uma regra pode ser global E curinga; identidade de objeto evita contar duas vezes.
            a_apagar = {id(r): r for r in globais}
            a_apagar.update({id(r): r for r in curingas})

            print(f"\n{rotulo}")
            print(f"  globais (is_global ou tenant GLOBAL): {len(globais)}")
            if campos_chave:
                print(f"  com curinga em {'/'.join(campos_chave)}: {len(curingas)}")
            print(f"  a apagar: {len(a_apagar)}")

            for r in list(a_apagar.values())[:10]:
                descr = getattr(r, "description", "") or getattr(r, "entry_cfop", "")
                print(f"    - tenant={r.tenant_id} {descr!s:.60}")
            if len(a_apagar) > 10:
                print(f"    ... e mais {len(a_apagar) - 10}")

            if aplicar:
                for r in a_apagar.values():
                    db.delete(r)
            total += len(a_apagar)

        if aplicar:
            db.commit()
            print(f"\nApagadas {total} regras.")
        else:
            db.rollback()
            print(f"\nSIMULAÇÃO: {total} regras seriam apagadas. Rode com --aplicar para valer.")
        return total
    except Exception as e:
        db.rollback()
        print(f"Erro: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    limpar(aplicar="--aplicar" in sys.argv)
