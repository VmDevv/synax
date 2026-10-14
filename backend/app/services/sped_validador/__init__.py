"""Validador de EFD ICMS IPI — confere o arquivo antes da transmissão.

Uso:

    from backend.app.services.sped_validador import validar_bytes
    resultado = validar_bytes(conteudo)
    for i in resultado.inconsistencias:
        print(i.linha, i.registro, i.campo, i.regra, i.mensagem, i.gravidade)

É biblioteca, não tela: a aba "Validar SPED" usa, e a auditoria pode usar do
mesmo jeito. Os dados do PVA estão em `dados/` (gerados por
`scripts/extrair_dados_pva.py`); a regra de cada conferência, em `motor.py`.
"""
from .motor import (ADVERTENCIA, ERRO, IMPLEMENTADAS, ArquivoNaoValidavel,
                    Inconsistencia, LeiauteInesperado, LeiauteSemDescritor,
                    Resultado, detectar_leiaute, validar_arquivo,
                    validar_bytes, validar_texto)

__all__ = [
    "ADVERTENCIA", "ERRO", "IMPLEMENTADAS", "ArquivoNaoValidavel",
    "Inconsistencia", "LeiauteInesperado", "LeiauteSemDescritor", "Resultado",
    "detectar_leiaute", "validar_arquivo", "validar_bytes", "validar_texto",
]
