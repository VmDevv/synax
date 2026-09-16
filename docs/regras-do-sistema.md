# Regras do sistema — Conferência de Entradas (Synax)

Referência de **todas as regras** que o motor de auditoria aplica e de **como cada
mapeamento funciona**: qual é a chave, quem consulta, o que acontece quando a regra
não existe e por onde ela entra no banco.

Escopo: `backend/app/services/audit_engine/cross_checker.py` (motor),
`backend/app/domain/models/rules.py` (tabelas),
`backend/app/infrastructure/repositories/` (leitura),
`backend/app/api/routes/uploads.py` (curadoria) e `admin.py` (administração).

---

## 1. Os três princípios que valem para tudo

### 1.1 Regra é do tenant. Não existe regra global.

Toda leitura de regra passa por `_tenant_scope` (`rules_repository.py`) e por
`BaseRepository.get_all` (`base_repository.py`), que filtram **exatamente dois
critérios**:

```
model.tenant_id == tenant da auditoria
model.status    == 'aprovado'
```

O campo `is_global` ainda existe nas tabelas, mas **nenhuma consulta o lê**. Até
25/08/2026 o escopo era `tenant_id == meu OU is_global`, e 32 regras semeadas sob um
tenant fantasma `"GLOBAL"` decidiam crédito para toda empresa do sistema. Para apagar
o que ficou no banco: `scripts/limpar_regras_globais.py` (simula por padrão,
`--aplicar` grava).

Exceção legítima: **`cest_mva_rules`** (Portaria 16). É dado legal, não decisão de
crédito, e é lida fora do escopo por tenant.

### 1.2 A chave é exata. Curinga não existe.

O motor monta dicionários em RAM e faz `dict.get()` seco. Não há fallback, não há
`LIKE`, não há `"*"`. Gravar curinga em qualquer campo de chave produz uma regra que
**nunca casa** — parece trabalho feito e não é. As telas recusam isso visivelmente:

| Onde | O que é recusado |
|---|---|
| `POST /api/mapping/cfop` | CNPJ da empresa, CNPJ do fornecedor ou código do produto vazio ou `*` |
| `POST /api/mapping/ipi_rule` | CST de IPI vazio ou `*` |
| `POST /admin/rules/cfop/add` | `*` ou `TODOS` nos três campos |
| `POST /admin/rules/ipi/add` | CST vazio ou `*` |
| Lote de CFOP (`/api/mapping/cfop/bulk`) | item sem `product_code` é pulado |

### 1.3 Maker-checker: quem grava define o status.

`rule_status_for(profile)` em `backend/app/core/authz.py`:

| Papel | Status da regra gravada | Efeito |
|---|---|---|
| `admin` | `aprovado` | vale na próxima auditoria |
| `operador` | `pendente_revisao` | fica na Curadoria até um admin aprovar |
| demais | `pendente_revisao` | idem, e o tenant é forçado para o do próprio perfil |

E o **motor nunca grava regra por conta própria**. Heurística vira sugestão em
memória (`results[...]`), nunca linha no banco. O único registro que o motor grava é
o `MissingInvoiceLog` — fato apurado, não palpite.

---

## 2. As sete tabelas de regras

| Tabela | Modelo | Chave primária (= chave de busca) | Decide |
|---|---|---|---|
| `cfop_classifications` | `CfopClassification` | `company_cnpj` + `supplier_cnpj` + `supplier_cfop` + `product_code` + `tenant_id` | qual CFOP de entrada a NF-e deveria ter |
| `cte_cfop_classifications` | `CteCfopClassification` | `company_cnpj` + `supplier_cfop` + `emitter_uf` + `tenant_id` | qual CFOP o frete deveria ter |
| `cfop_credit_rules` | `CfopCreditRule` | `cfop` + `tenant_id` | se o CFOP dá crédito de **ICMS** |
| `ipi_credit_rules` | `IpiCreditRule` | `cfop` + `cst_expected` + `tenant_id` | se dá crédito de **IPI** |
| `pis_cofins_credit_rules` | `PisCofinsCreditRule` | `cfop` + `cst_expected` + `tenant_id` | se dá crédito de **PIS/COFINS** |
| `icms_anticipation_rules` | `IcmsAnticipationRule` | `uf_origem` + `uf_destino` + `cfop` + `tenant_id` | MVA/alíquotas da **antecipação** |
| `cest_mva_rules` | `CestMvaRule` | `cest` + `tenant_id` | MVA da Portaria 16 (dado legal) |

Todas carregam `status`, `is_global` (morto), `created_by` e timestamps.

