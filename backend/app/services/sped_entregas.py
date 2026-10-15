"""Entrega da EFD: amarrar o arquivo transmitido ao arquivo validado no Synax.

A assinatura e a transmissão continuam no PVA. O Synax só LÊ o que o PVA gera
— o arquivo assinado e o recibo `.REC` — para responder duas perguntas: "a
competência desta empresa já foi entregue?" e "o que foi entregue é o mesmo
arquivo que passou no validador?".

A segunda pergunta se responde com o hash do CORPO do arquivo: tudo até o
fechamento do registro 9999, inclusive. Por quê até ali, e não o arquivo
inteiro: a ajuda do PVA (p. 10) diz que, quando o arquivo é importado e
validado sem edição, "a assinatura do arquivo será alocada ao final do arquivo
texto original". O arquivo assinado é, portanto, o original com um bloco a
mais depois do 9999. O hash do arquivo inteiro mudaria na assinatura; o do
corpo, não — e muda, como deve, quando alguém edita a escrituração no PVA e
"gera arquivo para entrega" de novo.

O leitor do `.REC` entra quando houver recibo real para conferir o formato e o
algoritmo do hash dele; inventar o formato daria um leitor que parece funcionar.
"""
from __future__ import annotations

import hashlib
import io
import re

_BOM = b"\xef\xbb\xbf"
# O registro 9999 até o pipe que fecha o QTD_LIN. Pelo registro, não pela
# linha: se o original não terminar em quebra de linha, a assinatura pode vir
# grudada na mesma linha do 9999.
_FIM_9999 = re.compile(rb"^\|9999\|[^|\r\n]*\|")


def _hash_do_fluxo(fluxo) -> str:
    h = hashlib.sha256()
    pendente = b""
    primeira = True
    for linha in fluxo:
        if primeira:
            if linha.startswith(_BOM):
                linha = linha[len(_BOM):]
            primeira = False
        fim = _FIM_9999.match(linha)
        if fim:
            h.update(pendente)
            h.update(fim.group(0))
            return h.hexdigest()
        h.update(pendente)
        pendente = linha
    # Sem 9999 (arquivo truncado ou inválido): o arquivo inteiro, sem a quebra
    # de linha final, que não é conteúdo.
    h.update(pendente.rstrip(b"\r\n"))
    return h.hexdigest()


def hash_do_corpo(bruto: bytes) -> str:
    """SHA-256 do arquivo até o fim do registro 9999 (BOM fora)."""
    return _hash_do_fluxo(io.BytesIO(bruto))


def hash_do_corpo_arquivo(caminho: str) -> str:
    """O mesmo, lendo do disco em fluxo — SPED de 300 MB não vai inteiro à RAM."""
    with open(caminho, "rb") as f:
        return _hash_do_fluxo(f)
