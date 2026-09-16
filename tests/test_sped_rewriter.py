# -*- coding: utf-8 -*-
"""Motor de reescrita do SPED: refazer a cascata sem tocar no resto do arquivo.

O módulo aplica no arquivo as correções que a curadoria aprovou e a pessoa
aceitou. Reescrever SPED é diferente de auditar SPED: aqui um campo mexido a
mais rejeita o arquivo inteiro no PVA, e o cliente descobre isso na entrega.

As travas que esta suíte protege, em ordem de importância:

1. **Campo não tocado sai byte a byte como entrou.** É por isso que o motor
   tem leitura própria e não usa o `sped_parser` da auditoria — aquele lê 15
   dos 38 campos do C170 e converte tudo para float. Patch vazio tem de
   devolver o arquivo idêntico, incluindo os campos vazios (`||` não é
   `|0,00|` para o PVA).

2. **O C190 é reconstruído, não editado.** A chave é (CST, CFOP, ALIQ);
   trocar o CFOP de um item o MOVE de grupo. O grupo que esvazia sai do
   arquivo, o grupo que nasce entra, e as somas dos campos 06 a 09 e 11 são
   as dos C170 daquela combinação — validação explícita do Guia 3.2.2.

3. **Os contadores de linha acompanham.** Apagar um C190 muda o C990 e o 9999.
   Duas exceções simétricas que o Guia escreve com todas as letras e que erram
   por um: o 9990 conta o 9999 ("apesar de não pertencer ao Bloco 9"), e o
   0990 conta o 0000 ("mesmo não pertencendo ao bloco 0").

4. **Correção inadmissível é recusada inteira, e em voz alta.** "O primeiro
   caractere deve ser o mesmo para todos os itens de um documento fiscal"
   (C170 campo 11) — e as regras do Synax são POR ITEM, então é a trava mais
   fácil de violar sem perceber. Metade aplicada é pior que nada aplicado.

5. **Dinheiro é Decimal.** `0.1 + 0.2` não fecha centavo e o PVA compara soma
   exata.

6. **VL_DOC não é reescrito.** É o valor de face da nota; correção de tributo
   não muda o que o fornecedor cobrou. Divergência vira aviso para decisão
   humana, nunca gravação silenciosa.
"""
import io
import json
import os
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, os.path.abspath('.'))

from backend.app.services.sped_rewriter import (  # noqa: E402
    CorrecaoFrete, CorrecaoItem, Patch, aplicar, ler, ler_texto,
    linhas_para_planilha, montar, nome_sugerido, para_json, prever, texto,
    texto_da_previa)
from backend.app.services.sped_rewriter.cascata import (  # noqa: E402
    reconstruir_bloco_9, reconstruir_totais_de_blocos)
from backend.app.services.sped_rewriter.patch import (  # noqa: E402
    CAMPOS_C100, CAMPOS_C170, CAMPOS_D100, CAMPOS_D190, OPCOES_DE_CORRECAO,
    decimal_para_texto, texto_para_decimal)

# O C190 não tem correção manual: ele é RECONSTRUÍDO a partir dos itens. Fica
# como dicionário vazio para a conferência das opções não precisar de exceção.
CAMPOS_C190_EDITAVEIS: dict = {}

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
# Um SPED mínimo, mas com os contadores CERTOS — é isso que permite provar que
# a contagem do módulo bate com a de um arquivo bem formado.
# ============================================================================
CHAVE = "35260827865757000102550010000123451000012345"


def linha(reg: str, campos: dict, total: int) -> str:
    """Monta a linha com `total` campos, preenchendo o resto com vazio."""
    valores = [""] * total
    valores[0] = reg
    for numero, valor in campos.items():
        valores[numero - 1] = valor
    return "|" + "|".join(valores) + "|"


def c170(num_item, cod_item, vl_item, cst, cfop, aliq="", bc="", icms="",
         ipi="", bc_ipi="", vl_desc="", cst_ipi="", aliq_ipi="", st="",
         bc_st=""):
    return linha("C170", {
        2: num_item, 3: cod_item, 5: "10,00000", 6: "UN", 7: vl_item,
        8: vl_desc, 9: "0", 10: cst, 11: cfop, 13: bc, 14: aliq, 15: icms,
        16: bc_st, 18: st, 20: cst_ipi, 22: bc_ipi, 23: aliq_ipi, 24: ipi,
    }, 38)


def c190(cst, cfop, aliq, opr, bc, icms, ipi="0,00", red="0,00"):
    return linha("C190", {2: cst, 3: cfop, 4: aliq, 5: opr, 6: bc, 7: icms,
                          8: "0,00", 9: "0,00", 10: red, 11: ipi}, 12)


def montar_sped(itens, analiticos, vl_doc, vl_bc, vl_icms, vl_merc=None,
                vl_frt="0,00"):
    """Arquivo completo com Bloco 0, C, 9 e todos os contadores corretos."""
    corpo_c100 = linha("C100", {
        2: "0", 3: "1", 4: "FORN001", 5: "55", 6: "00", 7: "1", 8: "12345",
        9: CHAVE, 10: "12042026", 11: "12042026", 12: vl_doc, 13: "0",
        14: "0,00", 15: "0,00", 16: vl_merc or vl_doc, 17: "9", 18: vl_frt,
        19: "0,00", 20: "0,00", 21: vl_bc, 22: vl_icms, 23: "0,00",
        24: "0,00", 25: "0,00", 26: "0,00", 27: "0,00", 28: "0,00",
        29: "0,00"}, 29)
    bloco_c = ["|C001|0|", corpo_c100] + itens + analiticos
    bloco_c.append(f"|C990|{len(bloco_c) + 1}|")

    bloco_0 = ["|0000|018|0|01042026|30042026|COMERCIAL MASSUCATTI LTDA|"
               "27865757000102|ES|123456789|3205309|||A|1|",
               "|0001|0|"]
    bloco_0.append(f"|0990|{len(bloco_0) + 1}|")   # o 0000 entra na conta

    # Bloco 9: uma linha 9900 por tipo, inclusive para 9900/9990/9999.
    tipos = {}
    for l in bloco_0 + bloco_c:
        reg = l.split("|")[1]
        tipos[reg] = tipos.get(reg, 0) + 1
    tipos["9001"] = 1
    total_9900 = len(tipos) + 3
    tipos["9900"] = total_9900
    tipos["9990"] = 1
    tipos["9999"] = 1

    # As 9900 saem na ORDEM DOS REGISTROS NO ARQUIVO — que é como os geradores
    # reais escrevem, com os 9xxx no fim — e não em ordem alfabética. É essa
    # diferença que pega o motor reordenando linha que ninguém mandou mexer:
    # num SPED de julho/2026 do escritório, a reordenação sozinha reescrevia
    # três linhas do Bloco 9 numa correção que não mudou contagem nenhuma.
    bloco_9 = ["|9001|0|"]
    bloco_9 += [f"|9900|{reg}|{qtd}|" for reg, qtd in tipos.items()]
    bloco_9.append(f"|9990|{1 + total_9900 + 1 + 1}|")

    linhas = bloco_0 + bloco_c + bloco_9
    linhas.append(f"|9999|{len(linhas) + 1}|")
    return "\r\n".join(linhas) + "\r\n"


