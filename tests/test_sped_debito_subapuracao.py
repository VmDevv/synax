# -*- coding: utf-8 -*-
"""Ajuste de sub-apuração (C195/C197) nos C190 de CFOP 5106/6106.

Ferramenta pedida pelo Sandro em 14/09/2026, sobre um SPED real do escritório:
para cada C190 com CFOP 5106 ou 6106, inserir logo depois um C195 e um C197
(código MG23000999) repetindo a base, a alíquota e o ICMS que já estavam
naquele C190 — e, porque esse código é de SUB-APURAÇÃO, fechar as duas pontas
que ele obriga: o Bloco 1 (1900/1910/1920), o E110 campo 07 e o 0460 que
declara o código de observação do C195.

O que esta suíte protege:

1. **O mapeamento de campo é o do EXEMPLO REAL, não o da descrição por
   extenso.** O pedido original descreveu "base no campo 5, ICMS no campo 6"
   do C190 — mas isso está incorreto: só fecha a conta (`24.944,10 × 7% =
   1.746,08`) com base no campo 6 e ICMS no campo 7, que é também a numeração
   oficial do Guia 3.2.2 já usada em `cascata.SOMAS_C190`. Esta suíte usa os
   NÚMEROS do exemplo — não a prosa — como padrão-ouro, byte a byte.

2. **O filtro de CFOP é exato e por texto.** CFOP 6124 (do mesmo exemplo real,
   mas fora do alvo) não pode disparar o ajuste.

3. **Não duplica.** Rodar o mesmo arquivo (ou o arquivo já ajustado) de novo
   não pode dobrar o débito declarado — nem no C197, nem no 1920, nem no E110.
   Isso mudaria a apuração de verdade, diferente de uma regra morta que só
   "não funciona".

4. **As duas pontas do estorno existem.** O par C195/C197 sozinho reprovou no
   PVA com 1000 erros no primeiro uso real: o COD_AJ com "2" e "3" no 3º e 4º
   caractere OBRIGA o registro 1900 e filhos, e manda o mesmo valor para o
   E110 campo 07. Sem os dois, ou o arquivo não valida, ou o mesmo ICMS é
   cobrado duas vezes.

5. **O COD_OBS do 0460 é do contribuinte, não nosso.** No arquivo real o
   código 1010 já estava ocupado por outra observação, e o Guia proíbe dois
   0460 com o mesmo código: a ferramenta tem de escolher outro e continuar
   reconhecendo o próprio trabalho na passada seguinte.

6. **Nada fora do que foi pedido é tocado**, e os totalizadores (0990, C990,
   1990, Bloco 9) fecham exatamente como a correção de SPED já garante —
   reaproveitando `reconstruir_totais_de_blocos`/`reconstruir_bloco_9`, não
   reimplementando.

7. **Sem correspondência não gera arquivo** (mesma regra do resto do sistema:
   nunca "sucesso" com zero) — e C190 órfão (fora de um C100) não derruba o
   processamento, só fica de fora com aviso.
"""
import io
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.abspath('.'))

from backend.app.services.sped_debito_subapuracao import aplicar  # noqa: E402
from backend.app.services.sped_rewriter import ler_texto  # noqa: E402

OK = FALHAS = 0
_falhou = []


def check(nome, condicao, detalhe=""):
    global OK, FALHAS
    if condicao:
        OK += 1
        print(f"  OK   | {nome}")
    else:
        FALHAS += 1
        _falhou.append(nome)
        print(f" FALHA | {nome}  [{detalhe}]")


# ============================================================================
# Fixture: um SPED mínimo mas REALISTA — com Bloco 1 e Bloco E, que é o que
# todo SPED válido traz. Sem eles, todo teste exercitaria só o caminho de
# exceção (criar o esqueleto do zero) e nenhum o caminho normal.
# ============================================================================

def linha(reg, campos, total):
    valores = [""] * total
    valores[0] = reg
    for numero, valor in campos.items():
        valores[numero - 1] = valor
    return "|" + "|".join(valores) + "|"


def c100(chave, num, vl_doc="1763,05", cod_sit="00"):
    return linha("C100", {2: "1", 3: "0", 4: "6753", 5: "55", 6: cod_sit,
                          7: "001", 8: num, 9: chave, 10: "01042026",
                          11: "01042026", 12: vl_doc}, 29)


def c190(cst, cfop, aliq, opr, bc, icms, ipi="0,00"):
    return linha("C190", {2: cst, 3: cfop, 4: aliq, 5: opr, 6: bc, 7: icms,
                          8: "0,00", 9: "0,00", 10: "0", 11: ipi}, 12)


def valor(texto):
    return Decimal((texto or "0").replace(".", "").replace(",", ".") or "0")


def texto_valor(numero):
    return f"{numero:.2f}".replace(".", ",")


def e110_zerado(debitos):
    """Um E110 que FECHA pela fórmula do Guia — sem isso o motor se recusa
    (e com razão) a mexer na apuração, e o teste mediria a recusa."""
    v = texto_valor(debitos)
    return linha("E110", {2: v, 3: "0,00", 4: "0,00", 5: "0,00", 6: "0,00",
                          7: "0,00", 8: "0,00", 9: "0,00", 10: "0,00", 11: v,
                          12: "0,00", 13: v, 14: "0,00", 15: "0,00"}, 15)


