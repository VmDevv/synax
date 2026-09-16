# Sub-apuração reprovada no PVA (1000 erros) — RESOLVIDO

Escrito em 14/09/2026 como passagem de bastão; fechado no mesmo dia. Fica como
registro da causa raiz, porque ela não é dedutível do código nem do pedido.

**Status:** implementado e travado em teste. Resta **uma pergunta para o
Sandro** (seção 5), que não bloqueia a ferramenta.

---

## 1. O que aconteceu

A ferramenta **Ajuste de sub-apuração** (`/ajuste-sped`) gerou o `Sped_Ajustado.txt`
da B&C COMERCIO E DISTRIBUICAO DE ELETRONICOS LTDA (CNPJ 37.829.594/0002-39) e o
PVA recusou com **"Total de Erros 1000"**, repetindo em quase todo registro depois
das inserções:

> Organização hierárquica dos blocos/registros do arquivo está fora dos padrões
> estabelecidos

A hierarquia C100 → C190 → C195 (nível 3) → C197 (nível 4) **estava certa**. O erro
não era de posição das linhas novas — era de registro FALTANDO em outro bloco.

## 2. Causa raiz (Guia Prático EFD-ICMS/IPI v2.0.22)

O Guia, na descrição do registro C197/D197:

> Neste caso o Estado publicará a Tabela 5.3 com códigos que contenham os
> dígitos "3", "4", "5", "6", "7" e "8" no **quarto caractere**, sendo que cada
> um dos dígitos possibilitará a escrituração de uma apuração em separado **no
> registro 1900 e filhos**.
>
> 1. o **terceiro** caractere do código de ajuste for igual a "2 – Estorno de
>    Débito" e o **quarto** for "3".."8": o valor do campo 07 - VL_ICMS gera um
>    **ajuste a crédito (campo 07 - VL_AJ_CREDITOS) no registro E110** e também
>    um lançamento a débito no **registro 1920 (campo 02 -
>    VL_TOT_TRANSF_DEBITOS_OA)** da sub-apuração definida no registro 1900.

O código que a ferramenta grava é **`MG23000999`**:

```
M  G  2  3  0  0  0  9  9  9
      ^  ^
      |  +-- 4º caractere = "3"  -> sub-apuração "Apuração em separado 1"
      +----- 3º caractere = "2"  -> Estorno de Débito
```

Gravar esse código obriga o arquivo a ter o Bloco 1 completo e o lançamento no
E110 — e a ferramenta não criava nada disso. Eram três buracos:

| # | Buraco | Consequência |
|---|--------|--------------|
| 1 | **Bloco 1 sem 1900/1910/1920.** O 1900 é "obrigatório se houver C197/D197 onde o 4º dígito do COD_AJ for 3..8". | Era **esta** a causa dos 1000 erros de hierarquia. |
| 2 | **Nenhum 0460 declarado.** O COD_OBS do C195 "deve constar do registro 0460", e esse código é de atribuição do contribuinte — "1010" **não** é convenção universal. No arquivo real o 1010 **já estava em uso** para `ESTORNO DE DEBITO e-PTA n. 45.000023946-42`. | C195 apontando para código inexistente (ou para outro significado). |
| 3 | **E110 intocado.** O estorno tem de virar ajuste a crédito no campo 07 do E110. | Sem isso o mesmo ICMS ficava **contado duas vezes**: débito na apuração normal (via C190) **e** débito na sub-apuração (1920). Erro de dinheiro, não só de validação. |

## 3. O que foi feito

**`sped_rewriter/cascata.py`** — `atualizar_e110()` ganhou o parâmetro
`campo_credito` (padrão `E110_TOT_CREDITOS = 6`, comportamento antigo intacto)
para poder lançar no campo 07 (`E110_AJ_CREDITOS`). Novos: a constante
`E110_AJ_CREDITOS`, a entrada 7 em `NOMES_E110` e o dicionário `MOTIVOS_E110`.
Os dois campos **não** são intercambiáveis: 06 é o crédito das entradas, 07 é o
ajuste decorrente de documento fiscal, e é por componente que o PVA confere.

**`sped_rewriter/documento.py`** — `inserir_antes()` (simétrico ao
`inserir_apos`, para quem precisa abrir grupo) e os níveis de `0460`, `1001`,
`1010`, `1900`, `1910`, `1920` e `1990` no mapa `NIVEL`, que antes ignorava o
Bloco 1 inteiro.

**`services/sped_debito_subapuracao.py`** — reescrito. Depois de inserir os
pares C195/C197 (parte que não mudou), ele agora:

- **resolve o COD_OBS do 0460** — reusa o 0460 que já descreve este ajuste,
  senão "1010" se estiver livre, senão o primeiro "SBAPnn" livre — e cria a
  linha 0460 na **ordem crescente do bloco** (não "antes do 0990": ordem de
  registro dentro do bloco é exatamente o que o PVA reprovou);
