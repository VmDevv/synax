# PIS/COFINS na auditoria do EFD Contribuições

Referência completa de **como o motor confere PIS e COFINS** quando a auditoria roda
sobre o arquivo do **EFD Contribuições**, ligado pela caixa "SPED Contribuições
(PIS/COFINS)" da tela de auditoria.

Complementa a seção 8 de [`regras-do-sistema.md`](regras-do-sistema.md), que descreve
as mesmas regras no arquivo do **EFD ICMS/IPI**. As regras de crédito são as MESMAS
nos dois leiautes — o que muda é a qualidade do lado do SPED, e é disso que trata
este documento.

Escopo no código:

| O quê | Onde |
|---|---|
| Base do XML (tese do STF) | `audit_engine/xml_parser.py` |
| Leitura do arquivo | `audit_engine/sped_contribuicoes.py` e `sped_parser.py` |
| Confronto e divergências | `audit_engine/cross_checker.py` |
| Propostas de correção | `sped_rewriter/ponte.py` |
| Gravação no arquivo | `sped_rewriter/cascata.py` |

Todos os números deste documento foram medidos no arquivo real da MEGA, competência
agosto/2026, em 18/09/2026.

---

## 1. O princípio: a base é CALCULADA, nunca copiada

O confronto de PIS/COFINS não compara o valor do imposto. Compara a **base de
cálculo** — a do XML, recalculada pelo motor, contra a escriturada no SPED.

A base do XML é recalculada porque é exatamente a exclusão do ICMS que se quer
auditar. As tags `vBC` de PIS e de COFINS que o fornecedor escreveu no XML **não são
usadas** no confronto: elas são a base dele, não a do comprador.

```
bruto = vProd + vFrete + vSeg + vOutro + vIPI + vIPIDevol + vICMSST + vFCPST − vDesc
base  = bruto − vICMS(normal + Simples) − vICMSST − vFCPST − vIPI − vIPIDevol
base  = max(0, base)
```

**As quatro parcelas do meio se cancelam.** IPI, IPI devolvido, ICMS ST e FCP ST
entram no bruto e saem na linha seguinte. A fórmula que sobra é:

```
base = vProd + vFrete + vSeg + vOutro − vDesc − vICMS
```

Elas continuam escritas em duas linhas de propósito, porque é assim que a tese é
enunciada: primeiro o valor bruto da operação, depois o que a lei manda excluir. Quem
for mexer precisa ver as duas contas, não só o resultado.

As duas bases do XML — PIS e COFINS — são **idênticas por construção**. O parser
calcula uma vez e grava nos dois campos. Por isso a auditoria emite **uma única
linha** de divergência para o par, e não duas.

A base nunca é negativa. Nota com ICMS maior que o valor do item é erro de emissão, e
uma base negativa viraria crédito com sinal trocado no relatório.

---

## 2. O que muda no leiaute de Contribuições

Nada nas regras. Tudo na qualidade do lado do SPED.

O confronto é base contra base desde 28/08/2026, decisão do Sandro que reverteu o
confronto por valor de um dia antes. No arquivo do **EFD ICMS/IPI** essa decisão tem
um efeito conhecido e aceito: o campo `VL_BC_PIS` do C170 quase nunca vem preenchido,
porque PIS/COFINS é escriturado no OUTRO arquivo. A linha sai "SPED R$ 0,00 vs Base
Calculada R$ 820,00" em quase todo item. **Não é defeito do cálculo: é o campo de
origem que vem vazio.**

O arquivo do EFD Contribuições fecha esse buraco.

| Preenchimento de `VL_BC_PIS` no C170 | Medido |
|---|---|
| EFD Contribuições da MEGA | 11317 de 11320 itens |
| EFD ICMS/IPI da mesma empresa, mesmo mês | 737 de 1135 itens |
| EFD ICMS/IPI de outro cliente | 0 de 1301 itens |

Lendo o arquivo certo, o lado do SPED passa a existir de verdade, e a divergência que
sai é diferença real de base, não ausência de campo.

Duas ausências do leiaute que afetam PIS/COFINS:

- **Não existe C190.** O somatório do analítico fica em zero, o que já faz o motor
  pular o confronto interno do documento sozinho. Nada a fazer.
- **Não existe D190.** Isso tem consequência no frete, e está na seção 9.

---

## 3. Mercadoria (C170) — elegibilidade