def montar_sped(blocos_c, obs_0460=(), com_bloco_1=True, com_bloco_e=True,
                ind_mov_1="1", bloco_1_extra=(), com_e116=False):
    """`blocos_c`: linhas de C100 + filhos, já prontas, na ordem do arquivo."""
    bloco_0 = ["|0000|018|0|01042026|30042026|EMPRESA TESTE LTDA|"
               "11222333000181|MG|123456789|3106200|||A|1|", "|0001|0|"]
    for codigo, descricao in obs_0460:
        bloco_0.append(f"|0460|{codigo}|{descricao}|")
    bloco_0.append(f"|0990|{len(bloco_0) + 1}|")

    bloco_c = ["|C001|0|"] + list(blocos_c)
    bloco_c.append(f"|C990|{len(bloco_c) + 1}|")

    bloco_e = []
    if com_bloco_e:
        # O débito do E110 é o ICMS que realmente está nos C190 do arquivo —
        # é ele que a sub-apuração vai estornar de lá.
        debitos = sum((valor(l.split("|")[7]) for l in blocos_c
                       if l.startswith("|C190|")), Decimal("0"))
        bloco_e = ["|E001|0|", "|E100|01042026|30042026|", e110_zerado(debitos)]
        if com_e116:
            # A guia que o cliente ia pagar pela apuração normal — o que o
            # estorno para a sub-apuração deixa desatualizado.
            bloco_e.append(linha("E116", {2: "000", 3: texto_valor(debitos),
                                          4: "18052026", 5: "121-0",
                                          10: "042026"}, 10))
        bloco_e.append(f"|E990|{len(bloco_e) + 1}|")

    bloco_1 = []
    if com_bloco_1:
        bloco_1 = [f"|1001|{ind_mov_1}|", "|1010|N|N|N|N|N|N|N|N|N|"]
        bloco_1 += list(bloco_1_extra)
        bloco_1.append(f"|1990|{len(bloco_1) + 1}|")

    linhas = bloco_0 + bloco_c + bloco_e + bloco_1
    tipos = {}
    for l in linhas:
        reg = l.split("|")[1]
        tipos[reg] = tipos.get(reg, 0) + 1
    tipos["9001"] = 1
    total_9900 = len(tipos) + 3
    tipos["9900"] = total_9900
    tipos["9990"] = 1
    tipos["9999"] = 1

    bloco_9 = ["|9001|0|"] + [f"|9900|{r}|{q}|" for r, q in tipos.items()]
    bloco_9.append(f"|9990|{1 + total_9900 + 1 + 1}|")

    linhas = linhas + bloco_9
    linhas.append(f"|9999|{len(linhas) + 1}|")
    return "\r\n".join(linhas) + "\r\n"


def linhas_de(texto):
    return [l for l in texto.split("\r\n") if l]


def por_reg(texto, reg):
    return [l for l in linhas_de(texto) if l.startswith(f"|{reg}|")]


def campo(linha_texto, numero):
    return linha_texto.split("|")[numero]


def fora_de_ordem(texto_sped):
    """Filhos de C100 em ordem decrescente de código — o erro do PVA.

    Dentro de um C100 os registros seguem a ordem crescente do código, e é
    isso que a mensagem "Organização hierárquica dos blocos/registros do
    arquivo está fora dos padrões estabelecidos" cobra. Devolve as linhas
    que aparecem depois de um irmão de código MAIOR.
    """
    quebras, anterior = [], ""
    for l in linhas_de(texto_sped):
        r = l.split("|")[1]
        if r == "C100":
            anterior = ""
            continue
        if not (len(r) == 4 and r[0] == "C" and r[1] == "1" and r[2:].isdigit()):
            anterior = ""
            continue
        if anterior and r < anterior:
            quebras.append(f"{anterior} -> {l[:60]}")
        anterior = r
    return quebras


# O exemplo real do Sandro: |C190|000|6124|7|26191,3|24944,1|1746,08|0|0|0|1247,2||
# — CFOP trocado para 5106/6106 onde o teste precisa que o ajuste dispare.
CHAVE_1 = "1" * 44
CHAVE_2 = "2" * 44
CHAVE_3 = "3" * 44
CHAVE_4 = "4" * 44

ALIQ, OPR, BC, ICMS, IPI = "7", "26191,3", "24944,1", "1746,08", "1247,2"

C190_5106 = c190("000", "5106", ALIQ, OPR, BC, ICMS, IPI)
C190_6106 = c190("000", "6106", ALIQ, OPR, BC, ICMS, IPI)
C190_6124 = c190("000", "6124", ALIQ, OPR, BC, ICMS, IPI)  # fora do alvo


# ============================================================================
print("\n=== 1. O exemplo real: os valores batem byte a byte ===")

sped = montar_sped([c100(CHAVE_1, "283"), C190_6106])
resultado = aplicar(sped)

check("um ajuste aplicado", len(resultado.aplicados) == 1, len(resultado.aplicados))

texto = resultado.documento.serializar()
c195_linhas = por_reg(texto, "C195")
c197_linhas = por_reg(texto, "C197")
check("um C195 criado", len(c195_linhas) == 1, c195_linhas)
check("um C197 criado", len(c197_linhas) == 1, c197_linhas)

check("C195 é BYTE A BYTE o do exemplo",
      c195_linhas[0] == "|C195|1010|DEBITO PARA SUB-APURAÇÃO|", c195_linhas[0])
check("C197 é BYTE A BYTE o do exemplo",
      c197_linhas[0] == "|C197|MG23000999|DEBITO PARA SUB-APURAÇÃO||24944,1|7|1746,08||",
      c197_linhas[0])

check("na ordem certa: C190, depois C195, depois C197",
      linhas_de(texto)[linhas_de(texto).index(C190_6106) + 1:][:2]
      == [c195_linhas[0], c197_linhas[0]])

ajuste = resultado.ajustes[0]
check("o ajuste relatado leva a chave da nota", ajuste.documento == CHAVE_1)
check("e os mesmos valores", (ajuste.base, ajuste.aliquota, ajuste.icms)
      == (BC, ALIQ, ICMS), (ajuste.base, ajuste.aliquota, ajuste.icms))
check("e não está marcado como 'já existia'", ajuste.ja_existia is False)

