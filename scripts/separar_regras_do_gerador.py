# -*- coding: utf-8 -*-
"""Tira da tabela da AUDITORIA o que a tela do gerador de SPED gravou lá.

De 08/10/2026 (quando a tela de mapeamento de CFOP do gerador entrou no ar) até
a separação de 09/10/2026, a caixa "Salvar como regra" — marcada por padrão —
gravava em `cfop_classifications`, a tabela que o motor da AUDITORIA lê: a
regra feita para montar o arquivo de um cliente que não escritura passava a
decidir o que a auditoria acusa. Hoje as regras do gerador moram em
`gerador_cfop_regras`.

Como achar o que a tela gravou: ela punha no `created_by` o UUID do perfil, e
todas as rotas da auditoria gravam o e-mail. Daí as duas listas:

1. **Criadas pelo gerador** — `created_by` com cara de UUID e criadas desde
   08/10/2026. Com `--aplicar` elas são COPIADAS para `gerador_cfop_regras`
   (mesma chave, mesmo CFOP, mesmo status; o que já existir lá não é
   sobrescrito) e APAGADAS de `cfop_classifications`.
2. **Regra antiga alterada no período** — criada antes de 08/10 e alterada
   depois. Pode ter sido o gerador (quando a chave já existia, ele trocava o
   CFOP e o status sem mudar o `created_by`) ou alguém na auditoria ou na
   Curadoria. O banco não guarda o valor anterior: o script só LISTA, para
   alguém conferir.

O que o script não acha: regra do gerador que um admin aprovou depois na
Curadoria — a aprovação troca o `created_by` pelo e-mail de quem aprovou, e
ela fica igual a uma regra da auditoria.

Roda sem gravar por padrão:

    venv/Scripts/python.exe scripts/separar_regras_do_gerador.py
    venv/Scripts/python.exe scripts/separar_regras_do_gerador.py --aplicar
"""
import os
import re
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.domain.models.rules import CfopClassification, GeradorCfopRegra  # noqa: E402

# A tela do gerador que gravava na tabela da auditoria entrou no ar neste dia.
INICIO = datetime(2026, 10, 8)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _linha(r) -> str:
    return (f"tenant={r.tenant_id} empresa={r.company_cnpj} fornecedor={r.supplier_cnpj} "
            f"CFOP {r.supplier_cfop} cProd {r.product_code} -> {r.entry_cfop} "
            f"[{r.status}] criada {r.created_at:%d/%m/%Y %H:%M} por {r.created_by}")


def separar(db, aplicar: bool = False) -> dict:
    """Faz o trabalho numa sessão recebida (os testes passam a do SQLite).
    Sem `aplicar`, desfaz tudo no fim. Devolve o que achou, já em texto — depois
    do rollback os objetos expiram e não dá mais para lê-los —, e o que fez."""
    do_gerador = [r for r in (db.query(CfopClassification)
                              .filter(CfopClassification.created_at >= INICIO).all())
                  if _UUID.match(str(r.created_by or "").strip())]
    alteradas = (db.query(CfopClassification)
                 .filter(CfopClassification.created_at < INICIO,
                         CfopClassification.updated_at >= INICIO).all())
    achado = {"do_gerador": [_linha(r) for r in do_gerador],
              "alteradas": [f"{_linha(r)} · alterada {r.updated_at:%d/%m/%Y %H:%M}"
                            for r in alteradas]}

    copiadas = ja_existiam = 0
    for r in do_gerador:
        chave = dict(tenant_id=r.tenant_id, company_cnpj=r.company_cnpj,
                     supplier_cnpj=r.supplier_cnpj, supplier_cfop=r.supplier_cfop,
                     product_code=r.product_code)
        if db.query(GeradorCfopRegra).filter_by(**chave).first() is None:
            db.add(GeradorCfopRegra(**chave, entry_cfop=(r.entry_cfop or "")[:4],
                                    product_description=r.product_description,
                                    status=r.status, created_by=r.created_by))
            copiadas += 1
        else:
            ja_existiam += 1
        db.delete(r)

    if aplicar:
        db.commit()
    else:
        db.rollback()
    return {**achado, "copiadas": copiadas, "ja_existiam": ja_existiam}


def main(aplicar: bool) -> int:
    from backend.app.infrastructure.database.session import SessionLocal

    db = SessionLocal()
    try:
        achado = separar(db, aplicar)
    except Exception as e:                                # noqa: BLE001
        db.rollback()
        print(f"Erro: {e}")
        raise
    finally:
        db.close()

    print(f"\n1. Gravadas pela tela do gerador em cfop_classifications: "
          f"{len(achado['do_gerador'])}")
    for linha in achado["do_gerador"][:50]:
        print(f"    - {linha}")
    print(f"\n2. Regras da auditoria alteradas desde {INICIO:%d/%m/%Y} "
          f"(conferir à mão): {len(achado['alteradas'])}")
    for linha in achado["alteradas"][:50]:
        print(f"    - {linha}")

    if aplicar:
        print(f"\nGRAVADO: {achado['copiadas']} copiada(s) para gerador_cfop_regras "
              f"({achado['ja_existiam']} já existia(m) lá) e "
              f"{len(achado['do_gerador'])} apagada(s) de cfop_classifications.")
    else:
        print(f"\nSIMULAÇÃO: {len(achado['do_gerador'])} regra(s) sairiam da tabela "
              "da auditoria. Nada foi gravado — rode com --aplicar para valer.")
    return len(achado["do_gerador"])


if __name__ == "__main__":
    main(aplicar="--aplicar" in sys.argv)
