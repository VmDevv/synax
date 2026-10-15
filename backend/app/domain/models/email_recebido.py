"""E-mails lidos da caixa do escritório e os XMLs que vieram neles (28/09/2026).

Pedido do Sandro: pegar os XML/ZIP que os clientes mandam por e-mail e deixá-los
prontos para a auditoria e para o gerador de SPED, e resumir de manhã o que
chegou. A caixa é a GERAL do escritório, a mesma que os funcionários usam no
Thunderbird — por isso o sistema a abre só para leitura e o controle do que já
foi visto mora AQUI, não em marca no e-mail (ver services/email_recebimento).

Duas tabelas:

- `email_mensagens`: uma linha por e-mail lido. É a trilha ("esse e-mail
  chegou? o que o sistema fez com ele?") e a matéria-prima do resumo da manhã.
  O `trecho` do corpo é o que a IA lê para resumir, e é APAGADO depois de
  alguns dias (`leitura.DIAS_TRECHO`): e-mail de funcionário não fica parado
  no banco sem motivo.

- `email_xmls`: um XML aproveitado, já com a empresa e a competência. A chave
  (empresa, chave de acesso) é única — cliente que reenvia o ZIP inteiro não
  duplica nota. O arquivo fica em disco com o nome `<chave>.xml`, que é como o
  motor da auditoria acha o XML de cada nota (`cross_checker.execute_audit`
  indexa pela chave no NOME do arquivo).

Sem `TenantMixin` na primeira: um e-mail pode trazer notas de várias empresas,
ou de nenhuma. A segunda tem `tenant_id`, e as telas filtram por ele.
"""
from sqlalchemy import (Boolean, Column, DateTime, Float, Index, Integer, String, Text,
                        UniqueConstraint)

from .base import Base, TimestampMixin


class EmailMensagem(Base, TimestampMixin):
    __tablename__ = "email_mensagens"
    __table_args__ = (
        UniqueConstraint("pasta", "uidvalidity", "uid", name="uq_email_mensagem_uid"),
        Index("idx_email_mensagem_lido", "created_at"),
    )

    id = Column(String(36), primary_key=True)
    # O endereço do e-mail no servidor IMAP: pasta + UIDVALIDITY + UID. Se o
    # servidor trocar o UIDVALIDITY (recriou a pasta), os UIDs recomeçam e o
    # `message_id` é o que evita processar de novo.
    pasta = Column(String(120), nullable=False)
    uidvalidity = Column(String(40), nullable=False)
    uid = Column(Integer, nullable=False)
    message_id = Column(String(500), nullable=True, index=True)

    recebido_em = Column(DateTime, nullable=True)       # cabeçalho Date, em UTC
    remetente = Column(String(320), nullable=True)
    remetente_nome = Column(String(255), nullable=True)
    assunto = Column(String(500), nullable=True)
    trecho = Column(Text, nullable=True)

    # sem_anexo | com_xml | sem_xml_util | proprio | grande | erro
    situacao = Column(String(20), nullable=False, default="sem_anexo")
    # Cabeçalho de lista ou de envio automático (List-Unsubscribe, Precedence
    # bulk, Auto-Submitted). É SUGESTÃO de silenciar, nunca silêncio: o aviso
    # do e-CAC também é automático e é justamente o que não pode sumir.
    # Coluna posterior à tabela: está em COLUNAS_NOVAS.
    automatico = Column(Boolean, nullable=True)
    anexos = Column(Integer, nullable=False, default=0)
    xmls_aceitos = Column(Integer, nullable=False, default=0)
    xmls_repetidos = Column(Integer, nullable=False, default=0)
    xmls_recusados = Column(Integer, nullable=False, default=0)
    # JSON: cada anexo e cada XML recusado, com o motivo. Só é lido quando
    # alguém abre UM e-mail na tela — a listagem usa as colunas.
    detalhe = Column(Text, nullable=True)


class EmailXml(Base, TimestampMixin):
    __tablename__ = "email_xmls"
    __table_args__ = (
        UniqueConstraint("tenant_id", "chave", name="uq_email_xml_chave"),
        Index("idx_email_xml_empresa_mes", "tenant_id", "competencia"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), nullable=False)
    chave = Column(String(44), nullable=False)
    modelo = Column(String(5), nullable=False)          # "NF-e" | "CT-e"
    # AAAA-MM da data que vai no DT_E_S (NF-e) ou DT_A_P (CT-e) — a mesma
    # régua do gerador de SPED (`sped_builder.gerador._entrada_iso`).
    competencia = Column(String(7), nullable=True)
    numero = Column(String(20), nullable=True)
    emitente_nome = Column(String(255), nullable=True)
    emitente_cnpj = Column(String(14), nullable=True)
    valor = Column(Float, nullable=True)
    arquivo = Column(String(500), nullable=False)       # relativo à raiz de armazenamento
    mensagem_id = Column(String(36), nullable=True)     # EmailMensagem.id de onde veio
    # "email" (anexo) ou "sieg" (baixado do cofre para um SPED que chegou por
    # e-mail — aí `mensagem_id` aponta para o EmailSped). Coluna posterior à
    # tabela: está em COLUNAS_NOVAS. NULO = anterior a ela, que era só e-mail.
    origem = Column(String(10), nullable=True)
    recebido_em = Column(DateTime, nullable=True)