# Os avisos que SOBRAM são os que ninguém pode responder a partir do arquivo:
# a norma legal do 1900 e o saldo credor anterior da sub-apuração. Nenhum
# outro — um aviso a mais aqui é sinal de que algo não foi resolvido sozinho.
check("três avisos, os três de informação que o arquivo não tem",
      len(resultado.avisos) == 3, resultado.avisos)
check("avisa que a norma legal do 1900 é provisória",
      any("NORMA LEGAL" in a for a in resultado.avisos), resultado.avisos)
check("avisa que o saldo credor anterior da sub-apuração foi presumido zero",
      any("saldo credor do período anterior" in a for a in resultado.avisos),
      resultado.avisos)
check("avisa que falta o 1926, a guia da sub-apuração",
      any("1926" in a for a in resultado.avisos), resultado.avisos)
check("e NÃO avisa de colisão de código (o 1010 estava livre)",
      not any("já estava em uso" in a for a in resultado.avisos))


# ============================================================================
print("\n=== 2. O filtro de CFOP é exato ===")

sped_5106 = montar_sped([c100(CHAVE_1, "1"), C190_5106])
r_5106 = aplicar(sped_5106)
check("CFOP 5106 também dispara", len(r_5106.aplicados) == 1, r_5106.avisos)

sped_6124 = montar_sped([c100(CHAVE_1, "1"), C190_6124])
r_6124 = aplicar(sped_6124)
check("CFOP 6124 (mesmo exemplo, fora do alvo) NÃO dispara",
      r_6124.documento is None and not r_6124.ajustes, r_6124.ajustes)
check("e o C190 original sai intocado",
      por_reg(sped_6124, "C190") == [C190_6124])
check("e nem o Bloco 1 nem o E110 são mexidos quando nada casa",
      not r_6124.apuracao, r_6124.apuracao)

sped_vazio = montar_sped([c100(CHAVE_1, "1"),
                          c190("000", "", ALIQ, OPR, BC, ICMS, IPI)])
r_vazio = aplicar(sped_vazio)
check("CFOP em branco não vira palpite", r_vazio.documento is None)


# ============================================================================
print("\n=== 3. Documento com DOIS C190: um C195 só, depois do último ===")

# Esta é a seção que o PVA escreveu. A primeira versão colava o par C195/C197
# logo depois de cada C190 — e num documento com dois C190 isso joga o segundo
# C190 para DEPOIS de um C195. Dentro do C100 a ordem é a crescente do código,
# então o PVA responde "Valor Esperado: C300" e derruba o arquivo inteiro.
# Eram 2 documentos em 615 no arquivo real da B&C.
# O caso real da B&C: mesmo CFOP e mesma alíquota, CSTs diferentes — que é
# uma chave de C190 distinta (CST, CFOP, ALIQ) e um segundo registro no mesmo
# documento.
c190_a = c190("000", "5106", "12", "1000,00", "900,00", "108,00")
c190_b = c190("020", "5106", "12", "2000,00", "1500,00", "180,00")
sped_dois = montar_sped([c100(CHAVE_1, "1"), c190_a, c190_b])
r_dois = aplicar(sped_dois)

check("os dois C190 viraram UM ajuste somado", len(r_dois.aplicados) == 1,
      len(r_dois.aplicados))
texto_dois = r_dois.documento.serializar()
check("UM C195 e UM C197 para o documento — a chave COD_AJ+COD_ITEM não "
      "pode repetir dentro do C195",
      len(por_reg(texto_dois, "C195")) == 1
      and len(por_reg(texto_dois, "C197")) == 1,
      (por_reg(texto_dois, "C195"), por_reg(texto_dois, "C197")))

ordem = linhas_de(texto_dois)
i_a = ordem.index(c190_a)
check("a ordem do leiaute: C190, C190, C195, C197",
      [l.split("|")[1] for l in ordem[i_a:i_a + 4]]
      == ["C190", "C190", "C195", "C197"], ordem[i_a:i_a + 4])
check("nenhum filho de C100 fora da ordem crescente (foi o erro do PVA)",
      not fora_de_ordem(texto_dois), fora_de_ordem(texto_dois))
check("o C197 SOMA os dois C190: base 900+1500, ICMS 108+180",
      campo(ordem[i_a + 3], 5) == "2400,00"
      and campo(ordem[i_a + 3], 7) == "288,00", ordem[i_a + 3])
check("e a tela marca que aquela linha é soma, não valor de um C190",
      r_dois.ajustes[0].agrupados == 2, r_dois.ajustes[0])
check("o 1920 e o E110 recebem a mesma soma, sem dobrar",
      campo(por_reg(texto_dois, "1920")[0], 2) == "288,00"
      and campo(por_reg(texto_dois, "E110")[0], 7) == "288,00",
      (por_reg(texto_dois, "1920"), por_reg(texto_dois, "E110")))

# E a idempotência tem de sobreviver à soma: é a trinca somada que ela casa.
r_dois2 = aplicar(texto_dois)
check("segunda passada: reconhece o ajuste somado, não duplica",
      r_dois2.documento is None and len(r_dois2.ja_existiam) == 1
      and not r_dois2.aplicados,
      (r_dois2.documento, len(r_dois2.ja_existiam), len(r_dois2.aplicados)))

# Alíquotas diferentes não somam: base × alíquota deixaria de dar o ICMS.
c190_p = c190("000", "5106", "12", "1000,00", "900,00", "108,00")
c190_q = c190("020", "5106", "18", "2000,00", "1500,00", "270,00")
r_aliq = aplicar(montar_sped([c100(CHAVE_1, "1"), c190_p, c190_q]))
check("documento com duas alíquotas NÃO é ajustado", r_aliq.documento is None,
      r_aliq.ajustes)