---

## 3. O mapeamento que comanda todos os outros: CFOP de referência

**Quem decide crédito é o CFOP MAPEADO, não o escriturado no SPED.**

`cfop_de_referencia(sped_cfop, mapped_cfop)`:

```
CFOP de referência = mapped_cfop  se existe regra de classificação para a operação
                   = sped_cfop    caso contrário
```

O CFOP do SPED é o que o cliente lançou — é justamente o que está sob auditoria,
então não pode ser também o critério dela. ICMS, IPI e PIS/COFINS consultam suas
regras por esse CFOP (variável `cfop_regra` no motor); `sped_cfop` não aparece em
nenhuma consulta de crédito.

**O que isso evita:** escriturado `1556` (sem crédito) e mapeado `1653` (com crédito)
produzia ao mesmo tempo "o CFOP deveria ser 1653" e "1556 não permite crédito, glose o
ICMS" — mandando desfazer um crédito que, com o CFOP corrigido, é devido.

Quando o CFOP usado não é o do SPED, toda mensagem sai com o sufixo de
`_nota_cfop()`: *"(CFOP mapeado 1653; no SPED está 1556)"*. A marca não é decorativa —
sem ela o usuário lê um número que não está no arquivo dele.

---

## 4. Mapeamento de CFOP da NF-e (`cfop_classifications`)

**Chave (4 campos, exata):** CNPJ da empresa · CNPJ do fornecedor · CFOP do XML ·
código do produto do fornecedor (`cProd`).

**Como o motor usa:**

```
mapping_key = (company_cnpj, emit_id do XML, cfop do XML, cProd)
mapped_cfop = cfop_mappings.get(mapping_key)
```

| Resultado | O que sai no relatório |
|---|---|
| achou e `sped_cfop != mapped_cfop` | `cfop_classifications_issues` — "o CFOP deveria ser X" |
| achou e é igual | nada (está certo) |
| não achou | `unmapped_items` / `cfops_desconhecidos` — vai para a aba de pendências com sugestão em memória |

A sugestão passa por `apply_specific_cfop_rules()`, hoje um interceptador vazio que
devolve o CFOP do próprio SPED — é o gancho para regras específicas futuras.

**Por que a chave inclui o produto:** o mesmo fornecedor, com o mesmo CFOP de saída,
vende itens que entram com CFOPs diferentes (revenda × uso e consumo × imobilizado).
Uma regra por fornecedor classificaria tudo igual.

---

## 5. Mapeamento de CFOP do CT-e (`cte_cfop_classifications`)

**Chave (3 campos):** CNPJ da empresa · CFOP do CT-e · UF do emitente.

O produto não entra: frete não tem item. A UF entra porque ela é o que separa
`1352` de `2352`.

```
mapping_key   = (company_cnpj, cfop do XML do CT-e, emit_uf)
expected_cfop = cte_cfop_mappings.get(mapping_key)
```

O CT-e do SPED (D100) pode ter **vários CFOPs** no analítico D190; o laço percorre
`d100["cfops"]` e trata cada um. Para cada CFOP escriturado:

1. `cfop_regra = cfop_de_referencia(sped_cfop, expected_cfop)` — mesma regra da NF-e;
2. crédito de ICMS do frete é decidido por esse CFOP;
3. se `expected_cfop` existe e difere → `cte_cfop_classifications_issues`;
4. se não existe → `unmapped_cte_items` (pendência, uma por chave).

Além disso, **regra do tomador**: se o `tomador_id` do XML não é o CNPJ da empresa, sai
em `cte_tomador_issues` — frete de terceiro escriturado como próprio.

O CT-e também é conferido em **PIS/COFINS** — ver a seção 8.4.

---

## 6. Crédito de ICMS (`cfop_credit_rules`)

**Chave:** CFOP (o de referência) → `allow_credit` booleano.

`permite_credito_icms(cfop, mapa)` é um `dict.get()` que devolve **três estados**:

| Retorno | Significado | Ação do motor |
|---|---|---|
| `True` | CFOP dá crédito | compara SPED × XML |
| `False` | CFOP não dá crédito | se o SPED creditou > R$ 0,01 → glosa em `icms_issues` |
| `None` | **não há regra** | CFOP entra em `unmapped_icms_cfops`; **nada é alegado** |

**`None` não é `False`.** Havia uma lista fixa de 10 CFOPs no código
(`DEFAULT_NO_CREDIT_CFOPS`) que glosava sem regra cadastrada — acusação que nenhum
admin aprovou, em toda empresa. Sem saber se o CFOP dá crédito, "SPED R$ X vs XML R$ Y"
acusaria toda nota legitimamente glosada.

