"""Resolução de certificados A1 guardados na rede.

Regras de segurança observadas aqui:
  - a senha só é decifrada no instante do uso e nunca vai para log;
  - o conteúdo do .pfx nunca é gravado em outro lugar;
  - o acesso é sempre filtrado pelo tenant do usuário (camada de aplicação).
"""
import os
import re
import logging
from datetime import date

from sqlalchemy.orm import Session

from ...domain.models.certificates import TenantCertificate

logger = logging.getLogger(__name__)

PASTA_PADRAO = r"\\srv-ap02\Desenvolvimento\Certificados"


def pasta() -> str:
    return os.environ.get("CERTIFICADOS_DIR") or PASTA_PADRAO


def listar_arquivos() -> list[str]:
    try:
        return [a for a in os.listdir(pasta()) if a.lower().endswith((".pfx", ".p12"))]
    except OSError:
        logger.warning("Pasta de certificados indisponível: %s", pasta(), exc_info=True)
        return []


def localizar_por_cnpj(cnpj: str, arquivos: list[str] | None = None) -> str | None:
    """Acha o .pfx cujo nome contém o CNPJ. None se não houver correspondência.

    Nem todo arquivo traz o CNPJ no nome; nesses casos o vínculo tem de ser
    feito manualmente e fica gravado em TenantCertificate.arquivo.
    """
    limpo = re.sub(r"\D", "", cnpj or "")
    if len(limpo) != 14:
        return None
    for nome in (arquivos if arquivos is not None else listar_arquivos()):
        if limpo in re.sub(r"\D", "", nome):
            return nome
    return None


def caminho_do_arquivo(nome: str) -> str:
    # Só o nome do arquivo é aceito: impede que um valor no banco aponte para
    # fora da pasta de certificados.
    return os.path.join(pasta(), os.path.basename(nome or ""))


def carregar_material(db: Session, tenant_id: str, cnpj: str) -> tuple[bytes, str]:
    """Devolve (conteúdo do .pfx, senha em claro) para uso imediato.

    Levanta ValueError com mensagem apresentável quando algo falta.
    """
    registro = db.query(TenantCertificate).filter_by(
        tenant_id=str(tenant_id), cnpj=re.sub(r"\D", "", cnpj or "")
    ).first()

    if not registro:
        raise ValueError("Certificado não cadastrado para esta empresa.")
    if not registro.ativo:
        raise ValueError("Certificado marcado como inativo.")

    nome = registro.arquivo or localizar_por_cnpj(registro.cnpj)
    if not nome:
        raise ValueError(
            "Arquivo do certificado não localizado na pasta. "
            "Vincule o arquivo manualmente a esta empresa."
        )

    caminho = caminho_do_arquivo(nome)
    try:
        with open(caminho, "rb") as f:
            pfx = f.read()
    except OSError as err:
        raise ValueError(f"Não foi possível ler o certificado '{nome}'.") from err

    try:
        senha = registro.senha()
    except Exception as err:
        # Chave Fernet trocada ou registro corrompido.
        raise ValueError("Não foi possível decifrar a senha do certificado.") from err

    return pfx, senha


def carregar_todos(db: Session, tenant_ids: list[str] | None = None) -> dict:
    """Todos os certificados numa consulta só, indexados por tenant_id.

    Evita o N+1: com 167 empresas, uma consulta por empresa custava ~29s
    (175ms de rede cada) só para montar a tela.
    """
    q = db.query(TenantCertificate)
    if tenant_ids is not None:
        if not tenant_ids:
            return {}
        q = q.filter(TenantCertificate.tenant_id.in_([str(t) for t in tenant_ids]))
    return {c.tenant_id: c for c in q.all()}


def cnpj_de_outra_empresa(nome_arquivo: str | None, cnpj: str) -> str | None:
    """O CNPJ escrito no NOME do arquivo, quando ele é de outra empresa (raiz
    diferente). Matriz no lugar da filial passa — a consulta informa o CNPJ da
    filial ao ADN. É um aviso barato, sem abrir o .pfx; quem barra de verdade
    é o CNPJ de DENTRO do certificado (adn_client)."""
    limpo = re.sub(r"\D", "", cnpj or "")
    blocos = re.findall(r"(?<!\d)\d{14}(?!\d)", nome_arquivo or "")
    if len(limpo) != 14 or not blocos or any(b[:8] == limpo[:8] for b in blocos):
        return None
    return blocos[0]


