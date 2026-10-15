"""Os XMLs que chegaram por e-mail, do jeito que as telas consomem.

**Na auditoria, a escolha é pela CHAVE das notas do SPED, não pelo mês.** A
nota emitida em 30/07 e recebida em 02/08 está escriturada no SPED de agosto,
mas a competência dela (a data de saída do fornecedor) é julho — escolher pelo
mês a deixaria de fora e a auditoria a acusaria como "XML faltante" com o XML
guardado aqui. O SPED já diz quais notas ele tem: C100 campo 09 (CHV_NFE) e
D100 campo 10 (CHV_CTE), nas mesmas posições nos dois leiautes.

**No gerador de SPED, vão TODOS os XMLs da empresa** que estão guardados: o
gerador já sabe separar os meses, escolher o mais recente e oferecer as notas
dos meses anteriores para a 1ª quinzena. Filtrar aqui duplicaria essa regra.
"""
import os
import shutil
from datetime import datetime

from sqlalchemy import func

from ...domain.models.email_recebido import EmailXml
from . import armazenamento

# (registro, índice do campo da chave no split por "|")
_CAMPO_CHAVE = {"C100": 9, "D100": 10}


def chaves_do_sped(caminho: str) -> set[str]:
    """As chaves de acesso dos C100 e D100 do arquivo. Lê linha a linha, sem
    carregar o SPED inteiro (um arquivo de 300 MB é possível)."""
    chaves = set()
    with open(caminho, "rb") as f:
        for bruta in f:
            # O BOM só pode estar na primeira linha, que é o |0000|.
            if not (bruta.startswith(b"|C100|") or bruta.startswith(b"|D100|")):
                continue
            partes = bruta.decode("latin-1").split("|")
            indice = _CAMPO_CHAVE.get(partes[1] if len(partes) > 1 else "")
            if indice and len(partes) > indice:
                chave = partes[indice].strip()
                if len(chave) == 44 and chave.isdigit():
                    chaves.add(chave)
    return chaves


def _copiar(linhas, destino: str) -> int:
    os.makedirs(destino, exist_ok=True)
    copiados = 0
    for arquivo, chave in linhas:
        origem = armazenamento.absoluto(arquivo)
        if not origem or not os.path.isfile(origem):
            continue
        # Nome = chave: é como o motor da auditoria acha o XML de cada nota.
        shutil.copyfile(origem, os.path.join(destino, f"{chave}.xml"))
        copiados += 1
    return copiados


def copiar_para_auditoria(db, tenant_id: str, sped_path: str, destino: str) -> tuple[int, int]:
    """Copia os XMLs recebidos cujas chaves estão no SPED. (copiados, chaves no SPED)."""
    chaves = chaves_do_sped(sped_path)
    if not chaves:
        return 0, 0
    linhas, lista = [], sorted(chaves)
    # Em lotes: o IN com milhares de chaves passa do limite de parâmetros do
    # SQLite e fica pesado no Postgres.
    for i in range(0, len(lista), 500):
        linhas += (db.query(EmailXml.arquivo, EmailXml.chave)
                   .filter(EmailXml.tenant_id == str(tenant_id),
                           EmailXml.chave.in_(lista[i:i + 500])).all())
    return _copiar(linhas, destino), len(chaves)


def copiar_todos(db, tenant_id: str, destino: str) -> int:
    linhas = (db.query(EmailXml.arquivo, EmailXml.chave)
              .filter(EmailXml.tenant_id == str(tenant_id)).all())
    return _copiar(linhas, destino)


def por_competencia(db, tenant_id: str) -> list[dict]:
    """[{competencia, nfe, cte, total, ultimo}] da empresa, do mês mais recente
    para o mais antigo."""
    linhas = (db.query(EmailXml.competencia, EmailXml.modelo, func.count(EmailXml.id),
                       func.max(EmailXml.created_at))
              .filter(EmailXml.tenant_id == str(tenant_id))
              .group_by(EmailXml.competencia, EmailXml.modelo).all())
    meses = {}
    for competencia, modelo, qtd, ultimo in linhas:
        m = meses.setdefault(competencia or "", {"competencia": competencia or "",
                                                 "nfe": 0, "cte": 0, "total": 0, "ultimo": None})
        m["nfe" if modelo == "NF-e" else "cte"] += qtd
        m["total"] += qtd
        if ultimo and (m["ultimo"] is None or ultimo > m["ultimo"]):
            m["ultimo"] = ultimo
    return sorted(meses.values(), key=lambda m: m["competencia"], reverse=True)


