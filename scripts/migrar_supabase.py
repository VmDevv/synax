# -*- coding: utf-8 -*-
"""Leva o banco do Synax para o Supabase NOVO (São Paulo) — 02/10/2026.

O projeto antigo (us-east-1) guarda o Synax E o VmHub; o novo guarda só o
Synax. O que define "do Synax" são os MODELS (`Base.metadata`): as 25 tabelas
que o código conhece. As 20 do VmHub e afins (companies, produtos, tb_logs,
users...) ficam no projeto antigo, que o VmHub continua usando.

Por padrão é um ENSAIO: faz tudo no banco novo dentro de UMA transação e a
desfaz no fim — mede o tempo e confere as contagens sem deixar nada gravado.
Para gravar de verdade (com o Synax parado):

    venv/Scripts/python.exe scripts/migrar_supabase.py --aplicar

O banco antigo é só LIDO, nos dois modos.

O que vai, nesta ordem:
1. As 25 tabelas, criadas pelos próprios models (mesmas colunas e índices).
2. Os usuários de LOGIN do Synax (auth.users + auth.identities de quem tem
   perfil em user_profiles): a senha vai junto (é o hash), ninguém precisa
   trocar — só entrar de novo, porque a sessão do projeto antigo não vale no
   novo. Coluna gerada (confirmed_at, email da identidade) fica de fora: o
   próprio Postgres a calcula.
3. Os dados, tabela por tabela, em ordem de chave estrangeira; e o contador
   das chaves numéricas acertado, senão o próximo INSERT colide com o id 1.
4. RLS LIGADO nas 25 tabelas, sem política nenhuma. O Synax entra pelo
   usuário `postgres`, que não passa pelo RLS; quem fica de fora é a API REST
   do Supabase (a chave publishable). No projeto antigo 20 das 25 estavam sem
   RLS — `tenant_certificates` e `app_settings` incluídas.
5. Conferência: a contagem de cada tabela no novo tem de bater com a do antigo.

A FERNET_SECRET_KEY do .env NÃO muda: senhas de certificado e credenciais do
SIEG e do e-mail estão cifradas com ela e vão como estão.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("SYNAX_AGENDADOR", "0")

from dotenv import dotenv_values  # noqa: E402
from sqlalchemy import Integer, create_engine, func, select, text  # noqa: E402

import backend.app.main  # noqa: E402,F401  — registra todos os models no metadata
from backend.app.domain.models.base import Base  # noqa: E402

LOTE_LINHAS = 500
LOTE_PESADO = 25           # audit_tasks: cada linha carrega o relatório inteiro (MB)
PESADAS = {"audit_tasks", "sped_correction_runs"}


def _colunas_inseriveis(conn, schema: str, tabela: str) -> list[str]:
    """Colunas que aceitam valor no INSERT (as geradas ficam de fora)."""
    return [r[0] for r in conn.execute(text("""
        select a.attname from pg_attribute a
        join pg_class c on c.oid = a.attrelid join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = :s and c.relname = :t and a.attnum > 0 and not a.attisdropped
          and a.attgenerated = ''
        order by a.attnum"""), {"s": schema, "t": tabela})]


def copiar_auth(origem, destino, ids: list) -> dict:
    """auth.users e auth.identities dos usuários do Synax, como JSON: o
    jsonb_populate_record converte cada campo para o tipo da coluna de destino,
    e coluna que só existe de um lado é simplesmente ignorada."""
    feitos = {}
    for tabela, chave in (("users", "id"), ("identities", "user_id")):
        dest_cols = set(_colunas_inseriveis(destino, "auth", tabela))
        linhas = origem.execute(text(
            f"select to_jsonb(x) from auth.{tabela} x where x.{chave} = any(cast(:ids as uuid[]))"),  # nosemgrep
            {"ids": [str(i) for i in ids]}).scalars().all()
        for registro in linhas:
            cols = [c for c in registro.keys() if c in dest_cols]
            lista = ", ".join(f'"{c}"' for c in cols)
            destino.execute(text(
                f"insert into auth.{tabela} ({lista}) "  # nosemgrep — nomes vêm do catálogo
                f"select {lista} from jsonb_populate_record(null::auth.{tabela}, cast(:j as jsonb))"),
                {"j": json.dumps(registro, default=str)})
        feitos[tabela] = len(linhas)
    return feitos


def copiar_tabela(origem, destino, tabela) -> int:
    lote = LOTE_PESADO if tabela.name in PESADAS else LOTE_LINHAS
    total = 0
    resultado = (origem.execution_options(stream_results=True, max_row_buffer=lote)
                 .execute(select(tabela)))
    for parte in resultado.mappings().partitions(lote):
        destino.execute(tabela.insert(), [dict(r) for r in parte])
        total += len(parte)
    return total


def acertar_sequencias(destino, tabela) -> None:
    for col in tabela.primary_key.columns:
        if isinstance(col.type, Integer) and col.autoincrement in (True, "auto"):
            seq = destino.execute(text("select pg_get_serial_sequence(:t, :c)"),
                                  {"t": f"public.{tabela.name}", "c": col.name}).scalar()
            if seq:
                destino.execute(text(
                    f'select setval(:s, coalesce((select max("{col.name}") from public.{tabela.name}), 0) + 1, false)'),  # nosemgrep
                    {"s": seq})


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--aplicar", action="store_true", help="grava no banco novo (padrão: ensaio desfeito)")
    ap.add_argument("--origem", default=".env", help="arquivo .env do banco ATUAL")
    ap.add_argument("--destino", default=".env.novo", help="arquivo .env do banco NOVO")
    args = ap.parse_args()

    url_origem = dotenv_values(args.origem)["DATABASE_URL"]
    url_destino = dotenv_values(args.destino)["DATABASE_URL"]
    if url_origem == url_destino:
        raise SystemExit("Origem e destino são o MESMO banco — confira os dois arquivos.")

    tabelas = Base.metadata.sorted_tables       # ordem de chave estrangeira
    inicio = time.perf_counter()
    eng_o = create_engine(url_origem)
    eng_d = create_engine(url_destino)

    with eng_o.connect() as origem, eng_d.connect() as destino:
        trans = destino.begin()
        try:
            ja_tem = destino.execute(text(
                "select count(*) from information_schema.tables where table_schema='public' "
                "and table_name = any(:t)"), {"t": [t.name for t in tabelas]}).scalar()
            if ja_tem:
                raise SystemExit(f"O banco novo já tem {ja_tem} tabela(s) do Synax. "
                                 "Migração é para banco vazio — nada foi feito.")

            print(f"1. Criando as {len(tabelas)} tabelas do Synax no banco novo")
            Base.metadata.create_all(bind=destino)

            ids = [r[0] for r in origem.execute(text("select id from public.user_profiles"))]
            print(f"2. Usuários de login: {len(ids)} perfil(is) do Synax")
            for k, n in copiar_auth(origem, destino, ids).items():
                print(f"   auth.{k}: {n}")

            print("3. Dados")
            contagens = {}
            for t in tabelas:
                t0 = time.perf_counter()
                n = copiar_tabela(origem, destino, t)
                acertar_sequencias(destino, t)
                contagens[t.name] = n
                print(f"   {t.name:30} {n:>7} linha(s)  {time.perf_counter() - t0:6.1f} s")

            print("4. RLS ligado nas tabelas do Synax")
            for t in tabelas:
                destino.execute(text(f"alter table public.{t.name} enable row level security"))  # nosemgrep

            print("5. Conferência (antigo x novo)")
            erros = 0
            for t in tabelas:
                no_antigo = origem.execute(select(func.count()).select_from(t)).scalar()
                no_novo = destino.execute(select(func.count()).select_from(t)).scalar()
                if no_antigo != no_novo:
                    erros += 1
                    print(f"   DIFERENTE {t.name}: antigo {no_antigo}, novo {no_novo}")
            usuarios_novo = destino.execute(text("select count(*) from auth.users")).scalar()
            print(f"   {len(tabelas) - erros} de {len(tabelas)} tabelas batem; "
                  f"{usuarios_novo} usuário(s) de login no novo")
            if erros:
                raise RuntimeError(f"{erros} tabela(s) não bateram — nada foi gravado.")

            if args.aplicar:
                trans.commit()
                print(f"\nGRAVADO no banco novo em {time.perf_counter() - inicio:.0f} s.")
            else:
                trans.rollback()
                print(f"\nENSAIO completo em {time.perf_counter() - inicio:.0f} s — desfeito, "
                      "o banco novo continua vazio. Rode com --aplicar no dia da troca.")
        except BaseException:
            if trans.is_active:
                trans.rollback()
            raise


if __name__ == "__main__":
    main()
