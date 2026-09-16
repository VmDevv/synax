# -*- coding: utf-8 -*-
"""NFS-e no layout NACIONAL (o que vem do ADN) — o leitor entendia só ABRASF.

Sintoma, medido na tela em 02/09/2026: das 251 notas baixadas do Ambiente de
Dados Nacional, a tabela mostrava NÚMERO, CNPJ do prestador e UF. Todo o resto
— emissão, competência, razão social, tomador, código do serviço, e TODOS os
valores — vinha vazio ou R$ 0,00.

A causa: o layout nacional (`http://www.sped.fazenda.gov.br/nfse`) não
compartilha UM nome de tag com o ABRASF. As três colunas que apareciam vinham
por acidente, de buscas globais no documento (`nNFSe` estava na lista de nomes,
e `<CNPJ>` e `<UF>` foram encontrados em qualquer lugar da árvore).

O que esta suíte trava:

1. **Os dois layouts convivem.** O ABRASF municipal continua sendo lido — o
   escritório recebe os dois — e a escolha é pelo conteúdo do arquivo.

2. **Prestador pessoa física não vira o CNPJ do tomador.** 4 das 251 notas
   reais têm `<emit><CPF>`. Sem `<CNPJ>` no emitente, a busca global pegava o
   `<CNPJ>` do TOMADOR e o gravava como prestador — e `prestador` é parte da
   chave primária do documento.

3. **`infNFSe/valores` e `infDPS/valores` não se misturam.** Os dois blocos têm
   o mesmo nome em níveis diferentes: um traz ISS, base e alíquota; o outro, o
   valor do serviço e as retenções federais. Uma busca em profundidade pega o
   primeiro e troca um pelo outro.

4. **`mun_prest` preenchido.** É parte da chave primária de `NfseDocument`;
   vazio, a deduplicação de notas fica degradada.

5. **Data sem fuso.** O layout nacional traz `2023-02-08T13:30:39-03:00`. Um
   datetime "consciente" numa coluna ingênua faz qualquer comparação de datas
   levantar TypeError.

6. **CNAE fica vazio de propósito.** O layout nacional não tem CNAE. Preencher
   com o `cNBS` — que é outra classificação — seria rotular errado dado fiscal.

Verificado contra os 251 XMLs reais do escritório antes de escrever: 251
reconhecidos, 0 recusados, e os campos batem com o que existe em cada arquivo.
"""
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('backend'))

from app.services.nfse.abrasf_parser import (  # noqa: E402
    parse_nfse_xml, uf_do_municipio)

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


