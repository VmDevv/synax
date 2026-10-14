"""De que lado da nota a empresa está: RECEBIDA (tomadora) ou EMITIDA
(prestadora).

O ADN entrega toda nota em que o CNPJ aparece — as que a empresa emitiu e as
que ela recebeu, misturadas —, e a tela mostrava prestador e tomador lado a
lado sem dizer qual dos dois era o cliente. Numa lista com todas as empresas
isso não se lê: em 01/10/2026 eram 678 recebidas e 138 emitidas na mesma
tabela. Uma regra só, usada pela tela e pelo relatório do lote, para os dois
nunca discordarem.

"Outra" é a nota em que a empresa não é nem uma coisa nem outra: tomador não
informado no XML, ou nota que chegou pela empresa errada (filial consultada
com o certificado da matriz, antes do `cnpjConsulta`). Ela aparece marcada,
não escondida.
"""
import re

RECEBIDA = "recebida"
EMITIDA = "emitida"
OUTRA = "outra"

ROTULOS = {RECEBIDA: "Recebida", EMITIDA: "Emitida", OUTRA: "Outro CNPJ"}


def digitos(texto) -> str:
    return re.sub(r"\D", "", texto or "")


def tipo(cnpj_empresa: str, prestador: str | None, tomador: str | None) -> str:
    cnpj = digitos(cnpj_empresa)
    if not cnpj:
        return OUTRA
    if prestador == cnpj:
        return EMITIDA
    if tomador == cnpj:
        return RECEBIDA
    return OUTRA


def descrever(nota, cnpj_empresa: str, nome_empresa: str = "") -> dict:
    """Tipo, rótulo e a CONTRAPARTE — quem está do outro lado: o prestador
    numa nota recebida, o tomador numa emitida."""
    t = tipo(cnpj_empresa, nota.prestador, nota.tomador)
    if t == EMITIDA:
        doc, nome = nota.tomador, nota.rz_tomador
    else:
        doc, nome = nota.prestador, nota.rz_prestador
    rotulo = ROTULOS[t]
    if t == OUTRA and not nota.tomador:
        rotulo = "Sem tomador"
    return {"tipo": t, "rotulo": rotulo, "empresa": nome_empresa,
            "contraparte_doc": doc or "", "contraparte_nome": nome or ""}


def retencoes_federais(nota) -> float:
    """IR + PIS + COFINS + CSLL + INSS retidos. O ISS fica de fora: a
    retenção dele vem como indicador ("ISS retido"), não somada aqui."""
    return sum(getattr(nota, c, 0.0) or 0.0 for c in ("ir", "pis", "cofins", "csll", "inss"))


def iss_retido(nota) -> float:
    return (nota.iss or 0.0) if (nota.iss_retido or "").lower().startswith("s") else 0.0