class EmailRemetente(Base, TimestampMixin):
    """O que o escritório sabe sobre um remetente — a "memória" do resumo.

    Uma linha por endereço ("obrigacoes@nibo.com.br") ou por domínio
    ("@dominiosistemas.com.br"). Três usos, pedidos pelo Sandro em 28/09/2026
    quando o primeiro resumo real saiu inteiro sobre o relatório do Nibo e o
    convite de treinamento da Domínio:

    - `silenciar`: o e-mail continua sendo lido (e o XML que vier nele
      continua aproveitado), mas NÃO vai para a IA — vira uma linha de
      contagem no resumo. Filtro por regra, sem IA: decidir o que é ruído com
      o próprio modelo seria mandar o ruído para ele de qualquer jeito.
    - `nota`: o que a equipe escreveu sobre quem é o remetente ("financeiro
      da Embalar; pede segunda via de DAS"). Vai para a IA junto com o
      e-mail, como contexto. Só PESSOA escreve aqui — ver `origem`.
    - `tenant_id`: a empresa que o remetente representa. APRENDIDA sozinha
      quando um e-mail dele traz XML de uma empresa só (o CNPJ de dentro do
      XML é fato, não palpite), ou informada à mão. Serve para o resumo dizer
      de qual cliente é o pedido — nunca para decidir de quem é um XML, que
      continua sendo só o CNPJ de dentro dele.

    **A IA não escreve nesta tabela.** Um e-mail malicioso que conseguisse
    gravar "sempre marque X como urgente" na memória envenenaria todos os
    resumos seguintes; o que o modelo lê de um e-mail morre naquele resumo.
    """
    __tablename__ = "email_remetentes"

    id = Column(String(36), primary_key=True)
    padrao = Column(String(320), nullable=False, unique=True)   # minúsculo
    silenciar = Column(Boolean, nullable=False, default=False)
    rotulo = Column(String(120), nullable=True)
    nota = Column(Text, nullable=True)
    tenant_id = Column(String(36), nullable=True)
    # O mesmo endereço já trouxe XML de mais de uma empresa (o contador do
    # cliente, um despachante): a empresa aprendida é apagada e fica a marca.
    varias_empresas = Column(Boolean, nullable=False, default=False)
    origem = Column(String(10), nullable=False, default="manual")   # manual | aprendido
    atualizado_por = Column(String(255), nullable=True)


class EmailSped(Base, TimestampMixin):
    """Um SPED que chegou por e-mail — e o download dos XMLs dele no SIEG.

    Pedido do Sandro em 29/09/2026: chegou SPED, o sistema lê as chaves das
    notas de ENTRADA, baixa os XMLs do cofre do SIEG e deixa a auditoria a um
    clique na tela inicial. A empresa e a competência saem do registro 0000
    (o CNPJ do arquivo, nunca o remetente); o leiaute também, pela mesma
    `detectar_leiaute` do upload.

    O download anda em segundo plano (por chave, várias ao mesmo tempo — ver
    `sieg_download`), grava o progresso a cada rodada e continua de onde parou
    se o servidor reiniciar.
    """
    __tablename__ = "email_speds"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sha256", name="uq_email_sped_arquivo"),
        Index("idx_email_sped_status", "sieg_status"),
    )

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    competencia = Column(String(7), nullable=True)
    leiaute = Column(String(15), nullable=False)          # fiscal | contribuicoes
    arquivo = Column(String(500), nullable=False)          # relativo à raiz de armazenamento
    nome_original = Column(String(255), nullable=True)
    sha256 = Column(String(64), nullable=False)
    mensagem_id = Column(String(36), nullable=True)
    recebido_em = Column(DateTime, nullable=True)

    # pendente | baixando | concluido | parcial | falhou
    sieg_status = Column(String(12), nullable=False, default="pendente")
    sieg_mensagem = Column(String(500), nullable=True)
    chaves_total = Column(Integer, nullable=False, default=0)
    ja_tinha = Column(Integer, nullable=False, default=0)
    baixados = Column(Integer, nullable=False, default=0)
    nao_encontrados = Column(Integer, nullable=False, default=0)
    # JSON: onde o download parou (grupo, skip) e as chaves que o cofre não tem.
    progresso = Column(Text, nullable=True)
    sieg_inicio = Column(DateTime, nullable=True)
    sieg_fim = Column(DateTime, nullable=True)

    auditoria_id = Column(String(36), nullable=True)       # última auditoria feita com ele