### 6.1 O que conta como ICMS creditável do XML

`icms_creditavel_xml()`:

```
crédito = vl_icms (ICMS normal + ICMS do Simples Nacional)
        + vl_icms_mono           ← só se "Considerar ICMS Mono" estiver marcado
```

### 6.2 Comparação por grupo de item idêntico

A comparação **não é linha a linha**: é o total do produto na nota, dos dois lados
(`agrupar_icms_por_item_identico`). A identidade do item dentro do XML é
`cProd + descrição normalizada + valor unitário` (`_chave_item_xml`).

Motivo: a mesma mercadoria aparece em duas linhas (mesmo código, mesmo preço,
quantidades diferentes) e as duas linhas do SPED eram comparadas contra a **mesma**
linha do XML — a segunda acusava divergência inexistente.

Só entram na soma do SPED as linhas cujo **CFOP de referência permite crédito** — e o
mapa `cfop_por_linha` precisa usar o mesmo critério do laço principal, senão a linha
entra na soma de um lado e sai glosada do outro.

### 6.3 Textos de divergência

| Situação | Mensagem |
|---|---|
| SPED creditou, CFOP não permite | *"CFOP X não permite crédito de ICMS, mas SPED creditou R$ Y"* |
| SPED = 0 e XML > 0, CFOP permite | *"ICMS não aproveitado: … o SPED não creditou nada"* |
| valores diferentes | *"Divergência de crédito de ICMS: SPED = R$ X, XML = R$ Y"* |

O texto de "não aproveitado" existe porque *"SPED R$ 0,00 vs XML R$ X"* fazia procurar
erro de valor onde o que houve foi crédito deixado na mesa. Quando o grupo tem mais de
uma linha, a mensagem diz *"— soma das N linhas deste mesmo produto na nota"*.

Tolerância em todas as comparações de valor: **R$ 0,05**.

---

## 7. Crédito de IPI (`ipi_credit_rules`)

**Chave: (CFOP de referência, CST de IPI do SPED).** As duas partes, sempre.

```
allow_ipi = ipi_rules_map.get((cfop_regra, sped_ipi_cst))
```

Até 25/08/2026 o mapa era só `{cfop: allow}` e a curadoria gravava `cst_ipi = "*"`:
o CST nem era consultado, uma resposta valia para qualquer CST daquele CFOP e duas
regras do mesmo CFOP se sobrescreviam em silêncio.

| Retorno | Ação |
|---|---|
| `None` **e** o SPED trouxe CST | pendência em `unmapped_ipi_rules`, com o **CST real** |
| `None` e o SPED não trouxe CST | nada — sem CST não há chave para cadastrar regra |
| `False` e `sped_ipi > 0,01` | glosa em `ipi_issues` |
| `True` e diferença > R$ 0,05 | divergência (ou "IPI não aproveitado" se o SPED zerou) |

---

## 8. Crédito de PIS/COFINS (`pis_cofins_credit_rules`)

**Chave: (CFOP de referência, CST).** PIS e COFINS consultam a **mesma tabela**, cada
um com o seu CST do SPED (`cst_pis`, `cst_cofins`). Cada CST sem regra vira uma linha
em `unmapped_pis_cofins_rules`.

### 8.1 Elegibilidade a crédito

O item é considerado **inelegível** se qualquer uma destas for verdadeira:

- CST do SPED está em `{07, 08, 09, 49, 70, 71, 72, 73, 74, 75, 98, 99}` (lista fixa no
  código, `is_non_credit_cst`);
- a regra de PIS devolveu `False`;
- a regra de COFINS devolveu `False`.

Inelegível **e** o SPED creditou > R$ 0,01 → uma linha em `pis_cofins_issues`
("CFOP/CST sem direito a crédito"). Inelegível e zerado → correto, nada sai.

> Diferente de ICMS e IPI: aqui `None` (sem regra) **não bloqueia** a comparação de
> base. Sem regra e com CST de crédito, o item segue elegível e a base é conferida.
> O CFOP de ICMS (1407, 1556…) não bloqueia PIS/COFINS quando o CST é de crédito.

### 8.2 A base conferida é calculada, nunca copiada do XML

A comparação é de **base de cálculo**, e a base do XML é **recalculada** pelo parser
segundo a tese do STF:

```
bruto = vProd + vFrete + vSeg + vOutro + vIPI + vIPIDevol + vICMSST + vFCPST − vDesc
base  = bruto − vICMS(normal+SN) − vICMSST − vFCPST − vIPI − vIPIDevol     (mínimo 0)
```