check("e sai nomeado no aviso, com as combinações",
      any(CHAVE_1 in a and "12%" in a and "18%" in a for a in r_aliq.avisos),
      r_aliq.avisos)

# O caminho normal (um C190 só) continua copiando a string, sem reformatar.
check("documento de um C190 só copia os valores byte a byte",
      campo(c197_linhas[0], 5) == BC and campo(c197_linhas[0], 6) == ALIQ,
      c197_linhas[0])
check("e não é marcado como soma", resultado.ajustes[0].agrupados == 1)

# Um 1920 é "um por período" (Guia): dois ajustes NÃO viram dois registros.
linhas_1920 = por_reg(texto_dois, "1920")
check("um único registro 1920, mesmo com dois ajustes",
      len(linhas_1920) == 1 and len(por_reg(texto_dois, "1900")) == 1
      and len(por_reg(texto_dois, "1910")) == 1, linhas_1920)
check("e o campo 02 do 1920 SOMA os dois (108,00 + 180,00)",
      campo(linhas_1920[0], 2) == "288,00", linhas_1920[0])


# ============================================================================
print("\n=== 4. Nada fora do que foi pedido é tocado ===")

antes = linhas_de(sped)
depois = linhas_de(texto)
# O que muda de propósito: as linhas novas, os contadores, o IND_MOV do Bloco 1
# e a apuração. Todo o resto sai byte a byte como entrou.
MEXIDOS = ("|C195|", "|C197|", "|0460|", "|1001|", "|1900|", "|1910|",
           "|1920|", "|E110|", "|C990|", "|0990|", "|1990|", "|9900|",
           "|9990|", "|9999|")
inalteradas = [l for l in antes if not l.startswith(MEXIDOS)]
check("toda linha original fora dessa lista sai IDÊNTICA",
      all(l in depois for l in inalteradas),
      [l for l in inalteradas if l not in depois])
check("o C190 em si não foi reescrito (mesma string, byte a byte)",
      C190_6106 in depois, "C190 mudou")
check("o 1010 do Bloco 1 continua intocado (não se confunde com o COD_OBS)",
      "|1010|N|N|N|N|N|N|N|N|N|" in depois)
check("e o 1010 criado do zero tem os 14 campos do leiaute atual, não os 10 "
      "da versão 2.0.22 do Guia",
      len(por_reg(aplicar(montar_sped([c100(CHAVE_1, "1"), C190_5106],
                                      com_bloco_1=False)).documento.serializar(),
                  "1010")[0].split("|")) == 16)


# ============================================================================
print("\n=== 5. As duas pontas do estorno: Bloco 1 e E110 ===")

linha_1900 = por_reg(texto, "1900")[0]
linha_1910 = por_reg(texto, "1910")[0]
linha_1920 = por_reg(texto, "1920")[0]
linha_e110 = por_reg(texto, "E110")[0]

check("o 1900 sai com IND_APUR_ICMS = 3 (o 4º caractere do MG23000999)",
      campo(linha_1900, 2) == "3", linha_1900)
check("e com a descrição provisória, visível dentro do próprio arquivo",
      "NORMA LEGAL" in campo(linha_1900, 3), linha_1900)
check("o 1910 cobre exatamente o período do 0000, sem lacuna",
      (campo(linha_1910, 2), campo(linha_1910, 3)) == ("01042026", "30042026"),
      linha_1910)
check("o 1920 tem os 13 campos do leiaute, nenhum vazio",
      len(linha_1920.split("|")) == 15
      and all(c.strip() for c in linha_1920.split("|")[1:14]), linha_1920)
check("1920 campo 02 = o ICMS transferido para a sub-apuração",
      campo(linha_1920, 2) == ICMS, linha_1920)
check("1920 campo 09 (saldo apurado) = (02+03+04) - (05+06+07+08)",
      campo(linha_1920, 9) == ICMS, linha_1920)
check("1920 campo 11 (ICMS a recolher) = campo 09 - campo 10",
      campo(linha_1920, 11) == ICMS, linha_1920)
check("1920 campo 12 (saldo credor a transportar) zerado: fechou devedora",
      campo(linha_1920, 12) == "0,00", linha_1920)

check("o 1001 virou IND_MOV = 0 (bloco COM dados)",
      por_reg(texto, "1001") == ["|1001|0|"], por_reg(texto, "1001"))

check("E110 campo 07 (VL_AJ_CREDITOS) recebeu o mesmo ICMS",
      campo(linha_e110, 7) == ICMS, linha_e110)
check("E110 campo 06 (crédito das entradas) NÃO foi tocado — outro campo, "
      "outra natureza", campo(linha_e110, 6) == "0,00", linha_e110)
check("o saldo devedor da apuração normal zerou: o débito foi para a "
      "sub-apuração", campo(linha_e110, 11) == "0,00"
      and campo(linha_e110, 13) == "0,00", linha_e110)
check("e o imposto não sumiu: saiu do E110 e apareceu no 1920",
      valor(campo(linha_1920, 11)) == valor(ICMS))

registros_apuracao = {a.registro for a in resultado.apuracao}
check("a trilha mostra o que mudou fora do documento fiscal",
      {"0460", "1001", "1900", "1910", "1920", "E110"} <= registros_apuracao,
      registros_apuracao)
check("e nenhuma dessas alterações se disfarça de correção de documento",
      all(a.origem in ("sub-apuração", "apuração") for a in resultado.apuracao),
      {a.origem for a in resultado.apuracao})


# ============================================================================
print("\n=== 6. Colisão de 0460: o COD_OBS é do contribuinte ===")

# O arquivo real: o 1010 já estava ocupado por outro e-PTA.
OCUPADO = ("1010", "ESTORNO DE DEBITO e-PTA n. 45.000023946-42")
sped_colisao = montar_sped([c100(CHAVE_4, "77"), C190_5106], obs_0460=[OCUPADO])
r_colisao = aplicar(sped_colisao)
texto_colisao = r_colisao.documento.serializar()

