from sqlalchemy import (Boolean, Column, Index, Integer, Numeric,
                        String, Text)
from .base import Base, TenantMixin, TimestampMixin

class AuditTask(Base, TenantMixin, TimestampMixin):
    __tablename__ = 'audit_tasks'
    __table_args__ = (
        # O Radar filtra e ordena por data em toda consulta; sem o índice, cada
        # abertura do painel varria a tabela inteira.
        Index('idx_audit_task_created', 'created_at'),
    )

    id = Column(String(36), primary_key=True)  # task_id (UUID)
    user_id = Column(String(36), nullable=True) # Quem disparou a ação
    file_name = Column(String(255), nullable=True) # Nome do arquivo que sofreu upload

    # Statuses: pendente, processando, concluido, erro
    status = Column(String(20), nullable=False, default='pendente')

    # Store JSON report or detailed info
    result_data = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)

    # Cópia dos dois números do `resumo` que ficam dentro de result_data.
    #
    # Estão repetidos aqui de propósito. result_data guarda o relatório inteiro
    # — todas as divergências, todos os itens, todas as notas —, e um mês de uma
    # empresa dá alguns MB de JSON. Somar as inconsistências de 200 auditorias
    # para desenhar o Radar significaria baixar e abrir centenas de MB a cada
    # abertura da tela. Com as colunas, é um SUM() no banco.
    #
    # São preenchidas ao concluir a auditoria (cross_checker). NULO quer dizer
    # "auditoria anterior a 02/09/2026" — o painel conta essas à parte em vez de
    # tratá-las como zero, e scripts/backfill_resumo_auditoria.py preenche o
    # histórico lendo o JSON uma única vez.
    notas_processadas = Column(Integer, nullable=True)
    inconsistencias = Column(Integer, nullable=True)

    # A competência do SPED (AAAA-MM), lida do registro 0000 — não a data do
    # upload. Rodar abril em setembro é rotina, então `created_at` não responde
    # "o abril desta empresa já foi conferido?". Texto porque ordena
    # cronologicamente e agrupa direto no banco; ver core/competencia.py.
    #
    # NULO tem dois significados, e os dois são honestos: auditoria anterior a
    # 03/09/2026 (scripts/backfill_resumo_auditoria.py preenche), ou SPED cujo
    # 0000 não deu para ler. Nenhum deles pode virar uma data chutada — a tela
    # afirmaria que um mês foi auditado sem ter sido.
    competencia = Column(String(7), nullable=True)

    # O valor em jogo, pelos mesmos motivos das contagens acima: o resumo das
    # auditorias por e-mail soma isto de dezenas de auditorias, e abrir o
    # result_data de cada uma para somar quatro números seria baixar o
    # relatório inteiro de todo mundo a cada envio. Mesma conta da Visão Geral
    # (`resumo_financeiro`), gravada quando o motor conclui (25/09/2026).
    #
    # NULO = auditoria anterior às colunas. O resumo lê o result_data dela UMA
    # vez e preenche — NULO nunca é tratado como zero.
    vl_estornar = Column(Numeric(14, 2, asdecimal=False), nullable=True)
    vl_aproveitar = Column(Numeric(14, 2, asdecimal=False), nullable=True)
    vl_recolher = Column(Numeric(14, 2, asdecimal=False), nullable=True)
    vl_conferir = Column(Numeric(14, 2, asdecimal=False), nullable=True)