Antes de comparar base, o motor decide se o item **pode** tomar crédito. O item é
considerado **inelegível** se qualquer uma destas for verdadeira:

1. o CST de PIS do SPED está na lista fixa de CSTs que, por definição, não geram
   crédito;
2. a regra cadastrada de PIS devolveu `False`;
3. a regra cadastrada de COFINS devolveu `False`.

A lista fixa, em `CSTS_PIS_COFINS_SEM_CREDITO`:

```
07  08  09  49  70  71  72  73  74  75  98  99
```

São os CSTs que o próprio código já declara sem crédito — suspensão, sem incidência,
diferimento, outras operações, aquisições sem direito. Independem de regra cadastrada.

**Inelegível e o SPED creditou acima de R$ 0,01** gera uma linha de divergência:
crédito indevido, com o valor que foi tomado. **Inelegível e zerado** está correto, e
nada sai.

> **PIS/COFINS é a exceção da casa: aqui `None` não bloqueia.** Em ICMS e em IPI, CFOP
> sem regra cadastrada impede a comparação — o motor não sabe e não alega. Em
> PIS/COFINS, item sem regra mas com CST de crédito **segue elegível** e a base é
> conferida. A razão é que o CST de PIS/COFINS é informação própria, e não depende do
> CFOP: um CFOP de ICMS como 1556 ou 1407 não deve bloquear PIS/COFINS quando o CST
> diz que há crédito.

### Esta pergunta não depende do XML (18/09/2026)

As três condições acima saem inteiras de dentro do SPED: o CST está no C170 e as
regras estão no banco. **O XML não entra nesta conta.** Mesmo assim, até 18/09/2026
a pergunta só era feita dentro do ramo que exige o XML lido e o item casado — a nota
sem XML saía da apuração inteira, ia para "XMLs Faltantes" e a regra nunca era
consultada.

O custo disso foi medido na MEGA de agosto/2026: **132 itens em 16 notas, R$ 2.026,78**
de crédito de PIS/COFINS que a regra manda glosar, todos em CFOP 1152 com CST 50,
sem nenhuma linha de divergência. E não foi azar — essas 16 notas são
**transferências entre filiais**, que nunca têm XML no pacote de entradas, porque a
nota é emitida pela própria empresa. O caso mais comum era justamente o que caía no
buraco.

Hoje a decisão vive em `sem_direito_a_credito()`, uma função só, chamada nos dois
lugares — no laço normal e na nota sem XML — para as duas respostas não poderem
divergir. Duas diferenças na nota sem XML, e nenhuma é arbitrária:

| | Nota com XML | Nota sem XML |
|---|---|---|
| Crédito indevido (glosa) | conferido | **conferido** |
| Confronto de BASE | conferido | não roda — a base calculada vem do XML |
| CFOP de referência | mapeado, se houver regra | o escriturado |

Sem XML não há CFOP do fornecedor, logo não há regra de classificação a consultar: o
CFOP de referência é o escriturado, que é o mesmo que `cfop_de_referencia` devolve
quando não existe mapeamento. A divergência sai marcada com `sem_xml: True` e o
motivo diz isso em português, porque quem lê a tela precisa saber que aquela linha
foi conferida só pelo SPED. A nota **continua** na lista de XMLs Faltantes: apontar a
glosa não a torna conferida, são duas informações diferentes.

---

## 4. Mercadoria — o confronto

Item elegível tem a base comparada:

```
|base do SPED (VL_BC_PIS do C170) − base calculada do XML| > R$ 0,05  →  divergência
```

A linha que sai traz os dois números, o CST, o CFOP de referência e o diagnóstico da
diferença. Ela é marcada com `comparacao: "base"`, e essa marca não é decorativa:

> **Os dois valores dessa linha são BASE DE CÁLCULO, não crédito.** Quem somar a lista
> para dizer "quanto há a aproveitar" estará somando base como se fosse imposto. Isso
> aconteceu na primeira tentativa e deu R$ 111 mil de crédito inventado. As outras
> linhas da mesma lista — as de crédito indevido — **são** crédito. A marca é o que
> separa as duas.

### O diagnóstico da diferença

`diagnose_val_diff()` compara a diferença encontrada com cada componente do item e,
quando bate dentro de R$ 0,05, nomeia a causa provável:

| A diferença é igual a | O que a linha sugere |
|---|---|
| desconto do item | desconto não deduzido no SPED |
| frete do item | frete somado indevidamente |
| IPI | IPI não excluído |
| ICMS destacado | **a exclusão do ICMS da base, tese do STF** |
| ICMS ST | ST não excluído |
| seguro e outras despesas | despesas acessórias |

