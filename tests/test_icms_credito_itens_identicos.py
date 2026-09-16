"""Crédito de ICMS: divergência REAL quando o mesmo item se repete na nota.

Pedido do usuário (18/08/2026): quando há itens idênticos (mesmo código, mesma
descrição, mesmo valor unitário) na mesma nota, soma-se o crédito de ICMS de
todas as linhas do XML e de todas as linhas do SPED daquele grupo, e compara-se
as SOMAS — não linha a linha.

Bug que isso corrige: o casamento antigo pegava o PRIMEIRO item do XML com
aquele código (`for x in xml_items_list: if x["cod_item"] == item["cod_item"]:
... break`) e comparava contra CADA linha do SPED com o mesmo código. Quando a
mesma mercadoria aparece em mais de uma linha na nota — típico quando o SPED e
o XML fracionam a mesma venda de formas diferentes —, isso repetia a
comparação contra a MESMA linha do XML e acusava divergência que não existe
(a soma bate; só a forma de particionar as linhas é diferente).

Duas camadas de teste:
1. Unitário — a função pura `agrupar_icms_por_item_identico` (sem tocar banco,
   SPED ou XML de verdade).
2. Ponta a ponta — `execute_audit` com um SPED e um XML reais em disco, para
   garantir que a soma por grupo está de fato conectada ao motor.
"""
import os, sys, uuid, json, tempfile
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_icms_grupo_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

if os.path.exists("_icms_grupo_test.db"):
    os.remove("_icms_grupo_test.db")

from sqlalchemy import types as _satypes
import sqlalchemy.dialects.postgresql as pg

class _UUIDShim(_satypes.TypeDecorator):
    impl = _satypes.Uuid
    cache_ok = True
    def __init__(self, *a, **kw):
        kw.pop('as_uuid', None); super().__init__()
    def process_bind_param(self, value, dialect):
        return uuid.UUID(value) if isinstance(value, str) else value

pg.UUID = _UUIDShim

import backend.app.infrastructure.auth.supabase_client as sb
class _Auth:
    def get_user(self, token):
        raise Exception("não usado neste teste")
class _Client:
    auth = _Auth()
sb.get_supabase_client = lambda: _Client()
sb.get_supabase_admin_client = lambda: _Client()

from backend.app.infrastructure.database.session import engine as _eng
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit(f"ABORTADO: engine aponta para {_eng.url.get_backend_name()}, não SQLite.")

from backend.app.infrastructure.database.session import SessionLocal, engine
from backend.app.domain.models.base import Base
from backend.app.domain.models import rules, audit  # registra os models no metadata
from backend.app.domain.models.audit import AuditTask
from backend.app.domain.models.rules import CfopCreditRule
from backend.app.infrastructure.repositories.rules_repository import RulesRepository
from backend.app.services.audit_engine.cross_checker import (
    execute_audit, agrupar_icms_por_item_identico, _chave_item_xml,
    casar_itens_da_nota, permite_credito_icms,
)

Base.metadata.create_all(bind=engine)

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
print("\n=== 1. Função pura: agrupar_icms_por_item_identico ===")

# CFOPs com crédito liberado, para a função somar as linhas do SPED.
REGRAS = {"1102": True, "2102": True}

print("\n--- 1a. Soma bate: SEM divergência real, mesmo que a linha 1 não bata ---")
sped_items = [
    {"cod_item": "P1", "cfop": "1102", "qtd": 10, "vl_item": 1000.0, "vl_icms": 120.0},
    {"cod_item": "P1", "cfop": "1102", "qtd": 5, "vl_item": 500.0, "vl_icms": 55.0},
]
# ATENÇÃO: a descrição do XML (xProd, texto do fornecedor) é DIFERENTE da
# descrição que o SPED tem para o mesmo código (registro 0200, tabela da
# empresa). É assim na vida real, e é justamente o que a primeira versão
# desta lógica não suportava.
xml_items = [
    {"cod_item": "P1", "descr": "PARAFUSO SEXTAVADO 3/8 POLEGADA", "vl_unit": 100.0, "vl_icms": 60.0},
    {"cod_item": "P1", "descr": "PARAFUSO SEXTAVADO 3/8 POLEGADA", "vl_unit": 100.0, "vl_icms": 115.0},
]
grupos = agrupar_icms_por_item_identico(sped_items, xml_items, REGRAS)
check("um único grupo para os 4 lançamentos", len(grupos) == 1, f"grupos={list(grupos.keys())}")
g = next(iter(grupos.values()))
check("soma do ICMS do SPED = 175,00", abs(g["sped_icms"] - 175.0) < 0.01, f"={g['sped_icms']}")
check("soma do ICMS do XML = 175,00", abs(g["xml_icms"] - 175.0) < 0.01, f"={g['xml_icms']}")
check("2 linhas de cada lado contabilizadas", g["n_sped"] == 2 and g["n_xml"] == 2, f"={g}")
check("linha a linha teria dado falso positivo (120 vs 60), mas a soma bate",
      abs(sped_items[0]["vl_icms"] - xml_items[0]["vl_icms"]) > 0.05
      and abs(g["sped_icms"] - g["xml_icms"]) < 0.05)

