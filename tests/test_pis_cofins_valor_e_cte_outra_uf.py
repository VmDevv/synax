# -*- coding: utf-8 -*-
"""Três pedidos do Sandro, todos sobre valor que sumia.

1. PIS/COFINS confrontado por BASE contra BASE.
   A base recalculada pela tese do STF (item menos ICMS, ICMS ST, FCP ST e IPI)
   contra o VL_BC_PIS escriturado no C170.

   Pedido do Sandro em 28/08/2026, revertendo o confronto por VALOR que vigorou
   em 27/08. O efeito é conhecido e foi aceito: o SPED Fiscal quase nunca
   preenche VL_BC_PIS — PIS/COFINS é escriturado na EFD Contribuições — então a
   linha sai "SPED R$ 0,00 vs Base Calculada R$ 820,00" na maioria dos itens.
   Isso está travado no caso 3c, de propósito, para não ser tratado como bug
   novo depois.

   `credito_pis_cofins_esperado()` continua no motor, fora de uso e testada
   isoladamente na seção 1, para o caso de a decisão voltar atrás.

2. ICMS do CT-e soma `vICMSOutraUF`.
   Tag DIFERENTE de `vICMS`. Lendo só `vICMS`, o frete com ICMS devido a outra
   UF chegava com crédito zero e a auditoria não comparava nada.

3. Item sem par confiável passa a ser LISTADO.
   Antes sumia num `continue` mudo — foi o que houve com a NF 2777 da Dist.
   Laurencini. Não conferir é resposta legítima; não avisar não é.
"""
import os, sys, uuid, json, tempfile, io, shutil, unicodedata
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_pis_valor_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

if os.path.exists("_pis_valor_test.db"):
    os.remove("_pis_valor_test.db")

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
from backend.app.domain.models import rules, audit  # noqa: F401
from backend.app.domain.models.audit import AuditTask
from backend.app.infrastructure.repositories.rules_repository import RulesRepository
from backend.app.domain.models.rules import CfopCreditRule, PisCofinsCreditRule
from backend.app.services.audit_engine.cross_checker import (
    execute_audit, credito_pis_cofins_esperado, ALIQ_PIS_PADRAO, ALIQ_COFINS_PADRAO,
)
from backend.app.services.audit_engine.xml_parser import parse_cte_xml

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

def sem_acento(t):
    return "".join(c for c in unicodedata.normalize("NFD", str(t))
                   if unicodedata.category(c) != "Mn").lower()


# ============================================================================
print("\n=== 1. O crédito esperado, isolado ===")
# Base 820,00 (1000 do item menos 180 de ICMS) com as alíquotas do XML.
pis, cof, ap, ac = credito_pis_cofins_esperado(
    {"vl_bc_pis": 820.00, "p_pis": 1.65, "p_cofins": 7.60})
check("PIS = base x 1,65%", pis == 13.53, pis)
check("COFINS = base x 7,60%", cof == 62.32, cof)

# Sem alíquota no XML, entram as do regime não-cumulativo.
pis2, cof2, ap2, ac2 = credito_pis_cofins_esperado({"vl_bc_pis": 820.00})
check("sem pPIS no XML, usa 1,65% de reserva", ap2 == ALIQ_PIS_PADRAO == 1.65, ap2)
check("sem pCOFINS no XML, usa 7,60% de reserva", ac2 == ALIQ_COFINS_PADRAO == 7.60, ac2)
check("a reserva dá o mesmo valor do não-cumulativo", (pis2, cof2) == (13.53, 62.32),
      (pis2, cof2))

# Alíquota do documento MANDA — fornecedor no cumulativo credita 0,65/3,00.
pis3, cof3, ap3, ac3 = credito_pis_cofins_esperado(
    {"vl_bc_pis": 1000.00, "p_pis": 0.65, "p_cofins": 3.00})
check("a alíquota do XML vence a de reserva", (ap3, ac3) == (0.65, 3.00), (ap3, ac3))
check("crédito do cumulativo sai certo", (pis3, cof3) == (6.50, 30.00), (pis3, cof3))

check("base zero não gera crédito",
      credito_pis_cofins_esperado({"vl_bc_pis": 0.0})[:2] == (0.0, 0.0))


# ============================================================================
print("\n=== 2. CT-e: vICMSOutraUF entra no crédito ===")
NS_CTE = "http://www.portalfiscal.inf.br/cte"
CH_CTE = "5" * 44