As tags `vBC` originais de PIS/COFINS do XML **não** são usadas no confronto — é
exatamente a exclusão do ICMS da base que se quer auditar. Divergência acima de
R$ 0,05 em qualquer das duas bases gera **uma única linha** de apontamento.

> **Em aberto (26/08/2026):** este confronto é de **base**, contra os campos
> `VL_BC_PIS`/`VL_BC_COFINS` do C170. Se esses campos vierem vazios no SPED — e o Sandro
> informou que o SPED Fiscal traz só o valor do crédito, não a base — a comparação vira
> "SPED R$ 0,00 vs base calculada R$ X" em todo item. O frete já foi migrado para
> confronto de valor (seção 8.4); os produtos **não**, porque mudar isso altera o
> resultado de toda auditoria existente. Decisão pendente.

### 8.3 Diagnóstico da diferença

`diagnose_val_diff()` compara a diferença encontrada com os componentes do item e,
quando bate (± R$ 0,05), nomeia a causa provável: desconto, frete, IPI, ICMS
(exclusão STF), ICMS ST, seguro/despesas acessórias, ou seis combinações de dois
componentes. Não bateu com nada → texto vazio, sem chute.

### 8.4 Frete (CT-e) — aproveitamento

Desde 26/08/2026 o PIS/COFINS também é conferido no frete. Antes o motor só lia o
C170 (produtos) e o CT-e ficava de fora — crédito legítimo que ninguém conferia.

**A base é calculada, não copiada — e aqui não há alternativa:** o CT-e modelo 57 não
tem grupo de PIS/COFINS no XML, e o D100/D190 do SPED só traz ICMS.

```
base creditável do frete = valor contábil do CT-e − ICMS destacado
```

O valor contábil é o `VL_OPR` somado do analítico D190; sem D190, o `VL_DOC` do
cabeçalho. Campo próprio no parser (`vl_contabil`), separado de `vl_doc` — ali o
fallback prefere `VL_SERV`, que é só a prestação. A base nunca é negativa. Decisão do
Sandro em 26/08/2026, mesmo critério dos produtos (exclusão do ICMS — tese do STF).

**O confronto é de VALOR, não de base.** O SPED Fiscal não escritura base de cálculo de
PIS/COFINS — só o valor do crédito. Então a base calculada vira valor pelas alíquotas do
regime não-cumulativo e é isso que se compara com o que o cliente lançou:

```
PIS devido    = base creditável × 1,65%
COFINS devida = base creditável × 7,60%
```

A base continua na aba, como memória da conta. As alíquotas são
`ALIQ_PIS`/`ALIQ_COFINS`, fixas no código — **no regime cumulativo (lucro presumido)
seriam 0,65% e 3%**, e no dia em que entrar cliente nesse regime elas têm de virar
cadastro por empresa, não um `if`. É o mesmo tipo de generalização que já mordeu no
`UF_EMPRESA = "ES"`.

**O que o cliente escriturou vem dos registros D101 (PIS) e D105 (COFINS)**, filhos do
D100, no layout da **EFD Contribuições**:
`|REG|IND_NAT_FRT|VL_ITEM|CST|NAT_BC_CRED|VL_BC|ALIQ|VL|COD_CTA|`. Registros repetidos
somam, pelo mesmo motivo do D190.

> **Cuidado: `D101` existe nos dois arquivos e são registros diferentes.** No SPED Fiscal
> (EFD ICMS/IPI) o D101 é o complemento de DIFAL da EC 87/15 —
> `|REG|VL_FCP_UF_DEST|VL_ICMS_UF_DEST|VL_ICMS_UF_REM|COD_CTA|`. Lido como se fosse PIS,
> o ICMS do remetente vira "CST" e o documento sai marcado como escriturado, fazendo o
> motor cobrar crédito de um arquivo que não tem o dado. Por isso a linha só é aceita
> quando tem a forma do registro de Contribuições: nove campos e um CST de dois dígitos
> na posição 4.

A verificação é **uma por documento**, não por CFOP: o D101/D105 é do documento inteiro.
CT-e com mais de um CFOP no D190 sai com observação dizendo isso.

| Situação | Resultado |
|---|---|
| CST sem crédito (ou regra `False`) e o SPED creditou | `pis_cofins_issues` — "crédito indevido" |
| elegível, SPED sem crédito e há crédito calculado | `pis_cofins_issues` — "PIS/COFINS do frete não aproveitado", com a memória da conta |
| elegível e valores diferentes (> R$ 0,05 em PIS ou COFINS) | `pis_cofins_issues` — "valor divergente" |
| CST presente sem regra `(CFOP, CST)` | pendência em `unmapped_pis_cofins_rules`, com o CST real |