print("\n--- 1b. O caso relatado: mesmo produto, QUANTIDADE diferente por linha ---")
# Mesmo nome, mesmo valor unitário, mesmo código — só a quantidade muda.
# O XML consolidou numa linha só; o SPED fracionou em duas.
sped_qtd = [
    {"cod_item": "P9", "cfop": "1102", "qtd": 3, "vl_item": 300.0, "vl_icms": 36.0},
    {"cod_item": "P9", "cfop": "1102", "qtd": 7, "vl_item": 700.0, "vl_icms": 84.0},
]
xml_qtd = [{"cod_item": "P9", "descr": "CANETA ESFEROGRAFICA AZUL", "vl_unit": 100.0, "vl_icms": 120.0}]
g_qtd = next(iter(agrupar_icms_por_item_identico(sped_qtd, xml_qtd, REGRAS).values()))
check("SPED 36 + 84 = 120 casa com os 120 do XML",
      abs(g_qtd["sped_icms"] - g_qtd["xml_icms"]) < 0.05,
      f"sped={g_qtd['sped_icms']} xml={g_qtd['xml_icms']}")
check("fracionamento diferente (2 linhas x 1 linha) não vira divergência",
      g_qtd["n_sped"] == 2 and g_qtd["n_xml"] == 1)

print("\n--- 1c. Descrições diferentes entre SPED e XML NÃO criam grupo fantasma ---")
# Este é o teste que faltava: com a descrição na chave, o mesmo item virava
# 2 grupos (um só-SPED, outro só-XML) e CADA UM era uma divergência falsa.
g_desc = agrupar_icms_por_item_identico(
    [{"cod_item": "P5", "cfop": "1102", "qtd": 1, "vl_item": 50.0, "vl_icms": 9.0}],
    [{"cod_item": "P5", "descr": "TEXTO TOTALMENTE DIFERENTE DO FORNECEDOR",
      "vl_unit": 999.99, "vl_icms": 9.0}],
    REGRAS)
check("1 grupo só, mesmo com descrição e valor unitário divergentes entre as fontes",
      len(g_desc) == 1, f"grupos={list(g_desc.keys())}")
gd = next(iter(g_desc.values()))
check("os dois lados caíram no MESMO grupo (nenhum lado zerado)",
      gd["sped_icms"] == 9.0 and gd["xml_icms"] == 9.0, f"={gd}")
check("e portanto NÃO acusa divergência", abs(gd["sped_icms"] - gd["xml_icms"]) < 0.05)

print("\n--- 1d. Soma diferente: divergência REAL continua aparecendo ---")
sped_items2 = [
    {"cod_item": "P2", "cfop": "1102", "qtd": 1, "vl_item": 100.0, "vl_icms": 18.0},
    {"cod_item": "P2", "cfop": "1102", "qtd": 1, "vl_item": 100.0, "vl_icms": 18.0},
]
xml_items2 = [
    {"cod_item": "P2", "descr": "CANETA", "vl_unit": 100.0, "vl_icms": 18.0},
    {"cod_item": "P2", "descr": "CANETA", "vl_unit": 100.0, "vl_icms": 10.0},
]
g2 = next(iter(agrupar_icms_por_item_identico(sped_items2, xml_items2, REGRAS).values()))
check("SPED = 36,00", abs(g2["sped_icms"] - 36.0) < 0.01, f"={g2['sped_icms']}")
check("XML = 28,00", abs(g2["xml_icms"] - 28.0) < 0.01, f"={g2['xml_icms']}")
check("diferença real de R$ 8,00 preservada", abs((g2["sped_icms"] - g2["xml_icms"]) - 8.0) < 0.01)