ITENS = [
    # O item 1 tem VL_DESC VAZIO de propósito: é a prova de que campo em branco
    # não vira "0,00" ao passar pelo motor.
    c170("1", "CAFE500", "1000,00", "000", "1102", "12,00", "1000,00", "120,00"),
    c170("2", "ACUCAR1", "500,00", "000", "1102", "12,00", "500,00", "60,00"),
    # O item 3 é o que será corrigido: entrou como 1556 (sem crédito) e a
    # curadoria aprovou 1102 (revenda, com crédito).
    c170("3", "OLEO900", "300,00", "060", "1556"),
]
ANALITICOS = [
    c190("000", "1102", "12,00", "1500,00", "1500,00", "180,00"),
    c190("060", "1556", "0,00", "300,00", "0,00", "0,00"),
]
SPED = montar_sped(ITENS, ANALITICOS, "1800,00", "1500,00", "180,00")


def linhas_de(texto):
    return [l for l in texto.split("\r\n") if l]


def por_reg(texto, reg):
    return [l for l in linhas_de(texto) if l.startswith(f"|{reg}|")]


CORRECAO = CorrecaoItem(
    documento=CHAVE, num_item="3",
    campos={"CST_ICMS": "000", "CFOP": "1102", "ALIQ_ICMS": "12,00",
            "VL_BC_ICMS": Decimal("300.00"), "VL_ICMS": Decimal("36.00")},
    motivo="revenda, não consumo — regra de CFOP de entrada",
    origem="regra aprovada #412", aprovada_por="joao@vm.com.br")


# ============================================================================
print("\n=== 1. Leitura fiel: o que entra tem de sair igual ===")

doc = ler_texto(SPED)
check("o arquivo volta byte a byte igual sem correção nenhuma",
      doc.serializar() == SPED,
      f"{len(doc.serializar())} vs {len(SPED)} caracteres")

check("a hierarquia amarra os itens ao documento",
      len(doc.filhos_reg(doc.por_reg("C100")[0], "C170")) == 3)
check("e os analíticos também",
      len(doc.filhos_reg(doc.por_reg("C100")[0], "C190")) == 2)

item1 = doc.por_reg("C170")[0]
check("os 38 campos do C170 sobrevivem à leitura", len(item1.campos) == 38,
      len(item1.campos))
check("campo vazio continua vazio (|| não é |0,00|)", item1.campo(8) == "",
      repr(item1.campo(8)))
check("o CRLF é preservado", doc.fim_padrao == "\r\n", repr(doc.fim_padrao))

vazio = ler_texto(SPED)
resultado_vazio = aplicar(vazio, Patch([]))
check("patch vazio não altera nada", not resultado_vazio.alteracoes)
check("e o arquivo continua idêntico", vazio.serializar() == SPED)


# ============================================================================
print("\n=== 2. Os contadores do módulo batem com um arquivo bem formado ===")

conferencia = ler_texto(SPED)
mudou = reconstruir_totais_de_blocos(conferencia) + reconstruir_bloco_9(conferencia)
check("recontar um arquivo correto não muda nada", not mudou,
      [f"{a.registro}.{a.campo}: {a.de}->{a.para}" for a in mudou])
check("e o texto continua idêntico — inclusive a ORDEM das linhas 9900",
      conferencia.serializar() == SPED,
      "o motor reordenou o Bloco 9 num arquivo que não precisava mudar")

ordem_original = [l.split("|")[2] for l in por_reg(SPED, "9900")]
ordem_saida = [l.split("|")[2] for l in por_reg(conferencia.serializar(), "9900")]
check("a ordem das 9900 do arquivo é preservada, não alfabetizada",
      ordem_saida == ordem_original and ordem_original != sorted(ordem_original),
      f"{ordem_saida} vs {ordem_original}")


# ============================================================================
print("\n=== 3. Trocar o CFOP do item refaz o analítico ===")

doc = ler_texto(SPED)
resultado = aplicar(doc, Patch([CORRECAO]))
saida = doc.serializar()

check("a correção não foi recusada", not resultado.recusas,
      [r.motivo for r in resultado.recusas])

analiticos = por_reg(saida, "C190")
check("sobrou um único C190 — os três itens ficaram na mesma combinação",
      len(analiticos) == 1, analiticos)
if analiticos:
    campos = analiticos[0].split("|")
    check("com CST 000 e CFOP 1102", campos[2] == "000" and campos[3] == "1102",
          analiticos[0])
    check("VL_BC_ICMS é a soma dos itens (1000+500+300)", campos[6] == "1800,00",
          campos[6])
    check("VL_ICMS é a soma dos itens (120+60+36)", campos[7] == "216,00",
          campos[7])
    check("VL_OPR não mudou — nenhum valor de mercadoria foi tocado",
          campos[5] == "1800,00", campos[5])

check("o grupo 060/1556 saiu do arquivo",
      not [l for l in analiticos if "|1556|" in l], analiticos)
check("e a saída foi registrada como cascata, não como pedido do usuário",
      any(a.registro == "C190" and a.origem == "cascata" and a.para == ""
          for a in resultado.alteracoes))

c100_saida = por_reg(saida, "C100")[0].split("|")
check("o C100 fecha com a soma dos analíticos: VL_BC_ICMS",
      c100_saida[21] == "1800,00", c100_saida[21])
check("o C100 fecha com a soma dos analíticos: VL_ICMS",
      c100_saida[22] == "216,00", c100_saida[22])
check("VL_DOC não foi reescrito — é o valor de face da nota",
      c100_saida[12] == "1800,00", c100_saida[12])
check("e não houve aviso, porque a soma dos VL_OPR continua batendo",
      not resultado.avisos, resultado.avisos)

item3 = [l for l in por_reg(saida, "C170") if l.startswith("|C170|3|")][0]
check("o item corrigido tem o CFOP novo", item3.split("|")[11] == "1102")
check("e o crédito que estava vazio agora está preenchido",
      item3.split("|")[15] == "36,00", item3.split("|")[15])


# ============================================================================
print("\n=== 4. O resto do arquivo não foi tocado ===")

antes = linhas_de(SPED)
depois = set(linhas_de(saida))

# Tudo que a correção não envolve tem de aparecer IDÊNTICO na saída.
intocados = [l for l in antes
             if not l.startswith(("|C100|", "|C170|3|", "|C190|", "|C990|",
                                  "|9990|", "|9999|"))
             and not l.startswith("|9900|C190|")]
