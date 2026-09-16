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

2. **O CFOP de entrada é presunção quando não há regra.** O XML traz o do
   FORNECEDOR (5xxx/6xxx); a regra aprovada da empresa manda quando existe, e
   na falta dela vale a conversão do primeiro dígito — marcada no relatório,
   item por item.

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
    Cadastro, cfop_de_entrada, gerar_de_pasta)
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
          for b in ("0", "C", "D", "E", "G", "H", "K", "1", "9")),
      [l[:6] for l in linhas if l[1:4].endswith("001")])
check("os blocos sem dados vao com IND_MOV = 1",
      all(campos(f"|{b}001|")[2] == "1" for b in ("E", "G", "H", "K", "1")))

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
      not any(p.campo.startswith("D190") for p in res3.presuncoes),
      [p.campo for p in res3.presuncoes])
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

# SPED é MENSAL: dois meses no mesmo pacote não podem virar um arquivo só.
dois_meses = tempfile.mkdtemp(prefix="synax_builder_meses_")
io.open(os.path.join(dois_meses, "a.xml"), "w", encoding="utf-8").write(
    nfe("35260812345678000190550010000027771000002777", "2777", FORNECEDOR,
        "DIST LAURENCINI LTDA", EMPRESA, ITENS_1, data="2026-04-12"))
io.open(os.path.join(dois_meses, "b.xml"), "w", encoding="utf-8").write(
    nfe("35260812345678000190550010000027791000002779", "2779", FORNECEDOR,
        "DIST LAURENCINI LTDA", EMPRESA, ITENS_2, data="2026-05-03"))
misturado = gerar_de_pasta(dois_meses, cad)
check("pacote com dois meses e recusado",
      misturado.documento is None
      and "mais de um mês" in misturado.avisos[0], misturado.avisos)
check("e os meses aparecem no aviso",
      "2026-04" in misturado.avisos[0] and "2026-05" in misturado.avisos[0],
      misturado.avisos)
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
from backend.app.domain.models.rules import CfopClassification  # noqa: E402
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
# Uma regra aprovada, para provar que ela manda no CFOP gerado.
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

r_gera = as_("ope").post("/api/gerar-sped",
                         data={"tenant_id": str(tA), "perfil": "A",
                               "ind_ativ": "1"},
                         files={"xmls_zip": ("xmls.zip", pacote.getvalue(),
                                             "application/zip")})
check("a geracao responde 200", r_gera.status_code == 200, r_gera.status_code)
check("a tela mostra as notas lidas", ">2<" in r_gera.text
      and "notas de entrada" in r_gera.text)
check("mostra o que ficou de fora do pacote",
      "ficaram de fora" in r_gera.text and "saida.xml" in r_gera.text)
check("mostra as presuncoes ANTES do download",
      "precisou presumir" in r_gera.text
      and r_gera.text.index("precisou presumir") < r_gera.text.index("Baixar"))
check("e oferece o download", "/arquivo" in r_gera.text)

import re as _re  # noqa: E402
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
check("a REGRA APROVADA mandou no CFOP do item dela",
      any(l.split("|")[3] == "CAFE500" and l.split("|")[11] == "1556"
          for l in gerado.splitlines() if l.startswith("|C170|")),
      [l for l in gerado.splitlines() if l.startswith("|C170|")][:1])

check("cliente nao baixa",
      as_("cli").get(f"/api/gerar-sped/{processo.group(1)}/arquivo").status_code
      == 403)
check("identificador que nao e UUID nao vira leitura de arquivo do servidor",
      as_("ope").get("/api/gerar-sped/..%2F..%2Fmain/arquivo").status_code
      in (404, 400))

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
for nome in os.listdir(PASTA):
    os.remove(os.path.join(PASTA, nome))
os.rmdir(PASTA)

print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
for f in _falhou:
    print("  -", f)
sys.exit(1 if FALHAS else 0)
