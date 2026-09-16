# -*- coding: utf-8 -*-
"""Diz por que um CT-e específico não gerou divergência de ICMS.

Motivo de existir: em 27/08/2026 o CT-e 211146 (Laurencini) tinha crédito de
ICMS no XML, a empresa escriturou sem crédito e a tela não acusou nada. "Não
apareceu" pode ser meia dúzia de coisas diferentes, e da tela não dá para
saber qual. Este script percorre o mesmo caminho do motor e imprime em qual
porta o documento parou.

O ponto que mais engana: o motor só enxerga como crédito as tags de ICMS que o
parser sabe ler. O CT-e tem VÁRIOS grupos de ICMS (ICMS00, ICMS20, ICMS45,
ICMS60, ICMS90, ICMSOutraUF, ICMSSN) e cada um guarda o valor numa tag de nome
diferente. Se o crédito estiver numa tag não lida, o XML chega com ICMS zero,
bate com o SPED que também está zerado, e não há divergência a apontar. Por
isso o relatório abaixo despeja o bloco <ICMS> cru: é ali que a resposta está.

Uso:
    venv/Scripts/python.exe scripts/diagnosticar_cte.py <sped.txt> <pasta_xml> 211146

Sem o número do CT-e, lista todos os CT-e do arquivo com o veredito de cada um.
Não grava nada, não consulta banco: só lê os arquivos.
"""
import os
import re
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.services.audit_engine.sped_parser import parse_sped
from backend.app.services.audit_engine.xml_parser import parse_cte_xml

NS_CTE = "http://www.portalfiscal.inf.br/cte"

# O que o parser de CT-e sabe transformar em crédito hoje. Toda tag de valor
# que aparecer no XML e NÃO estiver aqui é candidata a ser a causa do silêncio.
TAGS_LIDAS = {"vICMS", "vICMSOutraUF"}


def mapear_xmls(xml_dir):
    """Mesmo mapeamento do motor: nome do arquivo e a chave de 44 dígitos."""
    mapa = {}
    for root, _, files in os.walk(xml_dir):
        for f in files:
            if f.lower().endswith(".xml"):
                caminho = os.path.join(root, f)
                mapa[f[:-4]] = caminho
                m = re.search(r"\d{44}", f)
                if m:
                    mapa[m.group(0)] = caminho
    return mapa