linhas_0460 = por_reg(texto_colisao, "0460")
check("o 0460 que já existia continua lá, com a descrição dele",
      f"|0460|{OCUPADO[0]}|{OCUPADO[1]}|" in linhas_0460, linhas_0460)
check("e um SEGUNDO 0460 foi criado, com código diferente",
      len(linhas_0460) == 2 and campo(linhas_0460[1], 2) != "1010", linhas_0460)
codigo_novo = campo(linhas_0460[1], 2)
check("o código novo cabe no campo (COD_OBS é C 006)", len(codigo_novo) <= 6,
      codigo_novo)
check("o C195 aponta para o código NOVO, não para o 1010 alheio",
      campo(por_reg(texto_colisao, "C195")[0], 2) == codigo_novo,
      por_reg(texto_colisao, "C195")[0])
check("e a tela avisa que houve a troca",
      any("já estava em uso" in a for a in r_colisao.avisos), r_colisao.avisos)

# A armadilha que fecha o ciclo: com o código trocado, a segunda passada ainda
# tem de reconhecer o próprio trabalho — senão o débito duplica justamente no
# arquivo em que o 1010 estava ocupado.
r_colisao2 = aplicar(texto_colisao)
check("a segunda passada reconhece o ajuste mesmo com o código trocado",
      r_colisao2.documento is None and len(r_colisao2.ja_existiam) == 1,
      (r_colisao2.documento, r_colisao2.ja_existiam))

# E um arquivo que JÁ tem o 0460 desta ferramenta não ganha um segundo igual.
JA_TEM = ("SBAP01", "DEBITO PARA SUB-APURAÇÃO")
r_reuso = aplicar(montar_sped([c100(CHAVE_4, "78"), C190_5106],
                              obs_0460=[OCUPADO, JA_TEM]))
texto_reuso = r_reuso.documento.serializar()
check("0460 já declarado é REUSADO, não duplicado",
      len(por_reg(texto_reuso, "0460")) == 2, por_reg(texto_reuso, "0460"))
check("e o C195 aponta para ele",
      campo(por_reg(texto_reuso, "C195")[0], 2) == "SBAP01",
      por_reg(texto_reuso, "C195")[0])


# ============================================================================
print("\n=== 7. Idempotência: rodar duas vezes não duplica nada ===")

sped_ajustado = resultado.documento.serializar()
resultado2 = aplicar(sped_ajustado)
check("a segunda passada não gera arquivo novo (nada mudou)",
      resultado2.documento is None)
check("o C190 é reconhecido como já ajustado",
      len(resultado2.ja_existiam) == 1 and len(resultado2.aplicados) == 0,
      (len(resultado2.ja_existiam), len(resultado2.aplicados)))
check("com os mesmos valores relatados (só marcado 'já existia')",
      resultado2.ja_existiam[0].icms == ICMS
      and resultado2.ja_existiam[0].ja_existia is True)
check("e nada é lançado na apuração de novo", not resultado2.apuracao,
      resultado2.apuracao)

check("e o arquivo continua com UM C195 e UM C197 — não dois",
      len(por_reg(sped_ajustado, "C195")) == 1
      and len(por_reg(sped_ajustado, "C197")) == 1)
check("um 0460, um 1900, um 1910, um 1920 — nenhum duplicado",
      all(len(por_reg(sped_ajustado, r)) == 1
          for r in ("0460", "1900", "1910", "1920")))

# O erro de dinheiro que a idempotência existe para impedir: o débito na
# sub-apuração e o crédito no E110 não podem dobrar.
check("o 1920 continua com o valor de UMA nota, não de duas",
      campo(por_reg(sped_ajustado, "1920")[0], 2) == ICMS,
      por_reg(sped_ajustado, "1920")[0])
check("e o E110 campo 07 também",
      campo(por_reg(sped_ajustado, "E110")[0], 7) == ICMS,
      por_reg(sped_ajustado, "E110")[0])

# Dois documentos diferentes, cada um com seu C190 alvo: o 1920 é do PERÍODO,
# não do documento — soma os dois e continua sendo um só.
sped_dois_docs = aplicar(montar_sped([c100(CHAVE_1, "1"), c190_a,
                                      c100(CHAVE_2, "2"), c190_b]))
check("dois documentos: os dois entram de uma vez",
      len(sped_dois_docs.aplicados) == 2)
check("e o 1920 do período soma os dois",
      campo(por_reg(sped_dois_docs.documento.serializar(), "1920")[0], 2)
      == "288,00")


# ============================================================================
print("\n=== 8. O esqueleto do Bloco 1 e o E110 que faltam ===")

sped_sem_1 = montar_sped([c100(CHAVE_1, "9"), C190_5106], com_bloco_1=False)
r_sem_1 = aplicar(sped_sem_1)
texto_sem_1 = r_sem_1.documento.serializar()
check("arquivo sem Bloco 1 ganha 1001/1010/1990",
      por_reg(texto_sem_1, "1001") == ["|1001|0|"]
      and len(por_reg(texto_sem_1, "1010")) == 1
      and len(por_reg(texto_sem_1, "1990")) == 1,
      [l for l in linhas_de(texto_sem_1) if l.startswith("|1")])
check("e o 1010 nasce com N em tudo, avisando que a resposta é do cliente",
      por_reg(texto_sem_1, "1010") == ["|1010|" + "N|" * 13]
      and any("1010" in a and '"N"' in a for a in r_sem_1.avisos),
      r_sem_1.avisos)