**A trava que sustenta tudo isso:** o arquivo só é confrontado se trouxer **algum**
D101/D105 (`bloco_d_tem_pis_cofins`). O SPED Fiscal (EFD ICMS/IPI) não tem esses
registros — tratar a ausência como "não aproveitado" acusaria todo CT-e do mês por
causa do layout do arquivo, não de erro do cliente.

**A aba "PIS/COFINS sobre Frete" sai sempre**, com o crédito calculado de cada CT-e
(contábil, ICMS, base creditável, PIS e COFINS calculados, o que o SPED creditou e a
situação). É informativa —
não entra na contagem de inconsistências; as divergências reais é que vão para
`pis_cofins_issues`, com `tipo = "PIS/COFINS Frete"`. Quando o arquivo não escritura, a
aba avisa em destaque, para o número não ser lido como divergência.

---

## 9. Antecipação de ICMS

Dois mecanismos distintos, com fontes diferentes:

### 9.1 Cálculo do imposto devido (`icms_anticipation_rules`)

**Chave: (UF do emitente, UF do destinatário, CFOP do SPED).** As UFs saem do XML; a
regra traz os percentuais.

```
base = vl_item
base = base × (1 − redução_bc/100)        se redução > 0
base = base × (1 + MVA/100)               se MVA > 0
devido = base × (alíq_interna/100) − base × (alíq_interestadual/100)
```

`devido > R$ 0,01` → linha em `anticipation_results`.

Sem `uf_destino` na chave, uma regra PR→SP casava com nota do PR para qualquer estado,
e duas regras da mesma origem se sobrescreviam.

> **Observação para revisão:** esta é a única consulta de crédito/imposto que ainda usa
> `sped_cfop` em vez de `cfop_regra`. Consciente ou não, é uma divergência em relação à
> regra da seção 3.

### 9.2 Sinalização por NCM (lista em código)

`NCMS_ANTECIPACAO_MAP` (`domain/constants/ncms_antecipacao.py`, ~160 NCMs de autopeças
com a categoria da Portaria).

Condição para o item entrar em `antecipacao_icms`: **`mapped_cfop == "2102"`** (o
mapeado, exatamente — sem regra de classificação a linha nunca aparece) **e** o NCM do
item casar com a lista.

O casamento é por **prefixo decrescente**: NCM limpo completo, depois 8, 7, 6, 5 e 4
dígitos. Isso cobre entradas da lista escritas em posição (`3917`, `7320`) e em item
completo (`38151210`).

---

## 10. ICMS ST calculado (Portaria 16 — `cest_mva_rules`)

Aba de **cálculo do ST devido**, para mercadoria de revenda que veio sem ST destacado.

**Filtro de entrada:**

1. CFOP do SPED ∈ `{1102, 2102, 1403, 2403}` (revenda);
2. `vl_icms_st` do item **= 0** — nota que já veio com ST no campo próprio nunca entra;
3. fornecedor do **ES** com CST `60`/`060`/`500`/`0500`/`10500`/`5000` → fora;
4. o CEST do item tem regra na Portaria 16.

**Alíquota efetiva do ICMS da nota:**

| Emitente | Alíquota usada | ICMS considerado |
|---|---|---|
| Simples Nacional (CST de 3 dígitos ou `vl_icms_sn > 0`), ES | 17% | `valor × 17%` |
| Simples Nacional, RS/SC/PR/SP/RJ/MG | 7% | `valor × 7%` |
| Simples Nacional, demais UFs | 12% | `valor × 12%` |
| Regime normal | `p_icms` do item | ICMS destacado |

**MVA por faixa de alíquota** (`get_mva_for_aliquota`): ≤ 4,5% → MVA Aj. 4%;
≤ 7,5% → Aj. 7%; ≤ 13% → Aj. 12%; acima → MVA Original.

```
BC ST   = valor do item × (1 + MVA)
ICMS ST = máx(0, BC ST × 17% − ICMS da nota)
```

Os 17% da ponta são a alíquota interna do ES, fixa no código.

**Carga da tabela:** `cest_mva_rules` é lida com cache de processo; se a tabela estiver
vazia, o serviço semeia a partir de `Portaria 16.xlsx` na raiz do projeto. MVA acima de
15 é tratado como percentual e dividido por 100. O CEST é indexado com e sem pontuação.