def xml_cte(bloco_icms):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<cteProc xmlns="{NS_CTE}"><CTe><infCte Id="CTe{CH_CTE}">
 <ide><nCT>211146</nCT><serie>1</serie><CFOP>6352</CFOP>
   <UFIni>SP</UFIni><UFFim>ES</UFFim><toma3><toma>0</toma></toma3></ide>
 <emit><CNPJ>99888777000100</CNPJ><xNome>TRANSPORTADORA LAURENCINI</xNome>
   <enderEmit><UF>SP</UF></enderEmit></emit>
 <vPrest><vTPrest>1000.00</vTPrest></vPrest>
 <imp><ICMS>{bloco_icms}</ICMS></imp>
</infCte></CTe></cteProc>"""


def parse_bloco(bloco):
    d = tempfile.mkdtemp(prefix="cte_uf_")
    p = os.path.join(d, f"{CH_CTE}.xml")
    io.open(p, "w", encoding="utf-8").write(xml_cte(bloco))
    r = parse_cte_xml(p)
    shutil.rmtree(d, ignore_errors=True)
    return r


r = parse_bloco("<ICMS00><CST>00</CST><vBC>1000.00</vBC><pICMS>12.00</pICMS>"
                "<vICMS>120.00</vICMS></ICMS00>")
check("ICMS normal continua lido", r.get("vl_icms") == 120.00, r.get("vl_icms"))
check("sem OutraUF, a parcela é zero", r.get("vl_icms_outra_uf") == 0.0)

r = parse_bloco("<ICMSOutraUF><CST>90</CST><vBCOutraUF>1000.00</vBCOutraUF>"
                "<pICMSOutraUF>12.00</pICMSOutraUF>"
                "<vICMSOutraUF>120.00</vICMSOutraUF></ICMSOutraUF>")
check("ICMSOutraUF vira crédito (antes vinha ZERO)", r.get("vl_icms") == 120.00,
      r.get("vl_icms"))
check("a parcela fica visível em separado", r.get("vl_icms_outra_uf") == 120.00,
      r.get("vl_icms_outra_uf"))
check("a base sai de vBCOutraUF", r.get("vl_bc") == 1000.00, r.get("vl_bc"))

r = parse_bloco("<ICMS00><CST>00</CST><vBC>500.00</vBC><vICMS>60.00</vICMS></ICMS00>"
                "<ICMSOutraUF><vICMSOutraUF>40.00</vICMSOutraUF></ICMSOutraUF>")
check("as duas parcelas somam", r.get("vl_icms") == 100.00, r.get("vl_icms"))
check("a parcela própria fica separada", r.get("vl_icms_proprio") == 60.00,
      r.get("vl_icms_proprio"))


# ============================================================================
print("\n=== 3. Ponta a ponta: NF-e ===")
NS = "http://www.portalfiscal.inf.br/nfe"
CHAVE = "7" * 44
EMPRESA = "11222333000181"
FORNEC = "99888777000100"


def montar_xml(itens):
    """`itens` = lista de (cod, descr, v_prod, v_icms, p_pis, p_cofins)."""
    dets = ""
    for i, (cod, descr, v_prod, v_icms, p_pis, p_cof) in enumerate(itens, start=1):
        dets += f"""
 <det nItem="{i}"><prod><cProd>{cod}</cProd><xProd>{descr}</xProd>
   <CFOP>5102</CFOP><NCM>84713012</NCM><vProd>{v_prod:.2f}</vProd>
   <qCom>1.0000</qCom><vUnCom>{v_prod:.2f}</vUnCom></prod>
   <imposto>
     <ICMS><ICMS00><CST>00</CST><vBC>{v_prod:.2f}</vBC><pICMS>18.00</pICMS>
       <vICMS>{v_icms:.2f}</vICMS></ICMS00></ICMS>
     <PIS><PISAliq><CST>50</CST><vBC>{v_prod:.2f}</vBC><pPIS>{p_pis:.2f}</pPIS>
       <vPIS>{v_prod * p_pis / 100:.2f}</vPIS></PISAliq></PIS>
     <COFINS><COFINSAliq><CST>50</CST><vBC>{v_prod:.2f}</vBC>
       <pCOFINS>{p_cof:.2f}</pCOFINS>
       <vCOFINS>{v_prod * p_cof / 100:.2f}</vCOFINS></COFINSAliq></COFINS>
   </imposto>
 </det>"""
    total = sum(i[2] for i in itens)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="{NS}"><NFe><infNFe Id="NFe{CHAVE}">
 <ide><nNF>2777</nNF><serie>1</serie></ide>
 <emit><CNPJ>{FORNEC}</CNPJ><xNome>DIST LAURENCINI</xNome>
   <enderEmit><UF>ES</UF></enderEmit></emit>
 <dest><CNPJ>{EMPRESA}</CNPJ><enderDest><UF>ES</UF></enderDest></dest>{dets}
 <total><ICMSTot><vNF>{total:.2f}</vNF></ICMSTot></total>
</infNFe></NFe></nfeProc>"""