check("o Bloco 1 sai na ordem do leiaute: 1001, 1010, 1900, 1910, 1920, 1990",
      [l.split("|")[1] for l in linhas_de(texto_sem_1) if l.split("|")[1][0] == "1"
       and l.split("|")[1].isdigit()]
      == ["1001", "1010", "1900", "1910", "1920", "1990"],
      [l for l in linhas_de(texto_sem_1) if l.startswith("|1")])
check("e o Bloco 1 fica ANTES do Bloco 9",
      linhas_de(texto_sem_1).index("|1001|0|")
      < linhas_de(texto_sem_1).index("|9001|0|"))

# Empresa que JÁ tem outra sub-apuração escriturada: a nossa entra ao lado,
# sem invadir o grupo alheio e sem reescrever o 1920 dele.
OUTRA_SUB = ["|1900|4|APURACAO EM SEPARADO 2|", "|1910|01042026|30042026|",
             "|1920|500,00|0,00|0,00|0,00|0,00|0,00|0,00|500,00|0,00|500,00|"
             "0,00|0,00|"]
r_vizinha = aplicar(montar_sped([c100(CHAVE_1, "9"), C190_5106],
                                bloco_1_extra=OUTRA_SUB))
texto_vizinha = r_vizinha.documento.serializar()
check("a sub-apuração 2 do cliente sai byte a byte como entrou",
      all(l in linhas_de(texto_vizinha) for l in OUTRA_SUB),
      [l for l in OUTRA_SUB if l not in linhas_de(texto_vizinha)])
check("e a nossa (IND_APUR_ICMS 3) é criada ao lado, não por cima",
      len(por_reg(texto_vizinha, "1900")) == 2
      and len(por_reg(texto_vizinha, "1920")) == 2,
      por_reg(texto_vizinha, "1900"))
bloco_1_vizinha = [l.split("|")[1] for l in linhas_de(texto_vizinha)
                   if l.split("|")[1].isdigit() and l.split("|")[1][0] == "1"]
check("cada 1900 fica com o SEU 1910 e o SEU 1920 (grupos não se misturam)",
      bloco_1_vizinha == ["1001", "1010", "1900", "1910", "1920",
                          "1900", "1910", "1920", "1990"], bloco_1_vizinha)
check("e o 1920 novo leva só o ICMS desta ferramenta, não o dos 500,00 alheios",
      campo(por_reg(texto_vizinha, "1920")[0], 2) == ICMS,
      por_reg(texto_vizinha, "1920"))

sped_sem_e = montar_sped([c100(CHAVE_1, "9"), C190_5106], com_bloco_e=False)
r_sem_e = aplicar(sped_sem_e)
check("arquivo sem E110: o ajuste sai, mas a apuração não é inventada",
      r_sem_e.documento is not None
      and not por_reg(r_sem_e.documento.serializar(), "E110"))
check("e o motivo aparece nos avisos, com o valor que ficou de fora",
      any("não tem registro E110" in a and ICMS in a
          for a in r_sem_e.avisos), r_sem_e.avisos)
check("o Bloco 1 é montado do mesmo jeito, sem depender do E110",
      campo(por_reg(r_sem_e.documento.serializar(), "1920")[0], 2) == ICMS)

# Apuração que não fecha pela fórmula do Guia: o motor não reescreve o número
# de ninguém — regra herdada de `cascata.atualizar_e110`, e é ela que vale.
sped_torto = montar_sped([c100(CHAVE_1, "9"), C190_5106]).replace(
    "|" + texto_valor(valor(ICMS)) + "|0,00|0,00|0,00|0,00|0,00|0,00|0,00|0,00|",
    "|9999,99|0,00|0,00|0,00|0,00|0,00|0,00|0,00|0,00|")
r_torto = aplicar(sped_torto)
check("E110 que não fecha pela fórmula do Guia não é reescrito",
      any("não fecha pela fórmula" in a for a in r_torto.avisos), r_torto.avisos)
check("e mesmo assim o par C195/C197 é entregue",
      len(r_torto.aplicados) == 1 and r_torto.documento is not None)


# ============================================================================
print("\n=== 8b. As guias mudam de apuração — e o módulo não as inventa ===")

# O estorno tira o débito do E110 e põe no 1920. Com isso a E116 (guia da
# apuração normal) passa a divergir, e falta o 1926 (guia da sub-apuração).
# Os dois pedem código de receita e vencimento, que não estão no SPED — o
# módulo mede a diferença e mostra, em vez de inventar quanto e quando pagar.
r_guias = aplicar(montar_sped([c100(CHAVE_1, "9"), C190_5106], com_e116=True))
texto_guias = r_guias.documento.serializar()

check("a E116 antiga NÃO é reescrita (é obrigação de pagamento, não conta)",
      campo(por_reg(texto_guias, "E116")[0], 3) == ICMS,
      por_reg(texto_guias, "E116"))
check("mas a divergência com o E110 aparece, com os dois números",
      any("E116" in a and ICMS in a and "0,00" in a for a in r_guias.avisos),
      r_guias.avisos)
check("e o 1926 que falta também, com o valor da guia da sub-apuração",
      any("1926" in a and ICMS in a for a in r_guias.avisos), r_guias.avisos)
check("nenhum 1926 é criado com código de receita chutado",
      not por_reg(texto_guias, "1926"))

# Arquivo cuja sub-apuração fecha CREDORA não tem guia a discriminar.
r_sem_guia = aplicar(montar_sped([c100(CHAVE_1, "9"), C190_5106],
                                 bloco_1_extra=[
                                     "|1900|3|NORMA X|",
                                     "|1910|01042026|30042026|",
                                     "|1920|0,00|0,00|0,00|0,00|0,00|0,00|"
                                     "99999,00|0,00|0,00|0,00|99999,00|0,00|"]))
check("sub-apuração credora não cobra 1926",
      not any("1926" in a for a in r_sem_guia.avisos), r_sem_guia.avisos)
