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
    "user_profiles": [
        # Janela de novidades depois do login (15/10/2026). Sem default: NULO é
        # "nunca viu nenhuma", e aí aparecem só as do último mês.
        ("novidades_vistas_ate", "VARCHAR(10)"),
        # Consulta livre do Domínio para o operador (22/10/2026). Ninguém ganha
        # sem um admin marcar: o default é FALSE.
        ("pode_sql_dominio", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ],
    # Leitura da caixa de e-mail: a tabela nasceu em 28/09/2026 de manhã e esta
    # coluna à tarde (sugestão de silenciar remetente automático). Sem default:
    # NULO é "lido antes de existir a marca", não "não é automático".
    "email_mensagens": [
        ("automatico", "BOOLEAN"),
    ],
    # 29/09/2026: XML baixado do SIEG para um SPED que chegou por e-mail.
    "email_xmls": [
        ("origem", "VARCHAR(10)"),
    ],
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
        # O valor em jogo da auditoria (25/09/2026, para o resumo por e-mail).
        # NULO = auditoria anterior às colunas: o resumo lê o result_data dela
        # UMA vez e preenche; nunca vira zero por omissão.
        ("vl_estornar", "NUMERIC(14,2)"),
        ("vl_aproveitar", "NUMERIC(14,2)"),
        ("vl_recolher", "NUMERIC(14,2)"),
        ("vl_conferir", "NUMERIC(14,2)"),
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
    # Coleta automática de NFS-e: as tabelas nasceram em 24/09/2026 (fase 1) e
    # estas colunas no dia seguinte (relatório e e-mail). Se o servidor foi
    # reiniciado entre os dois, as tabelas já existem sem elas.
    "nfse_automacoes": [
        ("destinatarios", "TEXT"),
        # 25/09/2026: a mesma tabela passou a guardar os informativos (resumo
        # das auditorias, erros do sistema). Linha que já existia é coleta.
        ("tipo", "VARCHAR(30) NOT NULL DEFAULT 'coleta_nfse'"),
        ("config", "TEXT"),
        # Frequência mensal (o resumo das auditorias do mês fechado).
        ("dia_mes", "INTEGER"),
    ],
    # 02/10/2026: a tela de NFS-e abre nas notas do último lote. Sem default:
    # NULO é "gravada antes de existir a marca", não lote nenhum.
    "nfse_documents": [
        ("lote_id", "VARCHAR(36)"),
    ],
    # 23/10/2026: a aba Validar SPED passou a validar no PVA oficial, numa fila. Todas sem default: NULO é
    # "validação do validador antigo, anterior à fila" — nunca "o PVA aprovou" nem "zero erro".
    "sped_validacoes": [
        ("motor", "VARCHAR(10)"),
        ("situacao", "VARCHAR(12)"),
        ("estado_pva", "VARCHAR(30)"),
        ("aprovado", "BOOLEAN"),
        ("integrado", "BOOLEAN"),
        ("falha", "VARCHAR(500)"),
        ("duracao_ms", "INTEGER"),
        ("concluida_em", "TIMESTAMP"),
        ("py_erros", "INTEGER"),
        ("py_advertencias", "INTEGER"),
        # Mesmo dia, à tarde: assinar no PVA (aba e rotina automática). NULO = ninguém pediu assinatura.
        ("assinar", "BOOLEAN"),
        ("assinado", "BOOLEAN"),
        ("assinatura_motivo", "VARCHAR(500)"),
        ("assinatura_titular", "VARCHAR(255)"),
        ("assinado_em", "TIMESTAMP"),
        ("arquivo_assinado", "VARCHAR(500)"),
        ("origem", "VARCHAR(10)"),
        # Mesmo dia, fim da tarde: correção pelo Synax (formato e contagens). NULO = não veio de correção.
        ("corrige", "VARCHAR(36)"),
        ("correcoes", "INTEGER"),
    ],
    "nfse_automacao_execucoes": [
        ("relatorio", "VARCHAR(500)"),
        ("email_status", "VARCHAR(20)"),
        ("email_detalhe", "VARCHAR(500)"),
        ("tipo", "VARCHAR(30) NOT NULL DEFAULT 'coleta_nfse'"),
        ("periodo_inicio", "TIMESTAMP"),
        ("periodo_fim", "TIMESTAMP"),
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


def tabelas_sem_rls(conn, nomes: list) -> list:
    """As tabelas de `nomes` que existem no schema atual com RLS DESLIGADO."""
    return list(conn.execute(text(
        "select c.relname from pg_class c "
        "join pg_namespace n on n.oid = c.relnamespace "
        "where n.nspname = current_schema() and c.relkind = 'r' "
        "and not c.relrowsecurity and c.relname::text = any(:nomes)"),
        {"nomes": list(nomes)}).scalars())


def ligar_rls(conn, nomes: list) -> list:
    """Liga o RLS das tabelas do Synax que nasceram sem ele. Devolve quais.

    Na migração para o banco de São Paulo (02/10/2026) as 25 tabelas ganharam
    RLS ligado SEM política: o Synax entra como `postgres`, que ignora RLS, e
    quem chega pela API REST do Supabase com a chave pública não lê nada. O
    `create_all` não sabe disso — tabela nova nascia aberta para essa API, em
    silêncio, e as regras do gerador de SPED (09/10/2026) seriam as primeiras.
    Consultar antes evita o ALTER (e o lock dele) nas que já estão certas.
    """
    ligadas = []
    for nome in tabelas_sem_rls(conn, nomes):
        # DDL não aceita parâmetro; o nome veio do catálogo, filtrado pelos
        # nomes dos models, nunca de entrada.
        conn.execute(text(f'ALTER TABLE "{nome}" ENABLE ROW LEVEL SECURITY'))  # nosemgrep
        ligadas.append(nome)
    return ligadas


def _ligar_rls_nas_tabelas_do_synax(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        return  # SQLite (testes, dev) não tem RLS
    from ...domain.models.base import Base

    try:
        with engine.begin() as conn:
            ligadas = ligar_rls(conn, [t.name for t in Base.metadata.tables.values()])
        for nome in ligadas:
            logger.info("RLS ligado na tabela %s (nasceu sem ele)", nome)
    except Exception:  # noqa: BLE001 — tabela aberta é risco, não motivo para não subir
        logger.warning("Não foi possível conferir o RLS das tabelas", exc_info=True)


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
                # DDL não aceita parâmetro; tabela, nome e ddl vêm de COLUNAS_NOVAS,
                # constante deste módulo, nunca de entrada.
                conn.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {nome} {ddl}"))  # nosemgrep

    _criar_indices_faltantes(engine, tabelas_existentes)
    _ligar_rls_nas_tabelas_do_synax(engine)
