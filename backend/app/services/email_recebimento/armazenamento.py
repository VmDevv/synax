"""Onde o XML que chegou por e-mail fica em disco.

`<raiz>/<empresa>/<AAAA-MM>/<chave>.xml`. O nome é a CHAVE de propósito: é por
ela, no nome do arquivo, que o motor da auditoria acha o XML de cada nota
(`cross_checker.execute_audit` indexa `xmls_map` pelo nome). Um anexo chamado
"nota fiscal.xml" copiado com esse nome nunca seria achado.

A raiz é `EMAIL_XML_DIR` ou `recebidos_email/` na pasta do servidor — fora de
`temp_uploads/`, que é esvaziada no startup. O XML fica `DIAS_RETENCAO` dias:
cobre o ciclo de fechamento (a nota de agosto é auditada em setembro, às
vezes em outubro) e não deixa documento de cliente parado sem prazo.
"""
import os
import re

DIAS_RETENCAO = 90
_SEGURO = re.compile(r"^[0-9a-fA-F-]{1,36}$")


def raiz() -> str:
    return os.environ.get("EMAIL_XML_DIR") or os.path.join(os.getcwd(), "recebidos_email")


def relativo(tenant_id: str, competencia: str | None, chave: str) -> str:
    """Caminho relativo à raiz. Só aceita o que o próprio sistema produz —
    id de empresa, AAAA-MM e 44 dígitos —, para um valor torto do banco não
    virar caminho fora da raiz."""
    if not _SEGURO.match(str(tenant_id)):
        raise ValueError("id de empresa inválido")
    mes = competencia if re.fullmatch(r"\d{4}-\d{2}", competencia or "") else "sem-data"
    if not re.fullmatch(r"\d{44}", chave or ""):
        raise ValueError("chave inválida")
    return os.path.join(str(tenant_id), mes, f"{chave}.xml")


def relativo_sped(tenant_id: str, competencia: str | None, sha256: str) -> str:
    """`<empresa>/<AAAA-MM>/sped/<hash>.txt` — o nome é o hash do conteúdo, e
    o nome original fica no banco (é dado do remetente, não vira caminho)."""
    if not _SEGURO.match(str(tenant_id)):
        raise ValueError("id de empresa inválido")
    mes = competencia if re.fullmatch(r"\d{4}-\d{2}", competencia or "") else "sem-data"
    if not re.fullmatch(r"[0-9a-f]{64}", sha256 or ""):
        raise ValueError("hash inválido")
    return os.path.join(str(tenant_id), mes, "sped", f"{sha256[:24]}.txt")


def absoluto(caminho_relativo: str) -> str | None:
    """O caminho em disco, ou None se ele escapar da raiz."""
    base = os.path.abspath(raiz())
    alvo = os.path.abspath(os.path.join(base, caminho_relativo))
    if os.path.commonpath([base, alvo]) != base:
        return None
    return alvo


def gravar(caminho_relativo: str, dados: bytes) -> None:
    alvo = absoluto(caminho_relativo)
    if alvo is None:
        raise ValueError("caminho fora da raiz")
    os.makedirs(os.path.dirname(alvo), exist_ok=True)
    temporario = alvo + ".parcial"
    with open(temporario, "wb") as f:
        f.write(dados)
    os.replace(temporario, alvo)


def apagar(caminho_relativo: str) -> None:
    alvo = absoluto(caminho_relativo)
    if alvo is None:
        return
    try:
        os.remove(alvo)
    except OSError:
        pass
