"""Ajustes de esquema em bancos JÁ existentes.

`Base.metadata.create_all()` só cria tabelas que faltam — ele nunca altera uma
tabela que já existe. Uma coluna nova adicionada a um model depois do primeiro
deploy (como `Tenant.ativo`) fica invisível para todo banco que já tinha a
tabela `tenants`: o SQLite local de cada dev, os `.db` de teste, e a instância
de produção já provisionada. Sem isto, a primeira consulta que tocasse a coluna
nova quebraria com "no such column" (SQLite) ou "column does not exist"
(Postgres).

Mesmo padrão do `DO $$ IF NOT EXISTS ... ALTER TABLE $$` já usado em
scripts/01_setup_rls_auth.sql para a coluna `is_global` — aqui automatizado
para não depender de alguém lembrar de rodar o SQL manualmente em cada ambiente.
"""
import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# tabela -> [(coluna, DDL do tipo/default)]
COLUNAS_NOVAS = {
    "tenants": [
        ("ativo", "BOOLEAN NOT NULL DEFAULT TRUE"),
        # Sem default: UF em branco significa "ninguém informou ainda", e o
        # gerador de lançamentos recusa em vez de chutar um estado.
        ("uf", "VARCHAR(2)"),
    ],
    "audit_tasks": [
        # Sem default de propósito: NULO é informação. Distingue "auditoria
        # antiga, contadores nunca preenchidos" de "auditoria que rodou e não
        # achou nada". O painel do Radar mostra as duas coisas separadas, e
        # scripts/backfill_resumo_auditoria.py preenche o histórico.
        ("notas_processadas", "INTEGER"),
        ("inconsistencias", "INTEGER"),
        # Competência do SPED (AAAA-MM). Também sem default, e pelo mesmo
        # motivo: mês chutado faria a ficha da empresa afirmar que uma
        # competência foi auditada quando ninguém sabe qual foi.
        ("competencia", "VARCHAR(7)"),
    ],
    "sped_correction_runs": [
        # A tabela nasceu em 09/09/2026 e ganhou esta coluna em 10/09, quando o
        # E110 passou a poder ser reescrito. Como o `create_all` só cria tabela
        # que FALTA, o Postgres de produção ficou com a tabela velha: o arquivo
        # corrigido continuava saindo e a trilha morria no INSERT
        # ("column apuracao_atualizada does not exist"), em silêncio para quem
        # estava na tela. Default FALSE porque geração anterior a 10/09 de fato
        # não mexeu na apuração — aqui o valor não é chute, é o que aconteceu.
        ("apuracao_atualizada", "BOOLEAN DEFAULT FALSE"),
    ],
}


def _criar_indices_faltantes(engine: Engine, tabelas_existentes: set) -> None:
    """Cria os índices declarados nos models que o banco ainda não tem.

    Mesma armadilha das colunas, e pelo mesmo motivo: `create_all` monta os
    índices junto com a TABELA. Um `Index(...)` acrescentado ao model depois do
    primeiro deploy nunca chega a um banco que já tinha aquela tabela — e a
    ausência é silenciosa, porque a consulta continua devolvendo o resultado
    certo, só varrendo a tabela inteira para isso.

    `checkfirst=True` consulta o catálogo antes de emitir o CREATE, então rodar
    de novo não custa nada e não há como colidir com um índice já existente.
    """
    from ...domain.models.base import Base

    for tabela in Base.metadata.tables.values():
        if tabela.name not in tabelas_existentes:
            continue  # acabou de ser criada por create_all, já veio com tudo
        for indice in tabela.indexes:
            try:
                indice.create(bind=engine, checkfirst=True)
            except Exception:  # noqa: BLE001 — índice ausente é lentidão, não erro
                logger.warning("Não foi possível criar o índice %s", indice.name,
                               exc_info=True)


def aplicar(engine: Engine) -> None:
    insp = inspect(engine)
    tabelas_existentes = set(insp.get_table_names())

    for tabela, colunas in COLUNAS_NOVAS.items():
        if tabela not in tabelas_existentes:
            continue  # create_all já criou a tabela com a coluna certa
        presentes = {c["name"] for c in insp.get_columns(tabela)}
        for nome, ddl in colunas:
            if nome in presentes:
                continue
            logger.info("Adicionando coluna %s.%s (banco já existia sem ela)", tabela, nome)
            with engine.begin() as conn:
                # nosemgrep (DDL não aceita parâmetro; tabela, nome e ddl vêm de
                # COLUNAS_NOVAS, constante deste módulo, nunca de entrada)
                conn.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {nome} {ddl}"))

    _criar_indices_faltantes(engine, tabelas_existentes)