def por_empresa(db, tenant_ids: list[str] | None) -> list[dict]:
    """Resumo para a página: por empresa e competência. `tenant_ids=None` = todas."""
    consulta = db.query(EmailXml.tenant_id, EmailXml.competencia, EmailXml.modelo,
                        func.count(EmailXml.id), func.max(EmailXml.created_at))
    if tenant_ids is not None:
        if not tenant_ids:
            return []
        consulta = consulta.filter(EmailXml.tenant_id.in_([str(t) for t in tenant_ids]))
    grupos = {}
    for tenant_id, competencia, modelo, qtd, ultimo in (
            consulta.group_by(EmailXml.tenant_id, EmailXml.competencia, EmailXml.modelo).all()):
        g = grupos.setdefault((tenant_id, competencia or ""), {
            "tenant_id": tenant_id, "competencia": competencia or "",
            "nfe": 0, "cte": 0, "total": 0, "ultimo": None})
        g["nfe" if modelo == "NF-e" else "cte"] += qtd
        g["total"] += qtd
        if ultimo and (g["ultimo"] is None or ultimo > g["ultimo"]):
            g["ultimo"] = ultimo
    return sorted(grupos.values(), key=lambda g: g["ultimo"] or datetime.min, reverse=True)


# ------------------------------------------ SPED por e-mail e a caixa inicial

def sped_da_empresa(db, sped_id: str, tenant_id: str):
    """O SPED recebido, só se for DESTA empresa (a rota já validou a empresa
    para o perfil; aqui se garante que o id não é de outra)."""
    from ...domain.models.email_recebido import EmailSped
    if not sped_id:
        return None
    return (db.query(EmailSped)
            .filter(EmailSped.id == str(sped_id), EmailSped.tenant_id == str(tenant_id)).first())


def caminho_do_sped(sped) -> str | None:
    alvo = armazenamento.absoluto(sped.arquivo)
    return alvo if alvo and os.path.isfile(alvo) else None


def arquivos_para_zip(db, tenant_id: str, sped=None, competencia: str | None = None) -> list:
    """[(caminho em disco, nome no ZIP)]. Com SPED: os XMLs das notas dele
    (pela chave) e o próprio SPED; sem: os recebidos da empresa no mês."""
    from ..email_recebimento.sieg_download import chaves_de_entrada
    if sped is not None:
        caminho = caminho_do_sped(sped)
        chaves = chaves_de_entrada(caminho) if caminho else []
        linhas = []
        for i in range(0, len(chaves), 500):
            linhas += (db.query(EmailXml.arquivo, EmailXml.chave)
                       .filter(EmailXml.tenant_id == str(tenant_id),
                               EmailXml.chave.in_(chaves[i:i + 500])).all())
        saida = [(caminho, f"SPED_{sped.competencia or 'sem-data'}.txt")] if caminho else []
    else:
        consulta = db.query(EmailXml.arquivo, EmailXml.chave).filter(
            EmailXml.tenant_id == str(tenant_id))
        consulta = (consulta.filter(EmailXml.competencia == competencia) if competencia
                    else consulta.filter(EmailXml.competencia.is_(None)))
        linhas, saida = consulta.all(), []
    for arquivo, chave in linhas:
        origem = armazenamento.absoluto(arquivo)
        if origem and os.path.isfile(origem):
            saida.append((origem, f"xmls/{chave}.xml"))
    return saida


def speds_recebidos(db, tenant_ids: list[str] | None) -> list:
    """Os SPEDs guardados (os mais novos primeiro), para a tela E-mails."""
    from ...domain.models.email_recebido import EmailSped
    if tenant_ids is not None and not tenant_ids:
        return []
    consulta = db.query(EmailSped)
    if tenant_ids is not None:
        consulta = consulta.filter(EmailSped.tenant_id.in_([str(t) for t in tenant_ids]))
    return consulta.order_by(EmailSped.created_at.desc()).limit(100).all()