faltando = [l for l in intocados if l not in depois]
check("nenhuma linha alheia à correção mudou", not faltando,
      (faltando[:1] or [""])[0][:120])
check("inclusive os dois itens que não foram corrigidos",
      all(l in depois for l in (ITENS[0], ITENS[1])))
check("o campo VL_DESC vazio do item 1 continua vazio",
      ITENS[0] in depois and ITENS[0].split("|")[8] == "")
check("o cabeçalho 0000 é o mesmo", antes[0] in depois)


# ============================================================================
print("\n=== 5. Os contadores de linha acompanham ===")

c990 = por_reg(saida, "C990")[0]
check("o C990 caiu de 8 para 7 linhas (um C190 a menos)",
      c990 == "|C990|7|", c990)

conta_9900 = {l.split("|")[2]: l.split("|")[3] for l in por_reg(saida, "9900")}
check("o 9900 do C190 caiu para 1", conta_9900.get("C190") == "1",
      conta_9900.get("C190"))
check("o 9900 do C170 continua 3", conta_9900.get("C170") == "3")
check("existe linha 9900 para o próprio 9900",
      conta_9900.get("9900") == str(len(conta_9900)), conta_9900.get("9900"))
check("e para o 9990 e o 9999",
      conta_9900.get("9990") == "1" and conta_9900.get("9999") == "1")

qtd_9 = int(por_reg(saida, "9990")[0].split("|")[2])
esperado_9 = 1 + len(conta_9900) + 1 + 1     # 9001 + as 9900 + 9990 + 9999
check("o 9990 conta o 9999, apesar de ele não ser do Bloco 9",
      qtd_9 == esperado_9, f"{qtd_9} != {esperado_9}")

total = int(por_reg(saida, "9999")[0].split("|")[2])
check("o 9999 é o total de linhas do arquivo",
      total == len(linhas_de(saida)), f"{total} != {len(linhas_de(saida))}")
check("e o arquivo encolheu exatamente uma linha",
      len(linhas_de(saida)) == len(antes) - 1,
      f"{len(linhas_de(saida))} vs {len(antes)}")

zero = ler_texto(saida)
check("recontar a saída não acha mais nada para mudar",
      not (reconstruir_totais_de_blocos(zero) + reconstruir_bloco_9(zero)))


# ============================================================================
print("\n=== 6. Correção inadmissível é recusada INTEIRA e em voz alta ===")

doc = ler_texto(SPED)
misto = aplicar(doc, Patch([CorrecaoItem(
    documento=CHAVE, num_item="3", campos={"CFOP": "2102"},
    motivo="interestadual")]))
check("mudar o primeiro dígito de um item só é recusado",
      len(misto.recusas) == 1, misto.recusas)
check("com o motivo em português, citando a regra",
      misto.recusas and "mesmo dígito" in misto.recusas[0].motivo,
      misto.recusas and misto.recusas[0].motivo)
check("e NADA foi gravado no arquivo", doc.serializar() == SPED)

doc = ler_texto(SPED)
saida_errada = aplicar(doc, Patch([CorrecaoItem(
    documento=CHAVE, num_item="3", campos={"CFOP": "5102"}, motivo="x")]))
check("CFOP de saída num documento de entrada é recusado",
      len(saida_errada.recusas) == 1)
check("o motivo cita o IND_OPER",
      saida_errada.recusas and "IND_OPER" in saida_errada.recusas[0].motivo,
      saida_errada.recusas and saida_errada.recusas[0].motivo)

doc = ler_texto(SPED)
inexistente = aplicar(doc, Patch([
    CorrecaoItem(documento=CHAVE, num_item="9", campos={"CFOP": "1102"}),
    CorrecaoItem(documento="chave-que-nao-existe", num_item="1",
                 campos={"CFOP": "1102"}),
]))
check("item inexistente é recusado, não ignorado", len(inexistente.recusas) == 2,
      inexistente.recusas)
check("e o arquivo continua intacto", doc.serializar() == SPED)

doc = ler_texto(SPED)
campo_invalido = aplicar(doc, Patch([CorrecaoItem(
    documento=CHAVE, num_item="3", campos={"VL_DOC": "1,00"})]))
check("campo que não é do C170 é recusado",
      campo_invalido.recusas and "não é corrigível" in campo_invalido.recusas[0].motivo,
      campo_invalido.recusas)


# ============================================================================
print("\n=== 7. Dinheiro é Decimal, e vazio é vazio ===")

check("texto brasileiro vira Decimal", texto_para_decimal("1.234,56") == Decimal("1234.56"))
check("campo vazio soma como zero", texto_para_decimal("") == Decimal("0"))
check("lixo não estoura, vira zero", texto_para_decimal("abc") == Decimal("0"))
check("Decimal volta com vírgula", decimal_para_texto(Decimal("1234.5")) == "1234,50")
check("campo que era vazio e continua zero SEGUE vazio",
      decimal_para_texto(Decimal("0"), original="") == "")
check("mas campo que era 0,00 continua 0,00",
      decimal_para_texto(Decimal("0"), original="0,00") == "0,00")
check("arredondamento é meio-para-cima",
      decimal_para_texto(Decimal("0.125")) == "0,13")

centavos = montar_sped(
    [c170("1", "A", "0,10", "000", "1102", "12,00", "0,10", "0,01"),
     c170("2", "B", "0,20", "000", "1102", "12,00", "0,20", "0,02"),
     c170("3", "C", "0,05", "060", "1556")],
    [c190("000", "1102", "12,00", "0,30", "0,30", "0,03"),
     c190("060", "1556", "0,00", "0,05", "0,00", "0,00")],
    "0,35", "0,30", "0,03")
doc = ler_texto(centavos)
aplicar(doc, Patch([CorrecaoItem(
    documento=CHAVE, num_item="3",
    campos={"CST_ICMS": "000", "CFOP": "1102", "ALIQ_ICMS": "12,00",
            "VL_BC_ICMS": Decimal("0.05"), "VL_ICMS": Decimal("0.01")})]))
grupo = por_reg(doc.serializar(), "C190")[0].split("|")
check("0,10 + 0,20 + 0,05 fecha em 0,35 (float daria 0,35000000000000003)",
      grupo[6] == "0,35", grupo[6])
check("e o ICMS soma 0,04 sem escorregar", grupo[7] == "0,04", grupo[7])


# ============================================================================
print("\n=== 8. Frete: o VL_OPR que não vem do item é preservado ===")

com_frete = montar_sped(
    ITENS,
    [c190("000", "1102", "12,00", "1600,00", "1500,00", "180,00"),
     c190("060", "1556", "0,00", "300,00", "0,00", "0,00")],
    "1900,00", "1500,00", "180,00", vl_merc="1800,00", vl_frt="100,00")