def montar_sped(itens_sped):
    """`itens_sped` = lista de (cod, v_item, v_icms, v_pis, v_cofins[, base_pis]).

    O 6º campo é opcional: sem ele, VL_BC_PIS e VL_BC_COFINS saem VAZIOS, que é
    como o SPED Fiscal chega de verdade. Com ele, dá para exercitar o caso em
    que a escrituração traz a base — que é o que a conferência confronta desde
    28/08.
    """
    fd, p = tempfile.mkstemp(suffix=".txt"); os.close(fd)
    v = lambda x: f"{x:.2f}".replace(".", ",")  # noqa: E731
    total = sum(i[1] for i in itens_sped)
    linhas = [
        f"|0000|017|0|01072026|31072026|EMPRESA TESTE LTDA|{EMPRESA}|ES|||",
    ]
    for linha_sped in itens_sped:
        linhas.append(f"|0200|{linha_sped[0]}|PRODUTO {linha_sped[0]} GENERICO|||UN|00|||||")
    linhas.append(
        f"|C100|0|1|9999|55|00|1|2777|{CHAVE}|18072026|18072026|"
        f"{v(total)}|0|0,00|0,00|{v(total)}|0|0,00|0,00|0,00|0,00|0,00|0,00|0,00|0,00|")
    for idx, linha_sped in enumerate(itens_sped, start=1):
        cod, v_item, v_icms, v_pis, v_cof = linha_sped[:5]
        base = v(linha_sped[5]) if len(linha_sped) > 5 else ""
        # Layout C170: 11 CFOP, 15 VL_ICMS, 19 IND_APUR, 20 CST_IPI,
        # 24 VL_IPI, 25 CST_PIS, 26 VL_BC_PIS, 30 VL_PIS, 31 CST_COFINS,
        # 32 VL_BC_COFINS, 36 VL_COFINS.
        linhas.append("|" + "|".join([
            "C170", str(idx), cod, f"PRODUTO {cod}", "1,00", "UN", v(v_item), "0,00",
            "0", "00", "1102", "",
            v(v_item), "18,00", v(v_icms),
            "0,00", "0,00", "0,00",
            "0",
            "", "", "0,00", "0,00", "0,00",
            "50", base, "1,65", "", "", v(v_pis),
            "50", base, "7,60", "", "", v(v_cof),
            "", "0,00",
        ]) + "|")
    io.open(p, "w", encoding="latin1").write("\n".join(linhas) + "\n")
    return p


def rodar(itens_xml, itens_sped):
    xml_dir = tempfile.mkdtemp(prefix="pisval_")
    io.open(os.path.join(xml_dir, f"{CHAVE}.xml"), "w", encoding="utf-8").write(
        montar_xml(itens_xml))
    sped = montar_sped(itens_sped)
    db = SessionLocal()
    tid, task_id = str(uuid.uuid4()), str(uuid.uuid4())
    repo = RulesRepository(db, tid)
    repo.audit_tasks.add(AuditTask(id=task_id, status="pendente", user_id="t",
                                   file_name="s.txt"))
    repo.credit_rules.add(CfopCreditRule(cfop="1102", tenant_id=tid, allow_credit=True,
                                         description="t", status="aprovado", is_global=False))
    repo.pis_cofins_credit_rules.add(PisCofinsCreditRule(
        cfop="1102", cst_expected="50", tenant_id=tid, allow_credit=True,
        status="aprovado", is_global=False))
    execute_audit(task_id, tid, sped, xml_dir, db, sped_only_mode=False,
                  ignore_saidas=True, considerar_icms_mono=False)
    d = json.loads(RulesRepository(db, tid).audit_tasks.get_by_id(task_id).result_data or "{}")
    db.close()
    shutil.rmtree(xml_dir, ignore_errors=True)
    os.path.exists(sped) and os.remove(sped)
    return d