print("\n--- 1e. Produtos DIFERENTES não se misturam ---")
sped3 = [{"cod_item": "P3", "cfop": "1102", "qtd": 1, "vl_item": 50.0, "vl_icms": 5.0}]
xml3 = [
    {"cod_item": "P3", "descr": "Produto A", "vl_unit": 50.0, "vl_icms": 5.0},
    {"cod_item": "P4", "descr": "Produto B", "vl_unit": 50.0, "vl_icms": 999.0},
]
grupos3 = agrupar_icms_por_item_identico(sped3, xml3, REGRAS)
check("2 grupos (P3 e P4)", len(grupos3) == 2, f"={len(grupos3)}")
check("o grupo de P3 não herdou os 999,00 do P4",
      abs(grupos3[_chave_item_xml(xml3[0])]["xml_icms"] - 5.0) < 0.01,
      f"={grupos3[_chave_item_xml(xml3[0])]}")

print("\n--- 1f. Espaço/caixa na descrição não separa o mesmo item ---")
check("descrição com espaço duplo e caixa diferente cai na mesma chave",
      _chave_item_xml({"cod_item": "P1", "descr": "  aditivo  RADIADOR ", "vl_unit": 3.7})
      == _chave_item_xml({"cod_item": "p1", "descr": "ADITIVO RADIADOR", "vl_unit": 3.7}))

print("\n--- 1i. Nota REAL do cliente: item 26934 em duas linhas ---")
# DANFE enviada pelo usuario (20/08/2026). Os codigos da DANFE sao do
# FORNECEDOR; no SPED a empresa usa codigo interno proprio, e o casamento
# acontece por POSICAO. Foi esse descasamento que fez uma versao anterior
# zerar todo o xml_icms e inundar a tela de divergencias falsas.
xml_real = [
    {"cod_item": "43684", "descr": "AMORT DIANT C4 LOUNGE 13/ LE PEUGEOT 308 408", "vl_unit": 316.50, "vl_icms": 22.16},
    {"cod_item": "41214", "descr": "AMORT MALA FIAT IDEA 10/ ADVENTURE 06/", "vl_unit": 43.88, "vl_icms": 6.14},
    {"cod_item": "20940", "descr": "ATUADOR EMB GM ASTRA/VECTRA/ZAFIRA 2.0/2.2 8/16V", "vl_unit": 166.31, "vl_icms": 11.64},
    {"cod_item": "26934", "descr": "ADITIVO RADIADOR 1L ROSA ORGANICO", "vl_unit": 3.70, "vl_icms": 10.36},
    {"cod_item": "26934", "descr": "ADITIVO RADIADOR 1L ROSA ORGANICO", "vl_unit": 3.70, "vl_icms": 7.77},
    {"cod_item": "24400", "descr": "ADITIVO MOTOR PROLONGA", "vl_unit": 39.90, "vl_icms": 13.97},
]
sped_real = [
    {"cod_item": "INT-001", "cfop": "1102", "num_item": "1", "vl_icms": 22.16},
    {"cod_item": "INT-002", "cfop": "1102", "num_item": "2", "vl_icms": 6.14},
    {"cod_item": "INT-003", "cfop": "1102", "num_item": "3", "vl_icms": 11.64},
    {"cod_item": "INT-004", "cfop": "1102", "num_item": "4", "vl_icms": 10.36},
    {"cod_item": "INT-004", "cfop": "1102", "num_item": "5", "vl_icms": 7.77},
    {"cod_item": "INT-005", "cfop": "1102", "num_item": "6", "vl_icms": 13.97},
]
g_real = agrupar_icms_por_item_identico(sped_real, xml_real, REGRAS)
divergentes = [k for k, v in g_real.items() if abs(v["sped_icms"] - v["xml_icms"]) > 0.05]
check("nenhuma divergencia nesta nota (era o falso positivo relatado)",
      divergentes == [], f"divergentes={divergentes}")
