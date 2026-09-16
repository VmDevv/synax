# -*- coding: utf-8 -*-
"""Gera o SPED corrigido fora da tela — para conferir o arquivo no PVA.

Motivo de existir: antes de o escritório entregar um arquivo retificado a um
cliente, alguém precisa importar esse arquivo no PVA da EFD ICMS/IPI e ver a
validação passar. Fazer isso pela tela funciona, mas o ciclo é lento (subir o
SPED, esperar a auditoria, marcar, prever, baixar) e não deixa rastro do que foi
testado. Aqui é um comando: entra o SPED + o JSON da auditoria, sai o arquivo
corrigido, o relatório e a lista do que foi recusado.

O que ele NÃO é: um atalho para pular a curadoria. Ele aplica as mesmas regras
do motor — inclusive as recusas — e imprime tudo. O que ele pula é só a tela.

Uso:

    venv/Scripts/python.exe scripts/gerar_sped_corrigido.py <sped.txt> <result_data.json>
    venv/Scripts/python.exe scripts/gerar_sped_corrigido.py <sped.txt> <result_data.json> --so cfop_produto,credito_ipi
    venv/Scripts/python.exe scripts/gerar_sped_corrigido.py --task <task_id> --apurar

`--apurar` reescreve TAMBÉM a apuração do ICMS (E110): leva a diferença de
crédito para o campo 06 e refaz o saldo. É a mesma caixa da tela, e continua
sendo escolha explícita — sem a opção, o Bloco E sai como entrou.

Com `--task`, o SPED sai de `temp_uploads/<task_id>/sped.txt` e o JSON sai do
`result_data` da auditoria no banco — é o mesmo par que a tela usaria.

Nada é sobrescrito: o arquivo corrigido nasce ao lado do original, com o nome
tirado do registro 0000 (`Sped_Corrigido_<EMPRESA>.txt`), e o relatório vai para
um `.json` com o mesmo nome. O original não é tocado em nenhuma hipótese.
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.services.sped_rewriter import (  # noqa: E402
    aplicar, ler_texto, montar, montar_patch, nome_sugerido, para_json,
    propor, texto as texto_do_relatorio)
from backend.app.services.sped_rewriter.documento import ENCODING  # noqa: E402
from backend.app.services.sped_rewriter.resumo_ia import resumir  # noqa: E402


def carregar_da_task(task_id: str):
    """(texto do SPED, result_data) da auditoria — o mesmo par que a tela usa."""
    from backend.app.domain.models.audit import AuditTask
    from backend.app.infrastructure.database.session import SessionLocal

    caminho = os.path.join(os.getcwd(), "temp_uploads", task_id, "sped.txt")
    if not os.path.exists(caminho):
        raise SystemExit(
            f"SPED da auditoria {task_id} não está em disco ({caminho}).\n"
            "Ele é descartado após 7 dias; rode a auditoria de novo."
        )

    sessao = SessionLocal()
    try:
        task = sessao.query(AuditTask).filter(AuditTask.id == task_id).first()
        if not task:
            raise SystemExit(f"Auditoria {task_id} não encontrada no banco.")
        dados = json.loads(task.result_data or "{}")
    finally:
        sessao.close()

    with open(caminho, "r", encoding=ENCODING, newline="") as f:
        return f.read(), dados, caminho


def main(argv) -> int:
    apurar = "--apurar" in argv
    if apurar:
        argv.remove("--apurar")
    somente = ""
    if "--so" in argv:
        i = argv.index("--so")
        somente = argv[i + 1] if len(argv) > i + 1 else ""
        del argv[i:i + 2]

    if "--task" in argv:
        i = argv.index("--task")
        if len(argv) <= i + 1:
            raise SystemExit("Informe o task_id depois de --task.")
        texto, dados, origem = carregar_da_task(argv[i + 1])
    elif len(argv) >= 3:
        origem = argv[1]
        with open(origem, "r", encoding=ENCODING, newline="") as f:
            texto = f.read()
        with open(argv[2], "r", encoding="utf-8") as f:
            dados = json.load(f)
    else:
        print(__doc__)
        return 2

    propostas = propor(dados, texto)
    if somente:
        opcoes = {o.strip() for o in somente.split(",") if o.strip()}
        propostas = [p for p in propostas if p.opcao in opcoes]

    aplicaveis = [p for p in propostas if p.aplicavel]
    impedidas = [p for p in propostas if not p.aplicavel]

    print("=" * 74)
    print(f"Arquivo de origem : {origem}")
    print(f"Propostas         : {len(propostas)}  "
          f"(aplicáveis: {len(aplicaveis)}, impedidas: {len(impedidas)})")
    print("=" * 74)

    if impedidas:
        print("\nNÃO SERÃO APLICADAS")
        for p in impedidas:
            print(f"  x [{p.opcao}] {p.alvo} — {p.impedimento}")

    if not aplicaveis:
        print("\nNada aplicável: o arquivo sairia idêntico ao original. "
              "Nenhum arquivo foi gerado.")
        return 1

    patch = montar_patch(propostas, [p.id for p in aplicaveis],
                         aprovada_por="scripts/gerar_sped_corrigido.py",
                         atualizar_apuracao=apurar)
    doc = ler_texto(texto, caminho=origem)
    resultado = aplicar(doc, patch)

    destino = os.path.join(os.path.dirname(os.path.abspath(origem)),
                           nome_sugerido(doc))
    relatorio = montar(resultado, doc, texto, origem=origem, destino=destino)

    print("\n" + texto_do_relatorio(relatorio))

    resumo, da_ia, motivo = resumir(relatorio)
    print(f"\nRESUMO ({'IA' if da_ia else 'sistema'}): {resumo}")
    if motivo:
        print(f"  (a IA não escreveu: {motivo})")
    print()

    doc.gravar(destino)
    with open(destino.rsplit(".", 1)[0] + ".json", "w", encoding="utf-8") as f:
        f.write(para_json(relatorio))

    if not apurar and relatorio.impacto.icms_diferenca:
        print("A apuração (E110) NÃO foi tocada. Para levar a diferença de "
              f"crédito ({relatorio.impacto.icms_diferenca}) até ela, rode de "
              "novo com --apurar.\n")

    print(f"Arquivo corrigido : {destino}")
    print(f"Relatório         : {destino.rsplit('.', 1)[0]}.json")
    print("\nPróximo passo: importar o arquivo acima no PVA da EFD ICMS/IPI "
          "(Escrituração Fiscal > Importar) e conferir a validação.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
