"""Ajuste MG23000999 — débito para sub-apuração, nos C190 de CFOP 5106/6106.

Pedido do Sandro em 14/09/2026, sobre um SPED real do escritório: toda linha
C190 (Registro Analítico do Documento) cujo CFOP é 5106 ou 6106 precisa gerar,
logo depois dela, dois registros — um C195 e um C197 — repetindo os mesmos
valores de base, alíquota e ICMS que já estão no C190, para declarar esse
débito na sub-apuração do ICMS.

**Os números batem com o próprio exemplo que o Sandro mandou, e é isso que
prova o mapeamento de campo — não a numeração que ele escreveu por extenso.**
Ele descreveu "base no campo 5, ICMS no campo 6" do C190, mas o exemplo real
(`24.944,10 × 7% = 1.746,08`) só fecha se a base for o campo 6 e o ICMS o
campo 7 — que é também a numeração oficial do Guia Prático 3.2.2 e a mesma que
`cascata.py` já usa (`SOMAS_C190`). O campo 5 do C190 é VL_OPR (valor da
operação), não a base; usá-lo teria gravado o número errado no C197 com uma
aparência perfeitamente válida — o tipo de erro que só aparece quando alguém
audita a apuração meses depois.

    C190 (existente, intocado)
      campo 3  CFOP           -> filtro (5106 ou 6106)
      campo 4  ALIQ_ICMS      -> C197 campo 6
      campo 6  VL_BC_ICMS     -> C197 campo 5
      campo 7  VL_ICMS        -> C197 campo 7

    C195 (novo, filho do C100, irmão do C190 — Guia 3.2.2 nível 3)
      campo 2  COD_OBS         código resolvido no 0460 (ver adiante)
      campo 3  TXT_COMPL       "DEBITO PARA SUB-APURAÇÃO"

    C197 (novo, filho do C195 — Guia 3.2.2 nível 4)
      campo 2  COD_AJ          "MG23000999"
      campo 3  DESCR_COMPL_AJ  "DEBITO PARA SUB-APURAÇÃO"
      campo 4  COD_ITEM        (vazio — o ajuste não referencia um item do 0200)
      campo 5  VL_BC_ICMS      = C190 campo 6
      campo 6  ALIQ_ICMS       = C190 campo 4
      campo 7  VL_ICMS         = C190 campo 7
      campo 8  VL_OUTROS       (vazio)

**É UM PAR C195/C197 POR DOCUMENTO, no FIM do C100 — não um par por C190.**
Colar o par logo depois do C190 que o gerou é o desenho óbvio, é o que o
exemplo do pedido mostra (a nota dele tem um C190 só) e reprovou o arquivo da
B&C no PVA duas vezes seguidas, por dois motivos diferentes. A nota com mais
de um C190 alvo é rara — eram 2 em 615 — e quebra as duas regras:

1. **Ordem.** Dentro de um C100 os registros seguem a ordem crescente do
   código, e C195/C197 são os últimos. O par inserido no meio joga o segundo
   C190 para depois de um C195, e o PVA responde "Valor Esperado: C300" — ele
   já passou da faixa do C190 —, dessincroniza e marca toda linha seguinte.
2. **Chave.** Dois C197 no mesmo C195 batem em "Duplicidade de ocorrência da
   chave COD_AJ, COD_ITEM": o COD_AJ é fixo e o COD_ITEM é vazio (o ajuste é
   da operação, não de um item do 0200), então a chave é sempre a mesma.

Por isso os C190 alvo de um documento são SOMADOS num único C197 — base com
base, ICMS com ICMS —, pendurado num C195 que entra depois do último filho do
C100. Decisão do Sandro em 14/09/2026. O agrupamento é por **(CFOP,
ALÍQUOTA)**, não só por CFOP como pedido: o C197 declara os três valores na
mesma linha, e somar alíquotas diferentes daria uma linha em que base ×
alíquota não dá o ICMS. Documento com mais de uma combinação não é ajustado —
sai com aviso nomeando-o, porque um C197 por combinação esbarraria de novo na
chave duplicada. Documento com um C190 só (o caso de 613 dos 615) tem as
strings COPIADAS do C190, sem passar por Decimal: reformatar mudaria
"24944,1" para "24944,10" num arquivo que só pode diferir do original no que
foi pedido. Travado em `tests/test_sped_debito_subapuracao.py`, seção 3.

**O par C195/C197 sozinho REPROVA no PVA — e foi o que aconteceu no primeiro
uso real.** O arquivo da B&C voltou com "Total de Erros 1000", quase todos de
"organização hierárquica dos blocos/registros fora dos padrões". A hierarquia
das linhas novas estava certa; o que faltava era o resto do que o código
MG23000999 obriga. O Guia é explícito na descrição do C197/D197: um COD_AJ com
**"2" no terceiro caractere** (Estorno de Débito) e **"3" no quarto** (Apuração
em separado 1) não é ajuste isolado — ele MOVE ICMS de uma apuração para outra,
e a EFD exige que as duas pontas apareçam:

1. **Bloco 1 com 1900/1910/1920.** "Registro 1900 obrigatório se houver
   registro C197 ou D197 onde o 4º (quarto) dígito do campo 02 - COD_AJ for
   '3'..'8'". O 1920 campo 02 (VL_TOT_TRANSF_DEBITOS_OA) recebe a soma do
   VL_ICMS desses C197 — é o lado DÉBITO, dentro da sub-apuração.
2. **E110 campo 07 (VL_AJ_CREDITOS).** O mesmo valor entra como ajuste a
   crédito na apuração normal — é o lado que ESTORNA o débito de lá. Sem ele o
   ICMS é cobrado duas vezes: uma no E110 (via C190) e outra no 1920. Erro de
   dinheiro, não de validação.
3. **0460 declarando o COD_OBS do C195.** "O código informado deve constar do
   registro 0460", e essa codificação "é de atribuição do contribuinte" — não
   existe 1010 universal. No arquivo real o 1010 já estava ocupado por
   "ESTORNO DE DEBITO e-PTA n. 45.000023946-42", e o Guia proíbe dois 0460 com
   o mesmo COD_OBS. Por isso o código é RESOLVIDO (reusa o que já descreve
   este ajuste; senão o 1010, se livre; senão o primeiro "SBAPnn" livre) em
   vez de fixado no código-fonte.

O que o módulo deliberadamente NÃO resolve sozinho, e por isso avisa:

- **A norma legal do 1900 campo 03.** O Guia manda "descrever a norma legal que
  exige esta apuração em separado", e ela não está em lugar nenhum do arquivo.
  Sai um texto provisório, visível dentro do próprio SPED, e um aviso na tela.
- **O saldo credor anterior da sub-apuração (1920 campo 08).** É o saldo do mês
  passado DESTA sub-apuração; o arquivo do mês corrente não o contém. Num 1920
  criado do zero ele entra zerado, com aviso. Num 1920 que já existia, o valor
  do arquivo é preservado.
- **Documentos extemporâneos (COD_SIT 01 e 07).** O Guia exclui esses do 1920
  campo 02, mas os inclui no E110 campo 07 (somados "no primeiro período de
  apuração"). As duas pontas deixam de fechar entre si, e quem decide o que
  fazer é o contador — o módulo só avisa quais documentos são.

Os totalizadores (0990, C990, 1990, e as linhas 9900/9990/9999 do Bloco 9) são
refeitos pelas mesmas funções que a correção de SPED já usa —
`reconstruir_totais_de_blocos` e `reconstruir_bloco_9` contam por POSIÇÃO no
arquivo, então as linhas novas entram na conta sozinhas, sem lógica escrita de
novo aqui.

**O que este módulo NÃO faz:** não decide se o CFOP 5106/6106 realmente exige
esse ajuste em todo caso — isso é julgamento fiscal de quem sobe o arquivo.
O filtro é mecânico, exatamente como descrito; não valida a legislação de MG
por trás do código MG23000999.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .sped_rewriter.cascata import (E110_AJ_CREDITOS, E110_ICMS_RECOLHER, ZERO,
                                    atualizar_e110, reconstruir_bloco_9,
                                    reconstruir_totais_de_blocos)
from .sped_rewriter.documento import (Documento, Registro, ler_texto,
                                      nova_linha)
from .sped_rewriter.patch import (Alteracao, decimal_para_texto,
                                  texto_para_decimal)

# CFOPs que disparam o ajuste. Texto, não número: SPED é campo de texto e CFOP
# começa com o dígito do sentido da operação — comparar como int aceitaria
# "05106" e outras variações que nunca aparecem num arquivo real.
CFOPS_ALVO = frozenset({"5106", "6106"})

COD_AJ = "MG23000999"
DESCRICAO = "DEBITO PARA SUB-APURAÇÃO"

# Os dois caracteres do COD_AJ que mandam no resto do arquivo (Tabela 5.3 do
# Ato COTEPE 09/2008, lida pelo Guia na descrição do C197/D197). Saem do
# próprio código em vez de constantes soltas: trocar o COD_AJ e esquecer de
# trocar o IND_APUR_ICMS do 1900 geraria um arquivo que valida por fora e
# lança o ICMS na sub-apuração errada.
TIPO_ESTORNO_DEBITO = "2"       # 3º caractere -> ajuste a CRÉDITO no E110
TIPO_ESTORNO_CREDITO = "5"      # 3º caractere -> ajuste a DÉBITO no E110
IND_APUR_ICMS = COD_AJ[3]       # 4º caractere -> qual 1900 recebe o lançamento

# Código do 0460. "1010" é só a PRIMEIRA escolha: a codificação é de atribuição
# do contribuinte e pode já estar ocupada por outra observação.
COD_OBS_PADRAO = "1010"
PREFIXO_COD_OBS = "SBAP"        # COD_OBS é C 006: "SBAP99" cabe exato
MAX_COD_OBS = 99

# 1900 campo 03 pede a norma legal estadual, que o arquivo não tem. O texto
# provisório fica DENTRO do SPED de propósito: quem abrir o arquivo vê o que
# falta, mesmo sem ter lido o aviso da tela.
DESCRICAO_1900 = "SUB-APURACAO DO ICMS (INFORMAR A NORMA LEGAL ESTADUAL)"

IND_MOV_COM_DADOS = "0"         # 1001 campo 02: 0 = bloco COM dados

# COD_SIT (C100/D100 campo 06) que o Guia exclui do 1920 campo 02.
SITUACOES_EXTEMPORANEAS = frozenset({"01", "07"})

TAMANHO_C195 = 3
# C197 ganha um 9º campo — IND_BENEFICIO — a partir do período de apuração de
# JANEIRO DE 2027 (Guia 3.2.3, Subseção 16), e ele é de preenchimento
# obrigatório quando o terceiro dígito do COD_AJ é diferente de "9". O nosso é
# "2", então na virada este número tem de ir para 9 e o campo tem de ser
# preenchido, ou todo C197 gerado aqui passa a reprovar.
TAMANHO_C197 = 8
TAMANHO_0460 = 3
TAMANHO_1001 = 2
# 1010 tem 14 campos no leiaute atual (Guia 3.2.3): os 9 originais mais
# IND_GIAF1, IND_GIAF3, IND_GIAF4 e IND_REST_RESSARC. A versão 2.0.22 do Guia
# trazia 10, e é o número que um arquivo real de hoje NÃO aceita — o SPED da
# B&C (COD_VER 020) traz as 14. Gravar o tamanho velho criaria uma linha curta
# num arquivo que ninguém conferiria, porque o 1010 sempre já existe.
TAMANHO_1010 = 14
TAMANHO_1900 = 3
TAMANHO_1910 = 3
TAMANHO_1920 = 13
TAMANHO_1990 = 2

# 1920 (Guia 3.2.2). Os campos 02 e 05 são calculados a partir dos C197/D197;
# os demais vêm de registros 1921 e do período anterior, que este módulo não
# cria — num registro novo nascem zerados, num que já existia são preservados.
CAMPO_1920_TRANSF_DEBITOS = 2
CAMPO_1920_TRANSF_CREDITOS = 5
CAMPO_1920_SLD_APURADO = 9
CAMPO_1920_TOT_DED = 10
CAMPO_1920_ICMS_RECOLHER = 11
CAMPO_1920_SLD_CREDOR_TRANSP = 12
CAMPO_1920_DEB_ESP = 13
CAMPOS_1920_DEBITO = (2, 3, 4)          # entram somando no saldo
CAMPOS_1920_CREDITO = (5, 6, 7, 8)      # entram subtraindo

# Quem discrimina a guia de cada apuração: E116 para a normal, 1926 para a
# sub-apuração. Os dois têm a mesma validação — "a soma do valor das
# obrigações deste registro deve ser igual à soma dos campos VL_ICMS_RECOLHER
# e DEB_ESP" da apuração correspondente.
CAMPO_OBRIGACAO_VALOR = 3               # VL_OR, em E116 e 1926
E110_DEB_ESP = 15
NOMES_1920 = {2: "VL_TOT_TRANSF_DEBITOS_OA", 5: "VL_TOT_TRANSF_CREDITOS_OA",
              9: "VL_SLD_APURADO_OA", 11: "VL_ICMS_RECOLHER_OA",
              12: "VL_SLD_CREDOR_TRANSP_OA"}


@dataclass
class Ajuste:
    """Um par C195+C197 criado, para a tela mostrar antes do download."""

    documento: str          # chave da NF-e (ou modelo/série/número)
    num: str
    cfop: str
    aliquota: str
    base: str
    icms: str
    ja_existia: bool = False
    # Quantos C190 do documento entraram nesta linha. Maior que 1 significa
    # soma — e a tela marca, porque os números deixam de ser os de uma linha
    # do arquivo original e passam a ser conta nossa.
    agrupados: int = 1


@dataclass
class Resultado:
    documento: Documento | None = None
    ajustes: list[Ajuste] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    # O que mudou FORA do documento fiscal: Bloco 1 (a sub-apuração) e E110 (a
    # apuração normal). Fica separado dos `ajustes` porque responde outra
    # pergunta — "esta rodada mexeu no imposto a recolher?" —, e é a pergunta
    # que ninguém consegue responder depois de o arquivo já ter sido entregue.
    apuracao: list[Alteracao] = field(default_factory=list)

    @property
    def aplicados(self) -> list[Ajuste]:
        return [a for a in self.ajustes if not a.ja_existia]

    @property
    def ja_existiam(self) -> list[Ajuste]:
        return [a for a in self.ajustes if a.ja_existia]


# --------------------------------------------------------------- utilidades

def _do_bloco(r: Registro, bloco: str) -> bool:
    """A linha é um registro numérico do bloco `bloco` (0, 1, 9)?

    Só os blocos de código numérico têm essa ambiguidade: "0460" e "1900" não
    dizem a que bloco pertencem sem olhar o primeiro caractere, enquanto
    "C190" e "D100" dizem.
    """
    return len(r.reg) == 4 and r.reg.isdigit() and r.reg[0] == bloco


def _inserir_em_ordem(doc: Documento, novo: Registro,
                      apos_iguais: bool = False) -> bool:
    """Põe `novo` na posição que o leiaute exige, dentro do próprio bloco.

    A ordem dos registros num bloco é a ordem crescente do código, e o PVA
    confere isso — foi erro de "organização hierárquica" que reprovou o
    arquivo real. Inserir "antes do encerramento do bloco" parece equivalente
    e não é: um 0460 depois de um 0500 já existente sai fora de ordem.

    `apos_iguais` decide onde a linha nova entra na fila dos registros de
    MESMO código, e os dois casos existem: um 0460 é folha e entra no fim da
    fila (a lista do arquivo continua na ordem em que foi escrita), enquanto
    um 1900 ABRE um grupo e tem de entrar antes do primeiro igual — depois
    dele estão os filhos do outro, e a linha nova se penduraria neles.

    Devolve False quando não há onde inserir (bloco inexistente no arquivo).
    """
    bloco = novo.reg[0]
    alvos = [r for r in doc.registros if _do_bloco(r, bloco)
             and (r.reg > novo.reg if apos_iguais else r.reg >= novo.reg)]
    if not alvos:
        return False
    doc.inserir_antes(alvos[0], novo)
    return True


def _fim_do_grupo(doc: Documento, inicio: Registro) -> Registro:
    """A última linha do grupo aberto por `inicio` — onde entra um filho novo.

    Serve para pendurar um 1910 num 1900 que já tem filhos sem cair DENTRO do
    grupo do irmão seguinte: a varredura para no primeiro registro do bloco
    com código menor ou igual (outro 1900) e no encerramento do bloco (1990).
    """
    bloco = inicio.reg[0]
    pos = doc.registros.index(inicio)
    ultimo = inicio
    for r in doc.registros[pos + 1:]:
        if (_do_bloco(r, bloco) and r.reg > inicio.reg
                and not r.reg.endswith("990")):
            ultimo = r
        else:
            break
    return ultimo


def _anotar(resultado: Resultado, registro: str, campo: str, de: str,
            para: str, motivo: str, linha: int | None = None) -> None:
    resultado.apuracao.append(Alteracao(
        documento="(apuração)", registro=registro, linha_origem=linha,
        campo=campo, de=de, para=para, motivo=motivo, origem="sub-apuração"))


# ------------------------------------------------------------ 0460 (COD_OBS)

def _resolver_cod_obs(doc: Documento) -> tuple[str | None, bool]:
    """Qual COD_OBS o C195 vai apontar, e se ele JÁ está declarado no 0460.

    Nesta ordem: o código que já descreve este mesmo ajuste (rodar duas vezes
    não pode criar um segundo 0460 dizendo a mesma coisa), senão "1010" se
    estiver livre, senão o primeiro "SBAPnn" livre. `None` significa que não
    sobrou código — situação que só existe em arquivo absurdo, mas que não
    pode virar um C195 apontando para observação inexistente.
    """
    usados = {r.campo(2).strip(): r.campo(3).strip()
              for r in doc.por_reg("0460")}

    for codigo, texto in usados.items():
        if codigo and texto.upper() == DESCRICAO.upper():
            return codigo, True

    if COD_OBS_PADRAO not in usados:
        return COD_OBS_PADRAO, False

    for n in range(1, MAX_COD_OBS + 1):
        candidato = f"{PREFIXO_COD_OBS}{n:02d}"
        if candidato not in usados:
            return candidato, False
    return None, False


def _declarar_cod_obs(doc: Documento, codigo: str, resultado: Resultado) -> None:
    """Cria a linha 0460 do ajuste, na ordem certa do Bloco 0."""
    linha = nova_linha("0460", {2: codigo, 3: DESCRICAO}, TAMANHO_0460,
                       fim=doc.fim_padrao)
    aberturas = doc.por_reg("0001")
    if aberturas:
        linha.pai = aberturas[0]
    if not _inserir_em_ordem(doc, linha, apos_iguais=True):
        resultado.avisos.append(
            "O arquivo não tem Bloco 0 (nem o 0990): o registro 0460 que "
            f"declara o código de observação {codigo} não pôde ser criado. "
            "Confira se o arquivo é um SPED Fiscal completo.")
        return

    _anotar(resultado, "0460", "COD_OBS", "", codigo,
            f'observação "{DESCRICAO}" declarada para o C195')
    if codigo != COD_OBS_PADRAO:
        resultado.avisos.append(
            f"O código de observação {COD_OBS_PADRAO} já estava em uso neste "
            f"arquivo para outra descrição, então o ajuste usa o código "
            f"{codigo}. Dois registros 0460 não podem repetir o COD_OBS "
            "(Guia, registro 0460).")


# -------------------------------------------------------------- Bloco 1

def _garantir_bloco_1(doc: Documento, resultado: Resultado) -> bool:
    """Deixa o Bloco 1 pronto para receber 1900/1910/1920.

    Todo SPED válido já traz o esqueleto (1001 com IND_MOV=1, 1010 e 1990) —
    o caso normal é só virar o IND_MOV para "0". Criar o esqueleto do zero é a
    exceção, e sai com aviso porque o 1010 nasce respondendo "N" a tudo, e
    essas respostas são do contribuinte, não nossas.
    """
    ancora = (doc.por_reg("9001") or [None])[0]
    aberturas = doc.por_reg("1001")

    if aberturas:
        abertura = aberturas[0]
        antes = abertura.campo(2)
        if abertura.set(2, IND_MOV_COM_DADOS):
            _anotar(resultado, "1001", "IND_MOV", antes, IND_MOV_COM_DADOS,
                    "o Bloco 1 passou a ter dados: a sub-apuração do ICMS",
                    abertura.linha)
    elif ancora is None:
        resultado.avisos.append(
            "O arquivo não tem Bloco 1 nem Bloco 9: o esqueleto do Bloco 1 "
            "(1001/1010/1990), que o ajuste de sub-apuração exige, não pôde "
            "ser criado. Confira se o arquivo é um SPED Fiscal completo.")
        return False
    else:
        abertura = nova_linha("1001", {2: IND_MOV_COM_DADOS}, TAMANHO_1001,
                              fim=doc.fim_padrao)
        doc.inserir_antes(ancora, abertura)
        _anotar(resultado, "1001", "IND_MOV", "", IND_MOV_COM_DADOS,
                "Bloco 1 aberto para receber a sub-apuração")

    if not doc.por_reg("1010"):
        obrig = nova_linha("1010", {n: "N" for n in range(2, TAMANHO_1010 + 1)},
                           TAMANHO_1010, fim=doc.fim_padrao)
        obrig.pai = abertura
        doc.inserir_apos(abertura, obrig)
        resultado.avisos.append(
            "O registro 1010 (obrigatoriedade de registros do Bloco 1) não "
            'existia e foi criado respondendo "N" a todos os campos. Confira '
            'se alguma resposta é "S" para esta empresa antes de transmitir.')

    if not doc.por_reg("1990"):
        fecho = nova_linha("1990", {2: "0"}, TAMANHO_1990, fim=doc.fim_padrao)
        if ancora is not None:
            doc.inserir_antes(ancora, fecho)
        else:
            do_bloco = [r for r in doc.registros if _do_bloco(r, "1")]
            doc.inserir_apos(do_bloco[-1] if do_bloco else doc.registros[-1],
                             fecho)
    return True


# ------------------------------------------------------- 1900 / 1910 / 1920

def _documento_do_ajuste(ajuste: Registro) -> Registro | None:
    """Sobe do C197/D197 até o C100/D100 — é lá que mora o COD_SIT."""
    atual = ajuste.pai
    while atual is not None and atual.reg not in ("C100", "D100"):
        atual = atual.pai
    return atual


def _ajustes_da_sub_apuracao(doc: Documento, terceiro: str) -> list[Registro]:
    """Os C197/D197 desta sub-apuração, pelo 3º e 4º caractere do COD_AJ."""
    achados = []
    for r in doc.registros:
        if r.reg not in ("C197", "D197"):
            continue
        cod = r.campo(2).strip()
        if len(cod) >= 4 and cod[2] == terceiro and cod[3] == IND_APUR_ICMS:
            achados.append(r)
    return achados


def _somar_transferencias(doc: Documento, terceiro: str,
                          excluir_extemporaneos: bool
                          ) -> tuple[Decimal, list[str]]:
    """Soma o VL_ICMS (campo 07) dos ajustes; devolve os que ficaram de fora.

    O Guia exclui do 1920 campo 02 os documentos extemporâneos (COD_SIT "01")
    e os complementares extemporâneos ("07") — e NÃO os exclui do E110 campo
    07, onde manda somá-los "no primeiro período de apuração". Ou seja: as
    duas pontas do mesmo estorno deixam de bater, e isso é do leiaute, não
    deste código. Por isso os documentos deixados de fora voltam nomeados, em
    vez de sumirem dentro de uma soma.
    """
    total = ZERO
    fora: list[str] = []
    for ajuste in _ajustes_da_sub_apuracao(doc, terceiro):
        if excluir_extemporaneos:
            documento = _documento_do_ajuste(ajuste)
            if (documento is not None
                    and documento.campo(6).strip() in SITUACOES_EXTEMPORANEAS):
                fora.append(f"{documento.chave_documento()} "
                            f"(COD_SIT {documento.campo(6).strip()})")
                continue
        total += texto_para_decimal(ajuste.campo(7))
    return total, fora


def _sub_apuracao(doc: Documento, resultado: Resultado) -> None:
    """Cria (ou atualiza) o 1900/1910/1920 desta sub-apuração.

    O 1920 é recalculado por INTEIRO a cada passada — é "um por período" e
    tem de refletir todos os C197 do arquivo, inclusive os que já estavam
    lá. O E110 é o oposto: recebe só a diferença. Os dois comportamentos
    parecem inconsistentes e não são — o 1920 é uma conta que se refaz, e o
    E110 é um saldo de terceiros em que a gente encosta o mínimo.
    """
    zeros = doc.por_reg("0000")
    dt_ini = zeros[0].campo(4).strip() if zeros else ""
    dt_fin = zeros[0].campo(5).strip() if zeros else ""
    if not dt_ini or not dt_fin:
        resultado.avisos.append(
            "O registro 0000 não traz o período da escrituração (campos 04 e "
            "05), e os registros 1910/1920 têm de cobrir exatamente esse "
            "intervalo. A sub-apuração do Bloco 1 NÃO foi criada — o arquivo "
            "vai reprovar no PVA até ela ser preenchida à mão.")
        return

    reg_1900 = next((r for r in doc.por_reg("1900")
                     if r.campo(2).strip() == IND_APUR_ICMS), None)
    if reg_1900 is None:
        reg_1900 = nova_linha("1900", {2: IND_APUR_ICMS, 3: DESCRICAO_1900},
                              TAMANHO_1900, fim=doc.fim_padrao)
        if not _inserir_em_ordem(doc, reg_1900):
            resultado.avisos.append(
                "Não foi possível posicionar o registro 1900 no Bloco 1. A "
                "sub-apuração não foi criada.")
            return
        _anotar(resultado, "1900", "IND_APUR_ICMS", "", IND_APUR_ICMS,
                f"apuração em separado exigida pelo 4º caractere do {COD_AJ}")
        resultado.avisos.append(
            f'O registro 1900 foi criado com a descrição provisória '
            f'"{DESCRICAO_1900}": o campo 03 pede a NORMA LEGAL estadual que '
            "obriga a apuração em separado, e ela não está no arquivo. "
            "Substitua o texto antes de transmitir.")

    reg_1910 = next((f for f in reg_1900.filhos
                     if f.reg == "1910" and f.campo(2).strip() == dt_ini
                     and f.campo(3).strip() == dt_fin), None)
    if reg_1910 is None:
        reg_1910 = nova_linha("1910", {2: dt_ini, 3: dt_fin}, TAMANHO_1910,
                              fim=doc.fim_padrao)
        reg_1910.pai = reg_1900
        doc.inserir_apos(_fim_do_grupo(doc, reg_1900), reg_1910)
        _anotar(resultado, "1910", "DT_INI / DT_FIN", "",
                f"{dt_ini} a {dt_fin}",
                "o período da sub-apuração cobre o do registro 0000, sem lacuna")

    reg_1920 = next((f for f in reg_1910.filhos if f.reg == "1920"), None)
    nasceu_agora = reg_1920 is None
    if nasceu_agora:
        reg_1920 = nova_linha("1920", {}, TAMANHO_1920, fim=doc.fim_padrao)
        reg_1920.pai = reg_1910
        doc.inserir_apos(_fim_do_grupo(doc, reg_1910), reg_1920)

    debitos, extemporaneos = _somar_transferencias(
        doc, TIPO_ESTORNO_DEBITO, excluir_extemporaneos=True)
    creditos, _ = _somar_transferencias(
        doc, TIPO_ESTORNO_CREDITO, excluir_extemporaneos=False)

    def gravar(numero: int, valor: Decimal, motivo: str) -> None:
        antes = reg_1920.campo(numero)
        # "0" como original força "0,00" no lugar de campo vazio: todos os
        # campos do 1920 são obrigatórios, e "||" reprova no PVA.
        depois = decimal_para_texto(valor, antes or "0")
        if reg_1920.set(numero, depois) and numero in NOMES_1920:
            _anotar(resultado, "1920", NOMES_1920[numero], antes, depois,
                    motivo, reg_1920.linha or None)

    gravar(CAMPO_1920_TRANSF_DEBITOS, debitos,
           f"soma do VL_ICMS dos C197/D197 com \"{COD_AJ[2]}\" e "
           f"\"{COD_AJ[3]}\" no 3º e 4º caractere do código de ajuste")
    gravar(CAMPO_1920_TRANSF_CREDITOS, creditos,
           "soma dos estornos de crédito transferidos para esta sub-apuração")

    # Os campos 03, 04, 06 e 07 (que viriam de registros 1921), o 08 (saldo
    # credor do período anterior), o 10 e o 13 NÃO são recalculados: este
    # módulo não cria 1921 e não conhece o mês passado. Num registro novo eles
    # nascem zerados; num que já existia, o valor do arquivo é preservado —
    # zerar apagaria o trabalho de quem escriturou antes.
    for numero in range(2, TAMANHO_1920 + 1):
        if not reg_1920.campo(numero).strip():
            reg_1920.set(numero, "0,00")

    saldo = (sum((texto_para_decimal(reg_1920.campo(n))
                  for n in CAMPOS_1920_DEBITO), ZERO)
             - sum((texto_para_decimal(reg_1920.campo(n))
                    for n in CAMPOS_1920_CREDITO), ZERO))
    deducoes = texto_para_decimal(reg_1920.campo(CAMPO_1920_TOT_DED))
    if saldo >= 0:
        gravar(CAMPO_1920_SLD_APURADO, saldo,
               "(02+03+04) - (05+06+07+08), pela fórmula do Guia")
        gravar(CAMPO_1920_SLD_CREDOR_TRANSP, ZERO,
               "sub-apuração devedora: não há saldo credor a transportar")
        gravar(CAMPO_1920_ICMS_RECOLHER, max(ZERO, saldo - deducoes),
               "ICMS a recolher da sub-apuração (campo 09 - campo 10)")
    else:
        gravar(CAMPO_1920_SLD_APURADO, ZERO,
               "sub-apuração credora: não há saldo devedor")
        gravar(CAMPO_1920_ICMS_RECOLHER, ZERO,
               "sem saldo devedor, nada a recolher na sub-apuração")
        gravar(CAMPO_1920_SLD_CREDOR_TRANSP, -saldo,
               "saldo credor da sub-apuração a transportar")

    _conferir_guia_da_sub_apuracao(doc, reg_1920, resultado)

    if nasceu_agora:
        resultado.avisos.append(
            "O registro 1920 foi criado com o saldo credor do período anterior "
            "(campo 08) igual a 0,00: esse saldo é o da MESMA sub-apuração no "
            "mês passado, e não está neste arquivo. Se houver saldo a "
            "transportar, informe-o antes de transmitir.")
    if extemporaneos:
        resultado.avisos.append(
            "Documentos extemporâneos ficaram FORA da soma do 1920 campo 02, "
            "como manda o Guia, mas entram no E110 campo 07 — as duas pontas "
            "do estorno não vão fechar entre si: " + "; ".join(extemporaneos))


# --------------------------------------------------- as guias (E116 e 1926)
#
# Mover o débito de uma apuração para a outra move junto a OBRIGAÇÃO de
# recolher, e é aí que o arquivo deixa de ser só aritmética: o registro que
# discrimina a guia exige data de vencimento e código de receita da UF, e
# nenhum dos dois está no SPED. O módulo mede a diferença e a mostra; quem
# preenche é quem conhece o regime especial da empresa. Errar o código de
# receita não reprova no PVA — paga no lugar errado, que é pior.

def _soma_obrigacoes(registros: list[Registro]) -> Decimal:
    return sum((texto_para_decimal(r.campo(CAMPO_OBRIGACAO_VALOR))
                for r in registros), ZERO)


def _conferir_guia_da_sub_apuracao(doc: Documento, reg_1920: Registro,
                                   resultado: Resultado) -> None:
    """O 1920 tem ICMS a recolher — existe 1926 discriminando a guia?

    "A soma do valor das obrigações deste registro deve ser igual à soma dos
    campos VL_ICMS_RECOLHER_OA e DEB_ESP_OA, do registro 1920" (Guia, registro
    1926). Sem ele o PVA recusa, e criar um exigiria inventar COD_REC e
    DT_VCTO.
    """
    devido = (texto_para_decimal(reg_1920.campo(CAMPO_1920_ICMS_RECOLHER))
              + texto_para_decimal(reg_1920.campo(CAMPO_1920_DEB_ESP)))
    if devido <= 0:
        return

    fim = _fim_do_grupo(doc, reg_1920)
    inicio = doc.registros.index(reg_1920)
    grupo = doc.registros[inicio:doc.registros.index(fim) + 1]
    declarado = _soma_obrigacoes([r for r in grupo if r.reg == "1926"])
    if abs(declarado - devido) < Decimal("0.01"):
        return

    resultado.avisos.append(
        f"A sub-apuração fechou com {decimal_para_texto(devido, '0')} de ICMS a "
        f"recolher, e os registros 1926 do arquivo somam "
        f"{decimal_para_texto(declarado, '0')}. O 1926 discrimina a GUIA dessa "
        "sub-apuração e pede código de receita e data de vencimento, que não "
        "estão no SPED — informe-os antes de transmitir, ou o PVA recusa o "
        "arquivo.")


def _conferir_guia_da_apuracao_normal(doc: Documento,
                                      resultado: Resultado) -> None:
    """Depois de mexer no E110: a E116 ainda bate com o que ficou a recolher?

    Mesma validação do 1926, do outro lado: "a soma do valor das obrigações
    deste registro deve ser igual à soma dos campos VL_ICMS_RECOLHER e
    DEB_ESP, do registro E110". Quando o estorno leva o débito embora, a guia
    que estava lançada deixa de corresponder — e reescrevê-la seria decidir
    sozinho quanto o cliente vai pagar e quando.
    """
    e110s = doc.por_reg("E110")
    e116s = doc.por_reg("E116")
    if len(e110s) != 1 or not e116s:
        return

    devido = (texto_para_decimal(e110s[0].campo(E110_ICMS_RECOLHER))
              + texto_para_decimal(e110s[0].campo(E110_DEB_ESP)))
    declarado = _soma_obrigacoes(e116s)
    if abs(declarado - devido) < Decimal("0.01"):
        return

    resultado.avisos.append(
        f"A apuração normal ficou com {decimal_para_texto(devido, '0')} de ICMS a "
        f"recolher depois do estorno, mas os registros E116 do arquivo ainda "
        f"somam {decimal_para_texto(declarado, '0')} — é a guia que estava "
        "lançada antes. Ajuste (ou remova) a E116 antes de transmitir: o "
        "valor saiu desta apuração e foi para a guia da sub-apuração.")


# ----------------------------------------------------------------- E110

def _lancar_no_e110(doc: Documento, resultado: Resultado) -> None:
    """O outro lado do estorno: ajuste a crédito na apuração normal.

    Só o ICMS dos pares criados NESTA passada. Os que já existiam foram
    lançados quando foram criados — somá-los de novo dobraria o crédito, que
    é exatamente o erro que a idempotência do módulo existe para evitar.

    `atualizar_e110` se protege sozinha: sem E110, com mais de um E110, ou com
    a fórmula do arquivo não fechando, ela não grava nada e avisa.
    """
    delta = sum((texto_para_decimal(a.icms) for a in resultado.aplicados), ZERO)
    if delta == 0:
        return
    alteracoes, avisos = atualizar_e110(doc, delta,
                                        campo_credito=E110_AJ_CREDITOS)
    resultado.apuracao.extend(alteracoes)
    resultado.avisos.extend(avisos)
    if alteracoes:
        _conferir_guia_da_apuracao_normal(doc, resultado)


# ---------------------------------------------------------------- fachada

def _do_documento(r: Registro) -> bool:
    """O registro é filho de um C100? Todo filho dele está na faixa C1xx.

    É a regra que delimita o grupo do documento sem depender do mapa `NIVEL`,
    que não conhece C101, C105 e a maioria dos filhos do C100. O que vem
    depois do último C1xx é sempre outro documento (C300, C400, C500...) ou o
    encerramento do bloco.
    """
    return (len(r.reg) == 4 and r.reg[0] == "C" and r.reg[1] == "1"
            and r.reg[2:].isdigit() and r.reg != "C100")


def _filhos_do_documento(doc: Documento, c100: Registro) -> list[Registro]:
    """As linhas do documento, na ordem do arquivo."""
    pos = doc.registros.index(c100)
    filhos = []
    for r in doc.registros[pos + 1:]:
        if not _do_documento(r):
            break
        filhos.append(r)
    return filhos


def _fim_dos_ajustes(doc: Documento, c195: Registro) -> Registro:
    """O último C197 pendurado neste C195 — onde o próximo entra."""
    pos = doc.registros.index(c195)
    ultimo = c195
    for r in doc.registros[pos + 1:]:
        if r.reg != "C197":
            break
        ultimo = r
    return ultimo


def _ajustar_documento(doc: Documento, c100: Registro, alvos: list[Registro],
                       cod_obs: str, resultado: Resultado) -> None:
    """Põe UM C195 no fim do documento, com um C197 por C190 alvo.

    A posição é o que reprovou o arquivo da B&C no PVA pela segunda vez —
    ver o cabeçalho do módulo. Dentro do C100 a ordem é a crescente do código,
    e C195/C197 são os últimos: o par tem de vir depois do ÚLTIMO C190, não
    colado em cada um.

    A idempotência também mudou de lugar por causa disso. Antes bastava olhar
    os dois registros seguintes ao C190; agora o par não fica mais ao lado
    dele, então o que identifica um ajuste já feito é o C197 do documento com
    o mesmo COD_AJ e a mesma trinca (base, alíquota, ICMS) — a mesma que este
    código produz, somada ou copiada. Quem identifica é sempre o COD_AJ do
    C197, nunca o COD_OBS do C195: o código de observação é escolhido conforme
    o que estiver livre no 0460, e comparar com "1010" fixo faria a segunda
    passada não reconhecer o próprio trabalho da primeira — duplicando o
    débito justamente no arquivo em que o 1010 estava ocupado, que é o caso
    que obrigou a resolver o código.
    """
    chave_doc = c100.chave_documento()

    # UM C197 por documento, somando os C190 alvo. O PVA recusa dois C197 com
    # a mesma chave (COD_AJ, COD_ITEM) dentro do C195 — "Duplicidade de
    # ocorrência da chave COD_AJ, COD_ITEM" —, e a chave deste ajuste é sempre
    # a mesma: o COD_AJ é fixo e o COD_ITEM é vazio (o ajuste é da operação,
    # não de um item do 0200). Somar foi decisão do Sandro em 14/09/2026.
    #
    # O agrupamento é por (CFOP, ALÍQUOTA), não só por CFOP como pedido: o
    # C197 declara base, alíquota e ICMS na mesma linha, e somar alíquotas
    # diferentes produziria uma linha em que base × alíquota não dá o ICMS.
    # Dois C190 do mesmo CFOP com alíquotas diferentes existem — a chave do
    # C190 é (CST, CFOP, ALIQ) —, então a alíquota entra no agrupamento.
    grupos: dict[tuple[str, str], list[Registro]] = {}
    for c190 in alvos:
        grupos.setdefault((c190.campo(3).strip(), c190.campo(4).strip()),
                          []).append(c190)

    if len(grupos) > 1:
        combinacoes = ", ".join(f"CFOP {cfop} a {aliq}%"
                                for cfop, aliq in grupos)
        resultado.avisos.append(
            f"O documento {chave_doc} tem C190 alvo de mais de uma combinação "
            f"de CFOP e alíquota ({combinacoes}) e NÃO foi ajustado. Só dá "
            "para gerar um C197 por documento (a chave COD_AJ + COD_ITEM não "
            "pode repetir), e somar alíquotas diferentes daria uma linha em "
            "que base × alíquota não bate com o ICMS. Ajuste este documento à "
            "mão.")
        return

    (cfop, _aliq), linhas = next(iter(grupos.items()))
    if len(linhas) == 1:
        # Caminho normal: copia as strings do C190 como estão. Reformatar por
        # Decimal mudaria "24944,1" para "24944,10" num arquivo que só pode
        # diferir do original no que foi pedido.
        base, aliquota, icms = (linhas[0].campo(6), linhas[0].campo(4),
                                linhas[0].campo(7))
    else:
        base = decimal_para_texto(
            sum((texto_para_decimal(l.campo(6)) for l in linhas), ZERO), "0")
        icms = decimal_para_texto(
            sum((texto_para_decimal(l.campo(7)) for l in linhas), ZERO), "0")
        aliquota = linhas[0].campo(4)

    ajuste = Ajuste(documento=chave_doc, num=c100.campo(8), cfop=cfop,
                    aliquota=aliquota, base=base, icms=icms,
                    agrupados=len(linhas))
    resultado.ajustes.append(ajuste)

    filhos = _filhos_do_documento(doc, c100)
    ja_feitos = {(f.campo(5).strip(), f.campo(6).strip(), f.campo(7).strip())
                 for f in filhos
                 if f.reg == "C197" and f.campo(2).strip() == COD_AJ}
    if (base.strip(), aliquota.strip(), icms.strip()) in ja_feitos:
        ajuste.ja_existia = True
        return
    c195 = next((f for f in filhos if f.reg == "C195"
                 and f.campo(2).strip() == cod_obs
                 and f.campo(3).strip().upper() == DESCRICAO.upper()), None)
    if c195 is None:
        c195 = nova_linha("C195", {2: cod_obs, 3: DESCRICAO}, TAMANHO_C195,
                          fim=doc.fim_padrao)
        c195.pai = c100
        doc.inserir_apos(filhos[-1] if filhos else c100, c195)
        ponteiro = c195
    else:
        ponteiro = _fim_dos_ajustes(doc, c195)

    c197 = nova_linha("C197", {2: COD_AJ, 3: DESCRICAO, 5: ajuste.base,
                               6: ajuste.aliquota, 7: ajuste.icms},
                      TAMANHO_C197, fim=doc.fim_padrao)
    c197.pai = c195
    doc.inserir_apos(ponteiro, c197)


def aplicar(texto_sped: str) -> Resultado:
    """Lê o SPED, insere o ajuste em cada C190 de CFOP 5106/6106, devolve o resultado.

    `resultado.documento` vem `None` quando não há nada a fazer — nem C190
    correspondente, nem ajuste novo (tudo já estava aplicado) — para a tela
    não oferecer o download de uma cópia idêntica ao original.

    A ordem do fecho não é preferência: o 0460 e o Bloco 1 criam linhas, a
    sub-apuração só sabe somar depois de todos os C197 existirem, e os
    totalizadores contam por posição — por isso vêm por último.
    """
    doc = ler_texto(texto_sped or "")
    resultado = Resultado()
    resultado.avisos.extend(doc.avisos)

    cod_obs, ja_declarado = _resolver_cod_obs(doc)
    if cod_obs is None:
        resultado.avisos.append(
            "Todos os códigos de observação candidatos já estão em uso no "
            "registro 0460 deste arquivo. O ajuste não foi aplicado: um C195 "
            "apontando para código inexistente reprova no PVA.")
        return resultado

    # Agrupa por DOCUMENTO antes de mexer no arquivo: o C195 é um por C100, e
    # decidir onde ele entra exige conhecer todos os C190 alvo da nota.
    documentos: list[tuple[Registro, list[Registro]]] = []
    posicao: dict[int, int] = {}
    for c190 in doc.por_reg("C190"):
        cfop = c190.campo(3).strip()
        if cfop not in CFOPS_ALVO:
            continue

        c100 = c190.pai
        if c100 is None or c100.reg != "C100":
            resultado.avisos.append(
                f"C190 (CFOP {cfop}, linha {c190.linha or '?'}) não está "
                "dentro de um C100 — ignorado, o arquivo pode estar corrompido "
                "ou fora de ordem.")
            continue

        if id(c100) not in posicao:
            posicao[id(c100)] = len(documentos)
            documentos.append((c100, []))
        documentos[posicao[id(c100)]][1].append(c190)

    for c100, alvos in documentos:
        _ajustar_documento(doc, c100, alvos, cod_obs, resultado)

    if not resultado.aplicados:
        return resultado

    if not ja_declarado:
        _declarar_cod_obs(doc, cod_obs, resultado)
    if _garantir_bloco_1(doc, resultado):
        _sub_apuracao(doc, resultado)
    _lancar_no_e110(doc, resultado)

    reconstruir_totais_de_blocos(doc)
    reconstruir_bloco_9(doc)
    resultado.documento = doc

    return resultado