check("5 grupos: as duas linhas do 26934 viraram um so", len(g_real) == 5, f"={len(g_real)}")

g_26934 = g_real[_chave_item_xml(xml_real[3])]
check("item 26934: XML somou 10,36 + 7,77 = 18,13",
      abs(g_26934["xml_icms"] - 18.13) < 0.01, f"={g_26934['xml_icms']}")
check("item 26934: SPED somou os mesmos 18,13",
      abs(g_26934["sped_icms"] - 18.13) < 0.01, f"={g_26934['sped_icms']}")
check("item 26934: 2 linhas de cada lado",
      g_26934["n_sped"] == 2 and g_26934["n_xml"] == 2, f"={g_26934}")
check("NENHUM grupo ficou com xml_icms zerado (sintoma do bug do codigo)",
      all(v["xml_icms"] > 0 for v in g_real.values()),
      {k[0]: v["xml_icms"] for k, v in g_real.items()})
check("codigo do SPED diferente do codigo do XML nao impede o casamento",
      casar_itens_da_nota(sped_real, xml_real).get(3) is xml_real[3])


print("\n--- 1g. Linha sem direito a crédito não entra na soma do grupo ---")
# CFOP 1556 (uso e consumo) não dá crédito: a glosa é reportada linha a linha
# pela outra regra, e somar esse valor aqui inflaria o total do grupo.
sped4 = [
    {"cod_item": "P6", "cfop": "1102", "qtd": 1, "vl_item": 100.0, "vl_icms": 18.0},
    {"cod_item": "P6", "cfop": "1556", "qtd": 1, "vl_item": 100.0, "vl_icms": 18.0},
]
xml4 = [{"cod_item": "P6", "descr": "MATERIAL", "vl_unit": 100.0, "vl_icms": 18.0}]
# As duas linhas precisam de regra cadastrada: sem regra o motor não sabe se o
# CFOP dá crédito e a linha fica de fora da soma (nem glosa, nem compara).
regras4 = {"1102": True, "1556": False}
g4 = next(iter(agrupar_icms_por_item_identico(sped4, xml4, regras4).values()))
check("só a linha com direito a crédito entrou (18,00, não 36,00)",
      abs(g4["sped_icms"] - 18.0) < 0.01, f"={g4['sped_icms']}")
check("e o grupo não acusa divergência falsa",
      abs(g4["sped_icms"] - g4["xml_icms"]) < 0.05)

g4b = next(iter(agrupar_icms_por_item_identico(sped4, xml4, {}).values()))
check("sem NENHUMA regra cadastrada, nada entra na soma do SPED",
      abs(g4b["sped_icms"]) < 0.01, f"={g4b['sped_icms']}")

print("\n--- 1h. permite_credito_icms: só a regra cadastrada decide, sem chute ---")
# Havia uma lista de CFOPs "sem crédito" fixa no código que decidia quando não
# existia regra. Isso é regra global: valia para toda empresa e glosava crédito
# que nenhum admin aprovou. Sem regra, agora, a função devolve None e o motor cala.
check("regra cadastrada True libera", permite_credito_icms("1102", {"1102": True}) is True)
check("regra cadastrada False bloqueia", permite_credito_icms("1102", {"1102": False}) is False)
check("sem regra, devolve None (não False) para CFOP de uso/consumo",
      permite_credito_icms("1556", {}) is None)
check("sem regra, devolve None (não True) para CFOP comum",
      permite_credito_icms("1102", {}) is None)
check("1556 não é mais tratado de forma especial pelo código",
      permite_credito_icms("1556", {"1556": True}) is True)