---

## 11. DIFAL (informativo)

Sem regra cadastrada — CFOP fixo no código, olhando **SPED ou mapeado**:

| Aba | CFOPs |
|---|---|
| `difal_interno` | `1556`, `1551`, `1910` |
| `difal_interestadual` | `2556`, `2910`, `2551`, `2949` |

São **informativos**: não contam como inconsistência no resumo. O interno traz o
percentual efetivo (`ICMS ÷ total do item`) para ver redução de base; o interestadual
traz `vICMSUFDest` / `vICMSUFRemet` do XML.

---

## 12. Regras estruturais do cruzamento (não são tabela)

### 12.1 Casamento SPED × XML — `casar_itens_da_nota`

O `COD_ITEM` do C170 é o código **interno da empresa**; o `cProd` do XML é o do
**fornecedor**. Quase nunca batem. O motor casava por posição quando o código falhava;
numa nota real (58897) o SPED estava deslocado uma linha e **todo** item foi comparado
com o vizinho.

Ordem de tentativa, do sinal mais forte para o mais fraco:

1. **código do produto** igual;
2. **valor unitário + quantidade** (± R$ 0,01 / ± 0,0001);
3. **valor unitário** apenas;
4. **posição**, e só se o unitário ou a quantidade daquela posição confirmarem.

Duas passadas: a primeira **consome** a linha do XML (itens gêmeos de códigos
diferentes vão para linhas distintas); a segunda **reaproveita** linha já usada (o SPED
fracionou em duas o que o fornecedor emitiu em uma).

**Linha que não casou com confiança fica de fora do resultado e é pulada.** Alegar
divergência sem saber a que item ela corresponde é pior do que não alegar.

### 12.2 Totais da nota

- **C100 × C190:** soma do analítico contra o total do documento, dentro do próprio
  SPED (> R$ 0,05 → `value_discrepancies`);
- **SPED × XML:** usa a soma do C190 como referência quando ela existe (> R$ 0,01),
  senão o C100.

### 12.3 XML faltante e malha fina

| Situação | Saída |
|---|---|
| documento no SPED sem XML na pasta | `missing_xmls` / `xmls_faltantes` |
| XML na pasta sem documento no SPED | `unrecorded_invoices` **+** `MissingInvoiceLog` com status `pendente_proximo_mes` |

O `MissingInvoiceLog` é o **único** registro que o motor grava. XML cujo emitente é a
própria empresa é ignorado quando "ignorar saídas" está ligado.

O casamento XML ↔ SPED é pela **chave de acesso**: nome do arquivo sem `.xml` e também
qualquer sequência de 44 dígitos encontrada no nome.

### 12.4 Notas duplicadas

Detectadas no parser do SPED (mesmo documento repetido) e listadas em
`notas_duplicadas`.

### 12.5 ICMS Monofásico (combustíveis)

Opção da tela inicial: **"Considerar ICMS Mono"**. Ligada, o crédito soma as **quatro**
tags: `vICMSMono` (próprio), `vICMSMonoReten` (retido para a etapa seguinte),
`vICMSMonoRet` (cobrado anteriormente) e `vICMSMonoDif` (diferido).

Decisão do escritório em 21/08/2026 — **não** é o tratamento default da legislação para
todas elas. A aba "ICMS Mono" mostra as quatro em colunas separadas justamente para
permitir revisar a decisão, e lista o item **sempre que houver valor monofásico**,
marcado ou não; a coluna `considerado` diz se aquela auditoria de fato somou.

`vICMSMonoRet` e `vICMSMonoReten` são tags distintas, de CSTs distintos.

### 12.6 Contagem do resumo

`inconsistencias` soma apenas listas de erro real; itens com `status` em
`informativo/ok/correto/info` ou `is_error = False` são descartados. DIFAL fica de fora
e é contado em `informativos`.

### 12.7 Modo "somente SPED"

Sem XML, o motor espelha o próprio SPED como se fosse o XML. Nesse modo o agrupamento
de itens idênticos não roda (não há segundo lado para somar) e a comparação de ICMS
volta a ser item a item.

---

## 13. NFS-e em PDF — CFOP 1933 × 2933

Módulo separado (`services/nfse/`), sem tabela de regras.

```
prestador e tomador na MESMA UF  → 1933 (interna)
UFs diferentes                   → 2933 (interestadual)
```

**As duas UFs saem da própria nota**, lidas do PDF pela IA (`uf_prestador`,
`uf_tomador`). Não consulta banco, não exige empresa cadastrada.