doc = ler_texto(com_frete)
res_frete = aplicar(doc, Patch([CORRECAO]))
grupo = por_reg(doc.serializar(), "C190")[0].split("|")
check("o frete de 100,00 continua no VL_OPR do grupo que sobrou",
      grupo[5] == "1900,00", grupo[5])
check("a soma dos VL_OPR continua igual ao VL_DOC",
      texto_para_decimal(grupo[5]) == Decimal("1900.00"))
check("e por isso não há aviso de divergência", not res_frete.avisos,
      res_frete.avisos)


# ============================================================================
print("\n=== 9. O que o motor se recusa a decidir sozinho ===")

# Glosar o IPI arrasta três coisas junto, e o Guia manda todas: zerar base e
# alíquota, trocar o CST_IPI e INCORPORAR o valor à mercadoria ("devem ser
# adicionados ao valor das mercadorias [...] VL_MERC [...] VL_ITEM, uma vez que
# compõem o custo"). Fazer só a primeira deixaria o documento sem fechar.
com_ipi = montar_sped(
    [c170("1", "A", "1000,00", "000", "1102", "12,00", "1000,00", "120,00",
          ipi="50,00", bc_ipi="1000,00", cst_ipi="00", aliq_ipi="5,00"),
     c170("2", "B", "500,00", "000", "1102", "12,00", "500,00", "60,00")],
    [c190("000", "1102", "12,00", "1550,00", "1500,00", "180,00", ipi="50,00")],
    "1550,00", "1500,00", "180,00", vl_merc="1500,00")
doc = ler_texto(com_ipi)
res_ipi = aplicar(doc, Patch([CorrecaoItem(
    documento=CHAVE, num_item="1", campos={"VL_IPI": Decimal("0")},
    motivo="sem direito a crédito de IPI", aprovada_por="sandro@vm.com.br")]))
saida_ipi = doc.serializar()
item_ipi = [l for l in por_reg(saida_ipi, "C170") if l.startswith("|C170|1|")][0].split("|")
c100_ipi = por_reg(saida_ipi, "C100")[0].split("|")

check("a glosa zera também a base e a alíquota do IPI",
      (item_ipi[22], item_ipi[23]) == ("0,00", "0,00"),
      (item_ipi[22], item_ipi[23]))
check("o CST_IPI vai de 00 (com recuperação) para 49 (outras entradas)",
      item_ipi[20] == "49", item_ipi[20])
check("o IPI glosado entra no valor da mercadoria (1000 + 50)",
      item_ipi[7] == "1050,00", item_ipi[7])
check("e o VL_MERC do documento sobe o mesmo tanto",
      c100_ipi[16] == "1550,00", c100_ipi[16])
check("o VL_IPI do C100 acompanha o analítico", c100_ipi[25] == "0,00",
      c100_ipi[25])
check("o VL_DOC continua o do fornecedor", c100_ipi[12] == "1550,00",
      c100_ipi[12])
check("e agora NÃO há aviso: tirar de um campo e pôr no outro mantém o total",
      not res_ipi.avisos, res_ipi.avisos)
check("o VL_OPR do analítico também não se mexe",
      por_reg(saida_ipi, "C190")[0].split("|")[5] == "1550,00",
      por_reg(saida_ipi, "C190")[0])
check("e a migração é registrada como cascata, não como pedido do usuário",
      any(a.campo == "VL_ITEM" and a.origem == "cascata"
          for a in res_ipi.alteracoes))


# ============================================================================
print("\n=== 9b. Glosa de ICMS: o CST acompanha, o valor NÃO migra ===")

# ICMS próprio é imposto POR DENTRO — já está no preço da mercadoria. Somá-lo
# ao VL_ITEM como se faz com IPI e ICMS ST dobraria o custo. O Guia só manda
# migrar ICMS ST e IPI.
com_icms = montar_sped(
    [c170("1", "A", "1000,00", "000", "1102", "12,00", "1000,00", "120,00"),
     c170("2", "B", "500,00", "000", "1102", "12,00", "500,00", "60,00")],
    [c190("000", "1102", "12,00", "1500,00", "1500,00", "180,00")],
    "1500,00", "1500,00", "180,00")
doc = ler_texto(com_icms)
res_icms = aplicar(doc, Patch([CorrecaoItem(
    documento=CHAVE, num_item="1", campos={"VL_ICMS": Decimal("0")},
    motivo="uso e consumo, sem direito a crédito",
    aprovada_por="sandro@vm.com.br")]))
saida_icms = doc.serializar()
item_icms = [l for l in por_reg(saida_icms, "C170")
             if l.startswith("|C170|1|")][0].split("|")

check("a glosa zera base e alíquota do ICMS",
      (item_icms[13], item_icms[14]) == ("0,00", "0,00"),
      (item_icms[13], item_icms[14]))
check("o CST vai de 000 para 090 (Outras)", item_icms[10] == "090",
      item_icms[10])
check("o valor da mercadoria NÃO muda — ICMS é por dentro",
      item_icms[7] == "1000,00", item_icms[7])
check("o item migra para um C190 próprio, com o CST novo",
      any(l.split("|")[2] == "090" for l in por_reg(saida_icms, "C190")),
      por_reg(saida_icms, "C190"))
check("e o documento continua fechando",
      por_reg(saida_icms, "C100")[0].split("|")[12] == "1500,00"
      and not res_icms.avisos, res_icms.avisos)

# A origem da mercadoria (1º dígito) não tem nada a ver com direito a crédito.
importada = montar_sped(
    [c170("1", "A", "1000,00", "100", "1102", "12,00", "1000,00", "120,00")],
    [c190("100", "1102", "12,00", "1000,00", "1000,00", "120,00")],
    "1000,00", "1000,00", "120,00")
doc = ler_texto(importada)
aplicar(doc, Patch([CorrecaoItem(documento=CHAVE, num_item="1",
                                campos={"VL_ICMS": Decimal("0")})]))
check("mercadoria importada vira 190, preservando a origem",
      por_reg(doc.serializar(), "C170")[0].split("|")[10] == "190",
      por_reg(doc.serializar(), "C170")[0].split("|")[10])

# CST que já diz "sem crédito" não é mexido.
isenta = montar_sped(
    [c170("1", "A", "1000,00", "040", "1102", "", "", "")],
    [c190("040", "1102", "0,00", "1000,00", "0,00", "0,00")],
    "1000,00", "0,00", "0,00")
doc = ler_texto(isenta)
res_isenta = aplicar(doc, Patch([CorrecaoItem(
    documento=CHAVE, num_item="1", campos={"VL_ICMS": Decimal("60.00"),
                                           "VL_BC_ICMS": Decimal("500.00")})]))