print("\n=== 1j. Pareamento SPED x XML: o deslocamento da nota 58897 ===")
# Nota real (21/08/2026): o SPED estava deslocado UMA linha em relacao ao XML.
# O motor casava por posicao quando o codigo nao batia, entao cada item foi
# comparado com o vizinho e a tela encheu de divergencia inventada:
#   RESISTENCIA -> SPED 1,45 (que e do SENSOR HALL) x XML 2,15
#   SENSOR HALL -> SPED 2,06 (que e da VALVULA)     x XML 1,45
DANFE_58897 = [
    ("MQ1420", "VALVULA", 49.32, 8, 15.78), ("BI2040", "BICO INJETOR", 57.00, 4, 9.12),
    ("APX0343", "MOLA A GAS", 16.11, 2, 1.29), ("APX0332", "MOLA A GAS", 16.11, 4, 2.58),
    ("MQ0650", "CORPO DE BORBOLETA TBI", 218.11, 2, 17.45),
    ("APX0369", "MOLA A GAS", 16.11, 6, 3.87),
    ("APX0473", "CABO TRANSFERENCIA", 30.69, 24, 29.46),
    ("MQ0544", "VALVULA", 52.61, 5, 10.52), ("MQ0548", "VALVULA", 58.09, 8, 18.59),
    ("AT0505", "DISTRIBUIDOR", 87.68, 1, 3.51),
    ("MQ1097", "RESISTENCIA DO ELETROVENTILADOR", 26.85, 2, 2.15),
    ("MQ0172", "SENSOR HALL", 12.06, 3, 1.45), ("MQ0558", "VALVULA", 51.51, 1, 2.06),
    ("MQ0543", "VALVULA", 67.95, 3, 8.15),
    ("MQ1415", "SENSOR TEMPERATURA", 13.04, 30, 15.65),
    ("BI0106", "BICO INJETOR", 70.15, 3, 8.42),
    ("MQ1608", "RESFRIADOR DE OLEO", 127.14, 3, 15.26),
    ("MQ1606", "RESFRIADOR DE OLEO", 73.43, 1, 2.94),
    ("MQ1414", "VALVULA", 41.65, 4, 6.66), ("APX0395", "MOLA A GAS", 16.11, 2, 1.29),
    ("MQ1412", "VALVULA", 13.15, 3, 1.58),
]
xml_58897 = [{"cod_item": c, "descr": d, "vl_unit": u, "qtd": float(q), "vl_icms": i}
             for c, d, u, q, i in DANFE_58897]
# SPED com codigo INTERNO da empresa (nao bate com o cProd do fornecedor) e
# deslocado uma linha: a linha N do SPED e o item N+1 da nota.
sped_58897 = [{"cod_item": f"INT{n:04d}", "num_item": str(n), "cfop": "2102",
               "qtd": float(q), "vl_item": round(u * q, 2), "vl_icms": i}
              for n, (c, d, u, q, i) in enumerate(DANFE_58897[1:], start=1)]

pares_58897 = casar_itens_da_nota(sped_58897, xml_58897)
check("todas as 20 linhas do SPED foram pareadas",
      len(pares_58897) == len(sped_58897), f"={len(pares_58897)}/{len(sped_58897)}")

errados = [i for i, s in enumerate(sped_58897)
           if pares_58897.get(i) is None
           or abs(pares_58897[i]["vl_icms"] - s["vl_icms"]) > 0.005]
check("nenhuma linha pareada com o produto ERRADO (era o bug: todas)",
      errados == [], f"linhas erradas={errados}")

g_58897 = agrupar_icms_por_item_identico(sped_58897, xml_58897, {"2102": True},
                                         pares=pares_58897)
falsas = [k[0] for k, v in g_58897.items()
          if abs(v["sped_icms"] - v["xml_icms"]) > 0.05 and v["n_sped"] > 0]
check("nenhuma divergencia entre itens que o SPED escriturou",
      falsas == [], f"={falsas}")

# O item que o SPED de fato nao escriturou continua aparecendo — e divergencia real.
so_no_xml = [k[0] for k, v in g_58897.items() if v["n_sped"] == 0]
check("item ausente no SPED continua sendo apontado (MQ1420)",
      so_no_xml == ["MQ1420"], f"={so_no_xml}")