- **garante o Bloco 1** — vira o `IND_MOV` do 1001 para "0"; cria 1001/1010/1990
  quando não existem (com aviso, porque o 1010 nasce respondendo "N" a tudo);
- **cria/atualiza 1900 (IND_APUR_ICMS = 3), 1910 (o período do 0000) e 1920**,
  reusando os que já existirem — o 1900 não pode repetir IND_APUR_ICMS, o 1910
  não pode repetir DT_INI/DT_FIN e o 1920 é um por período;
- **lança no E110 campo 07** via `atualizar_e110(..., campo_credito=E110_AJ_CREDITOS)`.

Duas contas com comportamentos opostos de propósito: **o 1920 é recalculado por
inteiro** a cada passada (tem de somar todos os C197 do arquivo) e **o E110
recebe só a diferença** (o que já estava lá foi lançado antes). É isso que
mantém a idempotência dos dois.

`_ja_ajustado` passou a identificar o ajuste pelo **COD_AJ do C197**, nunca pelo
COD_OBS do C195 — com o código resolvido, comparar "1010" fixo faria a segunda
passada não reconhecer o próprio trabalho da primeira e **duplicar o débito**
justamente no arquivo em que o 1010 estava ocupado.

O que o arquivo não responde vira aviso na tela, nunca palpite: a **norma legal
do 1900 campo 03**, o **saldo credor anterior da sub-apuração (1920 campo 08)** e
os **documentos extemporâneos** (COD_SIT 01/07), que o Guia exclui do 1920 campo
02 mas inclui no E110 campo 07 — as duas pontas deixam de fechar entre si, e
quem decide é o contador.

**`partials/ajuste_sped_resultado.html`** — a tela ganhou a tabela "Apuração e
Bloco 1" (registro, campo, de → para, motivo) antes do botão de baixar: mover
imposto entre apurações é dinheiro, e não pode aparecer só dentro do arquivo que
alguém vai transmitir.

**`tests/test_sped_debito_subapuracao.py`** — fixture agora monta um SPED
realista (Bloco 1 e Bloco E), e a suíte foi de 63 para **106 testes**. Cobre: as
duas pontas do estorno, colisão de 0460, idempotência do 1920 e do E110,
esqueleto do Bloco 1 criado do zero, arquivo sem E110, E110 que não fecha pela
fórmula, ordem do Bloco 1 e os totalizadores 0990/C990/1990/9900/9990/9999.

Barreira: `venv/Scripts/python.exe tests/run_all.py` → **2763/2763 em 52 suítes,
sem regressão**.

O registro da decisão está no `CLAUDE.md`, em dois tópicos novos ("COD_AJ de
sub-apuração não é ajuste isolado" e "O COD_OBS do 0460 é de atribuição do
CONTRIBUINTE").

## 4. Como conferir no arquivo real

Subir o SPED da B&C em `/ajuste-sped` e validar o resultado no PVA. O que deve
aparecer no arquivo gerado, além dos pares C195/C197:

```
|0460|SBAP01|DEBITO PARA SUB-APURAÇÃO|      <- 1010 ocupado, pegou o próximo livre
|1001|0|                                     <- era 1
|1900|3|SUB-APURACAO DO ICMS (INFORMAR A NORMA LEGAL ESTADUAL)|
|1910|01042026|30042026|
|1920|<soma dos C197>|0,00|...|
|E110|...|<campo 07 = mesma soma>|...|
```

Dois campos ainda pedem a mão de alguém antes de transmitir, e a tela avisa: a
**norma legal** no 1900 campo 03 e o **saldo credor anterior** no 1920 campo 08.

## 5. Pergunta em aberto para o Sandro

A mensagem original trazia também:

> Criar código 1010 no campo 2 do registro 0460 com a descrição
> "ESTORNO DE DEBITO e-PTA n. 45.000046124-18"

No arquivo real o código **1010 já está ocupado** por
`ESTORNO DE DEBITO e-PTA n. 45.000023946-42`, e o Guia proíbe dois registros 0460
com o mesmo COD_OBS. Então isso **não pode** ser feito como está escrito. A
ferramenta hoje segue pelo caminho (c): reserva um código próprio para a
observação dela e não encosta no 1010 alheio. Falta o Sandro dizer se o caso é:

- (a) outro e-PTA que precisa de **código próprio** (ex.: 1011) e de C195
  apontando para ele — trabalho separado desta ferramenta;
- (b) **substituir** a descrição do 1010 existente; ou
- (c) era só o exemplo de como o escritório escreve essas observações — e aí
  está como está, nada a fazer.