# Até 09/09/2026 este caso só levantava aviso e a troca ficava com quem estava
# na tela. Decisão do Sandro: o motor ajusta. O arquivo não pode dizer, na mesma
# linha, que há R$ 60,00 de ICMS a apropriar e que a operação é isenta — quem
# recebe essa contradição é o PVA, na entrega.
check("item que GANHA crédito tem o CST ajustado para tributado (040 -> 000)",
      por_reg(doc.serializar(), "C170")[0].split("|")[10] == "000",
      por_reg(doc.serializar(), "C170")[0].split("|")[10])
check("e a troca é registrada como cascata, não como pedido do usuário",
      any(a.campo == "CST_ICMS" and a.origem == "cascata"
          and a.de == "040" and a.para == "000"
          for a in res_isenta.alteracoes),
      [(a.campo, a.de, a.para, a.origem) for a in res_isenta.alteracoes])

# A ORIGEM da mercadoria não é tocada: nacional não vira importada porque
# alguém corrigiu um crédito.
isenta_importada = montar_sped(
    [c170("1", "A", "1000,00", "140", "1102", "", "", "")],
    [c190("140", "1102", "0,00", "1000,00", "0,00", "0,00")],
    "1000,00", "0,00", "0,00")
doc_imp = ler_texto(isenta_importada)
aplicar(doc_imp, Patch([CorrecaoItem(
    documento=CHAVE, num_item="1", campos={"VL_ICMS": Decimal("60.00"),
                                           "VL_BC_ICMS": Decimal("500.00")})]))
check("e o primeiro dígito (origem) sobrevive ao ajuste (140 -> 100)",
      por_reg(doc_imp.serializar(), "C170")[0].split("|")[10] == "100",
      por_reg(doc_imp.serializar(), "C170")[0].split("|")[10])


# ============================================================================
print("\n=== 9c. As opções da tela de curadoria ===")

check("são sete", len(OPCOES_DE_CORRECAO) == 7, len(OPCOES_DE_CORRECAO))
check("com os códigos combinados",
      [o.codigo for o in OPCOES_DE_CORRECAO]
      == ["cfop_produto", "cfop_frete", "credito_icms_mercadoria",
          "credito_icms_frete", "credito_ipi", "valor_documento",
          "valor_frete"],
      [o.codigo for o in OPCOES_DE_CORRECAO])
_CAMPOS_POR_REG = {"C170": CAMPOS_C170, "C190": CAMPOS_C190_EDITAVEIS,
                   "D190": CAMPOS_D190, "C100": CAMPOS_C100, "D100": CAMPOS_D100}
check("todo campo declarado é de fato corrigível no registro que a opção diz",
      all(campo in _CAMPOS_POR_REG[o.registro]
          for o in OPCOES_DE_CORRECAO for campo in o.campos),
      [(o.codigo, o.campos) for o in OPCOES_DE_CORRECAO])
check("nenhuma opção expõe CST — a glosa o ajusta sozinha",
      not any("CST" in campo for o in OPCOES_DE_CORRECAO for campo in o.campos))
# As opções de valor (10/09/2026) expõem o VL_DOC e SÓ ele. Os outros quatro
# candidatos a "valor contábil" continuam de fora porque são DERIVADOS: saem da
# soma dos itens, e gravá-los à mão criaria a contradição que a cascata existe
# para não deixar acontecer.
check("valor: só o VL_DOC é oferecido; os derivados seguem fora",
      not any(campo in ("VL_ITEM", "VL_MERC", "VL_OPR", "VL_SERV")
              for o in OPCOES_DE_CORRECAO for campo in o.campos))
check("e a opção de valor toca um campo só",
      all(len(o.campos) == 1 for o in OPCOES_DE_CORRECAO
          if o.codigo.startswith("valor_")))


# ============================================================================
print("\n=== 10. Ida e volta pelo disco, no encoding do SPED ===")

pasta = tempfile.mkdtemp(prefix="synax_sped_")
origem = os.path.join(pasta, "SPED_042026.txt")
io.open(origem, "w", encoding="latin-1", newline="").write(SPED)

doc = ler(origem)
aplicar(doc, Patch([CORRECAO]))
destino = doc.gravar(os.path.join(pasta, "SPED_042026_corrigido.txt"))

check("o original não foi sobrescrito",
      io.open(origem, encoding="latin-1", newline="").read() == SPED)
gravado = io.open(destino, encoding="latin-1", newline="").read()
check("o corrigido foi gravado em latin-1 e reabre igual",
      ler(destino).serializar() == gravado)
check("com a razão social acentuada intacta",
      "COMERCIAL MASSUCATTI LTDA" in gravado)

for nome in os.listdir(pasta):
    os.remove(os.path.join(pasta, nome))
os.rmdir(pasta)


# ============================================================================
print("\n=== 11. Frete (D190): a regra é a INVERSA da do C190 ===")

# O D100 não tem registro de item — não existe "D170". Por isso o Guia amarra
# o analítico ao pai pela IGUALDADE, e não pela soma: "o valor informado deve
# ser igual ao valor do campo VL_BC_ICMS do registro D100, pai deste registro
# D190" (campos 06 e 07). Modelado no CT-e real de julho/2026 do escritório:
# entrada, modelo 57, CST 090, CFOP 2352, sem crédito.
CHAVE_CTE = "35260812345678000190570010001498311000149831"


def montar_sped_frete(analiticos_extras=(), bc="0,00", icms="0,00"):
    bloco_0 = ["|0000|018|0|01072026|31072026|LIES ALINHARES|"
               "27865757000102|ES|123456789|3205309|||A|1|", "|0001|0|",
               # O nome do transportador vive aqui; o D100 só guarda o código.
               "|0150|TRANSP01|IGUACU TRANSPORTE E LOGISTICA LTDA|1058|"
               "34992472000270||||||"]
    bloco_0.append(f"|0990|{len(bloco_0) + 1}|")

    d100 = linha("D100", {2: "0", 3: "1", 4: "TRANSP01", 5: "57", 6: "00",
                          7: "1", 8: "0", 9: "149831", 10: CHAVE_CTE,
                          11: "15072026", 12: "15072026", 13: "0",
                          15: "130,75", 16: "0,00", 17: "1", 18: "130,75",
                          19: bc, 20: icms, 21: "0,00"}, 25)
    d190 = linha("D190", {2: "090", 3: "2352", 4: "0", 5: "130,75", 6: bc,
                          7: icms, 8: "0,00"}, 9)
    bloco_d = ["|D001|0|", d100, d190] + list(analiticos_extras)
    bloco_d.append(f"|D990|{len(bloco_d) + 1}|")

    tipos = {}
    for l in bloco_0 + bloco_d:
        reg = l.split("|")[1]
        tipos[reg] = tipos.get(reg, 0) + 1
    tipos["9001"] = 1
    total_9900 = len(tipos) + 3
    tipos["9900"], tipos["9990"], tipos["9999"] = total_9900, 1, 1

    bloco_9 = ["|9001|0|"]
    bloco_9 += [f"|9900|{r}|{q}|" for r, q in tipos.items()]
    bloco_9.append(f"|9990|{1 + total_9900 + 1 + 1}|")
    linhas = bloco_0 + bloco_d + bloco_9
    linhas.append(f"|9999|{len(linhas) + 1}|")
    return "\r\n".join(linhas) + "\r\n"