Há ainda seis combinações de dois componentes. Não bateu com nada, o texto sai vazio —
sem chute.

---

## 5. Frete (CT-e) — a base

O frete tem caminho próprio, e ele **não mudou** com o leiaute novo.

No CT-e não há de onde copiar a base: o modelo 57 não tem grupo de PIS/COFINS no XML,
e o D100 do SPED só traz ICMS. A base é calculada com o mesmo critério dos produtos,
a exclusão do ICMS:

```
base creditável do frete = valor contábil do CT-e − ICMS destacado     (mínimo 0)
```

O valor contábil é o `VL_OPR` somado do analítico. **No EFD Contribuições não há
analítico**, então vale o valor do documento do cabeçalho. O campo é próprio no parser
(`vl_contabil`), separado do valor do documento, porque ali a preferência é pelo valor
da prestação, que não inclui as demais parcelas.

---

## 6. Frete — o confronto é de VALOR

Aqui a regra é diferente da mercadoria, e de propósito. O que o cliente escriturou de
PIS/COFINS de frete vem dos registros **D101 (PIS)** e **D105 (COFINS)**, e eles
trazem base, alíquota e valor. A base calculada vira valor pelas alíquotas do regime
não-cumulativo, e é o valor que se compara:

```
PIS devido    = base creditável × 1,65 %
COFINS devida = base creditável × 7,60 %
```

> **As alíquotas estão fixas no código e valem para TODA empresa.** No regime
> cumulativo, o lucro presumido, seriam 0,65 % e 3 %. No dia em que entrar cliente
> nesse regime, elas têm de virar cadastro por empresa, não um `if` no motor. É o
> mesmo tipo de generalização que já mordeu no `UF_EMPRESA` fixo em "ES".

As quatro situações que o frete produz:

| Situação | Resultado |
|---|---|
| CST sem crédito, ou regra `False`, e o SPED creditou | crédito indevido |
| elegível, SPED zerado e há crédito calculado | "não aproveitado", com a memória da conta |
| elegível e diferença acima de R$ 0,05 em PIS ou em COFINS | valor divergente |
| CST presente sem regra cadastrada | pendência, com o CST real |

**A trava que sustenta tudo isso:** a alegação só acontece se o arquivo trouxer
**algum** D101 ou D105. Arquivo que não escritura PIS/COFINS de frete não errou, só
não tem o dado — e tratar a ausência como "não aproveitado" acusaria todo CT-e do mês
por causa do leiaute, não de erro do cliente.

> **Cuidado com o D101: ele existe nos dois arquivos e são registros diferentes.** No
> EFD ICMS/IPI o D101 é o complemento de DIFAL da Emenda 87. Lido como se fosse PIS, o
> ICMS do remetente vira "CST" e o documento sai marcado como escriturado, fazendo o
> motor cobrar crédito de um arquivo que não tem o dado. Por isso a linha só é aceita
> quando tem a forma do registro de Contribuições: nove campos e um CST de dois
> dígitos na posição 4.

A conferência é **uma por documento**, não por CFOP: o D101/D105 é do documento
inteiro. CT-e com mais de um CFOP no analítico sai com observação dizendo isso.

### O que foi medido

Nos seis CT-e do arquivo real, a conta fecha **exatamente**, centavo a centavo:

| Base calculada | PIS calculado | PIS no SPED | COFINS calculada | COFINS no SPED |
|---|---|---|---|---|
| 154,62 | 2,55 | 2,55 | 11,75 | 11,75 |
| 326,98 | 5,40 | 5,40 | 24,85 | 24,85 |
| 422,28 | 6,97 | 6,97 | 32,09 | 32,09 |
| 4.561,08 | 75,26 | 75,26 | 346,64 | 346,64 |
| 5.089,18 | 83,97 | 83,97 | 386,78 | 386,78 |
| 7.741,59 | 127,74 | 127,74 | 588,36 | 588,36 |

Seis de seis sem divergência. É a validação de que a fórmula do frete reproduz o que o
escritório de fato escritura.

---

## 7. As regras cadastradas

**Chave: (CFOP de referência, CST).** PIS e COFINS consultam a **mesma tabela**, cada
um com o seu CST do SPED. A chave é exata, sem curinga, e a regra é do tenant — como
toda regra do sistema.