print("\n--- 1k. Itens gemeos de codigos diferentes vao para linhas distintas ---")
# APX0343 e APX0395: mesmo nome, mesmo valor unitario, MESMA quantidade e
# mesmo ICMS — so o codigo difere. Sem consumo, as duas linhas do SPED caiam
# na primeira do XML: um grupo somava 2,58 contra 1,29 e o outro ficava zerado.
xml_gemeos = [
    {"cod_item": "APX0343", "descr": "MOLA A GAS", "vl_unit": 16.11, "qtd": 2.0, "vl_icms": 1.29},
    {"cod_item": "APX0395", "descr": "MOLA A GAS", "vl_unit": 16.11, "qtd": 2.0, "vl_icms": 1.29},
]
sped_gemeos = [
    {"cod_item": "INT1", "num_item": "1", "cfop": "2102", "qtd": 2.0, "vl_item": 32.22, "vl_icms": 1.29},
    {"cod_item": "INT2", "num_item": "2", "cfop": "2102", "qtd": 2.0, "vl_item": 32.22, "vl_icms": 1.29},
]
pares_gemeos = casar_itens_da_nota(sped_gemeos, xml_gemeos)
check("cada linha do SPED foi para uma linha DIFERENTE do XML",
      pares_gemeos[0] is not pares_gemeos[1],
      "as duas apontaram para o mesmo item do XML")
g_gemeos = agrupar_icms_por_item_identico(sped_gemeos, xml_gemeos, {"2102": True},
                                          pares=pares_gemeos)
check("nenhum grupo com soma inflada nem zerada",
      all(abs(v["sped_icms"] - v["xml_icms"]) < 0.05 for v in g_gemeos.values()),
      {k[0]: v for k, v in g_gemeos.items()})


print("\n--- 1l. SPED fraciona em 2 o que o XML emitiu em 1 (reaproveitamento) ---")
# O oposto do caso acima: aqui as duas linhas do SPED PRECISAM apontar para a
# mesma linha do XML, senao a segunda fica sem par e a soma nao fecha.
xml_1linha = [{"cod_item": "P9", "descr": "CANETA", "vl_unit": 100.0, "qtd": 10.0, "vl_icms": 120.0}]
sped_2linhas = [
    {"cod_item": "P9", "num_item": "1", "cfop": "1102", "qtd": 3.0, "vl_item": 300.0, "vl_icms": 36.0},
    {"cod_item": "P9", "num_item": "2", "cfop": "1102", "qtd": 7.0, "vl_item": 700.0, "vl_icms": 84.0},
]
pares_frac = casar_itens_da_nota(sped_2linhas, xml_1linha)
check("as duas linhas do SPED foram pareadas", len(pares_frac) == 2, f"={len(pares_frac)}")
check("as duas apontam para a MESMA linha do XML (de propósito)",
      pares_frac[0] is pares_frac[1])
g_frac = agrupar_icms_por_item_identico(sped_2linhas, xml_1linha, {"1102": True},
                                        pares=pares_frac)
v_frac = next(iter(g_frac.values()))
check("soma fecha: 36 + 84 = 120 dos dois lados",
      abs(v_frac["sped_icms"] - 120.0) < 0.01 and abs(v_frac["xml_icms"] - 120.0) < 0.01,
      f"={v_frac}")


print("\n--- 1m. Sem sinal confiável, a linha fica de fora (não inventa par) ---")
# Codigo diferente, valor unitario diferente, quantidade diferente e a posicao
# nao confere: nao ha como afirmar a que item do XML a linha corresponde.
xml_outro = [{"cod_item": "ZZZ", "descr": "OUTRO PRODUTO", "vl_unit": 999.99,
              "qtd": 1.0, "vl_icms": 50.0}]
sped_sem_par = [{"cod_item": "AAA", "num_item": "1", "cfop": "1102",
                 "qtd": 7.0, "vl_item": 70.0, "vl_icms": 8.0}]
pares_sem = casar_itens_da_nota(sped_sem_par, xml_outro)
check("linha sem sinal confiável não é pareada por posição",
      pares_sem == {}, f"={pares_sem}")


# ============================================================================
print("\n=== 2. Ponta a ponta: execute_audit com SPED + XML reais ===")

CHV_OK = "1" * 44   # nota sem divergência real (soma bate)
CHV_DIVERGE = "2" * 44   # nota com divergência real (soma diferente)
CHV_SIMPLES = "3" * 44   # nota comum, sem duplicidade — não pode regredir


