"""Cadastra empresas a partir dos certificados A1 da pasta da rede.

Para cada .pfx: descobre a senha, abre o certificado, lê a RAZÃO SOCIAL e o
CNPJ de dentro do próprio X.509 (fonte mais confiável que o nome do arquivo),
cria a empresa se ainda não existir e grava a senha CIFRADA.

As empresas nascem SEM operador vinculado — o vínculo é feito depois pela tela
de Administração.

Uso (a partir da raiz do projeto):
    ./venv/Scripts/python.exe scripts/cadastrar_empresas_por_certificado.py --simular
    ./venv/Scripts/python.exe scripts/cadastrar_empresas_por_certificado.py --aplicar

Nenhuma senha é exibida na saída.
"""
import argparse
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.abspath("."))

from dotenv import load_dotenv
load_dotenv()

import openpyxl
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.core.security import encrypt_data
from backend.app.domain.models.auth_models import Tenant
from backend.app.domain.models.certificates import TenantCertificate
from backend.app.services.nfse import certificados

PLANILHA_PADRAO = (r"\\srv-dc02\documentos\Contratos e Alterações"
                   r"\__Certificados\_Certificados Senha.xlsx")

# ICP-Brasil: o CNPJ do titular vai neste OID dentro do subjectAltName.
OID_CNPJ_ICP = "2.16.76.1.3.3"


def senhas_da_planilha(caminho: str) -> tuple[dict, list]:
    """Devolve ({cnpj: senha}, [todas as senhas]) — para casar por CNPJ ou tentativa."""
    if not os.path.exists(caminho):
        return {}, []
    ws = openpyxl.load_workbook(caminho, data_only=True).active
    por_cnpj, todas = {}, []
    for r in range(3, ws.max_row + 1):
        cnpj = re.sub(r"\D", "", str(ws.cell(r, 2).value or ""))
        senha = str(ws.cell(r, 5).value or "").strip()
        if not senha:
            continue
        todas.append(senha)
        if len(cnpj) == 14:
            por_cnpj.setdefault(cnpj, senha)
    # dedup preservando ordem
    vistas, unicas = set(), []
    for s in todas:
        if s not in vistas:
            vistas.add(s)
            unicas.append(s)
    return por_cnpj, unicas


def dados_do_certificado(pfx: bytes, senha: str):
    """(razao_social, cnpj) lidos de dentro do certificado. None se a senha falhar."""
    try:
        _, cert, _ = pkcs12.load_key_and_certificates(pfx, senha.encode("utf-8"))
    except Exception:
        return None
    if cert is None:
        return None

    cn = ""
    try:
        atrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if atrs:
            cn = atrs[0].value or ""
    except Exception:
        pass

    # e-CNPJ ICP-Brasil: CN costuma ser "RAZAO SOCIAL:CNPJ"
    razao, cnpj = cn, ""
    if ":" in cn:
        razao, _, resto = cn.partition(":")
        cnpj = re.sub(r"\D", "", resto)

    if len(cnpj) != 14:
        # tenta o OID oficial no subjectAltName
        try:
            from cryptography import x509
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            for nome in san:
                bruto = getattr(getattr(nome, "value", None), "value", None) or b""
                achado = re.findall(rb"\d{14}", bruto if isinstance(bruto, bytes) else b"")
                if achado:
                    cnpj = achado[0].decode()
                    break
        except Exception:
            pass

    if len(cnpj) != 14:
        return None
    return razao.strip()[:255] or f"Empresa {cnpj}", cnpj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--planilha", default=os.environ.get("CERT_PLANILHA", PLANILHA_PADRAO))
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument("--simular", action="store_true")
    ap.add_argument("--tentar-senhas", action="store_true",
                    help="para arquivos sem CNPJ no nome, testa as senhas da planilha")
    args = ap.parse_args()
    aplicar = args.aplicar and not args.simular

    arquivos = certificados.listar_arquivos()
    if not arquivos:
        print(f"ERRO: nenhum certificado em {certificados.pasta()}")
        return 2
    print(f"certificados na pasta: {len(arquivos)}")

    por_cnpj, todas_senhas = senhas_da_planilha(args.planilha)
    print(f"senhas na planilha   : {len(por_cnpj)} por CNPJ, {len(todas_senhas)} distintas")

    url = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1)
    db = sessionmaker(bind=create_engine(url))()

    existentes = {re.sub(r"\D", "", t.cnpj or ""): t for t in db.query(Tenant).all()}
    print(f"empresas ja no Synax : {len(existentes)}")
    print()

    criadas = cert_novos = cert_atualizados = 0
    sem_senha = ilegiveis = 0

    for nome in sorted(arquivos):
        caminho = certificados.caminho_do_arquivo(nome)
        try:
            with open(caminho, "rb") as f:
                pfx = f.read()
        except OSError:
            ilegiveis += 1
            continue

        # 1) senha pelo CNPJ do nome do arquivo
        info = None
        for c in re.findall(r"\d{14}", nome):
            if c in por_cnpj:
                info = dados_do_certificado(pfx, por_cnpj[c])
                if info:
                    senha_ok = por_cnpj[c]
                    break
        # 2) sem CNPJ no nome: tenta as senhas conhecidas
        if not info and args.tentar_senhas:
            for s in todas_senhas:
                info = dados_do_certificado(pfx, s)
                if info:
                    senha_ok = s
                    break

        if not info:
            sem_senha += 1
            continue

        razao, cnpj = info
        empresa = existentes.get(cnpj)
        if not empresa:
            criadas += 1
            if aplicar:
                empresa = Tenant(id=uuid.uuid4(), name=razao, cnpj=cnpj)
                db.add(empresa)
                db.flush()
                existentes[cnpj] = empresa

        if not aplicar:
            cert_novos += 1
            continue

        registro = db.query(TenantCertificate).filter_by(
            tenant_id=str(empresa.id), cnpj=cnpj).first()
        if registro:
            registro.senha_encrypted = encrypt_data(senha_ok)
            registro.arquivo = nome
            registro.razao_social = razao
            registro.ativo = True
            cert_atualizados += 1
        else:
            db.add(TenantCertificate(
                tenant_id=str(empresa.id), cnpj=cnpj, arquivo=nome,
                senha_encrypted=encrypt_data(senha_ok),
                razao_social=razao, ativo=True))
            cert_novos += 1

    if aplicar:
        db.commit()

    print(f"empresas {'criadas' if aplicar else 'a criar'}      : {criadas}")
    print(f"certificados vinculados : {cert_novos} novo(s), {cert_atualizados} atualizado(s)")
    print(f"sem senha conhecida     : {sem_senha}"
          + ("" if args.tentar_senhas else "  (use --tentar-senhas para ampliar)"))
    print(f"arquivos ilegiveis      : {ilegiveis}")
    print()
    print("As empresas criadas ficam SEM operador vinculado.")
    print("MODO: " + ("APLICADO no banco" if aplicar else "SIMULAÇÃO — nada gravado"))

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
