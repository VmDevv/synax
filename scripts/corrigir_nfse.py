# -*- coding: utf-8 -*-
"""Corrige as NFS-e já gravadas (01/10/2026).

Três consertos, nesta ordem. Roda SEM gravar por padrão; para gravar:

    venv/Scripts/python.exe scripts/corrigir_nfse.py --aplicar

1. **Reler os XMLs guardados.** 201 notas foram indexadas por uma versão do
   leitor anterior a 02/09/2026, que não entendia o leiaute nacional: o XML
   tem o tomador e a competência, o banco não — e a tela mostrava a nota sem
   dizer de quem ela era. Relê, com o leitor de hoje, as notas sem tomador ou
   sem competência (`--todas` relê todas) e preenche o que faltava — inclusive
   o município, que é parte da CHAVE da nota e o leitor antigo gravava vazio.
   O número nunca muda, e campo que o leitor deixa vazio não apaga o que o
   banco já tinha.

2. **Zerar o cursor das filiais consultadas com o certificado da matriz.**
   Sem `cnpjConsulta`, o ADN entregava os documentos do DONO do certificado:
   15 filiais (MEGA, PERCAL, M.R., NORTE VISION, Associação dos Pescadores)
   recebiam as notas da matriz, e o cursor de NSU delas andou pela numeração
   da matriz. Zerado, a próxima busca traz as notas da filial desde o início.
   Precisa abrir os certificados (para ler o CNPJ de dentro deles); a senha é
   usada só em memória e nada dela é impresso.

3. **Tirar da filial a nota que era da matriz.** Só nas empresas do passo 2:
   nota em que a filial não é prestadora nem tomadora sai DELA quando outro
   estabelecimento da mesma empresa já a tem — eram as 49 notas da matriz da
   M.R. na filial. Em nenhuma outra empresa se apaga nota: a de outro CNPJ é
   só listada. O XML na pasta da rede não é apagado.
"""
import argparse
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.infrastructure.database.session import SessionLocal  # noqa: E402
from backend.app.domain.models.auth_models import Tenant  # noqa: E402
from backend.app.domain.models.certificates import TenantCertificate  # noqa: E402
from backend.app.domain.models.nfse import NfseDocument, NfseCursor  # noqa: E402
from backend.app.services.nfse import adn_client, certificados, storage  # noqa: E402
from backend.app.services.nfse.abrasf_parser import parse_nfse_xml  # noqa: E402

CHAVE = ("prestador", "numero", "mun_prest")


def _digitos(texto) -> str:
    return re.sub(r"\D", "", texto or "")


def _ler(conteudo: bytes) -> dict | None:
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


def reler(db, docs, todas: bool) -> Counter:
    """O leitor antigo gravou o município vazio (247 notas) e, com prestador
    pessoa física, o CNPJ do TOMADOR no lugar do prestador (4) — e os dois
    são parte da CHAVE. A chave é corrigida quando a nova não existe ainda na
    empresa; se já existe, a linha velha é a duplicata e sai."""
    placar = Counter()
    chaves = {(d.tenant_id, d.prestador, d.numero, d.mun_prest) for d in docs}
    for d in docs:
        if not todas and d.tomador and d.dt_competencia:
            continue
        placar["relidas"] += 1
        bruto = storage.ler(d.arquivo_xml) if d.arquivo_xml else None
        if not bruto:
            placar["sem arquivo na rede"] += 1
            continue
        dados = _ler(bruto)
        if not dados:
            placar["XML que o leitor não reconhece"] += 1
            continue
        nova = (d.tenant_id, dados.get("prestador") or "", dados.get("numero") or "",
                dados.get("mun_prest") or "")
        velha = (d.tenant_id, d.prestador, d.numero, d.mun_prest)
        if nova != velha:
            if nova[3] != velha[3]:
                placar["município preenchido (chave)"] += 1
            if nova[1] != velha[1]:
                placar["prestador corrigido (chave)"] += 1
            if nova[2] != velha[2] or not nova[1] or not nova[2]:
                placar["número diferente (não mexe)"] += 1
                continue
            if nova in chaves:
                placar["duplicata removida"] += 1
                db.delete(d)
                d._removida = True
                continue
            chaves.discard(velha)
            chaves.add(nova)
            d.prestador, d.mun_prest = nova[1], nova[3]
        mudou = nova != velha
        for campo, valor in dados.items():
            if campo in CHAVE or valor in (None, ""):
                continue
            if getattr(d, campo, None) != valor:
                if campo == "tomador" and not d.tomador:
                    placar["tomador preenchido"] += 1
                if campo == "dt_competencia" and not d.dt_competencia:
                    placar["competência preenchida"] += 1
                setattr(d, campo, valor)
                mudou = True
        placar["atualizadas" if mudou else "já estavam certas"] += 1
    return placar