def _sped_c100_c170(chave, num, itens):
    """Um C100 com N linhas C170. `itens` é lista de (cod, qtd, vl_item, vl_icms)."""
    vl_doc = round(sum(i[2] for i in itens), 2)
    linhas = [
        f"|C100|0|1|9999|55|00|1|{num}|{chave}|18072026|18072026|"
        f"{str(vl_doc).replace('.', ',')}|0|0,00|0,00|{str(vl_doc).replace('.', ',')}|"
        f"0|0,00|0,00|0,00|0,00|0,00|0,00|0,00|0,00|"
    ]
    for idx, (cod, qtd, vl_item, vl_icms) in enumerate(itens, start=1):
        campos = ["C170", str(idx), cod, "Descricao Complementar", str(qtd).replace(".", ","),
                  "UN", str(vl_item).replace(".", ","), "0,00", "0", "00", "1102", "",
                  str(vl_item).replace(".", ","), "18,00", str(vl_icms).replace(".", ","),
                  "0,00", "0,00", "0,00", "0", "", "", "0,00", "0,00", "0,00",
                  "", "0,00", "0,00", "0,00", "0,00", "0,00", "", "0,00", "0,00", "0,00", "0,00", "0,00"]
        linhas.append("|" + "|".join(campos) + "|")
    return linhas


def montar_sped(notas, produtos):
    fd, caminho = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    with open(caminho, "w", encoding="latin1") as f:
        f.write("|0000|017|0|01072026|31072026|EMPRESA TESTE LTDA|11222333000181|MG|||\n")
        for cod, descr in produtos.items():
            f.write(f"|0200|{cod}|{descr}|||00||||0,00||\n")
        for chave, num, itens in notas:
            for linha in _sped_c100_c170(chave, num, itens):
                f.write(linha + "\n")
    return caminho


NS = "http://www.portalfiscal.inf.br/nfe"


def _det_xml(n_item, cod, descr, vl_unit, vl_prod, vl_icms):
    return f"""
      <det nItem="{n_item}">
        <prod>
          <cProd>{cod}</cProd>
          <xProd>{descr}</xProd>
          <CFOP>5102</CFOP>
          <vProd>{vl_prod:.2f}</vProd>
          <vUnCom>{vl_unit:.4f}</vUnCom>
        </prod>
        <imposto>
          <ICMS><ICMS00>
            <CST>00</CST>
            <vBC>{vl_prod:.2f}</vBC>
            <pICMS>18.00</pICMS>
            <vICMS>{vl_icms:.2f}</vICMS>
          </ICMS00></ICMS>
        </imposto>
      </det>"""


def montar_xml(xml_dir, chave, num, itens_xml):
    """`itens_xml` é lista de (cod, descr, vl_unit, vl_prod, vl_icms)."""
    dets = "".join(_det_xml(i + 1, *item) for i, item in enumerate(itens_xml))
    vl_nf = round(sum(i[3] for i in itens_xml), 2)
    conteudo = f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="{NS}">
  <NFe>
    <infNFe Id="NFe{chave}">
      <ide><nNF>{num}</nNF><serie>1</serie></ide>
      <emit>
        <CNPJ>33444555000199</CNPJ>
        <xNome>Fornecedor Teste</xNome>
        <enderEmit><UF>SP</UF></enderEmit>
      </emit>
      {dets}
      <total><ICMSTot><vNF>{vl_nf:.2f}</vNF><vST>0.00</vST></ICMSTot></total>
    </infNFe>
  </NFe>