def diagnosticar(registro, cnpj: str, arquivos: list[str] | None = None) -> dict:
    """Estado do certificado a partir de dados já carregados — sem tocar o banco."""
    if not registro:
        return {"ok": False, "motivo": "Sem certificado cadastrado"}
    nome = registro.arquivo or localizar_por_cnpj(cnpj, arquivos)
    if not nome:
        return {"ok": False, "motivo": "Arquivo não encontrado na pasta"}
    outro = cnpj_de_outra_empresa(nome, cnpj)
    if outro:
        return {"ok": False, "motivo": f"Certificado de outra empresa ({outro})"}
    if arquivos is not None:
        if nome not in arquivos:
            return {"ok": False, "motivo": "Arquivo ausente na pasta"}
    elif not os.path.exists(caminho_do_arquivo(nome)):
        return {"ok": False, "motivo": "Arquivo ausente na pasta"}
    if not registro.ativo:
        return {"ok": False, "motivo": "Certificado inativo"}
    return {"ok": True, "arquivo": nome, "validade": registro.validade}


def diagnostico(db: Session, tenant_id: str, cnpj: str) -> dict:
    """Versão de conveniência para UMA empresa (consulta o banco)."""
    limpo = re.sub(r"\D", "", cnpj or "")
    registro = db.query(TenantCertificate).filter_by(
        tenant_id=str(tenant_id), cnpj=limpo).first()
    return diagnosticar(registro, limpo)


# ---------------------------------------------------------------- validade ---

# Um A1 vale 1 ano. 30 dias é prazo suficiente para renovar sem correria.
DIAS_ALERTA = 30


def classificar_validade(registro, diag: dict, hoje=None) -> dict:
    """Traduz o estado do certificado em cor, rótulo e dias restantes.

    Lê a validade JÁ GRAVADA no banco. Abrir os 164 arquivos .pfx a cada
    carregamento da tela custaria ~36 s (medido); a data é derivada uma vez por
    `atualizar_validades` e relida de graça daqui em diante.

    grupo: 'ok' | 'alerta' | 'problema' | 'desconhecido' — usado no resumo.
    """
    hoje = hoje or date.today()

    if not diag.get("ok"):
        motivo = diag.get("motivo") or "Indisponível"
        # Certificado inativo é decisão do escritório, não uma falha.
        grupo = "desconhecido" if "inativo" in motivo.lower() else "problema"
        cor = "slate" if grupo == "desconhecido" else "red"
        return {"cor": cor, "rotulo": motivo, "dias": None,
                "validade": None, "grupo": grupo}

    validade = getattr(registro, "validade", None)
    if not validade:
        return {"cor": "slate", "rotulo": "Validade não verificada", "dias": None,
                "validade": None, "grupo": "desconhecido"}

    dia = validade.date() if hasattr(validade, "date") else validade
    dias = (dia - hoje).days

    if dias < 0:
        return {"cor": "red", "rotulo": f"Expirado há {abs(dias)} dia(s)",
                "dias": dias, "validade": dia, "grupo": "problema"}
    if dias == 0:
        return {"cor": "amber", "rotulo": "Vence hoje", "dias": 0,
                "validade": dia, "grupo": "alerta"}
    if dias <= DIAS_ALERTA:
        return {"cor": "amber", "rotulo": f"Vence em {dias} dia(s)", "dias": dias,
                "validade": dia, "grupo": "alerta"}
    return {"cor": "emerald", "rotulo": f"Válido · {dias} dias", "dias": dias,
            "validade": dia, "grupo": "ok"}


def ler_validade(pfx: bytes, senha: str):
    """Data de expiração dentro do X.509. None se o arquivo não abrir."""
    from .adn_client import abrir_pkcs12  # importado aqui: evita ciclo no módulo
    try:
        _, cert, _ = abrir_pkcs12(pfx, senha)
    except Exception:
        return None
    if cert is None:
        return None
    # `not_valid_after` está depreciado nas versões novas do cryptography.
    bruto = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    return bruto.replace(tzinfo=None)


def atualizar_validades(db: Session, tenant_ids: list[str] | None = None) -> dict:
    """Abre cada .pfx, grava a validade no banco e devolve o resumo.

    Operação cara (~36 s para os 164 certificados: ler da rede, decifrar a senha
    e abrir o PKCS#12): roda sob comando, não a cada tela. O resultado fica no
    banco e a listagem volta a ser instantânea.
    """
    registros = carregar_todos(db, tenant_ids)
    arquivos = listar_arquivos()
    resumo = {"lidos": 0, "atualizados": 0, "sem_arquivo": 0, "nao_abriram": 0}

    for registro in registros.values():
        nome = registro.arquivo or localizar_por_cnpj(registro.cnpj, arquivos)
        if not nome or nome not in arquivos:
            resumo["sem_arquivo"] += 1
            continue
        try:
            with open(caminho_do_arquivo(nome), "rb") as f:
                pfx = f.read()
            senha = registro.senha()
        except Exception:
            resumo["sem_arquivo"] += 1
            continue

        resumo["lidos"] += 1
        validade = ler_validade(pfx, senha)
        if validade is None:
            resumo["nao_abriram"] += 1
            registro.ultimo_erro = "Não foi possível abrir o certificado."
            continue
        if registro.validade != validade:
            resumo["atualizados"] += 1
        registro.validade = validade
        registro.ultimo_erro = None

    db.commit()
    return resumo