check("e o saldo credor anterior do 1920 que já existia é PRESERVADO",
      campo(por_reg(r_sem_guia.documento.serializar(), "1920")[0], 8)
      == "99999,00", por_reg(r_sem_guia.documento.serializar(), "1920"))


# ============================================================================
print("\n=== 9. Sem correspondência nenhuma, ou arquivo vazio/lixo ===")

check("SPED sem CFOP 5106/6106 não gera nada", r_6124.documento is None)
check("string vazia não derruba a função", aplicar("").documento is None)
check("texto que não é SPED não derruba a função",
      aplicar("isto nao e um sped\nde jeito nenhum").documento is None)


# ============================================================================
print("\n=== 10. C190 órfão (fora de um C100) não derruba o processamento ===")

sped_orfao = "\r\n".join(["|0000|018|0|01042026|30042026|X|11222333000181|"
                          "MG|123456789|3106200|||A|1|", "|0990|2|",
                          C190_6106]) + "\r\n"
r_orfao = aplicar(sped_orfao)
check("nenhum crash, e nenhum ajuste aplicado", r_orfao.documento is None)
check("e o motivo aparece nos avisos",
      any("não está dentro de um C100" in a for a in r_orfao.avisos),
      r_orfao.avisos)


# ============================================================================
print("\n=== 11. Os totalizadores fecham — o leitor fiel relê sem perder linha ===")

relido = ler_texto(texto)
check("o leitor fiel relê o arquivo ajustado sem perder linha nenhuma",
      len(relido.registros) == len(linhas_de(texto)))
c100_relido = relido.por_reg("C100")[0]
check("e reconhece a hierarquia: C190/C195 filhos do C100",
      len(relido.filhos_reg(c100_relido, "C190")) == 1
      and len(relido.filhos_reg(c100_relido, "C195")) == 1)
c195_relido = relido.filhos_reg(c100_relido, "C195")[0]
check("e o C197 filho do C195 (nível 4, Guia 3.2.2)",
      len(relido.filhos_reg(c195_relido, "C197")) == 1)

r1900 = relido.por_reg("1900")[0]
check("e a do Bloco 1: 1910 filho do 1900, 1920 filho do 1910",
      len(relido.filhos_reg(r1900, "1910")) == 1
      and len(relido.filhos_reg(relido.filhos_reg(r1900, "1910")[0],
                                "1920")) == 1)


def conta_bloco(texto_sped, abertura, fechamento):
    linhas_sped = linhas_de(texto_sped)
    inicio = next(i for i, l in enumerate(linhas_sped)
                  if l.startswith(f"|{abertura}|"))
    fim = next(i for i, l in enumerate(linhas_sped)
               if l.startswith(f"|{fechamento}|"))
    return fim - inicio + 1, int(campo(linhas_sped[fim], 2))


for abertura, fechamento in (("0000", "0990"), ("C001", "C990"),
                             ("1001", "1990")):
    real, declarado = conta_bloco(texto, abertura, fechamento)
    check(f"{fechamento} conta exatamente {abertura}..{fechamento} (inclusive)",
          real == declarado, (real, declarado))

linhas_finais = linhas_de(texto)
check("9999 conta o arquivo inteiro",
      int(campo(por_reg(texto, "9999")[0], 2)) == len(linhas_finais))
tipos_9900 = {campo(l, 2): int(campo(l, 3)) for l in por_reg(texto, "9900")}
check("existe uma linha 9900 para cada registro NOVO",
      all(tipos_9900.get(r) == 1
          for r in ("C195", "C197", "0460", "1900", "1910", "1920")),
      tipos_9900)
check("e a contagem de cada tipo bate com o arquivo",
      all(quantidade == len(por_reg(texto, reg))
          for reg, quantidade in tipos_9900.items()
          if reg not in ("9900", "9990", "9999")),
      {r: (q, len(por_reg(texto, r))) for r, q in tipos_9900.items()
       if q != len(por_reg(texto, r))})


# ============================================================================
print("\n=== 12. A tela: RBAC, upload, download forçado ===")

import json  # noqa: E402
import types  # noqa: E402
import uuid as _uuid  # noqa: E402

import dotenv  # noqa: E402
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main  # noqa: E402
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_ajuste_sped_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_ajuste_sped_test.db"):
    os.remove("_ajuste_sped_test.db")

from sqlalchemy import types as _satypes  # noqa: E402
import sqlalchemy.dialects.postgresql as pg  # noqa: E402


class _UUIDShim(_satypes.TypeDecorator):
    impl = _satypes.Uuid
    cache_ok = True

    def __init__(self, *a, **kw):
        kw.pop('as_uuid', None)
        super().__init__()

    def process_bind_param(self, value, dialect):
        return _uuid.UUID(value) if isinstance(value, str) else value


pg.UUID = _UUIDShim

import backend.app.infrastructure.auth.supabase_client as sb  # noqa: E402
TOKENS = {}


class _Auth:
    def get_user(self, token):
        u = TOKENS.get(token.replace("Bearer ", "").strip())
        if not u:
            raise Exception("invalid")
        return types.SimpleNamespace(user=u)


class _Client:
    auth = _Auth()


sb.get_supabase_client = lambda: _Client()
sb.get_supabase_admin_client = lambda: _Client()
import backend.app.api.dependencies.auth as dep_auth  # noqa: E402
dep_auth.get_supabase_client = lambda: _Client()

from backend.app.infrastructure.database.session import engine as _eng  # noqa: E402
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit("ABORTADO: engine nao e SQLite")

import backend.app.main as m  # noqa: E402
from backend.app.infrastructure.database.session import SessionLocal  # noqa: E402
from backend.app.domain.models.base import Base  # noqa: E402
from backend.app.domain.models.auth_models import (  # noqa: E402
    Tenant, UserProfile, UserTenant)