def ler(xml: str):
    fd, tmp = tempfile.mkstemp(suffix=".xml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(xml)
    try:
        return parse_nfse_xml(tmp)
    finally:
        os.remove(tmp)


# ============================================================================
# Fixture com a MESMA estrutura dos arquivos reais (CNPJs fictícios).
# Note os dois blocos <valores>, em níveis diferentes, com números distintos
# de propósito: é a armadilha do item 3.
# ============================================================================
NACIONAL = """<?xml version="1.0" encoding="utf-8"?>
<NFSe versao="1.00" xmlns="http://www.sped.fazenda.gov.br/nfse">
 <infNFSe Id="NFS32049062244709461000103000000000000123028384347067">
  <xLocEmi>São Mateus</xLocEmi>
  <nNFSe>2600000054605</nNFSe>
  <cLocIncid>3204906</cLocIncid>
  <xTribNac>Funilaria e lanternagem.</xTribNac>
  <cStat>107</cStat>
  <dhProc>2026-08-10T09:00:00-03:00</dhProc>
  <emit>
    <CNPJ>11222333000181</CNPJ>
    <xNome>PRESTADOR EXEMPLO LTDA</xNome>
    <IM>04276830019</IM>
    <enderNac><xLgr>RUA A</xLgr><nro>10</nro><cMun>3204906</cMun>
      <UF>ES</UF><CEP>29930420</CEP></enderNac>
  </emit>
  <valores>
    <vBC>8275.88</vBC>
    <pAliqAplic>2.50</pAliqAplic>
    <vISSQN>206.90</vISSQN>
    <vTotalRet>500.00</vTotalRet>
    <vLiq>7775.88</vLiq>
  </valores>
  <DPS versao="1.00" xmlns="http://www.sped.fazenda.gov.br/nfse">
   <infDPS Id="DPS320490624470946100010300900000000000000001">
    <dhEmi>2026-08-08T13:30:39-03:00</dhEmi>
    <serie>900</serie>
    <dCompet>2026-08-01</dCompet>
    <cLocEmi>3204906</cLocEmi>
    <prest>
      <CNPJ>11222333000181</CNPJ>
      <regTrib><opSimpNac>3</opSimpNac><regEspTrib>0</regEspTrib></regTrib>
    </prest>
    <toma>
      <CNPJ>44555666000199</CNPJ>
      <xNome>TOMADOR EXEMPLO LTDA</xNome>
      <IM>082246734</IM>
      <end><endNac><cMun>3550308</cMun><CEP>01310000</CEP></endNac>
        <xLgr>AV B</xLgr><nro>1219</nro></end>
    </toma>
    <serv>
      <locPrest><cLocPrestacao>3204906</cLocPrestacao></locPrest>
      <cServ><cTribNac>141201</cTribNac><cNBS>101340000</cNBS>
        <xDescServ>Pintura de porão e alinhamento</xDescServ></cServ>
    </serv>
    <valores>
      <vServPrest><vServ>8275.88</vServ></vServPrest>
      <vDescCondIncond><vDescCond>10.00</vDescCond>
        <vDescIncond>20.00</vDescIncond></vDescCondIncond>
      <vDedRed><vDR>30.00</vDR></vDedRed>
      <trib>
        <tribMun><tribISSQN>1</tribISSQN><tpRetISSQN>2</tpRetISSQN>
          <pAliq>2.50</pAliq></tribMun>
        <tribFed>
          <piscofins><CST>00</CST><vPis>53.79</vPis><vCofins>248.28</vCofins></piscofins>
          <vRetIRRF>124.14</vRetIRRF><vRetCSLL>82.76</vRetCSLL><vRetCP>5.00</vRetCP>
        </tribFed>
      </trib>
    </valores>
   </infDPS>
  </DPS>
 </infNFSe>
</NFSe>"""

# Prestador pessoa física: o emitente tem CPF e NÃO tem CNPJ. O único <CNPJ> do
# documento é o do tomador — exatamente o caso que gravava o prestador errado.
PESSOA_FISICA = (NACIONAL
                 .replace("<CNPJ>11222333000181</CNPJ>\n    <xNome>",
                          "<CPF>06377159692</CPF>\n    <xNome>")
                 .replace("      <CNPJ>11222333000181</CNPJ>\n"
                          "      <regTrib>", "      <CPF>06377159692</CPF>\n"
                          "      <regTrib>"))

SEM_TOMADOR = NACIONAL[:NACIONAL.find("<toma>")] + NACIONAL[NACIONAL.find("</toma>") + 7:]


# ============================================================================
print("\n=== 1. O layout nacional é reconhecido e vem completo ===")

d = ler(NACIONAL)
check("a nota é reconhecida", d is not None)
if not d:
    print("RESULTADO: 0/1 passaram")
    sys.exit(1)

check("número (nNFSe)", d["numero"] == "2600000054605", d["numero"])
check("prestador (emit/CNPJ)", d["prestador"] == "11222333000181", d["prestador"])
check("razão social do prestador (emit/xNome)",
      d["rz_prestador"] == "PRESTADOR EXEMPLO LTDA", d["rz_prestador"])
check("UF do prestador", d["uf_prest"] == "ES", d["uf_prest"])
check("inscrição municipal (emit/IM)", d["insc_prest"] == "04276830019", d["insc_prest"])
check("emissão (dhEmi)", d["dt_emissao"] and
      d["dt_emissao"].strftime("%d/%m/%Y %H:%M") == "08/08/2026 13:30", d["dt_emissao"])
check("competência (dCompet)", d["dt_competencia"] and
      d["dt_competencia"].strftime("%d/%m/%Y") == "01/08/2026", d["dt_competencia"])
check("tomador (toma/CNPJ)", d["tomador"] == "44555666000199", d["tomador"])
check("razão social do tomador", d["rz_tomador"] == "TOMADOR EXEMPLO LTDA", d["rz_tomador"])
check("código do serviço (cTribNac)", d["cod_servico"] == "141201", d["cod_servico"])
check("discriminação (xDescServ)",
      d["discriminacao"] == "Pintura de porão e alinhamento", d["discriminacao"])
check("chave de acesso guardada", d["cod_verificacao"].startswith("NFS3204906"),
      d["cod_verificacao"])
check("status a partir do cStat 107",
      d["status"] == "Autorizado o uso da NFS-e", d["status"])


# ============================================================================
print("\n=== 2. mun_prest preenchido (faz parte da chave primária) ===")

check("município do prestador vem do emit/enderNac/cMun",
      d["mun_prest"] == "3204906", d["mun_prest"])
check("e nunca fica vazio quando o XML tem o dado", bool(d["mun_prest"]))


# ============================================================================
print("\n=== 3. Os dois blocos <valores> não se misturam ===")

# infNFSe/valores: vBC, pAliqAplic, vISSQN, vLiq
# infDPS/valores:  vServ, retenções, descontos, deduções
check("valor do serviço vem do infDPS (vServ)", d["valor_servico"] == 8275.88,
      d["valor_servico"])
check("base de cálculo vem do infNFSe (vBC)", d["base_calculo"] == 8275.88,
      d["base_calculo"])
check("ISS vem do infNFSe (vISSQN)", d["iss"] == 206.90, d["iss"])
check("alíquota vem do infNFSe (pAliqAplic)", d["aliquota"] == 2.50, d["aliquota"])
check("líquido vem do infNFSe (vLiq)", d["valor_liquido"] == 7775.88,
      d["valor_liquido"])
check("IRRF", d["ir"] == 124.14, d["ir"])
check("CSLL", d["csll"] == 82.76, d["csll"])
check("PIS", d["pis"] == 53.79, d["pis"])
check("COFINS", d["cofins"] == 248.28, d["cofins"])
check("INSS (vRetCP)", d["inss"] == 5.00, d["inss"])
check("deduções (vDR)", d["deducoes"] == 30.00, d["deducoes"])
check("desconto condicionado", d["desconto_condic"] == 10.00, d["desconto_condic"])
check("desconto incondicionado", d["desconto_incondic"] == 20.00, d["desconto_incondic"])

# vTotalRet é o TOTAL retido. Somá-lo em out_retencoes duplicaria IR+CSLL+PIS+COFINS.
check("vTotalRet NÃO entra como 'outras retenções' (duplicaria o total)",
      d["out_retencoes"] == 0.0, d["out_retencoes"])
soma = d["ir"] + d["csll"] + d["pis"] + d["cofins"] + d["inss"] + d["out_retencoes"]
check("a soma das retenções bate com as parcelas", round(soma, 2) == 513.97, soma)


# ============================================================================
print("\n=== 4. Prestador pessoa física não vira o CNPJ do tomador ===")

pf = ler(PESSOA_FISICA)
check("a nota de pessoa física é reconhecida", pf is not None)
check("o prestador é o CPF do emitente",
      pf and pf["prestador"] == "06377159692", pf and pf["prestador"])
check("e NÃO é o CNPJ do tomador (era o defeito)",
      pf and pf["prestador"] != "44555666000199", pf and pf["prestador"])
check("o tomador continua correto",
      pf and pf["tomador"] == "44555666000199", pf and pf["tomador"])


# ============================================================================
print("\n=== 5. UF do tomador sai do código IBGE (o layout não a traz) ===")

check("município do tomador", d["cod_mun_toma"] == "3550308", d["cod_mun_toma"])
check("UF do tomador deduzida do código (35 = SP)", d["uf_toma"] == "SP", d["uf_toma"])
check("3204906 -> ES", uf_do_municipio("3204906") == "ES")
check("3550308 -> SP", uf_do_municipio("3550308") == "SP")
check("3106200 -> MG", uf_do_municipio("3106200") == "MG")
check("5300108 -> DF", uf_do_municipio("5300108") == "DF")
check("código desconhecido não inventa UF", uf_do_municipio("9999999") == "")
check("vazio não quebra", uf_do_municipio("") == "" and uf_do_municipio(None) == "")


# ============================================================================
print("\n=== 6. Data sem fuso (senão qualquer comparação levanta TypeError) ===")

check("emissão é ingênua", d["dt_emissao"].tzinfo is None)
check("competência é ingênua", d["dt_competencia"].tzinfo is None)
check("dá para comparar duas datas da mesma nota",
      d["dt_competencia"] <= d["dt_emissao"])


# ============================================================================
print("\n=== 7. Traduções e o que NÃO se preenche ===")

check("opSimpNac 3 (ME/EPP) vira 'Sim'", d["optante_sn"] == "Sim", d["optante_sn"])
n1 = ler(NACIONAL.replace("<opSimpNac>3</opSimpNac>", "<opSimpNac>1</opSimpNac>"))
check("opSimpNac 1 (não optante) vira 'Não'", n1["optante_sn"] == "Não", n1["optante_sn"])

check("tpRetISSQN 2 (retido pelo tomador) vira 'Sim'",
      d["iss_retido"] == "Sim", d["iss_retido"])
n2 = ler(NACIONAL.replace("<tpRetISSQN>2</tpRetISSQN>", "<tpRetISSQN>1</tpRetISSQN>"))
check("tpRetISSQN 1 (não retido) vira 'Não'", n2["iss_retido"] == "Não", n2["iss_retido"])

check("CNAE fica VAZIO: o layout nacional não tem esse campo",
      d["cnae"] == "", d["cnae"])
check("e o cNBS não é usado no lugar dele (é outra classificação)",
      "101340000" not in str(d.get("cnae", "")))


# ============================================================================
print("\n=== 8. Nota incompleta não derruba o leitor ===")

st = ler(SEM_TOMADOR)
check("nota sem bloco <toma> é lida assim mesmo", st is not None)
check("e os campos do tomador ficam vazios, não com lixo",
      st and st["tomador"] == "" and st["rz_tomador"] == "" and st["uf_toma"] == "",
      st and (st["tomador"], st["rz_tomador"], st["uf_toma"]))
check("o prestador continua certo", st and st["prestador"] == "11222333000181")

zerada = ler(NACIONAL.replace("<vServ>8275.88</vServ>", "<vServ>0.00</vServ>")
             .replace("<vBC>8275.88</vBC>", "<vBC>0.00</vBC>")
             .replace("<vLiq>7775.88</vLiq>", "<vLiq>0.00</vLiq>"))
check("nota de valor zero é lida como zero, não recusada",
      zerada is not None and zerada["valor_servico"] == 0.0,
      zerada and zerada["valor_servico"])

check("XML que não é NFS-e devolve None",
      ler("<?xml version='1.0'?><qualquerCoisa><a>1</a></qualquerCoisa>") is None)
check("nacional sem nNFSe é recusado",
      ler(NACIONAL.replace("<nNFSe>2600000054605</nNFSe>", "")) is None)


# ============================================================================
print("\n=== 9. O ABRASF municipal continua sendo lido ===")

ABRASF = """<?xml version="1.0" encoding="UTF-8"?>
<ConsultarNfseResposta xmlns="http://www.abrasf.org.br/nfse.xsd">
 <ListaNfse><CompNfse><Nfse><InfNfse>
  <Numero>298231</Numero>
  <DataEmissao>2026-07-16T10:22:00</DataEmissao>
  <Competencia>2026-07-01T00:00:00</Competencia>
  <ValoresNfse><BaseCalculo>267.26</BaseCalculo><Aliquota>2.00</Aliquota>
    <ValorIss>5.35</ValorIss><ValorLiquidoNfse>267.26</ValorLiquidoNfse></ValoresNfse>
  <PrestadorServico>
    <IdentificacaoPrestador><Cnpj>03813799000123</Cnpj>
      <InscricaoMunicipal>11994101</InscricaoMunicipal></IdentificacaoPrestador>
    <RazaoSocial>COMLINK S. A.</RazaoSocial>
    <Endereco><Uf>SP</Uf><CodigoMunicipio>3543402</CodigoMunicipio></Endereco>
  </PrestadorServico>
  <DeclaracaoPrestacaoServico><InfDeclaracaoPrestacaoServico>
    <Servico><Valores><ValorServicos>267.26</ValorServicos></Valores>
      <ItemListaServico>0105</ItemListaServico></Servico>
    <Tomador><IdentificacaoTomador><CpfCnpj><Cnpj>48135251000100</Cnpj></CpfCnpj></IdentificacaoTomador>
      <RazaoSocial>SETE INDUSTRIA LTDA</RazaoSocial>
      <Endereco><Uf>ES</Uf><CodigoMunicipio>3203205</CodigoMunicipio></Endereco>
    </Tomador>
  </InfDeclaracaoPrestacaoServico></DeclaracaoPrestacaoServico>
 </InfNfse></Nfse></CompNfse></ListaNfse>
</ConsultarNfseResposta>"""

a = ler(ABRASF)
check("o ABRASF continua reconhecido", a is not None)
check("número", a and a["numero"] == "298231", a and a["numero"])
check("prestador (não confunde com o tomador)",
      a and a["prestador"] == "03813799000123", a and a["prestador"])
check("razão social", a and a["rz_prestador"] == "COMLINK S. A.")
check("município do prestador", a and a["mun_prest"] == "3543402", a and a["mun_prest"])
check("valor do serviço", a and a["valor_servico"] == 267.26)
check("ISS", a and a["iss"] == 5.35)
check("os dois layouts produzem as MESMAS chaves",
      set(a.keys()) == set(d.keys()),
      set(a.keys()) ^ set(d.keys()))


FONTE = io.open(os.path.join("backend", "app", "services", "nfse",
                             "abrasf_parser.py"), encoding="utf-8").read()
check("a escolha do layout é pelo conteúdo, não por configuração",
      'infNFSe' in FONTE and "_parse_nacional" in FONTE)
check("o filho é buscado DIRETO, para os dois <valores> não se misturarem",
      "def _filho(" in FONTE and "Filho DIRETO" in FONTE)


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

sys.exit(1 if FALHAS else 0)