def _sem_ns(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def dissecar_icms(xml_path):
    """Devolve (nome_do_grupo, [(tag, valor)], linhas do bloco cru)."""
    try:
        root = ET.parse(xml_path).getroot()
    except Exception as e:
        return None, [], [f"(não deu para abrir o XML: {e})"]

    icms = root.find(f".//{{{NS_CTE}}}imp/{{{NS_CTE}}}ICMS")
    if icms is None:
        return None, [], ["(o CT-e não tem bloco <imp><ICMS>)"]

    grupos, valores, cru = [], [], []
    for grupo in icms:
        nome = _sem_ns(grupo.tag)
        grupos.append(nome)
        cru.append(f"<{nome}>")
        for campo in grupo:
            tag = _sem_ns(campo.tag)
            txt = (campo.text or "").strip()
            cru.append(f"    <{tag}>{txt}</{tag}>")
            if tag.startswith(("v", "p")) and txt:
                try:
                    valores.append((tag, float(txt)))
                except ValueError:
                    pass
        cru.append(f"</{nome}>")
    return "+".join(grupos), valores, cru


def diagnosticar(d100, xml_path):
    """Devolve (veredito, [linhas de detalhe]). Segue a ordem do cross_checker."""
    det = []

    if str(d100.get("ind_oper", "0")) == "1":
        return ("PULADO", ["É documento de SAÍDA (IND_OPER=1) e a auditoria está "
                           "ignorando saídas."])

    if not xml_path:
        return ("SEM XML", ["Não existe XML com esta chave na pasta. O motor "
                            "reporta como documento sem XML, não como divergência "
                            "de ICMS."])

    xml = parse_cte_xml(xml_path)
    if "error" in xml:
        return ("XML ILEGÍVEL", [
            f"O parser de CT-e falhou neste arquivo: {xml['error']}",
            f"Arquivo: {xml_path}",
            "",
            ">> CAUSA: o motor descarta em silêncio o CT-e cujo XML não abre.",
        ])

    xml_icms = xml.get("vl_icms", 0.0)
    sped_icms = d100.get("vl_icms", 0.0)
    cfops = d100.get("cfops", [])
    grupo, valores, cru = dissecar_icms(xml_path)

    det.append(f"ICMS que o motor leu do XML .. R$ {xml_icms:,.2f}"
               f"  (próprio R$ {xml.get('vl_icms_proprio', 0.0):,.2f}"
               f" + OutraUF R$ {xml.get('vl_icms_outra_uf', 0.0):,.2f})")
    det.append(f"ICMS escriturado no SPED ..... R$ {sped_icms:,.2f}")
    det.append(f"CFOP do XML .................. {xml.get('cfop') or '(vazio)'}")
    det.append(f"Tem analítico D190? .......... {'sim' if d100.get('tem_d190') else 'NÃO'}")
    det.append(f"CFOPs vindos do D190 ......... {cfops or '(nenhum)'}")
    det.append(f"Grupo de ICMS no CT-e ........ {grupo or '(nenhum)'}")
    det.append("")
    det.append("Bloco <ICMS> do XML, como está no arquivo:")
    det.extend("    " + l for l in cru)

    # Tag de valor que o parser não sabe ler é a suspeita número um.
    # vBC* é base de cálculo, não crédito — listá-la só faria ruído.
    nao_lidas = [(t, v) for t, v in valores
                 if t.startswith("v") and not t.startswith("vBC")
                 and t not in TAGS_LIDAS and v > 0.01]

    if not cfops:
        return ("NÃO CONFERIDO", det + [
            "",
            ">> CAUSA: o CT-e não tem registro D190 no SPED, então o motor não",
            "   sabe qual CFOP foi escriturado. A conferência de ICMS do CT-e",
            "   roda por CFOP do analítico; sem CFOP, ela não roda nenhuma vez.",
        ])

    if xml_icms <= 0.01 and nao_lidas:
        return ("CRÉDITO EM TAG NÃO LIDA", det + [
            "",
            ">> CAUSA: o XML tem valor de ICMS, mas em tag que o parser não lê:",
        ] + [f"     {t} = R$ {v:,.2f}" for t, v in nao_lidas] + [
            "   Com ICMS zero dos dois lados, não há diferença a apontar e o",
            "   documento passa calado. É preciso decidir se esta tag é crédito",
            "   aproveitável e, se for, somá-la em parse_cte_xml.",
        ])

    if xml_icms <= 0.01:
        return ("SEM ICMS NO XML", det + [
            "",
            ">> O CT-e realmente não destaca ICMS. Nada a creditar, nada a acusar.",
        ])

    if abs(sped_icms - xml_icms) <= 0.05:
        return ("SEM DIVERGÊNCIA", det + [
            "", ">> Os dois lados batem. Nada a acusar."])

    return ("DEPENDE DA REGRA", det + [
        "",
        f">> Há diferença de R$ {abs(sped_icms - xml_icms):,.2f}. Se o motor não",
        "   acusou, é porque não existe regra de crédito de ICMS aprovada para o",
        "   CFOP de REFERÊNCIA desta empresa. Atenção: o CFOP de referência é o",
        "   MAPEADO na aba de CFOP de CT-e quando existe mapeamento, e só na",
        f"   falta dele o do D190 ({' ou '.join(cfops)}). A regra de crédito",
        "   precisa estar cadastrada para o CFOP mapeado, não para o do SPED —",
        "   se estiver no outro, o motor não acha e cala.",
    ])


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2

    sped_path, xml_dir = sys.argv[1], sys.argv[2]
    alvo = sys.argv[3] if len(sys.argv) > 3 else None

    dados = parse_sped(sped_path)
    xmls = mapear_xmls(xml_dir)
    d100s = dados.get("d100", {})

    print(f"CT-e no SPED: {len(d100s)}   XMLs na pasta: {len(set(xmls.values()))}")
    print("=" * 72)

    achou = False
    for chave, d100 in d100s.items():
        num = str(d100.get("num", ""))
        if alvo and num != str(alvo):
            continue
        achou = True
        veredito, det = diagnosticar(d100, xmls.get(chave))
        print(f"\nCT-e {num}  [{veredito}]")
        for linha in det:
            print("   " + linha)

    if alvo and not achou:
        print(f"\nCT-e {alvo} não está no bloco D do SPED informado.")
        print("Ou o documento não foi escriturado, ou está em outro arquivo.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