FRETE = montar_sped_frete()
doc = ler_texto(FRETE)
check("o CT-e volta byte a byte igual", doc.serializar() == FRETE)
check("e o D190 é filho do D100",
      len(doc.filhos_reg(doc.por_reg("D100")[0], "D190")) == 1)

# A numeração dos campos NÃO é a mesma do C100: chave é o 10 (não o 9) e o
# número é o 9 (não o 8). Tratar igual devolvia o número como se fosse chave, e
# a alteração do frete não casava com a correção que a gerou.
d100_real = doc.por_reg("D100")[0]
check("a chave do CT-e vem do campo 10, não o número do campo 9",
      d100_real.chave_documento() == CHAVE_CTE, d100_real.chave_documento())
check("e o número do documento é outro campo",
      d100_real.campo(9) == "149831" and d100_real.campo(9) != CHAVE_CTE)

doc = ler_texto(FRETE)
res_frete = aplicar(doc, Patch(fretes=[CorrecaoFrete(
    documento=CHAVE_CTE, cfop_atual="2352", campos={"CFOP": "2353"},
    motivo="estabelecimento comercial, não industrial",
    origem="regra aprovada", aprovada_por="sandro@vm.com.br")]))
saida = doc.serializar()
check("a correção de CFOP do frete é aceita", not res_frete.recusas,
      [r.motivo for r in res_frete.recusas])
check("o D190 fica com o CFOP novo",
      por_reg(saida, "D190")[0].split("|")[3] == "2353",
      por_reg(saida, "D190")[0])
check("nenhum D190 é criado ou apagado — não há item para reagrupar",
      len(por_reg(saida, "D190")) == 1)
check("e o arquivo não mudou de tamanho",
      len(linhas_de(saida)) == len(linhas_de(FRETE)))

# Creditar o ICMS do frete tem de mover os DOIS registros, juntos.
doc = ler_texto(FRETE)
res_credito = aplicar(doc, Patch(fretes=[CorrecaoFrete(
    documento=CHAVE_CTE, cfop_atual="2352",
    campos={"CST_ICMS": "000", "VL_BC_ICMS": Decimal("130.75"),
            "VL_ICMS": Decimal("15.69")},
    motivo="frete com direito a crédito")]))
saida = doc.serializar()
d190_saida = por_reg(saida, "D190")[0].split("|")
d100_saida = por_reg(saida, "D100")[0].split("|")
check("o D190 recebe base e imposto",
      (d190_saida[6], d190_saida[7]) == ("130,75", "15,69"),
      (d190_saida[6], d190_saida[7]))
check("e o D100 pai é espelhado — igualdade, não soma",
      (d100_saida[19], d100_saida[20]) == ("130,75", "15,69"),
      (d100_saida[19], d100_saida[20]))
check("a alteração do pai é marcada como cascata",
      any(a.registro == "D100" and a.origem == "cascata"
          for a in res_credito.alteracoes))

doc = ler_texto(FRETE)
saida_errada = aplicar(doc, Patch(fretes=[CorrecaoFrete(
    documento=CHAVE_CTE, cfop_atual="2352", campos={"CFOP": "5352"})]))
check("CFOP de saída num CT-e de entrada é recusado",
      len(saida_errada.recusas) == 1
      and "IND_OPER" in saida_errada.recusas[0].motivo,
      saida_errada.recusas and saida_errada.recusas[0].motivo)
check("e nada foi gravado", doc.serializar() == FRETE)

doc = ler_texto(FRETE)
reducao = aplicar(doc, Patch(fretes=[CorrecaoFrete(
    documento=CHAVE_CTE, cfop_atual="2352",
    campos={"VL_RED_BC": Decimal("10.00")})]))
check("VL_RED_BC com CST 090 é recusado (só vale para 20 ou 70)",
      len(reducao.recusas) == 1 and "20 ou 70" in reducao.recusas[0].motivo,
      reducao.recusas and reducao.recusas[0].motivo)

doc = ler_texto(FRETE)
inexistente = aplicar(doc, Patch(fretes=[CorrecaoFrete(
    documento=CHAVE_CTE, cfop_atual="1353", campos={"CFOP": "1352"})]))
check("CFOP que não existe no documento é recusado, dizendo o que existe",
      inexistente.recusas and "2352" in inexistente.recusas[0].motivo,
      inexistente.recusas and inexistente.recusas[0].motivo)

duplicado = montar_sped_frete(analiticos_extras=[
    linha("D190", {2: "090", 3: "2352", 4: "0", 5: "0,00", 6: "0,00",
                   7: "0,00", 8: "0,00"}, 9)])
doc = ler_texto(duplicado)
ambiguo = aplicar(doc, Patch(fretes=[CorrecaoFrete(
    documento=CHAVE_CTE, cfop_atual="2352", campos={"CFOP": "2353"})]))
check("dois D190 no mesmo CFOP dão recusa por ambiguidade",
      ambiguo.recusas and "não diz qual" in ambiguo.recusas[0].motivo,
      ambiguo.recusas and ambiguo.recusas[0].motivo)


# ============================================================================
print("\n=== 12. O relatório de modificações ===")

doc = ler_texto(SPED)
res = aplicar(doc, Patch([CORRECAO]))
rel = montar(res, doc, SPED, origem="SPED_042026.txt",
             destino="SPED_042026_corrigido.txt")

check("o relatório identifica a competência do 0000",
      rel.competencia == "04/2026", rel.competencia)
check("guarda o hash dos dois arquivos",
      len(rel.hash_origem) == 64 and rel.hash_origem != rel.hash_destino)
check("conta as linhas antes e depois",
      (rel.linhas_antes, rel.linhas_depois) == (26, 25),
      (rel.linhas_antes, rel.linhas_depois))
check("agrupa por documento", len(rel.documentos) == 1, len(rel.documentos))

doc_rel = rel.documentos[0]
check("separa o que o usuário aceitou...", len(doc_rel.aceitas) == 5,
      len(doc_rel.aceitas))
check("...do que veio por cascata", len(doc_rel.cascata) > 0,
      len(doc_rel.cascata))
check("e guarda quem aprovou, para o maker-checker",
      all(a.aprovada_por == "joao@vm.com.br" for a in doc_rel.aceitas))
check("os contadores do arquivo ficam à parte, fora do documento",
      rel.estruturais and all(a.origem == "bloco 9" for a in rel.estruturais))

impacto = rel.impacto
check("o crédito de ICMS antes é o do arquivo original",
      impacto.icms_creditos_antes == Decimal("180.00"),
      impacto.icms_creditos_antes)
