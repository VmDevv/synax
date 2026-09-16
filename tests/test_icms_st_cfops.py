# -*- coding: utf-8 -*-
"""Quais CFOPs de entrada entram no cálculo de ICMS ST.

Pedido do Sandro em 01/09/2026: incluir **1910 e 2910** (entrada de bonificação,
doação ou brinde) na regra de ST, que já cobria 1102/2102 (compra para
comercialização) e 1403/2403 (compra para comercialização com ST).

A razão de bonificação entrar é a mesma das compras: a mercadoria entra no
estoque para ser REVENDIDA, e o ST é devido ainda que não tenha havido pagamento
ao fornecedor.

A lista era literal no meio do laço do motor (`in ["1102", "2102", ...]`). Virou
`CFOPS_REVENDA_ST`, para o próximo CFOP ser um item numa lista e não uma caçada
dentro de `execute_audit`.

Cuidado ao mexer: esta lista é o critério de ENTRADA na aba, não de crédito.
Quem decide se há ST a recolher é a regra de CEST/MVA da Portaria 16, consultada
depois. Acrescentar CFOP aqui faz notas novas aparecerem na aba de ST.
"""
import io
import os
import sys

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('backend'))

from app.services.audit_engine.cross_checker import CFOPS_REVENDA_ST  # noqa: E402
from app.domain.constants.cfops_entrada import CFOPS_ENTRADA  # noqa: E402

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


ESPERADOS = ("1102", "2102", "1403", "2403", "1910", "2910")


# ============================================================================
print("\n=== 1. A lista de CFOPs ===")

for cfop in ESPERADOS:
    check(f"{cfop} entra no cálculo de ST", cfop in CFOPS_REVENDA_ST, CFOPS_REVENDA_ST)

check("são exatamente esses seis, sem sobra",
      set(CFOPS_REVENDA_ST) == set(ESPERADOS),
      set(CFOPS_REVENDA_ST) ^ set(ESPERADOS))

# Os pares interno/interestadual andam juntos: incluir 1910 sem 2910 deixaria a
# nota de fora do estado passar batida.
for interno in ("1102", "1403", "1910"):
    externo = "2" + interno[1:]
    check(f"o par {interno}/{externo} está completo",
          (interno in CFOPS_REVENDA_ST) == (externo in CFOPS_REVENDA_ST))

# Todo CFOP da lista precisa existir no catálogo, ou o seletor da tela e os
# rótulos ficariam sem descrição.
for cfop in CFOPS_REVENDA_ST:
    check(f"{cfop} está no catálogo de CFOPs de entrada", cfop in CFOPS_ENTRADA)

check("1910 é bonificação/doação/brinde",
      "bonifica" in CFOPS_ENTRADA.get("1910", "").lower(), CFOPS_ENTRADA.get("1910"))
check("2910 é a versão interestadual",
      "bonifica" in CFOPS_ENTRADA.get("2910", "").lower()
      and "interestadual" in CFOPS_ENTRADA.get("2910", "").lower(),
      CFOPS_ENTRADA.get("2910"))


# ============================================================================
print("\n=== 2. O motor usa a constante, não uma lista solta ===")

MOTOR = io.open(os.path.join("backend", "app", "services", "audit_engine",
                             "cross_checker.py"), encoding="utf-8").read()

check("a decisão consulta CFOPS_REVENDA_ST",
      "if str(sped_cfop).strip() in CFOPS_REVENDA_ST:" in MOTOR)
check("a lista literal antiga sumiu do laço",
      'in ["1102", "2102", "1403", "2403"]' not in MOTOR,
      "a lista fixa voltou para dentro do motor")
check("a constante é declarada uma vez só",
      MOTOR.count("CFOPS_REVENDA_ST = ") == 1, MOTOR.count("CFOPS_REVENDA_ST = "))


# ============================================================================
print("\n=== 3. O rótulo da tela diz a verdade ===")

# O badge é o que informa ao contador quais operações a aba cobre. Se ele ficar
# defasado, a aba passa a mostrar notas que o rótulo nega.
TELA = io.open(os.path.join("frontend", "templates", "partials", "curation",
                            "curation_results.html"), encoding="utf-8").read()

linha_badge = [l for l in TELA.splitlines() if "Revenda (" in l]
check("existe o rótulo dos CFOPs de revenda", len(linha_badge) == 1, linha_badge)
if linha_badge:
    rotulo = linha_badge[0]
    for cfop in CFOPS_REVENDA_ST:
        check(f"o rótulo cita {cfop}", cfop in rotulo, rotulo.strip()[:120])
    check("o rótulo não cita CFOP que não está na regra",
          not [c for c in ("1101", "2101", "1556")
               if c in rotulo and c not in CFOPS_REVENDA_ST],
          rotulo.strip()[:120])


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

sys.exit(1 if FALHAS else 0)
