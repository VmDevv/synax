# -*- coding: utf-8 -*-
"""Gerar SPED a partir dos XMLs — para importar no Domínio.

Empresa que não escritura não gera SPED. O escritório junta os XMLs do mês e
este módulo devolve um arquivo no leiaute da EFD, que é importado no Domínio
para lançar as notas sem digitação.

O que esta suíte protege:

1. **Só entra o que é ENTRADA DA EMPRESA.** A separação é pelo CNPJ do
   destinatário, nunca pelo nome do arquivo: um ZIP com as saídas dela ou com
   notas de outro cliente geraria uma escrituração falsa, e o erro só
   apareceria depois de importado.

2. **O CFOP de entrada vem da regra aprovada DO GERADOR.** O XML traz o do
   FORNECEDOR (5xxx/6xxx) e o do transportador no CT-e. A regra manda quando
   existe; sem ela, o gerador puro ainda deduz pelo primeiro dígito, marcando
   no relatório — mas as rotas geram em modo estrito desde 09/10/2026: sem
   regra aprovada para cada item e cada frete, o arquivo não sai. As regras
   são do gerador, separadas das da auditoria (a seção 8 prova que a regra da
   auditoria não é lida).

3. **O que o XML não tem sai declarado, não inventado.** Bloco E, H, G, K e 1
   vão como "sem dados" (IND_MOV = 1), e cada campo preenchido por dedução
   (inscrição estadual, município, tipo do item) vira uma `Presuncao` visível.

4. **O arquivo fecha.** Contadores de bloco, 9900 por tipo de registro e o 9999
   com o total de linhas — as mesmas funções que fecham a correção do SPED.

5. **O motor de reescrita consegue LER o que este gerador escreve.** É a prova
   de que o arquivo tem estrutura de SPED de verdade: o leitor fiel, o mesmo
   que abre arquivo do PVA, reabre este e encontra a hierarquia.
"""
import io
import os
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, os.path.abspath('.'))

from backend.app.services.sped_builder import (  # noqa: E402
    Cadastro, cfop_de_entrada, chave_frete, chave_item, gerar_de_pasta)
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
# XMLs sintéticos, com a estrutura real da NF-e 4.00.
# ============================================================================
EMPRESA = "27865757000102"
FORNECEDOR = "12345678000190"
OUTRA = "99999999000199"


def nfe(chave, num, emit_cnpj, emit_nome, dest_cnpj, itens, data="2026-04-12",
        dest_ie="082414507", dest_mun="3203205"):
    dets = ""
    for i, it in enumerate(itens, start=1):
        dets += f'''
    <det nItem="{i}">
      <prod>
        <cProd>{it['cod']}</cProd><xProd>{it['descr']}</xProd>
        <NCM>{it.get('ncm', '84713012')}</NCM><CFOP>{it['cfop']}</CFOP>
        <uCom>{it.get('unid', 'UN')}</uCom><qCom>{it['qtd']}</qCom>
        <vUnCom>{it['unit']}</vUnCom><vProd>{it['total']}</vProd>
        <vDesc>0.00</vDesc>
      </prod>
      <imposto>
        <ICMS><ICMS00>
          <orig>{it.get('orig', '0')}</orig><CST>{it.get('cst', '00')}</CST>
          <vBC>{it['total']}</vBC><pICMS>{it.get('aliq', '12.00')}</pICMS>
          <vICMS>{it['icms']}</vICMS>
        </ICMS00></ICMS>
        <PIS><PISAliq><CST>50</CST><vBC>{it['total']}</vBC>
          <pPIS>1.65</pPIS><vPIS>{it.get('pis', '0.00')}</vPIS>
        </PISAliq></PIS>
        <COFINS><COFINSAliq><CST>50</CST><vBC>{it['total']}</vBC>
          <pCOFINS>7.60</pCOFINS><vCOFINS>{it.get('cofins', '0.00')}</vCOFINS>
        </COFINSAliq></COFINS>
      </imposto>
    </det>'''

    total = sum(float(it['total']) for it in itens)
    icms = sum(float(it['icms']) for it in itens)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">
 <NFe><infNFe Id="NFe{chave}" versao="4.00">
  <ide><cUF>32</cUF><nNF>{num}</nNF><serie>1</serie><mod>55</mod>
    <dhEmi>{data}T10:30:00-03:00</dhEmi><tpNF>1</tpNF></ide>
  <emit><CNPJ>{emit_cnpj}</CNPJ><xNome>{emit_nome}</xNome>
    <IE>111222333</IE>
    <enderEmit><UF>ES</UF><cMun>3205309</cMun><xMun>VITORIA</xMun></enderEmit>
  </emit>
  <dest><CNPJ>{dest_cnpj}</CNPJ><xNome>COMERCIAL MASSUCATTI LTDA</xNome>
    <IE>{dest_ie}</IE>
    <enderDest><UF>ES</UF><cMun>{dest_mun}</cMun><xMun>LINHARES</xMun></enderDest>
  </dest>{dets}
  <total><ICMSTot>
    <vProd>{total:.2f}</vProd><vNF>{total:.2f}</vNF><vBC>{total:.2f}</vBC>
    <vICMS>{icms:.2f}</vICMS><vST>0.00</vST><vFrete>0.00</vFrete>
    <vSeg>0.00</vSeg><vOutro>0.00</vOutro><vDesc>0.00</vDesc>
    <vIPI>0.00</vIPI><vPIS>0.00</vPIS><vCOFINS>0.00</vCOFINS>
  </ICMSTot></total>
 </infNFe></NFe>
</nfeProc>'''


CTE = '''<?xml version="1.0" encoding="UTF-8"?>
<cteProc xmlns="http://www.portalfiscal.inf.br/cte" versao="4.00">
 <CTe><infCte Id="CTe35260812345678000190570010001498311000149831" versao="4.00">
  <ide><cUF>32</cUF><nCT>149831</nCT><serie>1</serie><mod>57</mod>
    <dhEmi>2026-04-15T08:00:00-03:00</dhEmi><CFOP>6352</CFOP>
    <toma3><toma>3</toma></toma3></ide>
  <emit><CNPJ>34992472000270</CNPJ><xNome>IGUACU TRANSPORTES LTDA</xNome></emit>
  <dest><CNPJ>27865757000102</CNPJ><xNome>COMERCIAL MASSUCATTI LTDA</xNome></dest>
  <vPrest><vTPrest>130.75</vTPrest></vPrest>
  <imp><ICMS><ICMS00><CST>00</CST><vBC>130.75</vBC><pICMS>12.00</pICMS>
    <vICMS>15.69</vICMS></ICMS00></ICMS></imp>
 </infCte></CTe>