**O CFOP de referência é o MAPEADO, não o escriturado.** O CFOP que o cliente lançou é
justamente o que está sob auditoria, então não pode ser também o critério dela. Quando
existe regra de classificação aprovada para a operação, é o CFOP dela que decide; só
na falta dela vale o do SPED. Quando os dois diferem, a mensagem da divergência diz de
onde saiu o número, para ninguém ler "CFOP 1653 permite crédito" numa nota lançada
como 1556 e não entender.

CST presente sem regra cadastrada vira **pendência** na aba de regras não mapeadas,
com o CST real — nunca com curinga.

---

## 8. A correção de PIS/COFINS

Implementada em 17/09/2026, com a condição explícita de não encostar na correção do
SPED Fiscal. São cascatas separadas: `aplicar` para o Fiscal e `aplicar_contribuicoes`
para este leiaute. **Misturá-las corrompe o arquivo** — rodar a do Fiscal aqui injeta
registros C190 que este leiaute não tem.

A tela oferece uma opção só, "Crédito de PIS/COFINS — mercadoria", e ela grava os
**seis campos juntos** no item, porque o Guia manda informar base, alíquota e valor em
conjunto:

```
VL_BC_PIS      ALIQ_PIS      VL_PIS
VL_BC_COFINS   ALIQ_COFINS   VL_COFINS
```

**O valor é DERIVADO da base pela alíquota do próprio documento**, nunca copiado do
XML. O valor que o fornecedor escreveu foi calculado sobre a base dele; o que se
escritura aqui é o crédito do comprador sobre a base da tese do STF:

```
VL_PIS = base calculada × alíquota do XML ÷ 100
```

Depois, o documento totaliza: os campos 26 e 27 do C100 passam a ser a soma do
PIS e do COFINS dos itens.

Na **glosa** basta zerar o valor. Base e alíquota vão a zero junto, pela cascata, que é
o que o Guia manda quando não há direito à apropriação — e desde 18/09/2026 o **CST
vai a 70** junto (ver abaixo).

### 8.1 A glosa vale nos DOIS leiautes (18/09/2026)

Pedido do Sandro. Os campos 25 a 36 do C170 — CST, base, alíquota e valor de cada
tributo — são os **mesmos nos dois leiautes**, e glosar neles não encosta em nada que
só exista aqui: não entra na chave do C190, não mexe no Bloco E. Por isso a opção
nasceu marcada como `LEIAUTE_AMBOS` em vez de virar um `if` dentro da cascata do
Fiscal — as duas cascatas continuam separadas, e é a mesma função que as duas chamam.

O que **não** atravessa é a correção de BASE, e a razão é a mesma decisão de
28/08/2026 que este documento descreve na seção 4: no EFD ICMS/IPI o `VL_BC_PIS` do
C170 quase nunca vem preenchido, então quase todo item gera a linha "SPED R$ 0,00 vs
Base Calculada R$ X". Transformar essas linhas em propostas de reescrita mandaria
gravar, aos milhares, base de um tributo que aquele arquivo não escritura. No
Fiscal a tela recebe **só as glosas**.

Uma consequência prática: o total do C100 (campos 26 e 27) só é refeito quando a
glosa mexeu em algum item **daquele documento**. Refazer sempre zeraria o VL_PIS de
uma nota corrigida só no ICMS, porque no Fiscal esses campos do C170 costumam vir
vazios e a soma de vazio é zero.

Para tudo isso funcionar, a auditoria passou a gravar na divergência a alíquota do XML
e a marca de glosa. **Auditoria rodada antes de 17/09/2026 não tem esses campos** e a
proposta sai impedida, com o motivo na tela, pedindo para rodar a auditoria de novo. É
a mesma lacuna que o ICMS teve até 09/09/2026.

---

## 9. O que o motor NÃO faz, e por quê

Nenhum destes é esquecimento.

- **O Bloco M não é tocado.** A apuração de PIS/COFINS deste arquivo é o Bloco M, com
  os créditos detalhados e a contribuição consolidada, mais o espelho da COFINS. Mexer
  ali muda tributo a recolher, e é a mesma classe de decisão que o E110 exigiu no
  Fiscal, onde virou uma caixa própria e um segundo aceite. A caixa da apuração **some
  da tela** neste leiaute, porque não há E110 para receber.
