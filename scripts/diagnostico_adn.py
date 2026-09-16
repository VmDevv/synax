"""Diagnóstico da conexão com o ADN da NFS-e.

Responde, com medida e não com suposição, por que a consulta não traz notas:
o certificado abre? o servidor aceita o mTLS? qual formato de URL responde?
o que exatamente vem no corpo?

Nenhuma senha é exibida.

Uso (a partir da raiz do projeto):
    ./venv/Scripts/python.exe scripts/diagnostico_adn.py --cnpj 12345678000199
    ./venv/Scripts/python.exe scripts/diagnostico_adn.py --empresa "MASSUCATTI"
    ./venv/Scripts/python.exe scripts/diagnostico_adn.py --cnpj ... --nsu 0
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.abspath("."))

from dotenv import load_dotenv
load_dotenv()

import httpx
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.domain.models.auth_models import Tenant
from backend.app.services.nfse import adn_client, certificados

BASE = adn_client.URL_BASE.rstrip("/")


def formatos(nsu: int) -> list[tuple[str, str, dict]]:
    """Variações plausíveis de chamada, para descobrir qual o ADN aceita."""
    return [
        ("NSU no caminho",          f"{BASE}/{nsu}", {}),
        ("NSU em query",            BASE,            {"NSU": nsu}),
        ("NSU em query minúsculo",  BASE,            {"nsu": nsu}),
    ]


def dados_do_cert(pfx: bytes, senha: str) -> str:
    _, cert, _ = pkcs12.load_key_and_certificates(pfx, senha.encode("utf-8"))
    cn = ""
    atrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if atrs:
        cn = atrs[0].value or ""
    return f"{cn}  |  válido até {cert.not_valid_after_utc:%d/%m/%Y}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cnpj", default="")
    ap.add_argument("--empresa", default="", help="trecho da razão social")
    ap.add_argument("--nsu", type=int, default=0)
    args = ap.parse_args()

    url = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1)
    db = sessionmaker(bind=create_engine(url))()

    consulta = db.query(Tenant)
    if args.empresa:
        consulta = consulta.filter(Tenant.name.ilike(f"%{args.empresa}%"))
    if args.cnpj:
        limpo = re.sub(r"\D", "", args.cnpj)
        consulta = consulta.filter(Tenant.cnpj.ilike(f"%{limpo}%"))
    empresa = consulta.first()
    if not empresa:
        print("Nenhuma empresa encontrada com esse filtro.")
        return 2

    cnpj = re.sub(r"\D", "", empresa.cnpj or "")
    print(f"empresa : {empresa.name}")
    print(f"CNPJ    : {cnpj}")
    print(f"URL base: {BASE}")
    print()

    try:
        pfx, senha = certificados.carregar_material(db, str(empresa.id), cnpj)
    except ValueError as err:
        print(f"[X] certificado indisponível: {err}")
        return 2

    try:
        print(f"[ok] certificado abre: {dados_do_cert(pfx, senha)}")
    except Exception as err:
        print(f"[X] o certificado NÃO abre com a senha gravada: {type(err).__name__}")
        return 2
    print()

    with adn_client._material_mtls(pfx, senha) as (cert_path, key_path):
        with httpx.Client(cert=(cert_path, key_path), timeout=60.0,
                          headers={"Accept": "application/json"}) as cliente:
            for rotulo, alvo, params in formatos(args.nsu):
                print(f"--- {rotulo}")
                print(f"    GET {alvo}" + (f"  params={params}" if params else ""))
                try:
                    r = cliente.get(alvo, params=params)
                except httpx.HTTPError as err:
                    print(f"    ERRO DE REDE/TLS: {type(err).__name__}: {err}")
                    print()
                    continue
                corpo = (r.text or "").strip().replace("\n", " ")
                print(f"    HTTP {r.status_code}  content-type={r.headers.get('content-type', '?')}")
                print(f"    corpo: {corpo[:400] or '(vazio)'}")
                if r.status_code == 200:
                    try:
                        docs = adn_client._extrair_documentos(r.json())
                        print(f"    documentos reconhecidos: {len(docs)}")
                        if docs:
                            print(f"    primeiro XML (200 car.): {docs[0][1][:200]}")
                    except ValueError:
                        print("    corpo não é JSON")
                print()

    db.close()
    print("Leitura do resultado:")
    print("  HTTP 200 + documentos      -> o formato certo é esse; ajuste ADN_NFSE_URL se preciso.")
    print("  HTTP 403/401               -> certificado sem permissão para o CNPJ (falta procuração).")
    print("  HTTP 404 em todos          -> a URL base está errada.")
    print("  200 com lote vazio         -> conexão OK, mas o município não distribui pelo ADN nacional.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