class SpedCorrectionRun(Base, TenantMixin, TimestampMixin):
    """Cada vez que alguém baixou um SPED corrigido — quem, quando, e o quê.

    Existe por dois motivos que não são o mesmo:

    1. **Trilha.** O arquivo sai daqui e vai para o cliente, que o transmite. É
       o único ponto do sistema em que o Synax produz um documento fiscal em
       vez de conferir um. Quem gerou e o que foi mudado tem de ficar gravado —
       log de servidor rotaciona e ninguém consulta.

    2. **Aviso na tela.** Rodar a mesma competência de novo é rotina, e sem
       isto a segunda pessoa a abrir a aba não tem como saber que o arquivo já
       foi corrigido e entregue. A tela mostra as gerações anteriores antes de
       oferecer o botão.

    Os hashes são o que amarra o registro ao arquivo: o de origem prova QUAL
    SPED foi corrigido (e denuncia arquivo trocado entre uma geração e outra), o
    de destino identifica exatamente o que foi entregue.
    """
    __tablename__ = 'sped_correction_runs'
    __table_args__ = (
        Index('idx_correcao_task', 'task_id'),
        Index('idx_correcao_tenant_data', 'tenant_id', 'created_at'),
    )

    id = Column(String(36), primary_key=True)
    task_id = Column(String(36), nullable=False)
    user_email = Column(String(255), nullable=True)
    file_name = Column(String(255), nullable=True)     # nome do arquivo gerado
    competencia = Column(String(7), nullable=True)

    hash_origem = Column(String(64), nullable=True)
    hash_destino = Column(String(64), nullable=True)

    correcoes_aceitas = Column(Integer, default=0)     # o que a pessoa marcou
    alteracoes = Column(Integer, default=0)            # linhas mexidas, com cascata
    recusas = Column(Integer, default=0)

    # Quanto de crédito o arquivo passou a ter. Numeric, não Float: é dinheiro
    # que vai para a apuração, e o resto do motor trabalha em Decimal.
    icms_diferenca = Column(Numeric(14, 2), default=0)
    ipi_diferenca = Column(Numeric(14, 2), default=0)

    # A apuração (E110) foi reescrita nesta geração? É a única coluna aqui que
    # responde "esta correção mexeu no imposto a recolher" sem abrir o JSON —
    # e é a primeira pergunta de quem revisa uma retificação entregue.
    apuracao_atualizada = Column(Boolean, default=False)

    # O relatório inteiro (`relatorio.para_json`), uma linha por alteração.
    #
    # É a prova do que foi entregue, e por isso fica aqui e não só no Excel que
    # a pessoa baixou. NUNCA leia esta coluna em tela de listagem: é a mesma
    # armadilha do `result_data` — carregar dezenas delas para desenhar um
    # painel baixa megabytes por abertura. As colunas acima existem exatamente
    # para responder sem abrir o JSON.
    detalhe = Column(Text, nullable=True)

    # O resumo executivo gerado por IA, quando havia chave configurada. Fica
    # gravado com o restante para o texto que foi entregue ao cliente ser o
    # mesmo que se lê aqui depois — regerar com o modelo daria outro.
    resumo_ia = Column(Text, nullable=True)


class SpedValidacao(Base, TenantMixin, TimestampMixin):
    """Cada EFD ICMS IPI que passou pela aba Validar SPED — quem, quando, qual.

    Responde "o abril desta empresa já foi validado, e como saiu?" sem guardar
    o arquivo: o SPED do cliente não fica no servidor, fica o resumo e o HASH
    DO CORPO (`services/sped_entregas.hash_do_corpo`). É esse hash que, quando
    o recibo da transmissão for lido, diz se o arquivo entregue é o mesmo que
    foi validado — a assinatura do PVA vai depois do 9999 e não o altera;
    editar a escrituração no PVA altera.

    As contagens são colunas para o painel do mês ser uma consulta só, sem
    abrir relatório nenhum. O relatório linha a linha não é guardado: ele é
    refeito em segundos validando o arquivo de novo.
    """
    __tablename__ = 'sped_validacoes'
    __table_args__ = (
        Index('idx_validacao_tenant_comp', 'tenant_id', 'competencia'),
    )

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), nullable=True)
    user_name = Column(String(255), nullable=True)
    file_name = Column(String(255), nullable=True)

    # Do registro 0000. Competência sem 0000 legível é NULA, nunca chutada
    # (mesma regra de `audit_tasks.competencia`).
    competencia = Column(String(7), nullable=True)
    cnpj = Column(String(14), nullable=True)
    # 0 = original, 1 = substituta. Entregar a substituta de um mês já
    # entregue é rotina, e o painel precisa saber qual das duas é qual.
    cod_fin = Column(String(1), nullable=True)
    leiaute = Column(String(3), nullable=True)

    linhas = Column(Integer, default=0)
    erros = Column(Integer, default=0)
    advertencias = Column(Integer, default=0)
    # Aplicações de regra específica do PVA que a v1 NÃO confere. Fica gravado
    # para "0 erros" nunca ser lido, depois, como "o PVA aceitou".
    nao_conferidas = Column(Integer, default=0)

    hash_corpo = Column(String(64), nullable=True)
    versao_pva = Column(String(20), nullable=True)   # dos dados do leiaute
