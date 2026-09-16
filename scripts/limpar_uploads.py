# -*- coding: utf-8 -*-
"""Descarta os SPEDs e XMLs parados em temp_uploads/.

O `execute_audit` apaga os arquivos ao terminar, mas três caminhos não passam
por lá e deixam tudo em disco: a auditoria que falhou antes do motor, a que
ficou esperando a confirmação da IA e o servidor reiniciou, e a que nunca rodou.
São documentos fiscais de clientes acumulando sem ninguém saber.

SIMULA POR PADRÃO. Sem `--aplicar` ele só lista o que apagaria — mesma escolha
de `limpar_regras_globais.py`: apagar documento de cliente não pode ser o efeito
de quem rodou o comando só para olhar.

Uso:
    venv/Scripts/python.exe scripts/limpar_uploads.py             # só mostra
    venv/Scripts/python.exe scripts/limpar_uploads.py --dias 15   # outro prazo
    venv/Scripts/python.exe scripts/limpar_uploads.py --aplicar   # apaga

O prazo padrão é 7 dias. Uma auditoria em andamento tem arquivos tocados há
minutos, e a que espera confirmação expira em 1 hora — sete dias está muito
acima dos dois, então não há como esta rotina apagar trabalho em curso.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.core.limpeza_uploads import DIAS_PADRAO, limpar_uploads_antigos

TEMP_DIR = os.path.join(os.getcwd(), "temp_uploads")


def mb(n: int) -> str:
    return f"{n / (1024 * 1024):,.1f} MB".replace(",", ".")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dias", type=int, default=DIAS_PADRAO,
                   help=f"apagar o que está parado há mais de N dias (padrão: {DIAS_PADRAO})")
    p.add_argument("--aplicar", action="store_true",
                   help="apaga de verdade; sem esta opção, apenas simula")
    p.add_argument("--dir", default=TEMP_DIR, help="pasta a limpar")
    args = p.parse_args()

    if not os.path.isdir(args.dir):
        print(f"Pasta não encontrada: {args.dir}")
        return 1

    modo = "APAGANDO" if args.aplicar else "SIMULAÇÃO (nada será apagado)"
    print(f"{modo} — {args.dir}")
    print(f"Critério: pasta de upload parada há mais de {args.dias} dia(s).\n")

    r = limpar_uploads_antigos(args.dir, args.dias, simular=not args.aplicar)

    for item in sorted(r["detalhe"], key=lambda d: -d["bytes"])[:20]:
        print(f"   {item['pasta']}  {item['arquivos']:>6} arquivos  {mb(item['bytes']):>12}")
    if len(r["detalhe"]) > 20:
        print(f"   ... e mais {len(r['detalhe']) - 20} pasta(s)")

    print()
    verbo = "apagadas" if args.aplicar else "seriam apagadas"
    print(f"   {r['apagadas']} pasta(s) {verbo}  ({mb(r['bytes'])} liberados)")
    print(f"   {r['mantidas']} pasta(s) mantidas (mexidas há menos de {args.dias} dia(s))")
    if r["erros"]:
        print(f"   {r['erros']} pasta(s) não puderam ser apagadas — ver log")

    if not args.aplicar and r["apagadas"]:
        print("\nNada foi apagado. Rode de novo com --aplicar para valer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