</nfeProc>"""
    caminho = os.path.join(xml_dir, f"{chave}.xml")
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(conteudo)
    return caminho


xml_dir = tempfile.mkdtemp(prefix="synax_icms_xml_")

# Nota 1: mesmo produto em 2 linhas de cada lado, fracionado DIFERENTE — a SOMA bate.
montar_xml(xml_dir, CHV_OK, "1001", [
    ("P1", "Parafuso Sextavado", 100.0, 600.0, 60.0),
    ("P1", "Parafuso Sextavado", 100.0, 900.0, 115.0),
])
# Nota 2: mesmo produto em 2 linhas — a soma É diferente (divergência real).
montar_xml(xml_dir, CHV_DIVERGE, "1002", [
    ("P2", "Caneta Azul", 100.0, 100.0, 18.0),
    ("P2", "Caneta Azul", 100.0, 100.0, 10.0),
])
# Nota 3: um item só, sem duplicidade — tem que continuar funcionando como antes.
montar_xml(xml_dir, CHV_SIMPLES, "1003", [
    ("P3", "Grampeador", 200.0, 200.0, 24.0),
])

sped_path = montar_sped(
    notas=[
        (CHV_OK, "1001", [("P1", 10, 1000.0, 120.0), ("P1", 5, 500.0, 55.0)]),
        (CHV_DIVERGE, "1002", [("P2", 1, 100.0, 18.0), ("P2", 1, 100.0, 18.0)]),
        (CHV_SIMPLES, "1003", [("P3", 1, 200.0, 30.0)]),
    ],
    produtos={"P1": "Parafuso Sextavado", "P2": "Caneta Azul", "P3": "Grampeador"},
)

db = SessionLocal()
tenant_id = str(uuid.uuid4())
task_id = str(uuid.uuid4())
repo = RulesRepository(db, tenant_id)
repo.audit_tasks.add(AuditTask(id=task_id, status="pendente", user_id="teste", file_name="sped.txt"))

# Sem regra cadastrada o motor não decide nada sobre o CFOP: ele vai para a fila
# de pendências e nenhuma divergência de ICMS é reportada. A regra tem que ser
# deste tenant — não existe regra global que valha para todo mundo.
repo.credit_rules.add(CfopCreditRule(cfop="1102", tenant_id=tenant_id, allow_credit=True,
                                     description="teste", status="aprovado", is_global=False))

execute_audit(task_id, tenant_id, sped_path, xml_dir, db, sped_only_mode=False, ignore_saidas=True)

db.refresh(repo.audit_tasks.get_by_id(task_id))
task = repo.audit_tasks.get_by_id(task_id)
check("auditoria concluiu sem erro", task.status == "concluido",
      f"status={task.status} erro={task.error_message}")

resultado = json.loads(task.result_data or "{}")
icms_issues = resultado.get("icms_issues", [])

print("\n--- 2a. Nota com soma batendo: SEM divergência (o ponto central do pedido) ---")
issues_ok = [i for i in icms_issues if i.get("key") == CHV_OK]
check("nenhuma divergência de ICMS para a nota cuja soma bate",
      issues_ok == [], f"issues={issues_ok}")

print("\n--- 2b. Nota com soma diferente: UMA divergência, com a soma certa ---")
issues_diverge = [i for i in icms_issues if i.get("key") == CHV_DIVERGE]
check("exatamente 1 issue reportada (não 1 por linha duplicada)",
      len(issues_diverge) == 1, f"issues={issues_diverge}")
if issues_diverge:
    d = issues_diverge[0]
    check("sped_icms somado = 36,00", abs(d["sped_icms"] - 36.0) < 0.01, f"={d['sped_icms']}")
    check("xml_icms somado = 28,00", abs(d["xml_icms"] - 28.0) < 0.01, f"={d['xml_icms']}")
    check("motivo explica que é a soma das linhas do mesmo produto",
          "soma das 2 linhas" in d["reason"], d["reason"])
    check("itens_agrupados = 2", d.get("itens_agrupados") == 2, f"={d.get('itens_agrupados')}")

print("\n--- 2c. Nota simples (sem duplicidade): comportamento igual ao de sempre ---")
issues_simples = [i for i in icms_issues if i.get("key") == CHV_SIMPLES]
check("SPED R$30 x XML R$24 diverge (diferença real de R$ 6,00)",
      len(issues_simples) == 1, f"issues={issues_simples}")
if issues_simples:
    s = issues_simples[0]
    check("sem menção a soma de linhas quando não há duplicidade",
          "soma das" not in s["reason"], s["reason"])
    check("itens_agrupados = 1 (grupo trivial)", s.get("itens_agrupados", 1) == 1)

db.close()
engine.dispose()
# execute_audit já limpa sped_path e xml_dir no próprio `finally` (arquivos
# efêmeros de auditoria fiscal não podem sobrar em disco) — aqui só cobre o
# que ele não toca.
import shutil as _shutil
_shutil.rmtree(xml_dir, ignore_errors=True)
if os.path.exists(sped_path):
    try:
        os.remove(sped_path)
    except OSError:
        pass
if os.path.exists("_icms_grupo_test.db"):
    try:
        os.remove("_icms_grupo_test.db")
    except OSError:
        pass


# ============================================================================
print("\n" + "=" * 60)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("FALHAS:")
    for nome in _falhou:
        print(f"  - {nome}")
sys.exit(1 if FALHAS else 0)
