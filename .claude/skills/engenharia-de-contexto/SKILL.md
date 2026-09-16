---
name: engenharia-de-contexto
description: Dar contexto a um agente de código sem inchar o prompt, e montar as barreiras que corrigem o agente sozinho. Use ao começar uma tarefa de programação num projeto grande, ao escrever ou revisar CLAUDE.md / AGENTS.md / skills, ao ingerir material novo no cofre, ao notar que a IA responde genérico ou relê os mesmos arquivos, ou antes de decidir entre skill, MCP e automação.
---

# Engenharia de contexto

Todo modelo começa frio. Sem contexto, ele responde genérico e queima token relendo
arquivo inteiro. Mas a solução óbvia — despejar tudo no prompt — é o outro extremo, e
custa mais caro ainda: a atenção do modelo se dilui e as instruções que importam somem no
meio das que não importam.

O trabalho é de curadoria, não de volume. **Contexto é orçamento, não balde.**

Isto é a técnica `Context engineering`, que o Technology Radar vol. 33 (nov/2025) colocou
em **Adopt** — junto com `Curated shared instructions for software teams`. Ver
[[technology-radar-vol-33]] e a skill `decisao-tecnica`.

## As duas metades

Um agente que trabalha sozinho precisa de controles nos dois lados do ato de escrever
código. O Radar chama esse conjunto de *coding agent harness*:

| | O que é | Exemplos aqui |
|---|---|---|
| **Antes** (feedforward) | Aumenta a chance de acertar de primeira | Skills, CLAUDE.md, base de conhecimento, spec antes de codar |
| **Depois** (feedback) | Detecta o erro e força a correção | Compilador, linter, type checker, suíte de testes, mutation testing |

A metade "depois" é a que as pessoas esquecem, e é a mais barata de construir. Um agente
com uma suíte de testes que roda em 30 segundos se autocorrige; um agente sem ela precisa
de você lendo cada diff.

> Em `Conferencia entradas` a metade "depois" já existe: `tests/run_all.py` roda 17 suítes
> em SQLite, sem rede, em menos de dois minutos. **Rode antes de dizer que terminou.**
> Ver [[conferencia-entradas]].

## Divulgação progressiva

`Progressive context disclosure` (Radar, Trial). Não carregue o acervo — carregue o índice,
e desça só no ramo necessário.

A estrutura do cofre já implementa isso:

```
Clippings/  →  material bruto, recém-capturado, ainda não lido
raw/        →  fonte integral preservada (transcrição, dump, snapshot)
wiki/       →  resumo com ligações; é isto que o agente lê primeiro
```

O resumo em `wiki/` não é um luxo de organização. É o que permite responder "onde eu falei
sobre X?" sem abrir nenhuma fonte integral. Quando um tópico se repete em várias fontes,
ele vira nota própria e as fontes passam a apontar para ela.

**Regra prática:** se responder a uma pergunta exige ler mais de dois arquivos inteiros,
falta um resumo na camada `wiki/`.

## Ingestão: transformar bruto em contexto

Ao receber material novo (clipping, transcrição, documento, dump de pasta):

1. **Leia a fonte inteira uma vez.** Sem isso o resumo vira paráfrase do título.
2. **Identifique os tópicos que já existem no cofre.** Um material novo quase sempre toca
   notas existentes — a ligação vale mais que o conteúdo novo.
3. **Escreva a nota em `wiki/`** no tipo certo: `processos/` (como se faz),
   `projetos/` (o que está em construção), `referencias/` (o que se consulta),
   `regras/` (o que a lei ou o negócio obriga).
4. **Frontmatter com `fontes:`** apontando o caminho do bruto. Rastreabilidade é o que
   permite conferir depois se o resumo distorceu.
5. **Ligue com `[[wikilinks]]`.** Link para nota que ainda não existe é bom sinal: marca
   o que falta escrever.
6. **Deixe o bruto onde está.** O resumo não substitui a fonte; ele indexa a fonte.

Preserve as lacunas: quando a fonte não diz algo, escreva que não diz. Um resumo que
esconde o que não sabe é pior que nenhum resumo — ver o bloco "Lacuna" em
[[conferencia-entradas]] como modelo.

## Skill, MCP ou automação?

Os três resolvem coisas diferentes e a confusão entre eles é a causa mais comum de
inchaço:

- **Skill** — ensina *como agir*. Texto, carregado sob demanda pela descrição. Custo zero
  quando não é acionada. É a escolha padrão.
- **MCP** — dá *acesso a um sistema* que o agente não alcança. Cada servidor conectado
  gasta contexto em toda sessão, sempre, mesmo sem uso.
- **Automação** — script determinístico. Quando o passo não precisa de julgamento, código
  comum é mais barato, mais rápido e mais confiável que um agente.

