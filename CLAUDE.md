# Conferência de Entradas (Synax)

Auditoria fiscal multi-tenant: cruza SPED com XML de NF-e/CT-e e cura as regras que o
motor não conseguiu casar. FastAPI + SQLAlchemy + Jinja2/HTMX, Supabase (Postgres).

## Barreira de feedback — rode antes de dizer que terminou

```bash
venv/Scripts/python.exe tests/run_all.py
```

65 suítes (~4340 testes), SQLite em processo, sem rede, ~2 min. Suíte isolada:
`venv/Scripts/python.exe tests/test_mapeamento_lote.py`. Cada suíte é um script que imprime
`OK`/`FALHA` e sai com código 1 se falhar — não é pytest. Suíte nova entra em
`tests/run_all.py`.

## Base de conhecimento

Cofre: `C:\Users\sandro.filho\Desktop\Trabalho\Cofre Obsidian Vm Contadores\Vm Contadores`.
Resumo do sistema em `wiki/projetos/conferencia-entradas.md`; regras fiscais em
`wiki/regras/`. **O histórico completo de cada decisão abaixo** (incidente, medição, data,
alternativa descartada) está em `wiki/projetos/conferencia-entradas-decisoes.md` — leia lá
antes de reverter qualquer uma. Não leia o código inteiro para se situar.

Decisão fiscal nova é do Sandro: antes de "corrigir" algo marcado aqui como decidido, fale
com ele.

## Regras do motor de auditoria

- **Regra é do tenant e a chave é exata, em todo imposto.** CFOP: `company_cnpj`,
  `supplier_cnpj`, `supplier_cfop`, `product_code`, `dict.get()` seco em
  `cross_checker.py`. Nunca grave `"*"`, `"TODOS"` nem regra global — cria regra que nunca
  casa. Exceção legítima: `cest_mva_rules` (Portaria 16, dado legal). Lixo antigo:
  `scripts/limpar_regras_globais.py`.
- **Sem regra cadastrada, o motor não alega nada.** `permite_credito_icms` devolve `None`
  (≠ `False`): o CFOP vai para pendências, sem glosa nem comparação.
- **Quem decide crédito é o CFOP MAPEADO** (`cfop_de_referencia` → `cfop_regra`), não o
  escriturado, que é o que está sob auditoria. `agrupar_icms_por_item_identico` precisa de
  `cfop_por_linha` com o mesmo critério do laço. Crédito devido e não tomado sai como
  "ICMS não aproveitado".
- **Item do SPED não casa por posição.** `casar_itens_da_nota`: código → unitário +
  quantidade → unitário → posição conferida. Sem sinal confiável, não confere — mas
  **aparece** em `results["itens_nao_conferidos"]` com o motivo; nunca `continue` mudo.
  Ainda silenciosos no CT-e: sem D190 e XML ilegível (`scripts/diagnosticar_cte.py`).
