# -*- coding: utf-8 -*-
"""Relê os XMLs de NFS-e já guardados e preenche as notas que ficaram vazias.

POR QUE ISTO EXISTE
-------------------
Até 02/09/2026 o leitor de NFS-e entendia só o layout ABRASF municipal. O que
chega pelo ADN é o layout NACIONAL, que não compartilha um nome de tag com ele —
então as notas baixadas entraram no banco com número, CNPJ do prestador e UF, e
todo o resto vazio ou R$ 0,00.

O leitor foi corrigido, mas as notas JÁ IMPORTADAS continuam do jeito que
entraram. Este script relê o XML de cada uma — que está guardado em disco, com o
caminho na coluna `arquivo_xml` — e regrava os campos.

NÃO reconsulta o ADN, de propósito: o ADN mantém cada documento por apenas 90
dias, e há notas de 2023 aqui. Os arquivos em disco são a única cópia que
sobrou. Reconsultar também exigiria certificado e senha e mexeria no cursor de
NSU, que é justamente o que não se pode perder.

A CHAVE DO DOCUMENTO MUDA
-------------------------
`prestador`, `numero` e `mun_prest` formam a chave primária. Duas delas mudam ao
reprocessar:

  - `mun_prest` estava VAZIO em todas as notas do ADN e passa a ter o código do
    município. Sem tratar isso, uma reimportação futura criaria linha duplicada.
  - `prestador` muda nas notas de prestador PESSOA FÍSICA: a busca antiga pegava
    o CNPJ do TOMADOR quando o emitente não tinha CNPJ.

Por isso a linha é removida e regravada quando a chave muda, dentro da mesma
transação. Quando a chave nova já existe, as duas são fundidas (fica a nova).

SIMULA POR PADRÃO — mesma convenção de limpar_uploads.py.

Uso:
    venv/Scripts/python.exe scripts/reprocessar_nfse.py                # só mostra
    venv/Scripts/python.exe scripts/reprocessar_nfse.py --tenant <id>  # uma empresa
    venv/Scripts/python.exe scripts/reprocessar_nfse.py --aplicar      # grava
"""
import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.domain.models.nfse import NfseDocument  # noqa: E402
from backend.app.infrastructure.database.session import SessionLocal  # noqa: E402
from backend.app.services.nfse import storage  # noqa: E402
from backend.app.services.nfse.abrasf_parser import parse_nfse_xml  # noqa: E402

# Campos que o parser devolve e que NÃO fazem parte da chave.
CHAVE = ("prestador", "numero", "mun_prest")


def _reler(rel: str) -> dict | None:
    """Relê o XML guardado. None quando o arquivo sumiu ou não é NFS-e."""
    conteudo = storage.ler(rel)
    if not conteudo:
        return None
    fd, tmp = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(conteudo)
        return parse_nfse_xml(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _preenchidos(dados: dict) -> int:
    """Quantos campos vieram com conteúdo — serve para medir o ganho."""
    return sum(1 for k, v in dados.items()
               if v not in (None, "", 0, 0.0, "-"))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--aplicar", action="store_true",
                   help="grava de verdade; sem esta opção, apenas simula")
    p.add_argument("--tenant", default="", help="limita a uma empresa (tenant_id)")
    p.add_argument("--limite", type=int, default=0, help="processa no máximo N notas")
    args = p.parse_args()

    db = SessionLocal()
    try:
        consulta = db.query(NfseDocument)
        if args.tenant:
            consulta = consulta.filter(NfseDocument.tenant_id == args.tenant)
        notas = consulta.all()
        if args.limite:
            notas = notas[:args.limite]

        print("GRAVANDO" if args.aplicar else "SIMULAÇÃO (nada será gravado)")
        print(f"Raiz dos XMLs: {storage.raiz()}")
        print(f"{len(notas)} nota(s) a reprocessar.\n")
        if not notas:
            return 0

        sem_arquivo = ilegivel = inalterada = 0
        atualizadas = rechaveadas = fundidas = 0
        antes = depois = 0

        # Índice das chaves existentes, para detectar colisão sem ir ao banco
        # a cada nota.
        existentes = {(n.tenant_id, n.prestador, n.numero, n.mun_prest or ""): n
                      for n in notas}

        for nota in notas:
            if not nota.arquivo_xml:
                sem_arquivo += 1
                continue

            dados = _reler(nota.arquivo_xml)
            if not dados:
                ilegivel += 1
                continue

            atual = {c: getattr(nota, c) for c in dados}
            if all(atual.get(k) == v for k, v in dados.items()):
                inalterada += 1
                continue

            antes += _preenchidos({c: getattr(nota, c) for c in dados})
            depois += _preenchidos(dados)

            chave_nova = (nota.tenant_id, dados["prestador"], dados["numero"],
                          dados["mun_prest"] or "")
            chave_velha = (nota.tenant_id, nota.prestador, nota.numero,
                           nota.mun_prest or "")

            if chave_nova == chave_velha:
                atualizadas += 1
                if args.aplicar:
                    for campo, valor in dados.items():
                        setattr(nota, campo, valor)
                continue

            # A chave mudou: some a linha antiga e entra a nova. Quando a chave
            # nova já pertence a outra linha, as duas eram a MESMA nota lida de
            # dois jeitos — fica uma só.
            colisao = existentes.get(chave_nova)
            if colisao is not None and colisao is not nota:
                fundidas += 1
                if args.aplicar:
                    for campo, valor in dados.items():
                        setattr(colisao, campo, valor)
                    colisao.arquivo_xml = nota.arquivo_xml or colisao.arquivo_xml
                    db.delete(nota)
                continue

            rechaveadas += 1
            if args.aplicar:
                novo = NfseDocument(tenant_id=nota.tenant_id,
                                    origem=nota.origem,
                                    arquivo_xml=nota.arquivo_xml,
                                    arquivo_pdf=nota.arquivo_pdf,
                                    **{**dados, "mun_prest": dados["mun_prest"] or ""})
                db.delete(nota)
                db.flush()          # a antiga sai antes de a nova entrar
                db.add(novo)
                existentes[chave_nova] = novo

        if args.aplicar:
            db.commit()

        print(f"   {atualizadas:>5} atualizadas no lugar")
        print(f"   {rechaveadas:>5} regravadas (a chave mudou: município ou prestador)")
        if fundidas:
            print(f"   {fundidas:>5} fundidas com uma linha que já existia")
        print(f"   {inalterada:>5} já estavam corretas")
        if sem_arquivo:
            print(f"   {sem_arquivo:>5} sem XML guardado (nada a reler)")
        if ilegivel:
            print(f"   {ilegivel:>5} com XML ausente no disco ou ilegível")

        mexidas = atualizadas + rechaveadas + fundidas
        if mexidas:
            print()
            print(f"   campos preenchidos: {antes} -> {depois} "
                  f"(média {antes / mexidas:.1f} -> {depois / mexidas:.1f} por nota)")

        if not args.aplicar and mexidas:
            print("\nNada foi gravado. Rode de novo com --aplicar para valer.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