- ~~**O CST não é trocado na glosa.**~~ **Mudou em 18/09/2026.** Era a fronteira mais
  defensável das três: os códigos de entrada sem direito a crédito são vários — 70 a
  75 e 98 — e escolher entre eles é decisão fiscal, não aritmética, então o motor
  zerava base, alíquota e valor e avisava que o CST ficara como estava. O avesso
  disso, porém, é um arquivo que se contradiz na mesma linha: imposto zero com CST 50
  dizendo que há direito a crédito. Quem recebe a contradição é o PVA, na importação.
  O Sandro decidiu: **CST 70** — "Operação de Aquisição sem Direito a Crédito", Tabela
  4.3.3 —, gravado nos campos 25 e 31 do C170, nos dois leiautes, com XML ou sem. O
  aviso continua existindo, agora dizendo o que foi gravado e lembrando que 71 a 75 e
  98 seguem possíveis conforme a natureza da operação; conferir isso no PVA continua
  sendo trabalho de quem entrega.
- **O PIS/COFINS do frete não é reescrito.** Ele vive nos D101 e D105, que a correção
  ainda não toca. A proposta aparece **impedida com o motivo**, nunca some da lista.

---

## 10. Lacunas conhecidas

Duas, ambas medidas, ambas em aberto.

### 10.1 O frete fica sem CFOP neste leiaute

O CFOP de referência do frete é colhido dentro do laço do analítico D190. **O EFD
Contribuições não tem D190**, o laço não roda, e o CFOP fica vazio. Consequências:

- a consulta de regra vira `("", CST)` e nunca casa com regra nenhuma;
- a pendência nasce com **CFOP vazio**, que é exatamente a regra que nunca casa contra
  a qual o resto do sistema se protege;
- a elegibilidade passa a depender só do CST.

Nos seis CT-e do arquivo real, todos caem nesse caminho. O efeito prático hoje é
pequeno, porque o CST 50 dá crédito e os valores fecham. Mas a pendência gerada é
inútil e o caminho é frágil.

### 10.2 As saídas dominam o arquivo, e o CST 01 não é de entrada

O EFD Contribuições escritura entradas **e** saídas. No arquivo real:

| | Documentos | Itens |
|---|---|---|
| Entrada | 268 | 760 |
| Saída | 1431 | 10560 |

Todos os 760 itens de entrada têm CST 50, que é de crédito, e base preenchida. Os de
saída têm CST 01, que é de **saída** tributada, e 49.

Com a opção **"Ignorar saídas" marcada**, que é o padrão da tela, o motor descarta o
documento inteiro e confere só os 760 itens de entrada. Correto.

**Desmarcada**, os 10560 itens de saída entram na conferência. E o CST 01 não está na
lista de CSTs sem crédito, então eles seriam tratados como elegíveis e teriam a base
conferida como se fossem crédito. A lista de CSTs sem crédito foi montada pensando no
arquivo Fiscal, onde saída não chega a esse ramo com tanta frequência.

**Recomendação:** manter "Ignorar saídas" marcada ao auditar o EFD Contribuições.

---

## 11. Números do arquivo real

MEGA, competência agosto/2026, EFD Contribuições.

| Medida | Valor |
|---|---|
| Documentos de mercadoria | 1699 |
| Itens lidos | 11320 |
| Itens com base de PIS preenchida | 11317 |
| Itens de entrada, que a auditoria confere | 760 |
| CT-e | 6 |
| CT-e com D101/D105 escriturados | 6 |
| Divergência de PIS/COFINS no frete | nenhuma |
| Estabelecimentos cobertos pelo arquivo | 6 |

---

## 12. Onde mexer

| Para mudar | Vá em |
|---|---|
| A fórmula da base do XML | `xml_parser.py`, no cálculo do item |
| A lista de CSTs sem crédito | `CSTS_PIS_COFINS_SEM_CREDITO`, em `cross_checker.py` |
| As alíquotas do frete | `ALIQ_PIS` e `ALIQ_COFINS`, em `cross_checker.py` |
| A tolerância de R$ 0,05 | no próprio confronto, em `cross_checker.py` |
| O que a correção grava | `ponte.py` e `cascata.py` |
| O leitor do arquivo | `sped_contribuicoes.py` |

Tudo travado em `tests/test_sped_contribuicoes.py` e
`tests/test_pis_cofins_valor_e_cte_outra_uf.py`. A segunda trava, na seção 3c, a
decisão de 28/08/2026 de conferir base contra base — ela existe para o caso não ser
retomado como defeito novo.