O Radar colocou `MCP by default` em **Caution** exatamente por isso: conectar servidor
porque existe, e não porque falta acesso, é imposto fixo sobre toda conversa. Skills são
a alternativa controlada.

## Escrevendo uma skill que funciona

- **A descrição é o gatilho.** Ela é o único texto sempre carregado. Escreva quando usar,
  com as palavras que aparecem no pedido real — não o que a skill contém.
- **Uma skill, um assunto.** Skill que faz tudo não dispara para nada.
- **Regra com o porquê junto.** Regra sem motivo é desobedecida na primeira exceção;
  com motivo, é aplicada a casos que você não previu.
- **Exemplo do projeto real** vale mais que exemplo genérico.
- **Volume tem custo.** `Agent instruction bloat` está em **Caution** no Radar. Instrução
  que não muda decisão só dilui as que mudam. Ao adicionar, pergunte o que sai.

## Antipadrões — o anel Caution do Radar

Estes têm nome porque são comuns, não porque são exóticos:

- **`Codebase cognitive debt`** — aceitar código que funciona sem construir o modelo mental
  de por que funciona. A dívida só aparece no primeiro bug difícil, quando ninguém sabe
  depurar o próprio sistema. *Antídoto: peça a explicação junto com o código, e recuse
  o diff que você não conseguiria defender.*
- **`Agent instruction bloat`** — CLAUDE.md que virou depósito. *Antídoto: poda periódica.*
- **`Coding throughput as a measure of productivity`** — medir linhas ou PRs gerados.
  Volume de código é custo, não entrega. *Antídoto: DORA metrics (Adopt).*
- **`Coding agent swarms`** — muitos agentes em paralelo no mesmo código. O custo de
  revisar e reconciliar cresce mais rápido que o ganho.
- **`Ignoring durability in agent workflows`** — fluxo longo sem estado persistido: cai no
  meio e recomeça do zero, às vezes refazendo efeito colateral já aplicado.
- **`AI-accelerated shadow IT`** — sistema que alguém gerou rápido, ninguém mantém, e a
  empresa passa a depender.

## Roteiro de uma tarefa

1. **Situe-se antes de editar.** Leia a nota de `wiki/` do projeto, não o código inteiro.
2. **Confirme a intenção quando as leituras divergirem.** Uma pergunta na hora certa custa
   menos que trinta arquivos na direção errada.
3. **Ache a causa, não o sintoma.** Reproduza — em teste, em script — antes de corrigir.
   Um patch sobre hipótese não verificada vira dois bugs.
4. **Mude o mínimo que resolve.**
5. **Rode as barreiras.** Testes, tipos, lint. Relate o resultado real, inclusive falha.
6. **Registre o que não é dedutível do código.** Decisão, motivo, alternativa descartada —
   isso vai para `wiki/`, não para comentário no fonte.

## Levar para outro projeto

Copie a pasta da skill para `.claude/skills/` do projeto destino. `decisao-tecnica` vai
inteira, sem ajuste — não tem nada específico daqui. Esta aqui precisa de três trocas:

1. **As três camadas** (`Clippings/ → raw/ → wiki/`) são o vocabulário deste cofre. Num
   projeto de código, o equivalente é `docs/` ou `CLAUDE.md` + as notas do cofre acessadas
   de fora. O princípio não muda: índice primeiro, fonte só quando necessário.
2. **A barreira de feedback** precisa apontar para o comando real daquele projeto — qual
   suíte roda, em quanto tempo, sem rede. Se o projeto ainda não tem uma, construir isso
   é a primeira tarefa, antes de qualquer feature.
3. **O exemplo de projeto** (`Conferencia entradas`) vira o projeto de lá.

> Num conciliador SPED × XML, o contexto que mais economiza tempo não é esta skill — são
> as notas que já existem no cofre sobre o mesmo domínio: [[credito-antecipacao-icms-cfop]],
> [[credito-ipi-pis-cofins-por-cfop-cst]], [[icms-frete-cte-sped-d190]],
> [[cfop-contribuinte-reducao-base-icms]] e [[conferencia-entradas]]. O motor de
> `Conferencia entradas` já faz cruzamento SPED × XML e já pagou o preço de alguns erros
> — casamento por chave exata de quatro campos, sem curinga, e a armadilha do "sucesso"
> reportado com zero registros gravados. Comece lendo isso, não do zero.

## Ver também

- `decisao-tecnica` — antes de adotar biblioteca, padrão ou ferramenta
- [[technology-radar-vol-33]] — os 118 blips e os quatro temas
- [[ingestao-base-conhecimento]] — o padrão raw → wiki em detalhe
- `obsidian-markdown` — sintaxe de wikilink, callout e propriedades