Duas versões anteriores erraram aqui e não devem voltar:

1. `UF_EMPRESA = "ES"` fixo no código — entrou cliente do RJ e toda nota RJ×RJ saiu
   2933; no PDF real de abril, 19 das 59 notas mudavam de CFOP por isso;
2. UF vinda do `Tenant.uf` — cálculo certo, mas exigia cadastro, e as 170 empresas
   estavam sem UF.

**Faltando alguma UF:** CFOP **presumido** 1933, `presumido=True`. Nada bloqueia, a nota
entra no TXT, mas sai marcada na tela e na coluna "Observação" da planilha. A marca não
é decorativa — foi presunção silenciosa que fez o bug 1 durar meses.

Outras regras do módulo:

- **CNPJ com dígito verificador conferido** (`cnpj_valido`): o PDF é digitalização, um 8
  lido como 3 passa em conferência visual e lança a nota no fornecedor errado;
- **cadastro `BASE_CNPJ`** completa cidade, endereço e código de serviço (padrão `176`).
  É **opcional**: fornecedor fora do cadastro é lançado com o que veio da nota;
- **`total_retencoes`** soma IR + PIS + COFINS + **CSLL** — a planilha antiga esquecia a
  CSLL e o total não batia com o do TXT;
- a auditoria manda o **PDF cru** para a IA (`claude-sonnet-5`, `effort: low`, saída
  travada por JSON Schema). Extrator de texto achata a página e perde qual número está
  em qual coluna, que é exatamente o que a auditoria precisa enxergar. Resposta truncada
  levanta erro em vez de devolver lista parcial.

---

## 14. Ciclo de vida de uma regra

```
auditoria roda
   └─ não achou regra → pendência em results (só memória)
        └─ humano confirma na Curadoria  → INSERT/UPDATE com status por papel
             ├─ admin    → 'aprovado'          → vale na próxima auditoria
             └─ operador → 'pendente_revisao'  → admin aprova na tela de regras
```

### 14.1 Curadoria — gravação unitária (tela da auditoria)

| Rota | Grava em | Chave montada |
|---|---|---|
| `POST /api/mapping/cfop` | `cfop_classifications` | empresa + fornecedor + CFOP + produto |
| `POST /api/mapping/cte_cfop` | `cte_cfop_classifications` | empresa + CFOP + UF |
| `POST /api/mapping/icms_rule` | `cfop_credit_rules` | CFOP |
| `POST /api/mapping/ipi_rule` | `ipi_credit_rules` | CFOP + CST |
| `POST /api/mapping/pis_cofins_rule` | `pis_cofins_credit_rules` | CFOP + CST |

Todas fazem **upsert** (atualiza se a chave já existe), regravam `created_by` e
**definem o status conforme o papel de quem gravou**.

### 14.2 Curadoria — lote

| Rota | O que faz |
|---|---|
| `POST /api/mapping/cfop/bulk` | aprova todas as sugestões da auditoria; filtro opcional por CNPJ (substring, sem pontuação — espelha a busca da tela); pula item sem `product_code` |
| `POST /api/mapping/cte_cfop/bulk` | idem para CT-e |
| `POST /api/mapping/cfop/bulk_by_cfop` | aplica um CFOP de entrada a **todos os produtos** de um CFOP de origem |

**Resposta de lote nunca é verde com zero.** Se nada casou, `_aviso_lote()` devolve
caixa âmbar dizendo o que não casou — já houve o bug de "Sucesso! 0 regras aplicadas".

### 14.3 Seletor de CFOP de entrada

`_opcoes_cfop_entrada()` monta três grupos, nesta ordem: **sugeridos** (o que o motor
propôs nesta auditoria), **recorrentes** (histórico aprovado desta empresa, os 20 mais
usados, por frequência real) e **padrão** (`CFOPS_ENTRADA`, tabela do Convênio S/Nº de
1970 usada só para rotular — nada dali é aplicado automaticamente).

A mesma tabela alimenta o seletor da aba de CT-e. Em 26/08/2026 entraram:
`1910/2910` (bonificação, doação ou brinde), `1911/2911` (amostra grátis), `2916`
(retorno de conserto ou reparo — o `1916` já existia), `1920/2920` (vasilhame ou
sacaria), `1922/2922` (simples faturamento para recebimento futuro) e, para frete,
`1932/2932` (transporte iniciado em UF diversa da inscrição do prestador).

### 14.4 Administração (`/admin/rules/...`)

