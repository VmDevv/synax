"""Automações do escritório: o que foi configurado e o que aconteceu.

Nasceram para a coleta de NFS-e (24/09/2026) e em 25/09 passaram a guardar
também os informativos — resumo das auditorias e erros do sistema. Os nomes
das tabelas (`nfse_*`) ficaram: renomear tabela que já está em produção é
risco sem ganho, e o `tipo` diz o que cada linha é.

As duas tabelas são do ESCRITÓRIO, não de um cliente: uma automação percorre
várias empresas, e só o admin a configura. Por isso não levam `TenantMixin` —
a empresa de cada resultado fica dentro do `detalhe` da execução.

Datas em UTC, como todo `created_at` do sistema (ver core/fuso.py). A HORA
configurada é a de Brasília, porque é a que a pessoa digita na tela.
"""
from sqlalchemy import Boolean, Column, DateTime, Index, Integer, String, Text

from .base import Base, TimestampMixin


class NfseAutomacao(Base, TimestampMixin):
    __tablename__ = "nfse_automacoes"

    id = Column(String(36), primary_key=True)
    nome = Column(String(120), nullable=False)
    ativo = Column(Boolean, nullable=False, default=True)

    # coleta_nfse | resumo_auditorias | erros_sistema. O que cada tipo aceita
    # de configuração vai em `config` (JSON) — ver services/automacoes/.
    tipo = Column(String(30), nullable=False, default="coleta_nfse",
                  server_default="coleta_nfse")
    config = Column(Text, nullable=True)

    # "diaria", "semanal" ou "mensal". Na semanal, `dias_semana` lista os dias
    # como números separados por vírgula, segunda = 0 (o `weekday()` do
    # Python). Na mensal, `dia_mes` é o dia (1 a 28 — o 29 não existe em
    # todo fevereiro, e automação que pula um mês não avisa ninguém).
    frequencia = Column(String(10), nullable=False, default="diaria")
    dias_semana = Column(String(20), nullable=True)
    dia_mes = Column(Integer, nullable=True)
    hora = Column(String(5), nullable=False)          # "HH:MM", horário de Brasília

    # "todas" = empresas ativas com certificado cadastrado; "selecionadas" =
    # a lista de `empresas` (ids de tenant separados por vírgula).
    escopo = Column(String(15), nullable=False, default="todas")
    empresas = Column(Text, nullable=True)

    # A próxima execução é CALCULADA na gravação e depois de cada rodada. É
    # também a trava contra rodar em dobro: quem roda a automação precisa
    # trocar este valor no banco, e só um consegue (ver automacao.reivindicar).
    proxima_execucao = Column(DateTime, nullable=True)
    ultima_execucao = Column(DateTime, nullable=True)
    criado_por = Column(String(120), nullable=True)

    # Quem recebe o informativo, separados por vírgula. Vazio = não envia.
    destinatarios = Column(Text, nullable=True)


class NfseAutomacaoExecucao(Base, TimestampMixin):
    """Uma rodada da coleta. É o que o informativo e o histórico da tela leem.

    Os números ficam em colunas para a listagem não precisar abrir o
    `detalhe` (mesma armadilha do `result_data` da auditoria): o JSON por
    empresa só é lido quando alguém abre UMA execução.
    """
    __tablename__ = "nfse_automacao_execucoes"
    __table_args__ = (
        Index("idx_nfse_exec_automacao", "automacao_id", "inicio"),
    )

    id = Column(String(36), primary_key=True)
    automacao_id = Column(String(36), nullable=False)
    # Copiado da automação: a execução continua legível (e a planilha
    # baixável) depois que a automação é excluída.
    tipo = Column(String(30), nullable=False, default="coleta_nfse",
                  server_default="coleta_nfse")
    gatilho = Column(String(10), nullable=False, default="agenda")   # agenda | manual
    # rodando | ok | com_falhas | falhou | vazia | interrompida
    status = Column(String(15), nullable=False, default="rodando")
    inicio = Column(DateTime, nullable=False)
    fim = Column(DateTime, nullable=True)

    empresas_total = Column(Integer, nullable=False, default=0)
    empresas_ok = Column(Integer, nullable=False, default=0)
    empresas_falha = Column(Integer, nullable=False, default=0)
    # O ADN pediu para esperar ("consumo indevido"). Não é falha nem sucesso:
    # a empresa não foi consultada nesta rodada, e o informativo diz isso.
    empresas_adiadas = Column(Integer, nullable=False, default=0)
    notas_novas = Column(Integer, nullable=False, default=0)
    notas_atualizadas = Column(Integer, nullable=False, default=0)
    certificados_vencendo = Column(Integer, nullable=False, default=0)

    detalhe = Column(Text, nullable=True)      # JSON: empresas e certificados
    mensagem = Column(String(500), nullable=True)

    # Planilha da rodada, relativa à raiz das NFS-e (storage.raiz()). NULO =
    # não foi salva (pasta da rede fora) — a mensagem da rodada diz isso.
    relatorio = Column(String(500), nullable=True)
    # enviado | falhou | sem_destinatario. O informativo que não sai tem de
    # aparecer no histórico: e-mail que ninguém recebe é o silêncio que a
    # automação existe para evitar.
    email_status = Column(String(20), nullable=True)
    email_detalhe = Column(String(500), nullable=True)

    # A janela que o informativo cobriu (UTC). É o `periodo_fim` da última
    # rodada que diz de onde a próxima começa, no modo "desde a última".
    periodo_inicio = Column(DateTime, nullable=True)
    periodo_fim = Column(DateTime, nullable=True)