def zerar_cursores_de_filial(db, empresas: dict, cursores: list) -> tuple[list[str], set]:
    """Devolve (linhas para o relatório, empresas consultadas com o
    certificado de OUTRO estabelecimento — as que receberam nota alheia)."""
    linhas, contaminadas = [], set()
    registros = db.query(TenantCertificate).filter(TenantCertificate.ativo == True).all()  # noqa: E712
    for reg in registros:
        nome, cnpj = empresas.get(reg.tenant_id, ("?", ""))
        if len(cnpj) != 14:
            continue
        try:
            pfx, senha = certificados.carregar_material(db, reg.tenant_id, cnpj)
        except ValueError:
            continue
        try:
            dono = adn_client.cnpj_do_certificado(pfx, senha)
        finally:
            pfx, senha = b"", ""
        if not dono or dono == cnpj:
            continue
        contaminadas.add(reg.tenant_id)
        for cur in cursores:
            if cur.tenant_id != reg.tenant_id or cur.cnpj != cnpj:
                continue
            if (cur.ultimo_nsu or "0") != "0":
                linhas.append(f"{nome[:40]:40} {cnpj}  certificado {dono}  NSU {cur.ultimo_nsu} -> 0")
                cur.ultimo_nsu = "0"
                cur.ultimo_erro = None
    return linhas, contaminadas


def tirar_notas_alheias(db, docs, empresas: dict, contaminadas: set):
    """Só nas empresas consultadas com o certificado de outro estabelecimento:
    tudo que o ADN mandou para elas era do dono do certificado. A nota sai
    quando outro estabelecimento da MESMA empresa já a tem — sem tomador no XML
    não dá para saber de quem é pela nota, e é pela consulta que se sabe.
    Fora dessas empresas nada é apagado: só se LISTA a nota de outro CNPJ.

    Devolve (removidas por empresa, notas de outro CNPJ que ficaram)."""
    por_cnpj = defaultdict(set)          # cnpj -> tenants com esse CNPJ
    for tid, (_n, c) in empresas.items():
        if c:
            por_cnpj[c].add(tid)
    docs = [d for d in docs if not getattr(d, "_removida", False)]
    tem = {(d.tenant_id, d.prestador, d.numero, d.mun_prest) for d in docs}

    removidas, ficaram = Counter(), []
    for d in docs:
        nome, cnpj = empresas.get(d.tenant_id, ("?", ""))
        if not cnpj or cnpj in (d.prestador, d.tomador):
            continue
        if d.tenant_id in contaminadas:
            irmas = {t for c, ts in por_cnpj.items() if c[:8] == cnpj[:8] for t in ts} - contaminadas
            if any((t, d.prestador, d.numero, d.mun_prest) in tem for t in irmas):
                removidas[nome] += 1
                db.delete(d)
                continue
        if d.tomador:
            ficaram.append(f"{nome[:34]:34} nota {d.numero} prestador {d.prestador} tomador {d.tomador}")
    return removidas, ficaram

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--aplicar", action="store_true", help="grava no banco (padrão: só simula)")
    ap.add_argument("--todas", action="store_true", help="relê todas as notas, não só as incompletas")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        empresas = {str(t.id): (t.name or "", _digitos(t.cnpj)) for t in db.query(Tenant).all()}
        docs = db.query(NfseDocument).all()
        cursores = db.query(NfseCursor).all()   # carregados antes: sem consulta no meio
        print(f"{len(docs)} NFS-e no banco.\n")

        # Sem autoflush, a simulação não manda UPDATE/DELETE nem dentro da
        # transação: tudo fica na memória e só o commit do --aplicar grava.
        with db.no_autoflush:
            print("1. Reler os XMLs guardados")
            for k, n in reler(db, docs, args.todas).most_common():
                print(f"   {n:5}  {k}")

            print("\n2. Cursor das filiais consultadas com o certificado da matriz")
            linhas, contaminadas = zerar_cursores_de_filial(db, empresas, cursores)
            for ln in linhas:
                print("   " + ln)
            print(f"   {len(linhas)} cursor(es) zerado(s)")

            print("\n3. Notas na empresa errada")
            removidas, sem_dono = tirar_notas_alheias(db, docs, empresas, contaminadas)
        for nome, n in removidas.most_common():
            print(f"   {n:5}  removidas de {nome}")
        print(f"   {sum(removidas.values())} removida(s) — a mesma nota continua na empresa dona dela")
        if sem_dono:
            print(f"   {len(sem_dono)} nota(s) em que a empresa não é prestadora nem tomadora (ficam):")
            for ln in sem_dono[:30]:
                print("     " + ln)

        if args.aplicar:
            db.commit()
            print("\nGRAVADO.")
        else:
            db.rollback()
            print("\nSimulação: nada foi gravado. Rode com --aplicar para gravar.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