| Rota | Efeito |
|---|---|
| `POST /rules/{tipo}/add` | cadastro manual, grava direto como `aprovado` |
| `PATCH /rules/{tipo}/toggle` | inverte `allow_credit` (ICMS, IPI, PIS/COFINS) |
| `POST /rules/{tipo}/approve` | `pendente_revisao` → `aprovado` |
| `POST /rules/{tipo}/batch-approve` | aprovação em lote; `rule_keys` = campos da chave separados por barra vertical, começando pelo tenant |
| `DELETE /rules/{tipo}/delete` | exclui uma regra |
| `GET /modals/cfop-siblings` + `POST /rules/cfop/batch-delete` | lista e exclui as regras "irmãs" (mesmo fornecedor + CFOP de origem), produto a produto |
| `POST /tenants/{id}/import-rules` | copia regras de outra empresa, por tipo, **sem sobrescrever** chave já existente no destino |

Toda rota valida papel e tenant (`authz.py`); operador só opera empresas de
`user_tenants`.

> A importação entre empresas **preserva o `status` da origem** — regra
> `pendente_revisao` chega pendente no destino, e continua sem efeito até ser aprovada.

---

## 15. Invariantes que não podem ser quebradas

1. **Nunca gravar `"*"` ou `"TODOS"` em campo de chave.** A busca é exata; a regra
   nasceria morta ou viraria curinga silencioso.
2. **`None` não é `False`.** Sem regra, o motor não alega nada — nem glosa, nem compara.
3. **Não reintroduzir lista fixa de CFOP sem crédito** no código.
4. **Não reintroduzir regra global** nem voltar a ler `is_global`.
5. **Crédito sai do CFOP de referência**, nunca do escriturado.
6. `agrupar_icms_por_item_identico` precisa receber `cfop_por_linha` com o **mesmo**
   critério do laço principal.
7. **Casamento por posição só com conferência**; linha sem sinal confiável fica de fora.
8. **O motor não persiste heurística** — só o `MissingInvoiceLog`.
9. **Lote com zero resultado não responde verde.**
10. **CFOP presumido sempre aparece marcado** na tela e na planilha.
11. **Ausência de dado no layout não é erro do cliente.** Sem nenhum D101/D105, o motor
    calcula e mostra o crédito de frete, mas não acusa ninguém.

Barreira de feedback antes de dizer que terminou:

```bash
venv/Scripts/python.exe tests/run_all.py
```

Suítes que guardam especificamente estas regras: `test_cfop_matching.py`,
`test_sem_regras_globais.py`, `test_cfop_mapeado_decide_credito.py`,
`test_icms_credito_itens_identicos.py`, `test_icms_mono.py`,
`test_mapeamento_lote.py`, `test_no_autosave.py`, `test_pdf_ia_lancamento.py`,
`test_sped_d190.py`, `test_pis_cofins_cte.py`.

---

## 16. Pontos que este levantamento encontrou e que merecem decisão

Nenhum foi alterado — são observações do código como está hoje (26/08/2026):

1. **Antecipação usa `sped_cfop`**, não o CFOP de referência (`cross_checker.py`, bloco
   "Sprint 4"). Todos os outros impostos migraram na decisão de 26/08/2026.
2. **`POST /admin/rules/pis_cofins/add` não valida CST vazio**, ao contrário do
   equivalente de IPI — dá para gravar regra com `cst_expected = ""`, que o motor nunca
   acha.
3. **`POST /admin/rules/cfop/approve` filtra por `id=cfop`**, mas `CfopClassification`
   não tem coluna `id` — a chamada estoura e cai no `except`, devolvendo 500. O caminho
   que funciona é o `batch-approve`.
4. **`POST /api/mapping/pis_cofins_rule` usa `cst_expected: str = Form(...)`**, que
   rejeita string vazia com 422 antes de entrar na função; como o htmx não troca o alvo
   em 4xx, o usuário clica e nada acontece. O IPI já foi corrigido para `Form("")` com
   validação interna.
5. **`is_global` continua nas sete tabelas** sem ser lido por nenhuma consulta. Coluna
   morta que ainda sugere um comportamento que não existe mais.
6. **Os produtos ainda confrontam BASE de PIS/COFINS** (`VL_BC_PIS`/`VL_BC_COFINS` do
   C170), enquanto o frete passou a confrontar valor. Se esses campos vierem vazios no
   SPED Fiscal, todo item vira apontamento. Ver a nota na seção 8.2.
7. **Alíquotas de PIS/COFINS fixas no código** (1,65% / 7,6%, não-cumulativo). Cliente do
   lucro presumido precisa de 0,65% / 3% — hoje não há onde cadastrar.