print("\n--- 3a. SPED escriturou a MESMA base: sem divergência ---")
# Item 1000,00 com ICMS 180,00 -> base STF 820,00. O SPED traz 820,00 também.
d = rodar([("P1", "PRODUTO UM", 1000.00, 180.00, 1.65, 7.60)],
          [("P1", 1000.00, 180.00, 13.53, 62.32, 820.00)])
check("base igual não gera divergência", d.get("pis_cofins_issues") == [],
      [i["reason"] for i in d.get("pis_cofins_issues", [])])

print("\n--- 3b. SPED escriturou a base CHEIA (sem excluir o ICMS) ---")
d = rodar([("P1", "PRODUTO UM", 1000.00, 180.00, 1.65, 7.60)],
          [("P1", 1000.00, 180.00, 16.50, 76.00, 1000.00)])
razoes = [i["reason"] for i in d.get("pis_cofins_issues", [])]
check("acusa a divergência de BASE", len(razoes) == 1, razoes)
check("a mensagem confronta as duas bases",
      razoes and "1000.00" in razoes[0] and "820.00" in razoes[0], razoes)
check("a mensagem fala em base, não em crédito",
      razoes and "base" in sem_acento(razoes[0]), razoes)
issue = d["pis_cofins_issues"][0]
check("o valor do SPED é a base escriturada", issue["sped_val"] == 1000.00,
      issue["sped_val"])
check("o valor do XML é a base calculada", issue["xml_val"] == 820.00,
      issue["xml_val"])
check("guarda a base calculada em campo próprio",
      issue["base_calculada"] == 820.00, issue.get("base_calculada"))
check("guarda também a base do SPED", issue["base_sped"] == 1000.00,
      issue.get("base_sped"))
check("o diagnóstico reconhece a diferença = ICMS (tese STF)",
      "STF" in issue.get("diagnostic", ""), issue.get("diagnostic"))

print("\n--- 3c. SPED sem base escriturada: acusa contra o campo vazio ---")
# Este é o efeito conhecido e ACEITO da decisão de 28/08: o C170 do SPED Fiscal
# quase nunca traz VL_BC_PIS, então a linha sai comparando com zero. Fica
# travado aqui para que ninguém trate isso como bug novo mais adiante.
d = rodar([("P1", "PRODUTO UM", 1000.00, 180.00, 1.65, 7.60)],
          [("P1", 1000.00, 180.00, 13.53, 62.32)])
razoes = [i["reason"] for i in d.get("pis_cofins_issues", [])]
check("base vazia no SPED gera a linha", len(razoes) == 1, razoes)
check("a linha é 'SPED R$ 0,00 vs Base Calculada R$ 820,00'",
      razoes and "0.00" in razoes[0] and "820.00" in razoes[0], razoes)

print("\n--- 3d. Item sem par confiável é LISTADO (NF 2777) ---")
# O SPED tem um item a mais, com código que não existe no XML e valor que não
# casa com nada: é o produto complementar/genérico que sumia calado.
d = rodar([("P1", "PRODUTO UM", 1000.00, 180.00, 1.65, 7.60)],
          [("P1", 1000.00, 180.00, 13.53, 62.32, 820.00),
           ("COMPL9", 333.33, 0.00, 0.00, 0.00)])
nc = d.get("itens_nao_conferidos", [])
check("o item sem par aparece na lista", len(nc) == 1, nc)
check("a lista diz de qual nota é", nc and nc[0]["num"] == "2777", nc)
check("a lista traz o código do produto", nc and nc[0]["cod_item"] == "COMPL9", nc)
check("a lista traz a descrição do 0200",
      nc and "COMPL9" in str(nc[0]["descr"]).upper(), nc)
check("a lista explica o motivo",
      nc and "nao casou" in sem_acento(nc[0]["motivo"]), nc)
check("o item casado continua sendo conferido normalmente",
      d.get("pis_cofins_issues") == [], d.get("pis_cofins_issues"))

print("\n--- 3e. Nota inteira casada não polui a lista ---")
d = rodar([("P1", "PRODUTO UM", 1000.00, 180.00, 1.65, 7.60)],
          [("P1", 1000.00, 180.00, 13.53, 62.32, 820.00)])
check("nada de falso positivo em nota que casou",
      d.get("itens_nao_conferidos") == [], d.get("itens_nao_conferidos"))


print("\n" + "=" * 60)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if FALHAS:
    print("FALHAS:")
    for m in _falhou:
        print("   ", m)
    print("FALHA")
    sys.exit(1)
print("OK")