check("depois da correção sobe para 216,00",
      impacto.icms_creditos_depois == Decimal("216.00"),
      impacto.icms_creditos_depois)
check("e a diferença é o crédito recuperado",
      impacto.icms_diferenca == Decimal("36.00"), impacto.icms_diferenca)

impresso = texto(rel)
check("o texto mostra o de-para do campo",
      "1556" in impresso and "1102" in impresso)
check("separa visualmente o aceito do recalculado",
      "ACEITO PELO USUÁRIO" in impresso
      and "RECALCULADO EM CONSEQUÊNCIA" in impresso)
check("mostra o impacto na apuração", "IMPACTO NA APURAÇÃO" in impresso)
check("e deixa claro que o Bloco E não foi gravado",
      "NÃO gravado no Bloco E" in impresso)
check("com o número em formato brasileiro", "216,00" in impresso)

dados = json.loads(para_json(rel))
check("o JSON traz o total de alterações",
      dados["total_alteracoes"] == rel.total_alteracoes)
check("e o impacto com a diferença calculada",
      dados["impacto"]["icms_diferenca"] == "36.00",
      dados["impacto"]["icms_diferenca"])

planilha = linhas_para_planilha(rel)
check("a planilha tem uma linha por alteração",
      len(planilha) == rel.total_alteracoes, len(planilha))
check("com as colunas que o exportador do projeto entende",
      set(planilha[0]) >= {"Documento", "Registro", "Campo", "De", "Para",
                           "Motivo", "Origem", "Aprovado por"})

# O documento de transporte tem de sair rotulado como qualquer outro.
doc = ler_texto(FRETE)
res_f = aplicar(doc, Patch(fretes=[CorrecaoFrete(
    documento=CHAVE_CTE, cfop_atual="2352", campos={"CFOP": "2353"},
    motivo="estabelecimento comercial")]))
rel_f = montar(res_f, doc, FRETE)
check("o CT-e aparece no relatório como um documento, não em branco",
      len(rel_f.documentos) == 1 and rel_f.documentos[0].modelo == "57",
      rel_f.documentos and rel_f.documentos[0])
check("com o número do CT-e e o valor do serviço",
      rel_f.documentos[0].numero == "149831"
      and rel_f.documentos[0].valor == "130,75",
      (rel_f.documentos[0].numero, rel_f.documentos[0].valor))
check("e o nome do transportador resolvido pelo 0150",
      rel_f.documentos[0].emitente == "IGUACU TRANSPORTE E LOGISTICA LTDA",
      rel_f.documentos[0].emitente)

doc = ler_texto(SPED)
res_recusa = aplicar(doc, Patch([CorrecaoItem(
    documento=CHAVE, num_item="3", campos={"CFOP": "2102"})]))
rel_recusa = montar(res_recusa, doc, SPED)
check("o relatório mostra a recusa em vez de calar",
      len(rel_recusa.recusas) == 1 and "RECUSADO" in texto(rel_recusa))
check("e diz que nada foi aplicado naquele documento",
      "NADA FOI APLICADO" in texto(rel_recusa))


# ============================================================================
print("\n=== 13. O nome do arquivo que a pessoa baixa ===")

doc = ler_texto(SPED)
check("o nome sai do NOME do 0000, não do cadastro do tenant",
      nome_sugerido(doc) == "Sped_Corrigido_COMERCIAL MASSUCATTI LTDA.txt",
      nome_sugerido(doc))
check("com a competência, quando pedida",
      nome_sugerido(doc, com_competencia=True)
      == "Sped_Corrigido_COMERCIAL MASSUCATTI LTDA_2026-04.txt",
      nome_sugerido(doc, com_competencia=True))

sujo = SPED.replace("COMERCIAL MASSUCATTI LTDA", 'A/B: C*D "E" LTDA.')
check("caractere que o Windows recusa em nome de arquivo é trocado",
      nome_sugerido(ler_texto(sujo)) == "Sped_Corrigido_A B C D E LTDA.txt",
      nome_sugerido(ler_texto(sujo)))
check("e o ponto final some — o Windows o ignoraria e o nome mudaria sozinho",
      not nome_sugerido(ler_texto(sujo)).replace(".txt", "").endswith("."))

sem_nome = SPED.replace("COMERCIAL MASSUCATTI LTDA", "")
check("arquivo sem nome empresarial não gera nome quebrado",
      nome_sugerido(ler_texto(sem_nome)) == "Sped_Corrigido_Empresa.txt",
      nome_sugerido(ler_texto(sem_nome)))


# ============================================================================
print("\n=== 14. A prévia: o que vai acontecer, antes de gravar ===")

# A prévia roda o patch numa CÓPIA descartável em vez de prever em paralelo.
# Duas implementações da mesma regra divergem, e a que mente é justamente a que
# a pessoa lê antes de decidir.
p = prever(SPED, Patch([CORRECAO]), origem="SPED_042026.txt")

check("o texto original não é tocado pela prévia",
      ler_texto(SPED).serializar() == SPED)
check("a correção aparece como aplicável", len(p.aceitas) == 1, p.itens)
check("com o alvo legível", p.aceitas[0].alvo == "item 3", p.aceitas[0].alvo)
check("e as mudanças que ela vai gravar", len(p.aceitas[0].mudancas) == 5,
      len(p.aceitas[0].mudancas))
check("a tela pode oferecer o botão de gerar", p.pronto_para_gerar)

check("os efeitos de cascata são antecipados: um C190 some",
      p.efeitos.c190_removidos == 1, p.efeitos.c190_removidos)
check("e nenhum nasce, porque o item foi para um grupo que já existia",
      p.efeitos.c190_criados == 0, p.efeitos.c190_criados)
check("o arquivo vai encolher uma linha", p.efeitos.linhas == -1,
      p.efeitos.linhas)

# O caso oposto: o item vai para uma combinação que ainda não existe.
p_nasce = prever(SPED, Patch([CorrecaoItem(
    documento=CHAVE, num_item="1", campos={"CFOP": "1556"},
    motivo="uso e consumo")]))
check("quando a combinação é nova, a prévia avisa que um C190 nasce",
      p_nasce.efeitos.c190_criados == 1 and p_nasce.efeitos.c190_removidos == 0,
      (p_nasce.efeitos.c190_criados, p_nasce.efeitos.c190_removidos))
check("e que o arquivo ganha uma linha", p_nasce.efeitos.linhas == 1,
      p_nasce.efeitos.linhas)
check("e os contadores de bloco serão recalculados",
      p.efeitos.contadores > 0, p.efeitos.contadores)

# A prova de que a prévia não mente: o que ela diz é o que a execução faz.
doc = ler_texto(SPED)
executado = aplicar(doc, Patch([CORRECAO]))
previstas = [(a.registro, a.campo, a.de, a.para)
             for a in p.relatorio.documentos[0].aceitas
             + p.relatorio.documentos[0].cascata]
