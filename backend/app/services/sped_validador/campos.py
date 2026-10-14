"""Conferências de UM valor, sem contexto: formato, data, dígito verificador.

Funções puras — recebem o texto do campo e devolvem a resposta. O motor decide
quando chamá-las; a auditoria pode chamá-las direto sobre um CNPJ ou uma chave
sem passar pelo arquivo.
"""
from __future__ import annotations

import calendar
import re
from decimal import Decimal, InvalidOperation

# Número do SPED: só dígitos, vírgula decimal, sem separador de milhar e sem
# sinal (Guia Prático, seção 2.4). "1.234,56", "-5" e "1234.56" são inválidos.
_NUMERO = re.compile(r"^\d+(,\d+)?$")
_DATA = re.compile(r"^\d{8}$")
_CONTROLE = re.compile(r"[\x00-\x1f\x7f]")


def numero(valor: str) -> Decimal | None:
    """Decimal do campo numérico, ou None se não estiver no formato do SPED."""
    if not _NUMERO.match(valor):
        return None
    try:
        return Decimal(valor.replace(",", "."))
    except InvalidOperation:
        return None


def casas_decimais(valor: str) -> int:
    return len(valor.split(",", 1)[1]) if "," in valor else 0


def digitos_significativos(valor: str) -> int:
    """Dígitos do número, sem a vírgula — é o que o "tamanho" de campo N mede."""
    return sum(1 for c in valor if c.isdigit())


def tem_controle(valor: str) -> bool:
    """Caractere de controle (tabulação, quebra) dentro de campo texto."""
    return bool(_CONTROLE.search(valor))


def data(valor: str) -> str | None:
    """"31012026" -> "20260131"; data inexistente ou fora do formato -> None."""
    if not _DATA.match(valor):
        return None
    dia, mes, ano = int(valor[0:2]), int(valor[2:4]), int(valor[4:8])
    if not (1 <= mes <= 12) or ano < 1:
        return None
    if not (1 <= dia <= calendar.monthrange(ano, mes)[1]):
        return None
    return f"{ano:04d}{mes:02d}{dia:02d}"


def br(aaaammdd: str) -> str:
    """"20260131" -> "31/01/2026", para mensagem."""
    if len(aaaammdd) != 8:
        return aaaammdd
    return f"{aaaammdd[6:8]}/{aaaammdd[4:6]}/{aaaammdd[0:4]}"


def _dv_mod11(valores: list[int], pesos: list[int]) -> int:
    resto = sum(v * p for v, p in zip(valores, pesos)) % 11
    return 0 if resto < 2 else 11 - resto


def cnpj_valido(valor: str) -> bool:
    """CNPJ numérico ou ALFANUMÉRICO (IN RFB 2.229/2024, a partir de 07/2026).

    No alfanumérico cada caractere vale o código ASCII menos 48 — "0".."9"
    continuam 0..9 e "A" vale 17 —, com os mesmos pesos; os dois dígitos
    verificadores seguem numéricos. O campo CNPJ do leiaute 020 já é "C",
    não "N": tratar só dígitos recusaria o CNPJ novo assim que ele aparecer.
    """
    if len(valor) != 14 or not valor[12:].isdigit():
        return False
    if not all(c.isdigit() or "A" <= c <= "Z" for c in valor[:12]):
        return False
    if len(set(valor)) == 1:
        return False
    numeros = [ord(c) - 48 for c in valor]
    dv1 = _dv_mod11(numeros[:12], [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    dv2 = _dv_mod11(numeros[:12] + [dv1],
                    [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    return numeros[12] == dv1 and numeros[13] == dv2


def cpf_valido(valor: str) -> bool:
    if len(valor) != 11 or not valor.isdigit() or len(set(valor)) == 1:
        return False
    d = [int(c) for c in valor]
    dv1 = _dv_mod11(d[:9], list(range(10, 1, -1)))
    dv2 = _dv_mod11(d[:9] + [dv1], list(range(11, 1, -1)))
    return d[9] == dv1 and d[10] == dv2


def chave_valida(valor: str) -> bool:
    """Chave de acesso de NF-e/CT-e/CF-e: 44 dígitos e DV módulo 11."""
    if len(valor) != 44 or not valor.isdigit():
        return False
    pesos = [2, 3, 4, 5, 6, 7, 8, 9]
    soma = sum(int(c) * pesos[i % 8]
               for i, c in enumerate(reversed(valor[:43])))
    resto = soma % 11
    dv = 0 if resto < 2 else 11 - resto
    return int(valor[43]) == dv