from fastapi.testclient import TestClient  # noqa: E402

Base.metadata.create_all(bind=_eng)
db = SessionLocal()
tA = _uuid.uuid4()
db.add(Tenant(id=tA, name="Empresa A", cnpj="11111111111111"))
u_adm, u_ope, u_cli = _uuid.uuid4(), _uuid.uuid4(), _uuid.uuid4()
db.add_all([
    UserProfile(id=u_adm, tenant_id=None, role="admin", name="Admin"),
    UserProfile(id=u_ope, tenant_id=tA, role="operador", name="Operador"),
    UserProfile(id=u_cli, tenant_id=tA, role="cliente", name="Cliente"),
])
db.add(UserTenant(user_id=u_ope, tenant_id=tA))
db.commit()
db.close()

TOKENS["adm"] = types.SimpleNamespace(id=str(u_adm), email="admin@synax.com")
TOKENS["ope"] = types.SimpleNamespace(id=str(u_ope), email="oper@synax.com")
TOKENS["cli"] = types.SimpleNamespace(id=str(u_cli), email="cli@synax.com")

from backend.app.api.dependencies.db import get_rls_db as _real_rls  # noqa: E402


def _fake_rls():
    ss = SessionLocal()
    try:
        yield ss
    finally:
        ss.close()


m.app.dependency_overrides[_real_rls] = _fake_rls
c = TestClient(m.app, raise_server_exceptions=False)


def as_(tok):
    c.cookies.clear()
    if tok:
        c.cookies.set("access_token", f"Bearer {tok}")
    return c


r = as_("adm").get("/ajuste-sped")
check("admin abre a tela", r.status_code == 200, r.status_code)
check("operador também", as_("ope").get("/ajuste-sped").status_code == 200)
check("cliente é barrado (ferramenta interna)",
      as_("cli").get("/ajuste-sped").status_code == 403)
check("anônimo também", as_(None).get("/ajuste-sped").status_code in (401, 403))
check("nenhum seletor de empresa na tela (ferramenta não usa tenant)",
      'name="tenant_id"' not in r.text)

sped_upload = montar_sped([c100(CHAVE_2, "500"), C190_5106])
r_post = as_("ope").post(
    "/api/ajuste-sped",
    files={"sped_file": ("sped.txt", sped_upload.encode("latin-1"), "text/plain")})
check("upload com correspondência responde 200", r_post.status_code == 200,
      r_post.status_code)
check("mostra 1 registro ajustado", "1" in r_post.text and "inseridos" in r_post.text)
check("e oferece o download", "/arquivo" in r_post.text)

# A mudança de apuração é dinheiro: não pode ficar só no arquivo baixado.
check("a tela mostra que a apuração foi mexida, ANTES do botão de baixar",
      "E110" in r_post.text and "1920" in r_post.text, r_post.text[-3000:])
check("e mostra os avisos do que ficou por preencher",
      "NORMA LEGAL" in r_post.text)

import re as _re  # noqa: E402
achado = _re.search(r"/api/ajuste-sped/([0-9a-f-]{36})/arquivo", r_post.text)
check("o link do arquivo traz o identificador", achado is not None, r_post.text[:200])

r_arq = as_("ope").get(f"/api/ajuste-sped/{achado.group(1)}/arquivo")
check("o download responde 200", r_arq.status_code == 200, r_arq.status_code)
check("como arquivo, nunca renderizado",
      r_arq.headers.get("content-type") == "application/octet-stream"
      and r_arq.headers.get("content-disposition", "").startswith("attachment;")
      and r_arq.headers.get("x-content-type-options") == "nosniff")
conteudo_baixado = r_arq.content.decode("latin-1")
check("e o conteúdo tem o C197 certo",
      "|C197|MG23000999|DEBITO PARA SUB-APURAÇÃO||24944,1|7|1746,08||"
      in conteudo_baixado, conteudo_baixado)
check("e o Bloco 1 da sub-apuração junto",
      "|1900|3|" in conteudo_baixado and "|1910|01042026|30042026|"
      in conteudo_baixado, conteudo_baixado)

check("cliente não baixa",
      as_("cli").get(f"/api/ajuste-sped/{achado.group(1)}/arquivo").status_code == 403)
check("identificador que não é UUID não vira leitura de arquivo do servidor",
      as_("ope").get("/api/ajuste-sped/..%2F..%2Fmain/arquivo").status_code
      in (404, 400))
check("cliente não processa upload pela rota direta",
      as_("cli").post("/api/ajuste-sped",
                      files={"sped_file": ("s.txt", b"x", "text/plain")}).status_code
      == 403)

sped_sem_match = montar_sped([c100(CHAVE_3, "1"), C190_6124])
r_sem = as_("adm").post(
    "/api/ajuste-sped",
    files={"sped_file": ("s.txt", sped_sem_match.encode("latin-1"), "text/plain")})
check("upload SEM correspondência responde 200 com aviso, não 4xx",
      r_sem.status_code == 200 and "5106" in r_sem.text, r_sem.status_code)
check("e não oferece download (nada foi gerado)", "/arquivo" not in r_sem.text)

TELA = io.open(os.path.join("frontend", "templates", "pages", "dashboard.html"),
              encoding="utf-8").read()
check("o dashboard leva para a ferramenta",
      "/ajuste-sped" in TELA and "sub-apuração" in TELA.lower())

import shutil as _shutil  # noqa: E402
_shutil.rmtree(os.path.join(os.getcwd(), "temp_uploads", achado.group(1)),
              ignore_errors=True)
_eng.dispose()
if os.path.exists("_ajuste_sped_test.db"):
    try:
        os.remove("_ajuste_sped_test.db")
    except OSError:
        pass


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
for f in _falhou:
    print("  -", f)
sys.exit(1 if FALHAS else 0)