realizadas = [(a.registro, a.campo, a.de, a.para)
              for a in executado.alteracoes if a.documento != "(arquivo)"]
check("a prévia lista exatamente as alterações que a execução faz",
      previstas == realizadas, f"{len(previstas)} vs {len(realizadas)}")

# Recusa: a trava do primeiro dígito, que é a que mais vai aparecer na tela.
p_recusa = prever(SPED, Patch([CorrecaoItem(
    documento=CHAVE, num_item="3", campos={"CFOP": "2102"},
    motivo="interestadual")]))
check("correção inadmissível aparece como recusada",
      len(p_recusa.recusadas) == 1, p_recusa.itens)
check("com o motivo em português", "mesmo dígito" in p_recusa.recusadas[0].motivo,
      p_recusa.recusadas[0].motivo)
check("e a tela NÃO oferece o download — sairia cópia igual ao original",
      not p_recusa.pronto_para_gerar)
check("o texto diz isso com todas as letras",
      "NADA a gerar" in texto_da_previa(p_recusa))

# Correção que não muda nada não pode ser contada como aplicada.
p_noop = prever(SPED, Patch([CorrecaoItem(
    documento=CHAVE, num_item="1", campos={"CFOP": "1102"})]))
check("correção que já está no arquivo sai como 'sem efeito'",
      len(p_noop.sem_efeito) == 1 and not p_noop.aceitas, p_noop.itens)
check("e o motivo explica por quê",
      "já está com esse valor" in p_noop.sem_efeito[0].motivo,
      p_noop.sem_efeito[0].motivo)
check("sem nada aplicável, não há o que gerar", not p_noop.pronto_para_gerar)

# Uma boa e uma ruim juntas: a boa continua valendo.
p_misto = prever(SPED, Patch([
    CORRECAO,
    CorrecaoItem(documento="chave-inexistente", num_item="1",
                 campos={"CFOP": "1102"})]))
check("recusar uma não impede as outras",
      len(p_misto.aceitas) == 1 and len(p_misto.recusadas) == 1, p_misto.itens)
check("e a tela ainda oferece o download do que dá para aplicar",
      p_misto.pronto_para_gerar)

# A glosa é o caso em que mais coisa muda sem a pessoa pedir.
p_glosa = prever(com_ipi, Patch([CorrecaoItem(
    documento=CHAVE, num_item="1", campos={"VL_IPI": Decimal("0")},
    motivo="sem direito a crédito de IPI")]))
check("a prévia avisa que o CST vai mudar junto",
      p_glosa.efeitos.cst_ajustados == 1, p_glosa.efeitos.cst_ajustados)
check("e quanto de imposto entra no custo da mercadoria",
      p_glosa.efeitos.valor_migrado == Decimal("50.00"),
      p_glosa.efeitos.valor_migrado)
texto_glosa = texto_da_previa(p_glosa)
check("o texto mostra a migração em reais", "50,00" in texto_glosa)
check("e explica que é obrigação do leiaute, não escolha",
      "MUDA JUNTO, POR OBRIGAÇÃO DO LEIAUTE" in texto_glosa)

# Frete também entra na prévia.
p_frete = prever(FRETE, Patch(fretes=[CorrecaoFrete(
    documento=CHAVE_CTE, cfop_atual="2352", campos={"CFOP": "2353"})]))
check("a correção de frete aparece com o alvo certo",
      p_frete.aceitas and p_frete.aceitas[0].alvo == "frete CFOP 2352",
      p_frete.aceitas and p_frete.aceitas[0].alvo)
check("e sem criar ou apagar analítico — o D190 não reagrupa",
      p_frete.efeitos.c190_criados == 0 and p_frete.efeitos.linhas == 0)

texto_previa = texto_da_previa(p)
check("o cabeçalho deixa claro que nada foi gravado",
      "nada foi gravado ainda" in texto_previa)
check("e o rodapé diz quantas serão gravadas",
      "Pronto para gerar" in texto_previa)

# O texto vai para a tela, mas também para o log e para o console do Windows,
# que é cp1252. Um caractere fora dessa tabela derruba a impressão inteira —
# foi o que aconteceu com um "✓" no primeiro rascunho.
for nome, conteudo in (("prévia", texto_previa),
                       ("prévia recusada", texto_da_previa(p_recusa)),
                       ("prévia da glosa", texto_glosa),
                       ("relatório", texto(rel))):
    try:
        conteudo.encode("cp1252")
        imprimivel, erro = True, ""
    except UnicodeEncodeError as e:  # noqa: PERF203
        imprimivel, erro = False, str(e)
    check(f"o texto da {nome} sobrevive ao console do Windows", imprimivel, erro)


# ============================================================================
print("\n=== 15. O que o MOTOR precisa entregar para a correção existir ===")

# Estas duas travas moram aqui, e não em test_correcao_sped.py, porque o que
# elas protegem está no cross_checker — e as duas já quebraram uma vez, em
# silêncio: a suíte da tela monta os dicionários à mão e passaria verde com o
# motor sem gravar nada disso.

CC = io.open(os.path.join("backend", "app", "services", "audit_engine",
                          "cross_checker.py"), encoding="utf-8").read()

import re as _re  # noqa: E402
appends = _re.findall(
    r'results\["(?:icms|ipi)_issues"\]\.append\(\{.*?\n\s*\}\)', CC, _re.S)
check("as divergências de crédito continuam sendo sete no motor",
      len(appends) == 7, len(appends))
sem_marcador = [a[:80] for a in appends if '"acao"' not in a]
check("TODA divergência de crédito diz de que lado a correção vai",
      not sem_marcador,
      "sem 'acao', a ponte trata glosa como ajuste e grava o valor do XML "
      "onde o certo era zerar: " + str(sem_marcador[:1]))
duplicadas = [a[:80] for a in appends if a.count('"acao"') > 1]
check("e diz uma vez só", not duplicadas, duplicadas[:1])
check("os valores possíveis são só glosar e ajustar",
      set(_re.findall(r'"acao":\s*"(\w+)"', CC)) == {"glosar", "ajustar"},
      set(_re.findall(r'"acao":\s*"(\w+)"', CC)))

bloco_final = CC[CC.rindex("    finally:"):]
check("o SPED original SOBREVIVE à auditoria",
      "os.remove(sped_path)" not in bloco_final,
      "sem o original em disco a tela de correção nunca acha o arquivo — "
      "ela procura em temp_uploads/<task_id>/sped.txt")
check("mas os XMLs continuam sendo apagados (são o volume)",
      "shutil.rmtree(xml_dir)" in bloco_final)
check("e a retenção segue o prazo que já existe",
      "limpar_uploads" in bloco_final,
      "o comentário precisa dizer quem apaga o SPED depois, senão ele vira "
      "documento fiscal acumulando sem dono")


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

sys.exit(1 if FALHAS else 0)