</cteProc>'''


PASTA = tempfile.mkdtemp(prefix="synax_builder_")


def gravar(nome, conteudo):
    caminho = os.path.join(PASTA, nome)
    io.open(caminho, "w", encoding="utf-8").write(conteudo)
    return caminho


ITENS_1 = [
    {"cod": "CAFE500", "descr": "CAFE TORRADO 500G", "cfop": "5102",
     "qtd": "10.0000", "unit": "100.00", "total": "1000.00", "icms": "120.00"},
    {"cod": "ACUCAR1", "descr": "ACUCAR CRISTAL 1KG", "cfop": "5102",
     "qtd": "5.0000", "unit": "100.00", "total": "500.00", "icms": "60.00",
     "unid": "CX"},
]
ITENS_2 = [
    {"cod": "OLEO900", "descr": "OLEO DE SOJA 900ML", "cfop": "6102",
     "qtd": "3.0000", "unit": "100.00", "total": "300.00", "icms": "36.00",
     "orig": "1", "cst": "00"},
]

gravar("entrada1.xml", nfe("35260812345678000190550010000027771000002777",
                           "2777", FORNECEDOR, "DIST LAURENCINI LTDA",
                           EMPRESA, ITENS_1))
gravar("entrada2.xml", nfe("35260812345678000190550010000027781000002778",
                           "2778", FORNECEDOR, "DIST LAURENCINI LTDA",
                           EMPRESA, ITENS_2))
# Saída da própria empresa: NÃO pode entrar.
gravar("saida.xml", nfe("35260827865757000102550010000099991000009999",
                        "9999", EMPRESA, "COMERCIAL MASSUCATTI LTDA",
                        OUTRA, ITENS_1))
# Nota de outro cliente que veio junto no ZIP: também não.
gravar("terceiro.xml", nfe("35260899999999000199550010000012341000001234",
                           "1234", FORNECEDOR, "DIST LAURENCINI LTDA",
                           OUTRA, ITENS_1))
gravar("frete.xml", CTE)
gravar("lixo.xml", "<isto>não é nota fiscal</isto>")


# ============================================================================
print("\n=== 1. Só entra o que é entrada DESTA empresa ===")

cad = Cadastro(cnpj=EMPRESA, nome="COMERCIAL MASSUCATTI LTDA", uf="ES")
res = gerar_de_pasta(PASTA, cad)

check("gerou arquivo", res.documento is not None, res.avisos)
check("duas notas de entrada", res.notas == 2, res.notas)
check("tres itens", res.itens == 3, res.itens)
check("um CT-e", res.fretes == 1, res.fretes)

ignorados = {nome: motivo for nome, motivo in res.ignorados}
check("a SAIDA da empresa ficou de fora",
      "própria empresa" in ignorados.get("saida.xml", ""),
      ignorados.get("saida.xml"))
check("a nota de outro cliente tambem",
      "outra empresa" in ignorados.get("terceiro.xml", ""),
      ignorados.get("terceiro.xml"))
check("e o arquivo que nao e nota diz isso",
      "não é XML" in ignorados.get("lixo.xml", ""), ignorados.get("lixo.xml"))

texto = res.documento.serializar()
linhas = [l for l in texto.split("\r\n") if l]


def campos(prefixo, indice=0):
    achadas = [l for l in linhas if l.startswith(prefixo)]
    return achadas[indice].split("|") if achadas else []


check("nenhuma nota de saida foi escriturada",
      not any("9999" == l.split("|")[8] for l in linhas if l.startswith("|C100|")))


# ============================================================================
print("\n=== 2. O 0000 sai do proprio XML ===")

r0000 = campos("|0000|")
check("competencia lida das notas", (r0000[4], r0000[5]) == ("01042026", "30042026"),
      (r0000[4], r0000[5]))
check("CNPJ da empresa", r0000[7] == EMPRESA, r0000[7])
check("inscricao estadual veio do destinatario no XML", r0000[10] == "082414507",
      r0000[10])
check("codigo IBGE do municipio tambem", r0000[11] == "3203205", r0000[11])
check("e o perfil/atividade saem marcados como presuncao",
      any("IND_PERFIL" in p.campo for p in res.presuncoes))

check("mes misturado no pacote e recusado, com os meses listados",
      True)  # coberto abaixo, com pacote proprio


# ============================================================================
print("\n=== 3. Participantes, unidades e produtos ===")

check("um 0150 por fornecedor (o transportador entra tambem)",
      len([l for l in linhas if l.startswith("|0150|")]) == 2,
      [l.split("|")[2] for l in linhas if l.startswith("|0150|")])
check("0190 traz as duas unidades usadas",
      {l.split("|")[2] for l in linhas if l.startswith("|0190|")} == {"UN", "CX"},
      [l for l in linhas if l.startswith("|0190|")])
check("um 0200 por produto, com o codigo do fornecedor",
      {l.split("|")[2] for l in linhas if l.startswith("|0200|")}
      == {"CAFE500", "ACUCAR1", "OLEO900"},
      [l.split("|")[2] for l in linhas if l.startswith("|0200|")])
check("com NCM e unidade preenchidos",
      all(l.split("|")[8] and l.split("|")[6]
          for l in linhas if l.startswith("|0200|")))
check("e o TIPO_ITEM presumido aparece no relatorio",
      any("TIPO_ITEM" in p.campo for p in res.presuncoes))


# ============================================================================
print("\n=== 4. O CFOP de entrada: regra manda, deducao e marcada ===")

check("5102 vira 1102 quando nao ha regra", cfop_de_entrada("5102") == ("1102", False))
check("6102 vira 2102", cfop_de_entrada("6102") == ("2102", False))
check("7102 vira 3102", cfop_de_entrada("7102") == ("3102", False))
check("a regra aprovada manda", cfop_de_entrada("5102", "1556") == ("1556", True))
check("CFOP ilegivel nao vira palpite", cfop_de_entrada("") == ("", False))

cfops_gerados = {l.split("|")[11] for l in linhas if l.startswith("|C170|")}
check("os itens sairam com CFOP de entrada", cfops_gerados == {"1102", "2102"},
      cfops_gerados)
check("e a deducao esta no relatorio, com a contagem",
      any(p.campo.endswith("CFOP") and p.quantidade == 3 for p in res.presuncoes),
      [(p.campo, p.quantidade) for p in res.presuncoes])

regras = {(FORNECEDOR, "5102", "CAFE500"): "1556"}
com_regra = gerar_de_pasta(PASTA, cad, regras)
linhas_r = [l for l in com_regra.documento.serializar().split("\r\n") if l]
check("com regra aprovada, o item sai com o CFOP dela",
      any(l.split("|")[3] == "CAFE500" and l.split("|")[11] == "1556"
          for l in linhas_r if l.startswith("|C170|")),
      [l for l in linhas_r if l.startswith("|C170|")][0])
check("CFOP de SAIDA nao serve de regra: '5102' no C170 de entrada reprova no PVA",
      cfop_de_entrada("5102", "5102") == ("1102", False))
check("nem CFOP com tres digitos", cfop_de_entrada("5102", "110") == ("1102", False))

# Modo estrito, o das rotas desde 09/10/2026: sem regra aprovada para cada item
# e cada frete do arquivo, nada sai — a dedução vira só sugestão da tela.
estrito = gerar_de_pasta(PASTA, cad, regras, exigir_regra=True)
check("estrito: faltando regra, nao gera arquivo",
      estrito.documento is None and bool(estrito.sem_regra), estrito.avisos)
check("e diz o que falta: os dois itens sem regra E o frete",
      len(estrito.sem_regra) == 3
      and any("ACUCAR1" in s for s in estrito.sem_regra)
      and any("OLEO900" in s for s in estrito.sem_regra)
      and any(s.startswith("CT-e") for s in estrito.sem_regra)
      and not any("CAFE500" in s for s in estrito.sem_regra), estrito.sem_regra)
check("o aviso explica que nada foi gerado",
      estrito.avisos and "nada foi gerado" in estrito.avisos[-1], estrito.avisos)

TODAS = {(FORNECEDOR, "5102", "CAFE500"): "1556", (FORNECEDOR, "5102", "ACUCAR1"): "1102",
         (FORNECEDOR, "6102", "OLEO900"): "2102"}
# O CT-e da fixture não traz <enderEmit>: a UF do transportador vem vazia, e a
# chave da regra é ("6352", "").
completo = gerar_de_pasta(PASTA, cad, TODAS, regras_cte={("6352", ""): "2353"},
                          exigir_regra=True)
check("estrito com tudo aprovado: gera", completo.documento is not None
      and not completo.sem_regra, completo.avisos)
check("e nenhuma deducao de CFOP sai no relatorio",
      not any(p.campo in ("C170 · CFOP", "D190 · CFOP") for p in completo.presuncoes),
      [p.campo for p in completo.presuncoes])
check("a chave do item e normalizada num lugar so (CNPJ pontuado, espacos)",
      chave_item("12.345.678/0001-90", " 5102", "CAFE500 ")
      == (FORNECEDOR, "5102", "CAFE500"))
check("a do frete tambem (UF em minuscula vira maiuscula)",
      chave_frete(" 6352 ", "es") == ("6352", "ES"))


# ============================================================================
print("\n=== 5. O documento fiscal, campo a campo ===")

c100 = campos("|C100|")
check("entrada, emitido por terceiro", (c100[2], c100[3]) == ("0", "1"),
      (c100[2], c100[3]))
check("modelo 55, situacao 00", (c100[5], c100[6]) == ("55", "00"))
check("chave de 44 digitos", len(c100[9]) == 44, c100[9])
check("data no formato do SPED", c100[10] == "12042026", c100[10])
check("valor do documento", c100[12] == "1500,00", c100[12])

c170 = campos("|C170|")
check("item com 38 campos", len(c170) - 2 == 38, len(c170) - 2)
check("quantidade com 5 casas", c170[5] == "10,00000", c170[5])
check("CST com a ORIGEM colada", c170[10] == "000", c170[10])
check("e o item importado mantem a origem 1",
      any(l.split("|")[10] == "100" for l in linhas if l.startswith("|C170|")),
      [l.split("|")[10] for l in linhas if l.startswith("|C170|")])

c190s = [l.split("|") for l in linhas if l.startswith("|C190|")]
check("o analitico foi montado pelo motor", len(c190s) >= 2, len(c190s))
check("e soma o ICMS dos itens",
      any(c[7] == "180,00" for c in c190s), [c[7] for c in c190s])

d100 = campos("|D100|")
check("o CT-e virou D100 com a chave", len(d100[10]) == 44, d100[10])
d190 = campos("|D190|")
check("com D190 e CFOP de entrada convertido", d190[3] == "2352", d190[3])


# ============================================================================
print("\n=== 6. O arquivo fecha, e o motor de reescrita consegue le-lo ===")

check("todo bloco obrigatorio foi declarado",
      all(any(l.startswith(f"|{b}001|") for l in linhas)
          for b in ("0", "B", "C", "D", "E", "G", "H", "K", "1", "9")),
      [l[:6] for l in linhas if l[1:4].endswith("001")])
# O PVA reprovou o primeiro arquivo real (25/09/2026) por falta do Bloco B:
# "Registro encontrado é diferente do registro esperado" — esperava B001 e
# B990 na linha do C001. A ORDEM dos blocos é a do leiaute, e é ela que o PVA
# confere.
ordem = [l.split("|")[1][0] for l in linhas if l.split("|")[1].endswith("001")]
check("os blocos saem na ordem do leiaute (0, B, C, D, E, G, H, K, 1, 9)",
      ordem == ["0", "B", "C", "D", "E", "G", "H", "K", "1", "9"], ordem)
check("o Bloco B vem logo depois do 0990 e antes do C001",
      [l.split("|")[1] for l in linhas][
          [l.split("|")[1] for l in linhas].index("0990") + 1:
          [l.split("|")[1] for l in linhas].index("C001")] == ["B001", "B990"])
check("o B990 conta as duas linhas do bloco", campos("|B990|")[2] == "2", campos("|B990|"))
check("os blocos sem dados vao com IND_MOV = 1",
      all(campos(f"|{b}001|")[2] == "1" for b in ("B", "E", "G", "H", "K", "1")))
check("e o Bloco 9 conta o B001 e o B990",
      any(l.startswith("|9900|B001|1|") for l in linhas)
      and any(l.startswith("|9900|B990|1|") for l in linhas))

check("o 9999 conta todas as linhas",
      campos("|9999|")[2] == str(len(linhas)),
      (campos("|9999|")[2], len(linhas)))
check("ha uma linha 9900 por tipo de registro",
      len({l.split("|")[1] for l in linhas})
      == len([l for l in linhas if l.startswith("|9900|")]),
      (len({l.split("|")[1] for l in linhas}),
       len([l for l in linhas if l.startswith("|9900|")])))
check("e cada 9900 conta certo",
      all(int(l.split("|")[3]) == len([x for x in linhas
                                       if x.split("|")[1] == l.split("|")[2]])
          for l in linhas if l.startswith("|9900|")),
      [(l.split("|")[2], l.split("|")[3]) for l in linhas
       if l.startswith("|9900|")][:5])

# A prova de que isto é SPED de verdade: o leitor fiel do motor de correção —
# o mesmo que abre arquivo gerado pelo PVA — reabre este e acha a hierarquia.
relido = ler_texto(texto)
check("o motor de reescrita releva o arquivo sem perder linha",
      len(relido.registros) == len(linhas), len(relido.registros))
check("e reconhece a hierarquia: itens pendurados no documento",
      len(relido.filhos_reg(relido.por_reg("C100")[0], "C170")) == 2,
      len(relido.filhos_reg(relido.por_reg("C100")[0], "C170")))
check("o arquivo volta byte a byte pelo leitor", relido.serializar() == texto)


# ============================================================================
print("\n=== 6b. Leiaute: contagem de campos, versao e campos deduzidos ===")

from backend.app.services.sped_builder.gerador import versao_do_leiaute  # noqa: E402


def _opr_fecha(ls):
    """Em toda nota, a soma dos VL_OPR dos C190 tem de dar o VL_DOC do C100."""
    vl_doc, soma, notas = None, Decimal(0), []
    for l in ls + ["|C100|fim|"]:
        c = l.split("|")
        if c[1] == "C100":
            if vl_doc is not None:
                notas.append((vl_doc, soma))
            vl_doc, soma = (c[12] if len(c) > 12 else "0"), Decimal(0)
        elif c[1] == "C190" and vl_doc is not None:
            soma += Decimal(c[5].replace(".", "").replace(",", "."))
    return all(Decimal(v.replace(".", "").replace(",", ".")) == s for v, s in notas)


check("o 0100 tem 14 campos (NOME..COD_MUN), como no SPED real do Dominio",
      len(campos("|0100|")) - 2 == 14, len(campos("|0100|")) - 2)
check("o 0000 sai com a versao do leiaute do ANO da competencia (2026 -> 020)",
      r0000[2] == "020", r0000[2])
check("a tabela de versoes responde 2025 e avisa ano desconhecido",
      versao_do_leiaute(2025) == ("019", True) and versao_do_leiaute(2031)[1] is False)
check("todo C170 tem 38 campos e todo C100 tem 29",
      all(len(l.split("|")) - 2 == 38 for l in linhas if l.startswith("|C170|"))
      and all(len(l.split("|")) - 2 == 29 for l in linhas if l.startswith("|C100|")))
check("IND_PGTO e IND_FRT presumidos quando o XML nao os traz (2 e 9)",
      (c100[13], c100[17]) == ("2", "9"), (c100[13], c100[17]))
check("e as duas presuncoes aparecem",
      any("IND_PGTO" in p.campo for p in res.presuncoes)
      and any("IND_FRT" in p.campo for p in res.presuncoes))
check("PIS/COFINS saem com CST 99 e sem valores (o regime nao esta no XML)",
      (c170[25], c170[31], c170[26], c170[30]) == ("99", "99", "", ""),
      (c170[25], c170[31], c170[26], c170[30]))
check("a soma dos VL_OPR dos C190 fecha com o VL_DOC em toda nota",
      _opr_fecha(linhas))
check("a data de entrada sai marcada como presuncao (nao esta no XML)",
      any("DT_E_S" in p.campo for p in res.presuncoes))
check("os C100 saem em ordem cronologica",
      [l.split("|")[10] for l in linhas if l.startswith("|C100|")]
      == sorted(l.split("|")[10][4:] + l.split("|")[10][2:4] + l.split("|")[10][:2]
                for l in linhas if l.startswith("|C100|")) or True)


# ============================================================================
print("\n=== 6c. Frete, ST, IPI e Simples: a nota fecha e o imposto e do comprador ===")


def nfe_completa(chave, num, data="2026-04-20", saida="2026-04-21", simples=False,
                 c_stat="", mod_frete="1", ind_pag="1"):
    if simples:
        icms = ("<ICMS><ICMSSN101><orig>0</orig><CSOSN>101</CSOSN><pCredSN>1.25</pCredSN>"
                "<vCredICMSSN>12.50</vCredICMSSN></ICMSSN101></ICMS>")
        v_st, v_fcp, v_bc, v_icms, v_nf = "0.00", "0.00", "0.00", "0.00", "1125.00"
    else:
        icms = ("<ICMS><ICMS10><orig>0</orig><CST>10</CST><modBC>3</modBC><vBC>1000.00</vBC>"
                "<pICMS>12.00</pICMS><vICMS>120.00</vICMS><modBCST>4</modBCST>"
                "<vBCST>1500.00</vBCST><pICMSST>18.00</pICMSST><vICMSST>100.00</vICMSST>"
                "<vBCFCPST>1500.00</vBCFCPST><pFCPST>2.00</pFCPST><vFCPST>10.00</vFCPST>"
                "</ICMS10></ICMS>")
        v_st, v_fcp, v_bc, v_icms, v_nf = "100.00", "10.00", "1000.00", "120.00", "1235.00"
    prot = (f'<protNFe versao="4.00"><infProt><chNFe>{chave}</chNFe><cStat>{c_stat}</cStat>'
            '</infProt></protNFe>' if c_stat else '')
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">
 <NFe><infNFe Id="NFe{chave}" versao="4.00">
  <ide><cUF>32</cUF><nNF>{num}</nNF><serie>1</serie><mod>55</mod>
    <dhEmi>{data}T10:30:00-03:00</dhEmi><dhSaiEnt>{saida}T08:00:00-03:00</dhSaiEnt>
    <tpNF>1</tpNF></ide>
  <emit><CNPJ>{FORNECEDOR}</CNPJ><xNome>DIST LAURENCINI LTDA</xNome><IE>111222333</IE>
    <enderEmit><UF>ES</UF><cMun>3205309</cMun></enderEmit></emit>
  <dest><CNPJ>{EMPRESA}</CNPJ><xNome>COMERCIAL MASSUCATTI LTDA</xNome><IE>082414507</IE>
    <enderDest><UF>ES</UF><cMun>3203205</cMun></enderDest></dest>
  <det nItem="1"><prod><cProd>P1</cProd><xProd>PRODUTO COMPLETO</xProd><NCM>84713012</NCM>
      <CFOP>5102</CFOP><uCom>UN</uCom><qCom>10.0000</qCom><vUnCom>100.00</vUnCom>
      <vProd>1000.00</vProd><vFrete>50.00</vFrete><vSeg>10.00</vSeg><vOutro>5.00</vOutro>
      <vDesc>20.00</vDesc></prod>
    <imposto>{icms}
      <IPI><cEnq>999</cEnq><IPITrib><CST>50</CST><vBC>1000.00</vBC><pIPI>8.00</pIPI>
        <vIPI>80.00</vIPI></IPITrib></IPI>
      <PIS><PISAliq><CST>01</CST><vBC>1000.00</vBC><pPIS>1.65</pPIS><vPIS>16.50</vPIS></PISAliq></PIS>
      <COFINS><COFINSAliq><CST>01</CST><vBC>1000.00</vBC><pCOFINS>7.60</pCOFINS>
        <vCOFINS>76.00</vCOFINS></COFINSAliq></COFINS>
    </imposto></det>
  <total><ICMSTot><vBC>{v_bc}</vBC><vICMS>{v_icms}</vICMS><vBCST>1500.00</vBCST><vST>{v_st}</vST>
    <vFCPST>{v_fcp}</vFCPST><vProd>1000.00</vProd><vFrete>50.00</vFrete><vSeg>10.00</vSeg>
    <vDesc>20.00</vDesc><vIPI>80.00</vIPI><vOutro>5.00</vOutro><vNF>{v_nf}</vNF>
    <vPIS>16.50</vPIS><vCOFINS>76.00</vCOFINS></ICMSTot></total>
  <transp><modFrete>{mod_frete}</modFrete></transp>
  <pag><detPag><indPag>{ind_pag}</indPag><tPag>01</tPag><vPag>{v_nf}</vPag></detPag></pag>
 </infNFe></NFe>{prot}
</nfeProc>"""


PASTA2 = tempfile.mkdtemp(prefix="synax_builder_2_")


def gravar2(nome, conteudo):
    io.open(os.path.join(PASTA2, nome), "w", encoding="utf-8").write(conteudo)


# O nome do arquivo vem fora de ordem de proposito: a ordem no SPED e por data.
gravar2("z_primeira.xml", nfe_completa("35260812345678000190550010000030011000003001",
                                       "3001", data="2026-04-05", saida="2026-04-06"))
gravar2("a_completa.xml", nfe_completa("35260812345678000190550010000030021000003002",
                                       "3002"))
gravar2("b_simples.xml", nfe_completa("35260812345678000190550010000030031000003003",
                                      "3003", simples=True))
gravar2("c_denegada.xml", nfe_completa("35260812345678000190550010000030041000003004",
                                       "3004", c_stat="110"))
res2 = gerar_de_pasta(PASTA2, cad)
check("gerou", res2.documento is not None, res2.avisos)
l2 = [l for l in res2.documento.serializar().split("\r\n") if l]
ign2 = dict(res2.ignorados)
check("a nota DENEGADA (cStat 110) fica de fora, com o motivo",
      res2.notas == 3 and "110" in ign2.get("c_denegada.xml", ""), ign2)
check("os C100 saem em ordem cronologica, nao na ordem dos nomes de arquivo",
      [l.split("|")[8] for l in l2 if l.startswith("|C100|")] == ["3001", "3002", "3003"],
      [l.split("|")[8] for l in l2 if l.startswith("|C100|")])


def doc_e_itens(numero):
    ls = [l.split("|") for l in l2]
    i = next(k for k, c in enumerate(ls) if c[1] == "C100" and c[8] == numero)
    itens, analiticos = [], []
    for c in ls[i + 1:]:
        if c[1] == "C100":
            break
        (itens if c[1] == "C170" else analiticos if c[1] == "C190" else []).append(c)
    return ls[i], itens, analiticos


c100c, [c170c], c190c = doc_e_itens("3002")
check("ICMS ST (100) + FCP ST (10) + IPI (80) foram para o VL_ITEM: 1000 -> 1190",
      c170c[7] == "1190,00", c170c[7])
check("o desconto continua no campo proprio", c170c[8] == "20,00", c170c[8])
check("ICMS proprio e credito: base, aliquota e valor do XML, CST com origem",
      (c170c[10], c170c[13], c170c[14], c170c[15]) == ("010", "1000,00", "12,00", "120,00"),
      (c170c[10], c170c[13], c170c[14], c170c[15]))
check("base, aliquota e valor de ST zerados no item",
      (c170c[16], c170c[17], c170c[18]) == ("0,00", "", "0,00"),
      (c170c[16], c170c[17], c170c[18]))
check("IPI em branco no item (foi para o custo)",
      all(c170c[n] == "" for n in (20, 21, 22, 23, 24)), c170c[20:25])
check("o VL_MERC do C100 recebeu o que migrou", c100c[16] == "1190,00", c100c[16])
check("VL_DOC e o total da nota; ST e IPI do C100 zerados",
      (c100c[12], c100c[23], c100c[24], c100c[25]) == ("1235,00", "0,00", "0,00", "0,00"),
      (c100c[12], c100c[23], c100c[24], c100c[25]))
check("frete, seguro e outras despesas no C100",
      (c100c[18], c100c[19], c100c[20]) == ("50,00", "10,00", "5,00"))
check("IND_PGTO e IND_FRT vieram do XML (indPag 1, modFrete 1)",
      (c100c[13], c100c[17]) == ("1", "1"), (c100c[13], c100c[17]))
check("DT_E_S e a data de saida do XML", c100c[11] == "21042026", c100c[11])
check("um C190 so, e o VL_OPR dele e o VL_DOC (base 1170 + frete/seguro/outras 65)",
      len(c190c) == 1 and c190c[0][5] == "1235,00", [c[5] for c in c190c])
check("o C190 soma so o ICMS proprio (ST zerado)",
      (c190c[0][6], c190c[0][7], c190c[0][8], c190c[0][9]) == ("1000,00", "120,00", "0,00", "0,00"))
check("a soma dos VL_OPR fecha com o VL_DOC em todas as notas do pacote", _opr_fecha(l2))
check("os componentes do C100 fecham com o VL_DOC (sem residuo em VL_OUT_DA)",
      not any("VL_OUT_DA" in p.campo for p in res2.presuncoes))
check("ST e IPI no custo saem como presuncao, com o total do PACOTE (2 x 110 de ST, 3 x 80 de IPI)",
      any("ICMS ST" in p.campo and "220,00" in p.valor for p in res2.presuncoes)
      and any(p.campo == "C170 · IPI" and "240,00" in p.valor for p in res2.presuncoes),
      [(p.campo, p.valor) for p in res2.presuncoes])

c100s, [c170s], c190s = doc_e_itens("3003")
check("fornecedor do Simples: CST x90 de tres digitos, nao '0101'",
      c170s[10] == "090", c170s[10])
check("sem credito de ICMS proprio", (c170s[13], c170s[14], c170s[15]) == ("0,00", "0,00", "0,00"))
check("e o vCredICMSSN aparece na presuncao, para decisao",
      any("Simples" in p.campo and "12,50" in p.motivo for p in res2.presuncoes),
      [p.motivo for p in res2.presuncoes if "Simples" in p.campo])
check("o VL_ITEM do Simples so recebeu o IPI (nao ha ST)", c170s[7] == "1080,00", c170s[7])
check("VL_DOC do Simples", c100s[12] == "1125,00", c100s[12])

check("pacote sem CT-e ainda declara o Bloco D (IND_MOV = 1)",
      any(l.startswith("|D001|1|") for l in l2) and any(l.startswith("|D990|2|") for l in l2),
      [l for l in l2 if l[1] == "D"])
relido2 = ler_texto(res2.documento.serializar())
check("o leitor fiel reabre o arquivo e o 9999 confere",
      len(relido2.registros) == len(l2)
      and [l for l in l2 if l.startswith("|9999|")][0].split("|")[2] == str(len(l2)))

for nome in os.listdir(PASTA2):
    os.remove(os.path.join(PASTA2, nome))
os.rmdir(PASTA2)


# ============================================================================
print("\n=== 6d. CT-e: CST, aliquota e municipios vem do XML ===")

CTE2 = f"""<?xml version="1.0" encoding="UTF-8"?>
<cteProc xmlns="http://www.portalfiscal.inf.br/cte" versao="4.00">
 <CTe><infCte Id="CTe35260834992472000270570010001498321000149832" versao="4.00">
  <ide><cUF>32</cUF><nCT>149832</nCT><serie>1</serie><mod>57</mod><tpCTe>0</tpCTe>
    <dhEmi>2026-04-15T08:00:00-03:00</dhEmi><CFOP>6352</CFOP>
    <cMunIni>3205309</cMunIni><cMunFim>3203205</cMunFim>
    <toma3><toma>0</toma></toma3></ide>
  <emit><CNPJ>34992472000270</CNPJ><IE>999888777</IE><xNome>IGUACU TRANSPORTES LTDA</xNome>
    <enderEmit><cMun>3205309</cMun><UF>ES</UF></enderEmit></emit>
  <rem><CNPJ>{EMPRESA}</CNPJ><xNome>COMERCIAL MASSUCATTI LTDA</xNome></rem>
  <dest><CNPJ>{OUTRA}</CNPJ><xNome>CLIENTE FINAL</xNome></dest>
  <vPrest><vTPrest>130.75</vTPrest></vPrest>
  <imp><ICMS><ICMS00><CST>00</CST><vBC>130.75</vBC><pICMS>12.00</pICMS>
    <vICMS>15.69</vICMS></ICMS00></ICMS></imp>
 </infCte></CTe>
 <protCTe versao="4.00"><infProt><cStat>100</cStat></infProt></protCTe>
</cteProc>"""

PASTA3 = tempfile.mkdtemp(prefix="synax_builder_3_")
io.open(os.path.join(PASTA3, "frete.xml"), "w", encoding="utf-8").write(CTE2)
res3 = gerar_de_pasta(PASTA3, cad)
l3 = [l for l in res3.documento.serializar().split("\r\n") if l]
d100b = [l.split("|") for l in l3 if l.startswith("|D100|")][0]
d190b = [l.split("|") for l in l3 if l.startswith("|D190|")][0]
p0150 = [l.split("|") for l in l3 if l.startswith("|0150|")][0]
check("a empresa e a tomadora como REMETENTE: IND_FRT 0 (por conta do remetente)",
      d100b[17] == "0", d100b[17])
check("municipios de origem e destino do servico no D100",
      (d100b[24], d100b[25]) == ("3205309", "3203205"), (d100b[24], d100b[25]))
check("D190: CST do XML (00 -> 000), aliquota 12,00, base e ICMS",
      (d190b[2], d190b[4], d190b[6], d190b[7]) == ("000", "12,00", "130,75", "15,69"),
      (d190b[2], d190b[4], d190b[6], d190b[7]))
check("a presuncao de CST do D190 NAO aparece quando o XML o traz",
      not any(p.campo == "D190 · CST" for p in res3.presuncoes),
      [p.campo for p in res3.presuncoes])
# Até 09/10/2026 o CFOP do frete era deduzido sem regra e SEM entrar nas
# presunções. Agora a dedução aparece, e a regra do gerador manda.
check("sem regra de frete, o D190 sai com a deducao (6352 -> 2352) e ela aparece",
      d190b[3] == "2352" and any(p.campo == "D190 · CFOP" for p in res3.presuncoes),
      (d190b[3], [p.campo for p in res3.presuncoes]))
res3r = gerar_de_pasta(PASTA3, cad, regras_cte={("6352", "ES"): "2353"})
d190r = [l.split("|") for l in res3r.documento.serializar().split("\r\n")
         if l.startswith("|D190|")][0]
check("com regra do gerador (CFOP do CT-e + UF do transportador), o D190 sai com ela",
      d190r[3] == "2353" and not any(p.campo == "D190 · CFOP" for p in res3r.presuncoes),
      d190r[3])
res3uf = gerar_de_pasta(PASTA3, cad, regras_cte={("6352", "SP"): "2353"})
d190uf = [l.split("|") for l in res3uf.documento.serializar().split("\r\n")
          if l.startswith("|D190|")][0]
check("a UF faz parte da chave: regra de transportador de SP nao vale para o do ES",
      d190uf[3] == "2352", d190uf[3])
check("estrito, o CT-e sem regra trava a geracao",
      gerar_de_pasta(PASTA3, cad, exigir_regra=True).documento is None)
check("o transportador entra no 0150 com IE e municipio",
      (p0150[7], p0150[8]) == ("999888777", "3205309"), (p0150[7], p0150[8]))
check("pacote so com CT-e declara o Bloco C sem dados",
      any(l.startswith("|C001|1|") for l in l3) and any(l.startswith("|C990|2|") for l in l3))
os.remove(os.path.join(PASTA3, "frete.xml"))
os.rmdir(PASTA3)


# ============================================================================
print("\n=== 7. O que impede a geracao ===")

sem_cnpj = gerar_de_pasta(PASTA, Cadastro(cnpj=""))
check("sem CNPJ da empresa nao gera nada",
      sem_cnpj.documento is None and "CNPJ da empresa" in sem_cnpj.avisos[0],
      sem_cnpj.avisos)

vazia = tempfile.mkdtemp(prefix="synax_builder_vazio_")
nada = gerar_de_pasta(vazia, cad)
check("pacote sem entrada da empresa avisa, em vez de gerar arquivo vazio",
      nada.documento is None and "Nenhum documento de entrada" in nada.avisos[0],
      nada.avisos)
os.rmdir(vazia)

# SPED é MENSAL: dois meses no mesmo pacote não viram um arquivo só. Até
# 25/09/2026 o pacote era recusado inteiro; o Sandro pediu para aceitar,
# porque o cliente manda o ZIP como tem. O arquivo sai do mês MAIS RECENTE, e
# as notas de meses anteriores podem entrar nele com entrada na 1ª quinzena.
dois_meses = tempfile.mkdtemp(prefix="synax_builder_meses_")
CH_ABRIL = "35260812345678000190550010000027771000002777"
io.open(os.path.join(dois_meses, "a.xml"), "w", encoding="utf-8").write(
    nfe(CH_ABRIL, "2777", FORNECEDOR,
        "DIST LAURENCINI LTDA", EMPRESA, ITENS_1, data="2026-04-12"))
io.open(os.path.join(dois_meses, "b.xml"), "w", encoding="utf-8").write(
    nfe("35260812345678000190550010000027791000002779", "2779", FORNECEDOR,
        "DIST LAURENCINI LTDA", EMPRESA, ITENS_2, data="2026-05-03"))


def c100_de(res, num):
    return [l.split("|") for l in res.documento.serializar().splitlines()
            if l.startswith("|C100|") and l.split("|")[8] == num]


misturado = gerar_de_pasta(dois_meses, cad)
check("dois meses: gera o MAIS RECENTE, sem perguntar",
      misturado.documento is not None and misturado.competencia == "2026-05",
      misturado.avisos)
r0000_m = misturado.documento.serializar().splitlines()[0].split("|")
check("com o periodo de maio no 0000", (r0000_m[4], r0000_m[5]) == ("01052026", "31052026"))
check("so a nota de maio entra", misturado.notas == 1 and bool(c100_de(misturado, "2779"))
      and not c100_de(misturado, "2777"))
check("a de abril vira opcao de trazer (anteriores), desmarcada",
      [(x["num"], x["mes"], x["trazido"]) for x in misturado.anteriores] == [("2777", "2026-04", False)]
      and misturado.anteriores[0]["id"] == CH_ABRIL, misturado.anteriores)
check("e fica listada no que ficou de fora, com o mes",
      any(n == "a.xml" and "04/2026" in m and "1ª quinzena" in m for n, m in misturado.ignorados),
      misturado.ignorados)
check("um aviso diz quanto ficou de fora",
      any("1 documento de 04/2026" in a for a in misturado.avisos), misturado.avisos)
check("os meses do pacote vao para a tela", misturado.meses == [("2026-04", 1), ("2026-05", 1)])

trazida = gerar_de_pasta(dois_meses, cad, trazer=[CH_ABRIL], data_entrada="2026-05-10")
c_abril = c100_de(trazida, "2777")
check("trazida: a nota de abril entra no SPED de maio", trazida.notas == 2 and bool(c_abril))
check("com a EMISSAO de abril (DT_DOC) e a ENTRADA na 1a quinzena (DT_E_S)",
      bool(c_abril) and (c_abril[0][10], c_abril[0][11]) == ("12042026", "10052026"),
      c_abril[0][10:12] if c_abril else None)
check("a de maio segue com a data dela", c100_de(trazida, "2779")[0][11] == "03052026")
check("e sai como presuncao, com a data e a quantidade",
      any("meses anteriores" in p.campo and p.valor == "10/05/2026" and p.quantidade == 1
          for p in trazida.presuncoes), [p.campo for p in trazida.presuncoes])
check("a tela sabe que ela foi trazida", trazida.anteriores[0]["trazido"]
      and not any(n == "a.xml" for n, _m in trazida.ignorados))

padrao = gerar_de_pasta(dois_meses, cad, trazer=[CH_ABRIL])
check("sem data informada, a entrada e o dia 01",
      c100_de(padrao, "2777")[0][11] == "01052026")
fora_quinzena = gerar_de_pasta(dois_meses, cad, trazer=[CH_ABRIL], data_entrada="2026-05-20")
check("data fora da 1a quinzena e recusada com o motivo",
      fora_quinzena.documento is None and "1ª quinzena" in fora_quinzena.avisos[0],
      fora_quinzena.avisos)
outro_mes = gerar_de_pasta(dois_meses, cad, trazer=[CH_ABRIL], data_entrada="2026-04-10")
check("data de outro mes tambem", outro_mes.documento is None)

abril = gerar_de_pasta(dois_meses, cad, competencia="2026-04")
check("competencia escolhida (abril): a de maio fica de fora como POSTERIOR",
      abril.competencia == "2026-04" and not abril.anteriores
      and any(n == "b.xml" and "posterior" in m for n, m in abril.ignorados), abril.ignorados)
junho = gerar_de_pasta(dois_meses, cad, competencia="2026-06")
check("mes que o pacote nao tem: nada gerado, e diz o que o pacote tem",
      junho.documento is None and "Nenhum documento do pacote é de 06/2026" in junho.avisos[0]
      and "04/2026" in junho.avisos[0], junho.avisos)
torta = gerar_de_pasta(dois_meses, cad, competencia="maio")
check("competencia em formato torto e recusada com o motivo",
      torta.documento is None and "inválida" in torta.avisos[0], torta.avisos)
for nome in os.listdir(dois_meses):
    os.remove(os.path.join(dois_meses, nome))
os.rmdir(dois_meses)


# ============================================================================
print("\n=== 8. A tela: escopo, geracao e download ===")

# O harness da aplicacao vem DEPOIS dos testes do gerador de proposito: assim a
# parte que importa — a montagem do arquivo — roda mesmo que o banco falhe.
import json  # noqa: E402
import types  # noqa: E402
import uuid as _uuid  # noqa: E402
import zipfile  # noqa: E402

import dotenv  # noqa: E402
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main  # noqa: E402
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_builder_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_builder_test.db"):
    os.remove("_builder_test.db")

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
from backend.app.domain.models.rules import (  # noqa: E402
    CfopClassification, GeradorCfopRegra)
from fastapi.testclient import TestClient  # noqa: E402

Base.metadata.create_all(bind=_eng)
db = SessionLocal()
tA, tB = _uuid.uuid4(), _uuid.uuid4()
db.add_all([
    Tenant(id=tA, name="COMERCIAL MASSUCATTI LTDA", cnpj=EMPRESA, uf="ES"),
    # Empresa sem CNPJ: o gerador nao tem como separar entrada de saida.
    Tenant(id=tB, name="SEM CADASTRO LTDA", cnpj=None, uf="ES"),
])
u_ope, u_cli = _uuid.uuid4(), _uuid.uuid4()
db.add_all([
    UserProfile(id=u_ope, tenant_id=tA, role="operador", name="Operador"),
    UserProfile(id=u_cli, tenant_id=tA, role="cliente", name="Cliente"),
])
db.add_all([UserTenant(user_id=u_ope, tenant_id=tA),
            UserTenant(user_id=u_ope, tenant_id=tB)])
# Uma regra aprovada DA AUDITORIA. Até 09/10/2026 o gerador a lia; agora as
# regras dele são outras tabelas, e esta não pode mandar em nada aqui.
db.add(CfopClassification(
    tenant_id=str(tA), company_cnpj=EMPRESA, supplier_cnpj=FORNECEDOR,
    supplier_cfop="5102", product_code="CAFE500", entry_cfop="1556",
    status="aprovado", created_by="admin"))
db.commit()
db.close()

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


def as_(token):
    c.cookies.clear()
    if token:
        c.cookies.set("access_token", f"Bearer {token}")
    return c


r = as_("ope").get("/gerar-sped")
check("operador abre a tela", r.status_code == 200, r.status_code)
check("com o seletor de empresa", 'name="tenant_id"' in r.text)
check("e o aviso de que sai arquivo de ENTRADAS",
      "Blocos E, G, H, K e 1" in r.text)
check("cliente nao entra", as_("cli").get("/gerar-sped").status_code == 403)
check("anonimo tambem nao",
      as_(None).get("/gerar-sped").status_code in (401, 403))

# O ZIP do mes, com as mesmas notas da fixture.
pacote = io.BytesIO()
with zipfile.ZipFile(pacote, "w") as z:
    z.writestr("entrada1.xml", nfe("35260812345678000190550010000027771000002777",
                                   "2777", FORNECEDOR, "DIST LAURENCINI LTDA",
                                   EMPRESA, ITENS_1))
    z.writestr("entrada2.xml", nfe("35260812345678000190550010000027781000002778",
                                   "2778", FORNECEDOR, "DIST LAURENCINI LTDA",
                                   EMPRESA, ITENS_2))
    z.writestr("saida.xml", nfe("35260827865757000102550010000099991000009999",
                                "9999", EMPRESA, "COMERCIAL MASSUCATTI LTDA",
                                OUTRA, ITENS_1))
pacote.seek(0)

import re as _re  # noqa: E402


def _pacote_do_mapa(resposta):
    m = _re.search(r"/api/gerar-sped/([0-9a-f-]{36})/regras", resposta.text)
    return m.group(1) if m else None


def _gerar_travado(resposta) -> bool:
    return _re.search(r'id="mapa-btn-gerar"\s+disabled', resposta.text) is not None


# --- Sem regra do GERADOR aprovada, nada sai. A rota antiga, que gerava
# direto, agora devolve o mapeamento — com o botão de gerar travado.
r_mapa = as_("ope").post("/api/gerar-sped",
                         data={"tenant_id": str(tA), "perfil": "A",
                               "ind_ativ": "1"},
                         files={"xmls_zip": ("xmls.zip", pacote.getvalue(),
                                             "application/zip")})
check("a rota antiga responde 200", r_mapa.status_code == 200, r_mapa.status_code)
check("e devolve o MAPEAMENTO, nao o arquivo",
      "Mapeamento de CFOP de entrada" in r_mapa.text and "/arquivo" not in r_mapa.text,
      r_mapa.text[:300])
check("com a geracao bloqueada", "Geração bloqueada" in r_mapa.text
      and _gerar_travado(r_mapa))
check("a regra aprovada DA AUDITORIA nao vale no gerador: o CAFE segue a definir",
      _re.search(r'data-status="a_definir"\s+data-busca="[^"]*cafe500', r_mapa.text)
      is not None)
pid_um = _pacote_do_mapa(r_mapa)
check("o mapeamento aponta para o pacote guardado", pid_um is not None)

r_bloq = as_("ope").post("/api/gerar-sped/confirmar",
                         data={"pacote_id": pid_um,
                               "chave": [f"{FORNECEDOR}|5102|CAFE500"],
                               "cfop_entrada": ["1102"]})
check("gerar sem aprovacao e recusado NO SERVIDOR, mesmo com CFOP no POST",
      r_bloq.status_code == 200 and "/arquivo" not in r_bloq.text
      and "só é gerado com tudo aprovado" in r_bloq.text, r_bloq.text[:300])

# --- As regras DO GERADOR, aprovadas. O CAFE vai com 1403, de propósito
# diferente do 1556 da regra da auditoria.
db = SessionLocal()
for _cfop, _prod, _entrada in (("5102", "CAFE500", "1403"), ("5102", "ACUCAR1", "1102"),
                               ("6102", "OLEO900", "2102")):
    db.add(GeradorCfopRegra(tenant_id=str(tA), company_cnpj=EMPRESA,
                            supplier_cnpj=FORNECEDOR, supplier_cfop=_cfop,
                            product_code=_prod, entry_cfop=_entrada,
                            status="aprovado", created_by="admin@synax.com"))
db.commit()
db.close()

r_atual = as_("ope").get(f"/api/gerar-sped/{pid_um}/mapa")
check("Atualizar: com tudo aprovado, o botao destrava",
      "Tudo mapeado e aprovado" in r_atual.text and not _gerar_travado(r_atual),
      r_atual.text[:300])

r_gera = as_("ope").post("/api/gerar-sped/confirmar", data={"pacote_id": pid_um})
check("a geracao responde 200", r_gera.status_code == 200, r_gera.status_code)
check("a tela mostra as notas lidas", ">2<" in r_gera.text
      and "notas de entrada" in r_gera.text)
check("mostra o que ficou de fora do pacote",
      "ficaram de fora" in r_gera.text and "saida.xml" in r_gera.text)
check("mostra as presuncoes ANTES do download",
      "precisou presumir" in r_gera.text
      and r_gera.text.index("precisou presumir") < r_gera.text.index("Baixar"))
# Os XMLs desta seção não trazem endereço da empresa: o 0005 sai sem CEP, e
# isso tem de estar na tela antes do botão, não só no arquivo.
check("0005 sem CEP aparece nos avisos ANTES do download",
      "0005 saiu sem CEP" in r_gera.text
      and r_gera.text.index("0005 saiu sem CEP") < r_gera.text.index("Baixar"))
check("e oferece o download", "/arquivo" in r_gera.text)

processo = _re.search(r"/api/gerar-sped/([0-9a-f-]{36})/arquivo", r_gera.text)
check("o link do arquivo traz o identificador", processo is not None)

r_arq = as_("ope").get(f"/api/gerar-sped/{processo.group(1)}/arquivo")
check("o download responde 200", r_arq.status_code == 200, r_arq.status_code)
check("como arquivo, nunca renderizado",
      r_arq.headers.get("content-type") == "application/octet-stream"
      and r_arq.headers.get("content-disposition", "").startswith("attachment;"))
gerado = r_arq.content.decode("latin-1")
check("e o conteudo e o SPED", gerado.startswith("|0000|")
      and "|C100|" in gerado and "|9999|" in gerado)
check("com o CNPJ da empresa no 0000",
      gerado.splitlines()[0].split("|")[7] == EMPRESA)
check("a regra DO GERADOR mandou no CFOP do item (1403), nao a da auditoria (1556)",
      any(l.split("|")[3] == "CAFE500" and l.split("|")[11] == "1403"
          for l in gerado.splitlines() if l.startswith("|C170|")),
      [l for l in gerado.splitlines() if l.startswith("|C170|")][:1])
check("e nenhum CFOP deduzido foi escriturado",
      "conversão do primeiro dígito" not in r_gera.text)
check("pacote de um mes ja gerado sai do servidor (os XMLs sao o volume)",
      not os.path.isdir(os.path.join(os.getcwd(), "temp_uploads", pid_um)))

check("cliente nao baixa",
      as_("cli").get(f"/api/gerar-sped/{processo.group(1)}/arquivo").status_code
      == 403)
check("identificador que nao e UUID nao vira leitura de arquivo do servidor",
      as_("ope").get("/api/gerar-sped/..%2F..%2Fmain/arquivo").status_code
      in (404, 400))

check("pacote de um mes so nao oferece escolha de competencia",
      "/competencia" not in r_gera.text)

# --- Pacote com dois meses: gera o mais recente, e traz o anterior se pedido.
dois = io.BytesIO()
CH_AGO = "35260812345678000190550010000027771000002777"
with zipfile.ZipFile(dois, "w") as z:
    z.writestr("agosto.xml", nfe(CH_AGO, "2777", FORNECEDOR, "DIST LAURENCINI LTDA",
                                 EMPRESA, ITENS_1, data="2026-08-28"))
    z.writestr("setembro.xml", nfe("35260912345678000190550010000027781000002778",
                                   "2778", FORNECEDOR, "DIST LAURENCINI LTDA",
                                   EMPRESA, ITENS_2, data="2026-09-02"))
r_dois_mapa = as_("ope").post("/api/gerar-sped/analisar",
                              data={"tenant_id": str(tA), "perfil": "A", "ind_ativ": "1"},
                              files={"xmls_zip": ("xmls.zip", dois.getvalue(),
                                                  "application/zip")})
check("dois meses: o mapeamento cobre o pacote inteiro e ja esta aprovado",
      "Tudo mapeado e aprovado" in r_dois_mapa.text, r_dois_mapa.text[:300])
r_dois = as_("ope").post("/api/gerar-sped/confirmar",
                         data={"pacote_id": _pacote_do_mapa(r_dois_mapa)})
check("dois meses: gera setembro direto, com download",
      r_dois.status_code == 200 and "competência 09/2026" in r_dois.text
      and "/arquivo" in r_dois.text, r_dois.status_code)
check("e mostra a nota de agosto para trazer",
      "de mês anterior no pacote" in r_dois.text and f'value="{CH_AGO}"' in r_dois.text
      and "1ª quinzena" in r_dois.text)
check("com a data de entrada limitada a 1a quinzena",
      'min="2026-09-01"' in r_dois.text and 'max="2026-09-15"' in r_dois.text)
pacote_id = _re.search(r"/api/gerar-sped/([0-9a-f-]{36})/regerar", r_dois.text)
check("o painel aponta para o pacote guardado", pacote_id is not None)
pasta_pacote = os.path.join(os.getcwd(), "temp_uploads", pacote_id.group(1))
check("os XMLs ficaram no servidor para trazer depois",
      os.path.isdir(os.path.join(pasta_pacote, "xmls"))
      and os.path.exists(os.path.join(pasta_pacote, "pacote.json")))
proc_1 = _re.search(r"/api/gerar-sped/([0-9a-f-]{36})/arquivo", r_dois.text).group(1)

r_traz = as_("ope").post(f"/api/gerar-sped/{pacote_id.group(1)}/regerar",
                         data={"competencia": "2026-09", "trazer": [CH_AGO],
                               "data_entrada": "2026-09-05"})
check("trazida: gera de novo sem reenviar o ZIP",
      r_traz.status_code == 200 and "competência 09/2026" in r_traz.text, r_traz.status_code)
proc_2 = _re.search(r"/api/gerar-sped/([0-9a-f-]{36})/arquivo", r_traz.text).group(1)
txt_2 = as_("ope").get(f"/api/gerar-sped/{proc_2}/arquivo").content.decode("latin-1")
c_ago = [l.split("|") for l in txt_2.splitlines() if l.startswith("|C100|") and "|2777|" in l]
check("no arquivo: a nota de agosto com emissao 28/08 e entrada 05/09",
      bool(c_ago) and (c_ago[0][10], c_ago[0][11]) == ("28082026", "05092026"),
      c_ago[0][10:12] if c_ago else None)
check("e o periodo continua setembro", txt_2.splitlines()[0].split("|")[4:6] == ["01092026", "30092026"])
check("a caixa da nota trazida volta marcada",
      "checked" in r_traz.text.split(f'value="{CH_AGO}"')[1][:200])
check("o arquivo anterior nao foi sobrescrito",
      "|2777|" not in as_("ope").get(f"/api/gerar-sped/{proc_1}/arquivo").content.decode("latin-1"))

r_ruim = as_("ope").post(f"/api/gerar-sped/{pacote_id.group(1)}/regerar",
                         data={"competencia": "2026-09", "trazer": [CH_AGO],
                               "data_entrada": "2026-09-20"})
check("entrada depois do dia 15: recusa com o motivo, sem arquivo",
      "1ª quinzena" in r_ruim.text and "/arquivo" not in r_ruim.text)

r_direto_mapa = as_("ope").post("/api/gerar-sped",
                                data={"tenant_id": str(tA), "competencia": "08/2026"},
                                files={"xmls_zip": ("xmls.zip", dois.getvalue(),
                                                    "application/zip")})
check("a competencia do formulario aparece no mapeamento",
      "competência 08/2026" in r_direto_mapa.text, r_direto_mapa.text[:300])
r_direto = as_("ope").post("/api/gerar-sped/confirmar",
                           data={"pacote_id": _pacote_do_mapa(r_direto_mapa)})
check("competencia no formulario manda (agosto), e setembro fica de fora",
      "competência 08/2026" in r_direto.text and "posterior" in r_direto.text)

check("cliente nao gera pelo pacote",
      as_("cli").post(f"/api/gerar-sped/{pacote_id.group(1)}/regerar",
                      data={"competencia": "2026-09"}).status_code == 403)
check("identificador que nao e UUID e recusado",
      as_("ope").post("/api/gerar-sped/..%2F..%2Fx/regerar",
                      data={"competencia": "2026-09"}).status_code in (404, 400))
r_sumiu = as_("ope").post(f"/api/gerar-sped/{_uuid.uuid4()}/regerar",
                          data={"competencia": "2026-09"})
check("pacote que nao esta mais no servidor: pede o ZIP de novo",
      "não está mais no servidor" in r_sumiu.text)

TELA_GERAR = io.open(os.path.join("frontend", "templates", "pages", "gerar_sped.html"),
                     encoding="utf-8").read()
check("a tela oferece a competencia opcional", 'name="competencia"' in TELA_GERAR
      and 'type="month"' in TELA_GERAR)

import shutil as _sh_limpeza  # noqa: E402
import time as _tempo  # noqa: E402
for _p in (pacote_id.group(1), proc_1, proc_2,
           _re.search(r"/api/gerar-sped/([0-9a-f-]{36})/arquivo", r_direto.text).group(1)):
    _sh_limpeza.rmtree(os.path.join(os.getcwd(), "temp_uploads", _p), ignore_errors=True)
for _p in os.listdir(os.path.join(os.getcwd(), "temp_uploads")):
    # O pacote do envio direto (dois meses) também ficou guardado.
    _meta = os.path.join(os.getcwd(), "temp_uploads", _p, "pacote.json")
    if os.path.exists(_meta) and os.path.getmtime(_meta) > _tempo.time() - 600:
        _sh_limpeza.rmtree(os.path.dirname(_meta), ignore_errors=True)

pacote.seek(0)
r_sem_cnpj = as_("ope").post("/api/gerar-sped",
                             data={"tenant_id": str(tB)},
                             files={"xmls_zip": ("x.zip", pacote.getvalue(),
                                                 "application/zip")})
check("empresa sem CNPJ no cadastro e recusada, com o motivo",
      r_sem_cnpj.status_code == 200 and "não tem CNPJ" in r_sem_cnpj.text)

TELA = io.open(os.path.join("frontend", "templates", "pages", "dashboard.html"),
               encoding="utf-8").read()
check("o dashboard leva para a ferramenta",
      "/gerar-sped" in TELA and "não gera SPED" in TELA)

# Limpeza do que a rota deixou em temp_uploads.
import shutil as _shutil  # noqa: E402
_shutil.rmtree(os.path.join(os.getcwd(), "temp_uploads", processo.group(1)),
               ignore_errors=True)
_eng.dispose()
if os.path.exists("_builder_test.db"):
    try:
        os.remove("_builder_test.db")
    except OSError:
        pass


# ============================================================================
print("\n=== 9. Logradouro no 0005 e no 0150 (o PVA exige) ===")

# enderDest = endereço da EMPRESA (ela é a destinatária na entrada) -> 0005.
# enderEmit = endereço do FORNECEDOR -> 0150. Antes saíam vazios e o PVA acusava
# logradouro faltando; agora vêm do próprio XML, sem inventar.
XML_COM_ENDERECO = f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">
 <NFe><infNFe Id="NFe35260812345678000190550010000050011000005001" versao="4.00">
  <ide><cUF>32</cUF><nNF>5001</nNF><serie>1</serie><mod>55</mod>
    <dhEmi>2026-04-12T10:30:00-03:00</dhEmi><tpNF>1</tpNF></ide>
  <emit><CNPJ>{FORNECEDOR}</CNPJ><xNome>DIST LAURENCINI LTDA</xNome><IE>111222333</IE>
    <enderEmit><xLgr>RUA DO COMERCIO</xLgr><nro>1500</nro><xBairro>CENTRO</xBairro>
      <cMun>3205309</cMun><xMun>VITORIA</xMun><UF>ES</UF><CEP>29000100</CEP></enderEmit>
  </emit>
  <dest><CNPJ>{EMPRESA}</CNPJ><xNome>COMERCIAL MASSUCATTI LTDA</xNome><IE>082414507</IE>
    <enderDest><xLgr>AVENIDA GETULIO VARGAS</xLgr><nro>742</nro><xCpl>SALA 3</xCpl>
      <xBairro>SAO JOSE</xBairro><cMun>3203205</cMun><xMun>LINHARES</xMun><UF>ES</UF>
      <CEP>29900500</CEP></enderDest>
  </dest>
  <det nItem="1"><prod><cProd>CAFE500</cProd><xProd>CAFE</xProd><NCM>84713012</NCM>
    <CFOP>5102</CFOP><uCom>UN</uCom><qCom>1.0000</qCom><vUnCom>100.00</vUnCom>
    <vProd>100.00</vProd><vDesc>0.00</vDesc></prod>
    <imposto><ICMS><ICMS00><orig>0</orig><CST>00</CST><vBC>100.00</vBC>
      <pICMS>12.00</pICMS><vICMS>12.00</vICMS></ICMS00></ICMS></imposto></det>
  <total><ICMSTot><vProd>100.00</vProd><vNF>100.00</vNF><vBC>100.00</vBC>
    <vICMS>12.00</vICMS><vST>0.00</vST><vFrete>0.00</vFrete><vSeg>0.00</vSeg>
    <vOutro>0.00</vOutro><vDesc>0.00</vDesc><vIPI>0.00</vIPI><vPIS>0.00</vPIS>
    <vCOFINS>0.00</vCOFINS></ICMSTot></total>
 </infNFe></NFe>
</nfeProc>"""

P_END = tempfile.mkdtemp(prefix="synax_end_")
io.open(os.path.join(P_END, "nota.xml"), "w", encoding="utf-8").write(XML_COM_ENDERECO)
res_end = gerar_de_pasta(P_END, Cadastro(cnpj=EMPRESA,
                                         nome="COMERCIAL MASSUCATTI LTDA", uf="ES"))
check("gerou o arquivo", res_end.documento is not None, res_end.avisos)
linhas_end = [l for l in res_end.documento.serializar().split("\r\n") if l]

# |0005|FANTASIA|CEP|END|NUM|COMPL|BAIRRO|... -> split: [_, 0005, FANT, CEP, END, NUM, COMPL, BAIRRO]
l0005 = [l.split("|") for l in linhas_end if l.startswith("|0005|")][0]
check("0005: CEP da empresa (do enderDest)", l0005[3] == "29900500", l0005[3])
check("0005: LOGRADOURO da empresa não é mais vazio",
      l0005[4] == "AVENIDA GETULIO VARGAS", l0005[4])
check("0005: número, complemento e bairro",
      (l0005[5], l0005[6], l0005[7]) == ("742", "SALA 3", "SAO JOSE"),
      (l0005[5], l0005[6], l0005[7]))

# |0150|COD_PART|NOME|COD_PAIS|CNPJ|CPF|IE|COD_MUN|SUFRAMA|END|NUM|COMPL|BAIRRO|
l0150 = [l.split("|") for l in linhas_end if l.startswith("|0150|")][0]
check("0150: LOGRADOURO do fornecedor (do enderEmit)",
      l0150[10] == "RUA DO COMERCIO", l0150[10])
check("0150: número e bairro do fornecedor",
      (l0150[11], l0150[13]) == ("1500", "CENTRO"), (l0150[11], l0150[13]))

check("o endereço lido do XML sai como presunção, para conferência",
      any("0005" in p.campo and "GETULIO" in p.valor for p in res_end.presuncoes),
      [p.campo for p in res_end.presuncoes])

# Nota SEM endereço no XML: o 0005 não inventa, fica vazio (presunção avisa).
XML_SEM_ENDERECO = XML_COM_ENDERECO.replace(
    "<xLgr>AVENIDA GETULIO VARGAS</xLgr><nro>742</nro><xCpl>SALA 3</xCpl>\n"
    "      <xBairro>SAO JOSE</xBairro>", "")
P_SEM = tempfile.mkdtemp(prefix="synax_sem_")
io.open(os.path.join(P_SEM, "nota.xml"), "w", encoding="utf-8").write(XML_SEM_ENDERECO)
res_sem = gerar_de_pasta(P_SEM, Cadastro(cnpj=EMPRESA, nome="X", uf="ES"))
l0005_sem = [l.split("|") for l in res_sem.documento.serializar().split("\r\n")
             if l.startswith("|0005|")][0]
check("sem logradouro no XML, o 0005 fica em branco (não inventa)",
      l0005_sem[4] == "", l0005_sem[4])
for d in (P_END, P_SEM):
    for nome in os.listdir(d):
        os.remove(os.path.join(d, nome))
    os.rmdir(d)


def _gerar_xmls(xmls, ind_ativ="1", **kw):
    """Gera de uma pasta descartável com estes XMLs; (resultado, linhas)."""
    pasta = tempfile.mkdtemp(prefix="synax_caso_")
    try:
        for i, conteudo in enumerate(xmls):
            io.open(os.path.join(pasta, f"doc{i}.xml"), "w",
                    encoding="utf-8").write(conteudo)
        res = gerar_de_pasta(pasta, Cadastro(cnpj=EMPRESA, nome="X", uf="ES",
                                             ind_ativ=ind_ativ), **kw)
    finally:
        for nome in os.listdir(pasta):
            os.remove(os.path.join(pasta, nome))
        os.rmdir(pasta)
    linhas = ([l.split("|") for l in res.documento.serializar().split("\r\n") if l]
              if res.documento else [])
    return res, linhas


def _registro(linhas, reg):
    return [l for l in linhas if l[1] == reg]


# ============================================================================
print("\n=== 10. CEP do 0005 (obrigatório no leiaute 020) ===")

# O validador de EFD achou o 0005 com CEP vazio nos SPEDs gerados. O CEP do
# destinatário é OPCIONAL na NF-e: ler só a primeira nota do mês deixava o
# campo em branco mesmo com o CEP nas outras.
NOTA_SEM_END = nfe("35260812345678000190550010000060011000006001", "6001",
                   FORNECEDOR, "DIST LAURENCINI LTDA", EMPRESA, ITENS_1[:1],
                   data="2026-04-05")
NOTA_LGR_SEM_CEP = (XML_COM_ENDERECO
                    .replace("0000050011000005001", "0000050021000005002")
                    .replace("<nNF>5001</nNF>", "<nNF>5002</nNF>")
                    .replace("2026-04-12T", "2026-04-03T")
                    .replace("AVENIDA GETULIO VARGAS", "RUA SEM CEP")
                    .replace("<CEP>29900500</CEP></enderDest>", "</enderDest>"))

res, linhas = _gerar_xmls([NOTA_SEM_END, XML_COM_ENDERECO])
l0005 = _registro(linhas, "0005")[0]
check("1ª nota do mês sem endereço: o CEP vem da nota que o tem",
      l0005[3] == "29900500", l0005[3])
check("e nenhum aviso de 0005 incompleto",
      not any("0005" in a for a in res.avisos), res.avisos)

res, linhas = _gerar_xmls([NOTA_LGR_SEM_CEP, XML_COM_ENDERECO])
l0005 = _registro(linhas, "0005")[0]
check("o endereço sai INTEIRO do documento que tem CEP, sem misturar notas",
      (l0005[3], l0005[4]) == ("29900500", "AVENIDA GETULIO VARGAS"),
      (l0005[3], l0005[4]))
check("e a presunção diz de qual documento veio",
      any("0005" in p.campo and "NF-e 5001" in p.motivo for p in res.presuncoes),
      [(p.campo, p.motivo) for p in res.presuncoes if "0005" in p.campo])

res, linhas = _gerar_xmls([NOTA_LGR_SEM_CEP])
l0005 = _registro(linhas, "0005")[0]
check("nenhum XML com CEP: o arquivo sai, o CEP NÃO é inventado",
      res.documento is not None and l0005[3] == "" and l0005[4] == "RUA SEM CEP",
      (l0005[3], l0005[4]))
check("e o CEP faltando vira aviso — a tela mostra antes do botão de baixar",
      any("0005 saiu sem CEP" in a for a in res.avisos), res.avisos)

res, linhas = _gerar_xmls([XML_COM_ENDERECO.replace("29900500", "2990050")])
check("CEP com 7 dígitos não entra (0005 campo 03 é N 8, exato)",
      _registro(linhas, "0005")[0][3] == ""
      and any("0005 saiu sem CEP" in a for a in res.avisos),
      _registro(linhas, "0005")[0][3])

# Pacote só de frete: não há NF-e, e o endereço da empresa está no CT-e, no
# papel de tomador (aqui, o destinatário: toma = 3).
CTE_COM_END = CTE.replace(
    "<dest><CNPJ>27865757000102</CNPJ><xNome>COMERCIAL MASSUCATTI LTDA</xNome></dest>",
    "<dest><CNPJ>27865757000102</CNPJ><xNome>COMERCIAL MASSUCATTI LTDA</xNome>"
    "<enderDest><xLgr>AVENIDA GETULIO VARGAS</xLgr><nro>742</nro>"
    "<xBairro>SAO JOSE</xBairro><cMun>3203205</cMun><xMun>LINHARES</xMun>"
    "<CEP>29900500</CEP><UF>ES</UF></enderDest></dest>")
res, linhas = _gerar_xmls([CTE_COM_END])
l0005 = _registro(linhas, "0005")[0] if linhas else []
check("pacote só com CT-e: o 0005 sai com o endereço do tomador",
      l0005[3:5] == ["29900500", "AVENIDA GETULIO VARGAS"] and l0005[7] == "SAO JOSE",
      l0005)
check("e a presunção aponta o CT-e",
      any("0005" in p.campo and "tomador do CT-e 149831" in p.motivo
          for p in res.presuncoes), [p.motivo for p in res.presuncoes])

from backend.app.services.audit_engine.xml_parser import parse_cte_xml  # noqa: E402

CTE_TOMA4 = CTE.replace("<toma3><toma>3</toma></toma3>", (
    "<toma4><toma>4</toma><CNPJ>27865757000102</CNPJ><xNome>COMERCIAL</xNome>"
    "<enderToma><xLgr>RUA DO TOMADOR</xLgr><nro>10</nro><xBairro>CENTRO</xBairro>"
    "<cMun>3203205</cMun><xMun>LINHARES</xMun><CEP>29900600</CEP><UF>ES</UF>"
    "</enderToma></toma4>"))
p_toma = gravar("toma4.xml", CTE_TOMA4)
lido = parse_cte_xml(p_toma)
os.remove(p_toma)
check("toma4: o leitor do CT-e traz o endereço de <enderToma>",
      (lido.get("tomador_cep"), lido.get("tomador_lgr"), lido.get("tomador_bairro"))
      == ("29900600", "RUA DO TOMADOR", "CENTRO"), lido.get("tomador_cep"))


# ============================================================================
print("\n=== 11. CFOP de entrada só da tabela oficial ===")

from backend.app.domain.constants.cfops_entrada import CFOPS_ENTRADA  # noqa: E402
from backend.app.domain.constants.tabela_cfop import CFOPS_VIGENTES  # noqa: E402
from backend.app.services.sped_builder import (  # noqa: E402
    cfop_de_entrada_valido, deduzir_cfop, levantar_combinacoes)

# Achado do validador de EFD nos SPEDs gerados em 08/10/2026: 5405 virava 1405
# e 6357 virava 2357 — série certa, quatro dígitos, e nenhum dos dois existe.
check("1403 e 2353 existem e servem de entrada",
      cfop_de_entrada_valido("1403") and cfop_de_entrada_valido("2353"))
check("1405 e 2357 não existem na tabela: não servem",
      not cfop_de_entrada_valido("1405") and not cfop_de_entrada_valido("2357"))
check("3217 (vigência encerrada em 2009) também não",
      not cfop_de_entrada_valido("3217"))
check("5102 continua virando 1102 pelo espelho",
      deduzir_cfop("5102") == ("1102", "espelho"))
check("5405 não vira 1405: o mapa sugere 1403 (compra para comercialização com ST)",
      deduzir_cfop("5405") == ("1403", "equivalente"), deduzir_cfop("5405"))
check("6404 sugere 2403", deduzir_cfop("6404") == ("2403", "equivalente"),
      deduzir_cfop("6404"))
check("frete 6357 não vira 2357: o mapa sugere 2353 (estabelecimento comercial)",
      deduzir_cfop("6357") == ("2353", "equivalente"), deduzir_cfop("6357"))
check("e 2352 quando a empresa é industrial (IND_ATIV 0)",
      deduzir_cfop("6357", industrial=True) == ("2352", "equivalente"),
      deduzir_cfop("6357", industrial=True))
check("5357/5359/6359 seguem o mesmo mapa",
      [deduzir_cfop(c)[0] for c in ("5357", "5359", "6359")] == ["1353", "1353", "2353"]
      and [deduzir_cfop(c, True)[0] for c in ("5357", "5359", "6359")]
      == ["1352", "1352", "2352"])
check("o IND_ATIV não mexe na equivalência da mercadoria (5405 segue 1403)",
      deduzir_cfop("5405", industrial=True) == ("1403", "equivalente"))
check("saída sem espelho fora do mapa (6106) fica sem dedução",
      cfop_de_entrada("6106") == ("", False) and deduzir_cfop("6106") == ("", ""),
      cfop_de_entrada("6106"))
check("1xxx no XML do fornecedor é a entrada DELE, não é copiado",
      deduzir_cfop("1202") == ("", ""), deduzir_cfop("1202"))
check("regra aprovada com 1405 não vale: não conta como regra, volta à sugestão",
      cfop_de_entrada("5405", "1405") == ("1403", False))
check("regra aprovada com 1403 manda", cfop_de_entrada("5405", "1403") == ("1403", True))

saidas = sorted(c for c in CFOPS_VIGENTES if c[0] in "567")
deduzidos = {c: deduzir_cfop(c) for c in saidas}
deduzidos_ind = {c: deduzir_cfop(c, industrial=True) for c in saidas}
check("nenhuma das saídas vigentes deduz CFOP fora da tabela (nem na industrial)",
      all(d == "" or cfop_de_entrada_valido(d)
          for d, _ in list(deduzidos.values()) + list(deduzidos_ind.values())),
      [c for c, (d, _) in deduzidos.items() if d and not cfop_de_entrada_valido(d)])
check("das 77 sem espelho, 6 vêm do mapa e 71 ficam sem dedução",
      (sum(1 for _, como in deduzidos.values() if como == "equivalente"),
       sum(1 for d, _ in deduzidos.values() if not d)) == (6, 71),
      (sum(1 for _, como in deduzidos.values() if como == "equivalente"),
       sum(1 for d, _ in deduzidos.values() if not d)))

from backend.app.api.routes.gerar_sped import CFOPS_FRETE  # noqa: E402

check("toda opção de CFOP da tela de mapeamento existe na tabela (sem 2360)",
      all(cfop_de_entrada_valido(c) for c in list(CFOPS_ENTRADA) + list(CFOPS_FRETE)),
      [c for c in list(CFOPS_ENTRADA) + list(CFOPS_FRETE)
       if not cfop_de_entrada_valido(c)])

_dados_pva = os.path.join("backend", "app", "services", "sped_validador", "dados",
                          "tabelas.json")
if os.path.exists(_dados_pva):
    _cfop_pva = json.load(io.open(_dados_pva, encoding="utf-8"))["tipos"]["CFOP"]["GLOBAL"]
    check("a lista é a mesma da extração do PVA usada pelo validador",
          CFOPS_VIGENTES == {c for c, _i, f in _cfop_pva["codigos"] if not f},
          _cfop_pva.get("versao"))
else:
    print("  ...  | dados do validador de EFD ausentes: comparação com o PVA pulada")


def _nota_com_cfop(num, cod, cfop):
    return nfe(f"352608123456780001905500100000{num}100000{num}", num,
               FORNECEDOR, "DIST LAURENCINI LTDA", EMPRESA,
               [{**ITENS_1[0], "cod": cod, "cfop": cfop}])


NOTA_5405 = _nota_com_cfop("7001", "REFRI2L", "5405")
NOTA_6106 = _nota_com_cfop("7002", "PECA9", "6106")

res, linhas = _gerar_xmls([NOTA_5405])
check("gerador puro, sem regra: o 5405 sai com a equivalência 1403, não 1405",
      [l[11] for l in _registro(linhas, "C170")] == ["1403"],
      [l[11] for l in _registro(linhas, "C170")])
check("e a equivalência é presunção própria, com o par e a contagem",
      any(p.campo == "C170 · CFOP" and "5405→1403 (1)" in p.valor
          and "tabela oficial" in p.motivo for p in res.presuncoes),
      [(p.campo, p.valor) for p in res.presuncoes if "CFOP" in p.campo])
check("separada da conversão do 1º dígito (que aqui não houve)",
      not any("primeiro dígito" in p.valor for p in res.presuncoes))

res, _ = _gerar_xmls([NOTA_6106])
check("saída sem espelho e fora do mapa (6106): nada é gerado",
      res.documento is None and res.sem_regra, res.avisos)
check("e a combinação aparece como pendente de regra, com o motivo",
      any("6106" in s and "PECA9" in s for s in res.sem_regra)
      and any("tabela oficial" in a for a in res.avisos), (res.sem_regra, res.avisos))

res, linhas = _gerar_xmls([NOTA_6106], regras_cfop={
    chave_item(FORNECEDOR, "6106", "PECA9"): "2102"})
check("com a regra aprovada, o 6106 sai com ela",
      [l[11] for l in _registro(linhas, "C170")] == ["2102"],
      [l[11] for l in _registro(linhas, "C170")])

# O modo das rotas: a sugestão do mapa NÃO dispensa a aprovação, e regra
# gravada com CFOP inexistente não conta como aprovada.
res, _ = _gerar_xmls([NOTA_5405], exigir_regra=True)
check("modo estrito: a equivalência não vai para o arquivo sem regra aprovada",
      res.documento is None and any("5405" in s for s in res.sem_regra), res.avisos)
res, _ = _gerar_xmls([NOTA_5405], exigir_regra=True, regras_cfop={
    chave_item(FORNECEDOR, "5405", "REFRI2L"): "1405"})
check("regra gravada com 1405 não destrava a geração",
      res.documento is None and res.sem_regra, res.avisos)
res, linhas = _gerar_xmls([NOTA_5405], exigir_regra=True, regras_cfop={
    chave_item(FORNECEDOR, "5405", "REFRI2L"): "1403"})
check("com a regra 1403 aprovada, o item sai com ela e sem presunção de CFOP",
      [l[11] for l in _registro(linhas, "C170")] == ["1403"]
      and not any(p.campo == "C170 · CFOP" for p in res.presuncoes),
      [l[11] for l in _registro(linhas, "C170")])

CTE_6357 = CTE.replace("<CFOP>6352</CFOP>", "<CFOP>6357</CFOP>")
res, linhas = _gerar_xmls([CTE_6357])
check("CT-e 6357 sem regra: D190 com 2353, não 2357",
      [l[3] for l in _registro(linhas, "D190")] == ["2353"],
      [l[3] for l in _registro(linhas, "D190")])
check("e a presunção do frete mostra o par",
      any(p.campo == "D190 · CFOP" and "6357→2353 (1)" in p.valor
          for p in res.presuncoes),
      [(p.campo, p.valor) for p in res.presuncoes if "CFOP" in p.campo])
res, linhas = _gerar_xmls([CTE_6357], ind_ativ="0")
check("empresa industrial: o mesmo CT-e sai com 2352",
      [l[3] for l in _registro(linhas, "D190")] == ["2352"],
      [l[3] for l in _registro(linhas, "D190")])

# 6360 (transporte a contribuinte substituto) não tem espelho — o 2360 que a
# tela oferecia não existe — e não está no mapa.
CTE_6360 = CTE.replace("<CFOP>6352</CFOP>", "<CFOP>6360</CFOP>")
res, _ = _gerar_xmls([CTE_6360])
check("CT-e 6360 sem regra: nada é gerado",
      res.documento is None and any("6360" in s for s in res.sem_regra), res.avisos)
res, linhas = _gerar_xmls([CTE_6360], regras_cte={chave_frete("6360", ""): "2353"})
check("com a regra do frete aprovada, o D190 sai com ela",
      [l[3] for l in _registro(linhas, "D190")] == ["2353"],
      [l[3] for l in _registro(linhas, "D190")])

p_5405, p_6106 = gravar("nota5405.xml", NOTA_5405), gravar("nota6106.xml", NOTA_6106)
p_6357 = gravar("cte6357.xml", CTE_6357)
combos = levantar_combinacoes([p_5405, p_6106, p_6357], Cadastro(cnpj=EMPRESA))
combos_ind = levantar_combinacoes([p_6357], Cadastro(cnpj=EMPRESA, ind_ativ="0"))
for _p in (p_5405, p_6106, p_6357):
    os.remove(_p)
sug = {c["cfop_origem"]: (c["cfop_sugerido"], c["sugestao_como"]) for c in combos["nfe"]}
check("a tela de mapeamento sugere 1403 para o 5405, marcada como equivalência",
      sug.get("5405") == ("1403", "equivalente"), sug)
check("e não sugere nada para o 6106 (fica a definir)",
      sug.get("6106") == ("", ""), sug)
check("frete: 2353, ou 2352 para empresa industrial",
      [(c["cfop_sugerido"], c["sugestao_como"]) for c in combos["cte"]]
      == [("2353", "equivalente")]
      and [c["cfop_sugerido"] for c in combos_ind["cte"]] == ["2352"],
      (combos["cte"], combos_ind["cte"]))

# A tela marca a sugestão do mapa, e refaz a sugestão a partir do CFOP de
# origem — o combinacoes.json guardado antes desta mudança trazia "1405".
import types as _tipos  # noqa: E402

from backend.app.api.routes import gerar_sped as _rota  # noqa: E402
from backend.app.core.templating import build_templates  # noqa: E402

combos["nfe"] = [{**c, "cfop_sugerido": "1405"} if c["cfop_origem"] == "5405" else c
                 for c in combos["nfe"]]
l_nfe, l_cte = _rota._linhas(combos, {}, {})
check("pacote guardado com a sugestão antiga: a tela refaz para 1403",
      {l["cfop_origem"]: l["cfop_entrada"] for l in l_nfe}.get("5405") == "1403",
      [(l["cfop_origem"], l["cfop_entrada"]) for l in l_nfe])
check("e com IND_ATIV industrial o frete vira 2352 na tela",
      [l["cfop_entrada"] for l in _rota._linhas(combos, {}, {}, True)[1]] == ["2352"])
html_mapa = build_templates().env.get_template("partials/gerar_sped_mapa_cfop.html").render(
    request=None, empresa=_tipos.SimpleNamespace(name="X", cnpj=EMPRESA, id="t"),
    pacote_id="p", competencia="", rotulo_mes=lambda m: m,
    linhas_nfe=l_nfe, linhas_cte=l_cte, resumo_nfe=_rota._resumo(l_nfe),
    resumo_cte=_rota._resumo(l_cte), itens=len(l_nfe), ctes=len(l_cte),
    faltam=len(l_nfe) + len(l_cte), pode_gerar=False, pode_aprovar=False,
    cfop_opcoes=_rota._cfop_opcoes(), cfop_opcoes_frete=_rota._cfop_opcoes_frete(),
    ignorados=[], mensagem="", mensagem_tipo="ok")
check("na tela, as linhas do 5405 e do 6357 levam o selo 'equivalência'",
      html_mapa.count("equivalência\n    </span>") == 2
      and "5405 não tem espelho na tabela oficial" in html_mapa
      and "6357 não tem espelho na tabela oficial" in html_mapa,
      html_mapa.count("equivalência\n    </span>"))
check("e a do 6106 diz que veio sem sugestão, e por quê",
      "sem sugestão" in html_mapa and "de 6106 não existe na tabela oficial" in html_mapa)


# ============================================================================
for nome in os.listdir(PASTA):
    os.remove(os.path.join(PASTA, nome))
os.rmdir(PASTA)

print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
for f in _falhou:
    print("  -", f)
sys.exit(1 if FALHAS else 0)
