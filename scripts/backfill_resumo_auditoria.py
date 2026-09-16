# -*- coding: utf-8 -*-
"""Copia para colunas próprias o que já existia dentro de `result_data`.

São três colunas, e as três nasceram do mesmo problema: `result_data` guarda o
relatório inteiro de cada auditoria — alguns MB por linha —, e nenhuma tela pode
abrir centenas deles para desenhar um número.

  - `notas_processadas` e `inconsistencias` (02/09/2026, com o painel do Radar);
  - `competencia` (03/09/2026, com a ficha da empresa): o mês do SPED, lido do
    registro 0000. É ela que responde "o abril desta empresa já foi conferido?",
    coisa que a data do upload não responde — reprocessar abril em setembro é
    rotina.

O motor passou a gravar as três ao concluir. Tudo que rodou ANTES está nulo, e
as telas contam essas auditorias à parte em vez de fingir que deram zero ou
chutar um mês. Este script lê o JSON de cada auditoria antiga UMA vez e copia o
que achar — colunas já preenchidas não são tocadas.

Por que é um script e não um passo do startup: ler centenas de relatórios é
coisa de minutos, e o servidor não pode ficar preso nisso para subir.

SIMULA POR PADRÃO — mesma convenção de limpar_uploads.py e
limpar_regras_globais.py.

Uso:
    venv/Scripts/python.exe scripts/backfill_resumo_auditoria.py             # só mostra
    venv/Scripts/python.exe scripts/backfill_resumo_auditoria.py --aplicar   # grava
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import or_, select  # noqa: E402

from backend.app.core import competencia as comp  # noqa: E402
from backend.app.domain.models.audit import AuditTask  # noqa: E402
from backend.app.infrastructure.database.session import SessionLocal  # noqa: E402

LOTE = 50


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--aplicar", action="store_true",
                   help="grava de verdade; sem esta opção, apenas simula")
    p.add_argument("--limite", type=int, default=0,
                   help="processa no máximo N auditorias (0 = todas)")
    args = p.parse_args()

    db = SessionLocal()
    try:
        # Só os IDs primeiro. Trazer os objetos inteiros de uma vez significaria
        # carregar todos os result_data na memória ao mesmo tempo.
        stmt = (select(AuditTask.id)
                .where(AuditTask.status == "concluido")
                .where(or_(AuditTask.inconsistencias.is_(None),
                           AuditTask.competencia.is_(None)))
                .order_by(AuditTask.created_at.desc()))
        ids = [linha[0] for linha in db.execute(stmt).all()]
        if args.limite:
            ids = ids[:args.limite]

        modo = "GRAVANDO" if args.aplicar else "SIMULAÇÃO (nada será gravado)"
        print(f"{modo}")
        print(f"{len(ids)} auditoria(s) concluída(s) com alguma coluna em branco.\n")
        if not ids:
            print("Nada a fazer — o histórico já está preenchido.")
            return 0

        preenchidas = com_competencia = sem_resumo = sem_periodo = ilegiveis = 0
        soma_notas = soma_incons = 0

        for i, task_id in enumerate(ids, 1):
            tarefa = db.query(AuditTask).filter(AuditTask.id == task_id).first()
            if not tarefa or not tarefa.result_data:
                sem_resumo += 1
                continue

            try:
                relatorio = json.loads(tarefa.result_data) or {}
            except (ValueError, TypeError):
                # JSON truncado ou gravado por uma versão antiga. Deixar nulo é
                # o certo: dizer zero seria afirmar que a auditoria não achou
                # nada, quando o que houve foi não conseguir ler.
                ilegiveis += 1
                continue

            resumo = relatorio.get("resumo") or {}
            tem_contadores = ("notas_processadas" in resumo
                              or "inconsistencias" in resumo)
            # A competência sai do mesmo relatório, mas por outro caminho — uma
            # auditoria pode ter um e não ter o outro, e cada coluna é
            # preenchida por conta própria.
            nova_comp = comp.de_data(relatorio.get("periodo_inicio"))

            if tarefa.inconsistencias is None and tem_contadores:
                notas = int(resumo.get("notas_processadas") or 0)
                incons = int(resumo.get("inconsistencias") or 0)
                soma_notas += notas
                soma_incons += incons
                preenchidas += 1
                if args.aplicar:
                    tarefa.notas_processadas = notas
                    tarefa.inconsistencias = incons
            elif tarefa.inconsistencias is None:
                sem_resumo += 1

            if tarefa.competencia is None and nova_comp:
                com_competencia += 1
                if args.aplicar:
                    tarefa.competencia = nova_comp
            elif tarefa.competencia is None:
                # Relatório sem período legível. A coluna fica nula: um mês
                # chutado faria a ficha da empresa afirmar que a competência
                # foi conferida.
                sem_periodo += 1

            if args.aplicar and i % LOTE == 0:
                db.commit()

            # O objeto sai da sessão para que o result_data que ele carrega
            # possa ser liberado — sem isto a sessão segura todos na memória.
            if not args.aplicar:
                db.expunge(tarefa)

            if i % 25 == 0:
                print(f"   ... {i}/{len(ids)}")

        if args.aplicar:
            db.commit()

        print()
        verbo = "preenchida(s)" if args.aplicar else "seriam preenchida(s)"
        print(f"   {preenchidas} auditoria(s) com contadores {verbo}")
        print(f"   somando {soma_notas:,} notas e {soma_incons:,} inconsistências"
              .replace(",", "."))
        print(f"   {com_competencia} auditoria(s) com competência {verbo}")
        if sem_resumo:
            print(f"   {sem_resumo} sem bloco 'resumo' no relatório (ficam nulas)")
        if sem_periodo:
            print(f"   {sem_periodo} sem período legível no relatório (ficam nulas)")
        if ilegiveis:
            print(f"   {ilegiveis} com relatório ilegível (ficam nulas)")

        if not args.aplicar and (preenchidas or com_competencia):
            print("\nNada foi gravado. Rode de novo com --aplicar para valer.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
