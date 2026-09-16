# -*- coding: utf-8 -*-
"""Cópia de segurança do banco, para poder mexer no Supabase sem medo.

Gera DOIS arquivos em `backups/`, com funções diferentes:

  1. `synax_<data>.dump` — formato custom do PostgreSQL, comprimido. É o que
     RESTAURA. Permite voltar o banco inteiro ou só uma tabela.
  2. `synax_<data>_esquema.sql` — só a estrutura, em texto legível. É o que se
     ABRE PARA LER. Traz as políticas de RLS e os GRANTs exatamente como estão
     hoje — o que importa justamente quando se vai mexer em permissão: dá para
     comparar o antes e o depois, e para saber ao que voltar.

DECISÕES QUE NÃO DÃO PARA DEDUZIR DO CÓDIGO
-------------------------------------------
* **Usa `DIRECT_URL`, não `DATABASE_URL`.** O `DATABASE_URL` aponta para o
  pooler em modo TRANSAÇÃO (porta 6543), que não mantém estado de sessão — o
  `pg_dump` falha nele. O `DIRECT_URL` (porta 5432) é o pooler em modo SESSÃO e
  funciona. O script recusa a 6543 com mensagem, em vez de gerar dump quebrado.

* **A senha NUNCA vai na linha de comando.** Argumento de processo aparece na
  lista de tarefas da máquina para qualquer usuário logado. Ela viaja pelo
  ambiente do subprocesso (`PGPASSWORD`), que só o `pg_dump` enxerga.

* **Só o schema `public`.** É onde estão os dados do escritório. Os schemas
  `auth` e `storage` são do Supabase, gerenciados por ele, e o usuário da
  aplicação não tem permissão para despejá-los inteiros — pedir isso faria o
  backup falhar no meio.

* **Confere o resultado.** Um arquivo de zero byte também "termina sem erro".
  No fim o script relê o dump com `pg_restore --list` e conta os objetos; se não
  conseguir ler de volta, avisa que o backup NÃO presta. Backup que ninguém
  releu é um arquivo, não um backup.

* **O arquivo é dado de cliente.** Traz os relatórios de auditoria, as notas de
  serviço e as senhas cifradas dos certificados. `backups/` está no .gitignore.

Uso:
    venv/Scripts/python.exe scripts/backup_banco.py
    venv/Scripts/python.exe scripts/backup_banco.py --saida "D:/Backups/Synax"
    venv/Scripts/python.exe scripts/backup_banco.py --listar
"""
import argparse
import glob
import os
import subprocess
import sys
from datetime import datetime
from shutil import which
from urllib.parse import unquote, urlparse

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, RAIZ)

PASTA_PADRAO = os.path.join(RAIZ, "backups")

# O pg_dump precisa ser da MESMA versão do servidor ou mais novo. O instalador
# do Windows não o põe no PATH, então procuramos onde ele realmente fica.
MOLDES_WINDOWS = (
    r"C:\Program Files\PostgreSQL\{v}\bin",
    r"C:\Program Files (x86)\PostgreSQL\{v}\bin",
)


def achar_binario(nome: str) -> str | None:
    achado = which(nome)
    if achado:
        return achado
    for versao in range(18, 12, -1):
        for molde in MOLDES_WINDOWS:
            caminho = os.path.join(molde.format(v=versao), nome + ".exe")
            if os.path.exists(caminho):
                return caminho
    return None


def conexao():
    """(ambiente com a senha, descrição SEM a senha). Recusa o pooler transacional."""
    import dotenv
    dotenv.load_dotenv(os.path.join(RAIZ, ".env"), override=True)

    bruto = os.environ.get("DIRECT_URL") or os.environ.get("DATABASE_URL")
    if not bruto:
        raise SystemExit("ERRO: DIRECT_URL/DATABASE_URL não estão no .env")

    u = urlparse(bruto)
    porta = u.port or 5432
    if porta == 6543:
        raise SystemExit(
            "ERRO: esta é a porta do pooler em modo TRANSAÇÃO (6543), que não\n"
            "      serve para pg_dump. Ponha no .env um DIRECT_URL com a porta\n"
            "      5432 (Supabase > Settings > Database > Session pooler).")

    env = dict(os.environ)
    env.update({
        "PGHOST": u.hostname or "",
        "PGPORT": str(porta),
        "PGUSER": unquote(u.username or ""),
        "PGDATABASE": (u.path or "/postgres").lstrip("/"),
        "PGPASSWORD": unquote(u.password or ""),   # só no ambiente, nunca em argv
        "PGSSLMODE": "require",
    })
    banco = (u.path or "/postgres").lstrip("/")
    return env, u.hostname + ":" + str(porta) + "/" + banco