def excluir_sped(db, sped) -> int:
    """Apaga o SPED recebido e os XMLs que o SIEG baixou PARA ELE. Devolve
    quantos XMLs saíram. O que chegou por e-mail fica — é de outra origem e
    pode servir a outra auditoria. O e-mail original e o SIEG não são tocados:
    é só a cópia do Synax."""
    xmls = (db.query(EmailXml).filter(EmailXml.origem == "sieg",
                                      EmailXml.mensagem_id == sped.id).all())
    for x in xmls:
        armazenamento.apagar(x.arquivo)
        db.delete(x)
    armazenamento.apagar(sped.arquivo)
    db.delete(sped)
    db.commit()
    return len(xmls)


def excluir_xmls(db, tenant_id: str, competencia: str | None) -> int:
    """Apaga os XMLs guardados da empresa naquele mês (de e-mail E do SIEG).
    Devolve quantos."""
    consulta = db.query(EmailXml).filter(EmailXml.tenant_id == str(tenant_id))
    consulta = (consulta.filter(EmailXml.competencia == competencia) if competencia
                else consulta.filter(EmailXml.competencia.is_(None)))
    xmls = consulta.all()
    for x in xmls:
        armazenamento.apagar(x.arquivo)
        db.delete(x)
    db.commit()
    return len(xmls)


def novidades(db, tenant_ids: list[str] | None, dias: int = 15) -> dict:
    """O que a caixa da tela inicial mostra: SPEDs recebidos (com o
    download do SIEG) e os XMLs que chegaram soltos, por empresa e mês.
    `tenant_ids=None` = todas (admin)."""
    from datetime import timedelta
    from ...domain.models.auth_models import Tenant
    from ...domain.models.email_recebido import EmailSped

    desde = datetime.utcnow() - timedelta(days=dias)
    if tenant_ids is not None and not tenant_ids:
        return {"speds": [], "xmls": []}
    nomes = {str(t.id): t.name for t in db.query(Tenant.id, Tenant.name).all()}
    consulta = db.query(EmailSped).filter(EmailSped.created_at >= desde)
    if tenant_ids is not None:
        consulta = consulta.filter(EmailSped.tenant_id.in_([str(t) for t in tenant_ids]))
    speds = [{"s": s, "empresa": nomes.get(s.tenant_id, "(empresa removida)")}
             for s in consulta.order_by(EmailSped.created_at.desc()).limit(30).all()]
    meses_com_sped = {(x["s"].tenant_id, x["s"].competencia) for x in speds}

    consulta = (db.query(EmailXml.tenant_id, EmailXml.competencia, EmailXml.modelo,
                         func.count(EmailXml.id), func.max(EmailXml.created_at))
                .filter(EmailXml.created_at >= desde,
                        (EmailXml.origem == "email") | EmailXml.origem.is_(None)))
    if tenant_ids is not None:
        consulta = consulta.filter(EmailXml.tenant_id.in_([str(t) for t in tenant_ids]))
    grupos = {}
    for tenant_id, competencia, modelo, qtd, ultimo in (
            consulta.group_by(EmailXml.tenant_id, EmailXml.competencia, EmailXml.modelo).all()):
        g = grupos.setdefault((tenant_id, competencia), {
            "tenant_id": tenant_id, "competencia": competencia,
            "empresa": nomes.get(tenant_id, "(empresa removida)"),
            "nfe": 0, "cte": 0, "ultimo": ultimo,
            # Já chegou o SPED desse mês: o item do SPED cobre estes XMLs.
            "tem_sped": (tenant_id, competencia) in meses_com_sped})
        g["nfe" if modelo == "NF-e" else "cte"] += qtd
        if ultimo and ultimo > g["ultimo"]:
            g["ultimo"] = ultimo
    xmls = sorted(grupos.values(), key=lambda g: g["ultimo"] or datetime.min, reverse=True)
    return {"speds": speds, "xmls": xmls}