- **Precedência do casamento, não negociável:** determinístico → par aprovado
  (`item_match_rules`, chave de 5 campos, `aplicar_pares_conhecidos`) → sugestão da IA.
  Par de operador é `pendente_revisao` e o motor ignora até aprovação (aba "Itens
  SPED×XML", sem edição inline de propósito).
- **IA de casamento** (`ia_matcher.py`, OpenAI `gpt-5.1`): só recebe os itens que sobraram,
  só descrição (do 0200) e valor total; nunca desfaz casamento determinístico; auditoria
  para em `aguardando_ia`. Par aceito viaja marcado (`casado_por_ia`, `ia_confianca`) e a
  tela mostra o ícone — presunção nunca é silenciosa. Sem `OPENAI_API_KEY` tudo roda igual.
  Anthropic continua no auditor de PDF e no reporter de erros.
- **PIS/COFINS se cruza BASE contra BASE**, e "SPED R$ 0,00 vs Base Calculada" é
  ESPERADO (o Fiscal quase nunca preenche `VL_BC_PIS`). Decisão aceita, travada em
  `tests/test_pis_cofins_valor_e_cte_outra_uf.py` seção 3c. `credito_pis_cofins_esperado()`
  fica fora de uso e testada. Frete (CT-e) continua por valor, `pis_cofins_da_base()`.
- **Regra de PIS/COFINS é consultada também na nota SEM XML** (`sem_direito_a_credito()`,
  chamada nos dois lugares); divergência sai com `sem_xml: True`.
- **ICMS do CT-e = `vICMS` + `vICMSOutraUF`**; `vl_icms_proprio`/`vl_icms_outra_uf`
  separados e a mensagem abre a conta.
- **ICMS Monofásico soma as quatro tags** (`vICMSMono`, `vICMSMonoReten`, `vICMSMonoRet`,
  `vICMSMonoDif`) quando a opção está marcada. Decisão do escritório, não da lei.
- **COD_SIT:** só `SITUACOES_SEM_EFEITO = {"02","03","04","05"}` sai da apuração
  (`sped_parser.py`). Mudar isso muda a contagem de toda auditoria (`tests/test_cod_sit.py`).
- **Fornecedor da nota é o 0150 e é conferido** (`conferir_cnpj_do_emitente`). Quem decide
  o lado é o IND_EMIT: terceiros + entrada → COD_PART é o emitente; emissão própria →
  confere com o 0000. Mesma raiz sai `filial_divergente`. Não roda no modo "só SPED".
  `parse_sped()["participants"]` é `{COD_PART: dict}` — use `nome_participante()` /
  `id_participante()`, nunca indexe direto.
- **Dois leiautes de SPED (Fiscal e Contribuições)**; só o 0000 muda de posição, e ler um
  pelo outro não dá erro, dá lixo. `detectar_leiaute` no upload, `ler_sped` recusa com
  `LeiauteInesperado`. Um percurso só (`sped_parser.percorrer`) com tabela de leiaute.
  Contribuições não tem C190/D190; apuração é Bloco M, não tocada; o arquivo é por PJ
  (todas as filiais, campo `estabelecimento`) e separar por filial está em aberto.
- **Competência** (`audit_tasks.competencia`, AAAA-MM) vem do 0000; sem ele é `None`, nunca
  um mês padrão. Calendário em `core/competencia.py`; telas nunca cobram o mês corrente.

## Correção do SPED (`services/sped_rewriter/`)

- **Leitura própria (`documento.py`), campo não tocado sai byte a byte.** Não use o
  `sped_parser` da auditoria para reescrever.
- **Fiscal e Contribuições são cascatas separadas** (`aplicar`/`aplicar_contribuicoes`,
  `propor`/`propor_contribuicoes`), escolhidas por `resumo.leiaute` (sem marca = Fiscal).
  Sem `if` dentro das de sempre: misturar injeta C190 no Contribuições. Opções por leiaute
  em `opcoes_do_leiaute()`; fonte única `OPCOES_DE_CORRECAO` em `patch.py` (CST não é
  opção).
- **Cascata:** C170 → C190 → C100 → Bloco 9; frete D190 → D100 por igualdade. Correção que
  viola o mesmo 1º dígito de CFOP no documento (C170 campo 11) recusa o DOCUMENTO inteiro.
  9990 conta o 9999, 0990 conta o 0000; ordem das 9900 preservada.
- **Apuração só sob caixa própria** (`Patch.atualizar_apuracao`, `cascata.atualizar_e110`):
  aplica a DIFERENÇA no campo 06, confere a fórmula do Guia antes de escrever, recusa com
  mais de um E110. E520, E111 e saldo anterior intocados.
- **Glosa** zera base/alíquota, troca o CST (ICMS `x00`→`x90`, IPI `00`→`49`, PIS/COFINS
  → `CST_PIS_COFINS_GLOSADO = "70"`) e leva ICMS ST e IPI para VL_ITEM/VL_MERC — não o ICMS
  próprio nem PIS/COFINS. **Ganho de crédito** ajusta o CST para tributado
  (`ajustar_cst_do_credito`).
- **PIS/COFINS:** glosa vale nos DOIS leiautes; correção de BASE só no Contribuições
  (no Fiscal, `so_glosa=True`). Grava os seis campos juntos; valor derivado da base pelas
  alíquotas FIXAS `ALIQ_PIS_CONTRIBUICOES`/`ALIQ_COFINS_CONTRIBUICOES` (1,65/7,60) em
  `ponte.py`, nunca do XML. C100 campos 26/27 só refeitos se a glosa mexeu no documento.
  Bloco M e PIS/COFINS do frete (D101/D105) não são reescritos — proposta sai impedida.
- **Correção de tributo não muda VL_DOC.** Só as opções de valor (`valor_documento`/
  `valor_frete`) mexem nele, e fecham a nota pondo o resíduo em VL_OUT_DA
  (`cascata.fechar_componentes`). **Nunca ratear a diferença entre itens.** Itens somando
  MAIS que o documento é recusado.
- **A ponte (`ponte.py`) confere cada divergência contra o arquivo em disco.** O que não dá
  para automatizar aparece com o motivo (ex.: `itens_agrupados > 1`), nunca some.
- **Download:** `octet-stream` + `attachment` + `nosniff`, nome em `filename` e `filename*`.
  Cada geração grava `SpedCorrectionRun`; a coluna `detalhe` nunca é lida em listagem.
- **Resumo executivo (`resumo_ia.py`, GPT): a IA escreve, o motor calcula.**
  `_so_usa_os_numeros_dados()` descarta texto com número que não foi enviado; vale o
  `resumo_local()`. A tela marca a origem do texto.

## Gerador de SPED a partir de XMLs (`services/sped_builder/`)

- Entrega só ENTRADAS para importar no Domínio; E, G, H, K e 1 com IND_MOV=1; Blocos B, C e
  D existem sempre. COD_VER pelo ano (`COD_VER_POR_ANO`). Política fiscal (direito do
  comprador: ST/IPI no custo, PIS/COFINS CST 99, CSOSN x90) no cabeçalho de `gerador.py`.
- **CNPJ do cadastro separa entrada de saída.** Pacote com vários meses gera o mais recente
  (ou o escolhido); notas de meses anteriores entram só se marcadas, com DT_E_S na 1ª
  quinzena. `COD_ITEM` = cProd do fornecedor (sufixo com CNPJ em colisão). Nota com cStat
  fora de 100/150 fica de fora com motivo.
- **Regras de CFOP próprias** (`gerador_cfop_regras`, `gerador_cte_cfop_regras` por CFOP ×
  UF do transportador). Não lê nem grava as tabelas da auditoria. `/confirmar` e
  `/regerar` usam só o aprovado no BANCO, `exigir_regra=True`. Lixo antigo:
  `scripts/separar_regras_do_gerador.py`.
- **CFOP de entrada só da tabela oficial** (`cfop_de_entrada_valido`,
  `domain/constants/tabela_cfop.py`). Sem espelho, sugestão só do mapa curto
  `EQUIVALENTES_SEM_ESPELHO` — não aumente por palpite.
- Tudo preenchido sem estar no XML vira `Presuncao` e aparece antes do botão de baixar.
  Travado em `tests/test_sped_builder.py` e `tests/test_gerar_sped_mapa.py`.

## Débito de sub-apuração C195/C197 (`services/sped_debito_subapuracao.py`)

- C190 com CFOP 5106/6106: base no campo **6**, ICMS no campo **7** (numeração do Guia).
- Um par C195/C197 por DOCUMENTO, no fim do C100, somando os C190 alvo por (CFOP,
  ALÍQUOTA); mais de uma combinação = nota não ajustada, com aviso. Um C190 só: strings
  copiadas, sem Decimal.
- Idempotência pelo COD_AJ do C197 (nunca pelo COD_OBS). COD_OBS do 0460 é resolvido
  (reusa, senão 1010 livre, senão `SBAPnn`), inserido em ordem.
- COD_AJ `MG23000999` obriga 1001/1010/1900/1910/1920 e crédito no E110 campo 07: 1920
  recalculado inteiro, E110 recebe só a diferença. E116/1926 NÃO são reescritos (exigem
  DT_VCTO e COD_REC, que não estão no SPED) — o módulo mede e avisa.
- Guia que vale é o 3.2.3 (1010 com 14 campos). **A partir de jan/2027** o C197 ganha
  `IND_BENEFICIO`: `TAMANHO_C197` vai para 9 e o campo precisa de valor.

## NFS-e, e-mail e SIEG

- **Lote de NFS-e em segundo plano** (`services/nfse/lote.py`), um por vez, mesma
  `_trava("coleta_nfse")` da coleta automática. Grava em `<EMPRESA CNPJ>\<AAAA-MM-DD>\`,
  pasta achada pelo CNPJ no fim do nome. ADN consulta com `cnpjConsulta` do
  estabelecimento; certificado de outra raiz é recusado (`cnpj_do_certificado`).
  "CONSUMO INDEVIDO" é `ConsumoIndevido` (adiada), não "sem notas".
- **Agendador** é thread no `lifespan` (testes não sobem; `SYNAX_AGENDADOR=0` desliga).
  Trava por UPDATE condicionado, uma por TIPO; não roda em dobro nem por dia perdido.
  Informativos somam COLUNAS de `audit_tasks`, nunca `result_data`.
- **Caixa de e-mail é só leitura:** `select(readonly=True)` + `BODY.PEEK[]`; `imap.py`
  não pode ter STORE/COPY/MOVE/EXPUNGE/CLOSE (a suíte confere a fonte). Estado no banco
  (pasta, UIDVALIDITY, UID + Message-ID).
- **De quem é o XML só o CNPJ de dentro dele responde** (NF-e: destinatário; CT-e:
  tomador). Recusado sai com motivo. Gravado como `<chave>.xml`; a auditoria escolhe pela
  chave do SPED, nunca pelo mês.
- **IA do resumo da manhã lê e escreve, não age nem conta**; conteúdo de e-mail é dado,
  nunca instrução; a IA nunca grava na memória de remetentes (`email_remetentes`). Ruído
  sai por regra antes da IA.
- **SIEG:** download só por chave (`BaixarXml`, em paralelo); o lote exige `email` junto
  da `api_key` e sem ele levanta `SiegIndisponivel`. Cliente sempre via
  `configuracoes.cliente_sieg(db)`, nunca `SiegClient()` numa rota.

## Outras regras de domínio

- **CFOP 1933 vs 2933** sai de `uf_prestador` × `uf_tomador` lidas no PDF; faltando UF,
  presume 1933 e marca. Nunca UF fixa no código nem do `Tenant`.
- **Auditoria de PDF de NFS-e** (`pdf_ia_auditor.py`, `claude-sonnet-5`, `effort: "low"`,
  `MAX_TOKENS = 128000`) manda o PDF cru, saída por `output_config.format`; truncado
  levanta erro; >20 MB pela Files API, apagado no `finally`.
- **Validar SPED valida no PVA oficial** (`services/pva_motor.py`; motor em `Desktop\Projetos\pva-motor`, HTTP só
  em 127.0.0.1, `PVA_MOTOR_URL`; o Painel Dev liga junto com o Synax). O Synax só pede `/saude`, `/validar` e
  `/assinar`: nunca liga, desliga nem **transmite** (a suíte confere a fonte; o envio à Receita não está ligado e
  não deve ser ligado daqui — pedido de transmitir é recusado com o motivo). Fila de um por vez
  (`validar_sped_fila.py`), resultado em colunas de `sped_validacoes` (`motor` NULO = validador antigo). Aprovado só
  com VALIDADA/GERADA_PARA_ENTREGA, sem erro e sem falha; motor fora do ar e EFD-Contribuições respondem antes da
  fila. **Assinar** só arquivo aprovado, com o e-CNPJ (.pfx + senha) cifrado em `app_settings`, conferido ao gravar;
  sem certificado ou com ele recusado, valida e diz por que não assinou. **Rotina automática** `validar_sped`
  (`services/automacoes/validar_sped.py`): pasta → PVA arquivo por arquivo pelo mesmo caminho da aba; assinado é
  CÓPIA no destino, nunca por cima; nada some (pulado sai com o motivo). **Correção pelo Synax**
  (`validar_sped_correcao.py`): sozinho só formato e contagens (espaço nas pontas, quebra final, x990/Bloco 9), só
  onde o PVA apontou e a linha confere; prévia antes → depois; cópia volta ao PVA como validação nova (`corrige`);
  o resto vira pendência com o que fazer — valor, apuração e cadastro nunca saem sozinhos, e a rotina nunca corrige.
  O arquivo enviado fica 7 dias (`sped_original.txt`) para isso. O `sped_validador` em Python roda junto só
  para comparar (`py_erros`, CSV do admin) — apagar é decisão do Sandro.
- **Radar/painel nunca abrem `result_data`** (`painel_atividade.py` nomeia coluna por
  coluna); ranking é a última auditoria, não a soma.
- **Lucro Presumido (`/lucro-presumido`) e Domínio (`/dominio`): a tela é aqui, o motor é
  o do programa `Desktop\sped-pis-cofins`**, chamado pela linha de comando com saída JSON
  (`services/efd_contribuicoes/programa.py`; o contrato e a versão `CONTRATO` estão em
  `spedpc/__main__.py` de lá). Não escreva gerador nem consulta ao Domínio aqui: correção
  fiscal se faz uma vez, no programa. Quem vê qual empresa é `efd_contribuicoes/
  empresas.py` (lista do Domínio × tenants pelo CNPJ da matriz; operador só as dele,
  conferido em toda ação). A pasta de "Salvar" é a do programa, nunca a do formulário;
  planilha/zip de lote só o admin baixa. **Consulta livre (SQL) do Domínio só com
  `authz.pode_sql_dominio`**: admin sempre, operador com a marca `pode_sql_dominio` que o
  admin dá na edição do usuário (lê qualquer empresa; a trava de escrita é do programa).
  O resto do programa (Conferência do Lucro Real, Automações, Configurações) segue lá, 8782.

## Armadilhas do framework e do front

- **`str = Form(...)` rejeita string vazia** (422 antes da função, e o htmx não troca alvo
  em 4xx). Use `Form("")` e valide dentro, devolvendo 200 com mensagem.
- **`hx-boost="true"` no `<body>`:** todo form de download precisa de `hx-boost="false"`
  (`target="_blank"` não protege). E o boost descarta o `<head>`: `<style>` de página vai
  dentro do `{% block content %}`; o que é global fica em `layout/base.html` e
  `static/js/ui.js`.
- **Conteúdo sob demanda dispara com `intersect once`**, não com clique numa aba.
- **`hx-include` é seletor do documento inteiro** — use id.
- **Sessão vencida:** `HX-Request` conta como navegador (204 + `HX-Redirect`); o `next` do
  login é sempre uma TELA (`_tela_de_volta`, `destino_seguro` recusa `/api/` e `/static/`).
- `/saude` é a conferência do Painel Dev, sem sessão nem banco e fora do log.
- **HTML em f-string escapa do autoescape:** `_js()` dentro de `<script>`, `html.escape()`
  em texto.
- `blue-*` do Tailwind é a escala da marca (#0377AA = blue-600) — não "corrija".
- Data do banco é UTC; na tela, sempre pelo filtro `| hora_br` (`core/fuso.py`).
- Operação em lote nunca responde verde com zero: diga o que não casou.

## Convenções

- Autorização passa por `backend/app/core/authz.py`: toda rota que grava valida papel e
  tenant (operador só opera empresas de `user_tenants`).
- `rule_status_for`: operador grava `pendente_revisao`, admin grava `aprovado`.
  Maker-checker — não contorne.
- **Coluna nova em model já em produção entra em `COLUNAS_NOVAS`**
  (`schema_upgrades.py`); `create_all` não altera tabela existente. Tabela nova nasce com
  RLS (`schema_upgrades.ligar_rls`).
- **Credencial do escritório** fica em `app_settings`, cifrada; ordem banco → `.env`; a
  chave nunca volta para a tela e campo em branco mantém a gravada.
- **Chave de API nunca no código**, nem provisória (`tests/test_pdf_ia_auditor.py` trava).
- Script/estilo de CDN leva SRI; Tailwind servido de `frontend/static/js/vendor/`.
  Trocou versão, recalcule o hash. `requirements.txt` espelha o venv. XML de terceiro só
  com `defusedxml`.
- Funcionalidade que muda o dia a dia ganha NOVIDADE em
  `backend/app/services/novidades.json` no mesmo commit (texto aprovado pelo Sandro;
  correção invisível não vira novidade). Ver `services/novidades.py`.
- Comentário explica **por quê**, não o quê.