def mb(caminho: str) -> str:
    return "{:,.1f} MB".format(os.path.getsize(caminho) / 1024 / 1024).replace(",", ".")


def listar(pasta: str) -> int:
    arquivos = sorted(glob.glob(os.path.join(pasta, "*.dump")), reverse=True)
    if not arquivos:
        print("Nenhum backup em " + pasta)
        return 0
    print("Backups em " + pasta + ":\n")
    for a in arquivos:
        quando = datetime.fromtimestamp(os.path.getmtime(a))
        print("  {:<38} {:>10}  {:%d/%m/%Y %H:%M}".format(
            os.path.basename(a), mb(a), quando))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--saida", default=PASTA_PADRAO, help="pasta de destino")
    p.add_argument("--listar", action="store_true", help="lista os backups já feitos")
    args = p.parse_args()

    if args.listar:
        return listar(args.saida)

    pg_dump = achar_binario("pg_dump")
    pg_restore = achar_binario("pg_restore")
    if not pg_dump:
        raise SystemExit(
            "ERRO: pg_dump não encontrado. Instale o cliente do PostgreSQL\n"
            "      (postgresql.org/download/windows) numa versão igual ou\n"
            "      maior que a do servidor.")

    env, onde = conexao()
    os.makedirs(args.saida, exist_ok=True)
    carimbo = datetime.now().strftime("%Y-%m-%d_%H-%M")
    arq_dump = os.path.join(args.saida, "synax_" + carimbo + ".dump")
    arq_sql = os.path.join(args.saida, "synax_" + carimbo + "_esquema.sql")

    print("Servidor .... " + onde)
    print("pg_dump ..... " + pg_dump)
    print("Destino ..... " + args.saida + "\n")

    print("[1/3] Copiando estrutura e dados...")
    # Lista de argumentos fixa, sem shell; do ambiente só vai a URL do banco.
    proc = subprocess.run(  # nosemgrep
        [pg_dump, "--format=custom", "--compress=9", "--schema=public",
         "--no-owner", "--file=" + arq_dump],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        print("  FALHOU: " + proc.stderr.decode("utf-8", "replace")[:400])
        if os.path.exists(arq_dump):
            os.remove(arq_dump)
        return 1
    print("  " + os.path.basename(arq_dump) + "  (" + mb(arq_dump) + ")")

    print("[2/3] Copiando a estrutura em texto legível...")
    with open(arq_sql, "wb") as saida:
        # Mesmo caso do pg_dump acima.
        proc = subprocess.run(  # nosemgrep
            [pg_dump, "--schema-only", "--schema=public", "--no-owner"],
            env=env, stdout=saida, stderr=subprocess.PIPE)
    if proc.returncode == 0:
        print("  " + os.path.basename(arq_sql) + "  (" + mb(arq_sql) + ")")
    else:
        print("  aviso: não saiu (" + proc.stderr.decode("utf-8", "replace")[:200] + ")")

    print("[3/3] Conferindo se o arquivo pode ser lido de volta...")
    if not pg_restore:
        print("  aviso: pg_restore não encontrado; não deu para conferir.")
        return 0
    volta = subprocess.run([pg_restore, "--list", arq_dump], capture_output=True)
    if volta.returncode != 0:
        print("  *** O ARQUIVO NÃO PÔDE SER LIDO DE VOLTA. NÃO CONFIE NELE. ***")
        return 1

    linhas = volta.stdout.decode("utf-8", "replace").splitlines()
    tabelas = sum(1 for l in linhas if " TABLE DATA " in l)
    politicas = sum(1 for l in linhas if " POLICY " in l)
    indices = sum(1 for l in linhas if " INDEX " in l)
    print("  legível: {} tabela(s) com dados, {} índice(s), {} política(s) de RLS"
          .format(tabelas, indices, politicas))

    print()
    print("=" * 70)
    print("PRONTO. Se algo der errado no Supabase, para voltar ao estado de agora:")
    print("=" * 70)
    print("  restaurar TUDO:")
    print('    "' + pg_restore + '" --clean --if-exists --no-owner \\')
    print('        --schema=public --dbname="<DIRECT_URL>" "' + arq_dump + '"')
    print()
    print("  restaurar UMA tabela só (ex.: as regras de CFOP):")
    print('    "' + pg_restore + '" --data-only --table=cfop_classifications \\')
    print('        --dbname="<DIRECT_URL>" "' + arq_dump + '"')
    print()
    print("  ver as permissões e políticas de hoje:")
    print("    abra " + os.path.basename(arq_sql) + " num editor de texto")
    print()
    print("AVISO: o arquivo contém os dados de TODOS os clientes e as senhas")
    print("cifradas dos certificados. Trate como documento sigiloso.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
