# -*- coding: utf-8 -*-
"""Quem decide crédito é o CFOP MAPEADO, não o escriturado no SPED.

Pedido do Sandro (26/08/2026). O CFOP do SPED é o que o cliente lançou — é
justamente o que está sob auditoria, então não pode ser também o critério da
auditoria. Quando existe regra de classificação para a operação, ela diz qual
CFOP a entrada deveria ter, e é esse que determina se o imposto é creditável.

O bug que isto fecha: escriturado 1556 (sem crédito), mapeado 1653 (com
crédito). O motor cuspia as duas coisas ao mesmo tempo —

    "o CFOP deveria ser 1653"                     (aba de CFOP)
    "1556 não permite crédito, glose o ICMS"      (aba de ICMS)

— mandando desfazer um crédito que, com o CFOP corrigido, é devido. A
divergência certa é a oposta: o crédito que faltou aproveitar.

Vale para ICMS, IPI e PIS/COFINS. Sem regra de classificação nada muda: o
CFOP do SPED continua sendo o único que existe.
"""
import os, sys, uuid, json, tempfile, io, shutil, unicodedata
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_cfop_mapeado_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

if os.path.exists("_cfop_mapeado_test.db"):
    os.remove("_cfop_mapeado_test.db")

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
from backend.app.domain.models import rules, audit  # noqa: F401 — registra os models
from backend.app.domain.models.audit import AuditTask
from backend.app.infrastructure.repositories.rules_repository import RulesRepository
from backend.app.domain.models.rules import (
    CfopCreditRule, CfopClassification, IpiCreditRule, PisCofinsCreditRule,
)
from backend.app.services.audit_engine.cross_checker import (
    execute_audit, cfop_de_referencia, _nota_cfop, permite_credito_icms,
    agrupar_icms_por_item_identico,
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


def sem_acento(txt):
    return "".join(c for c in unicodedata.normalize("NFD", str(txt))
                   if unicodedata.category(c) != "Mn").lower()


# ============================================================================
print("\n=== 1. A regra, isolada ===")
check("com mapeamento, vale o mapeado", cfop_de_referencia("1556", "1653") == "1653")
check("sem mapeamento, vale o do SPED", cfop_de_referencia("1556", None) == "1556")
check("mapeamento vazio nao apaga o do SPED", cfop_de_referencia("1556", "") == "1556")
check("mapeado igual ao do SPED e o mesmo CFOP", cfop_de_referencia("1102", "1102") == "1102")

check("quando difere, a mensagem diz de onde veio o CFOP",
      "1653" in _nota_cfop("1556", "1653") and "1556" in _nota_cfop("1556", "1653"),
      _nota_cfop("1556", "1653"))
check("quando nao difere, nao polui a mensagem", _nota_cfop("1102", "1102") == "")
check("sem regra continua sendo None, nao False", permite_credito_icms("9999", {}) is None)


# ============================================================================
print("\n=== 2. Varredura: nenhuma consulta de credito por sped_cfop ===")
FONTE = io.open("backend/app/services/audit_engine/cross_checker.py", encoding="utf-8").read()
for consulta in ['icms_rules_map.get(sped_cfop)',
                 'ipi_rules_map.get((sped_cfop',
                 'pis_cofins_rules_map.get((sped_cfop']:
    check(f"sumiu: {consulta}", consulta not in FONTE)
for consulta in ['icms_rules_map.get(cfop_regra)',
                 'ipi_rules_map.get((cfop_regra, sped_ipi_cst))',
                 'pis_cofins_rules_map.get((cfop_regra, sped_pis_cst)',
                 'pis_cofins_rules_map.get((cfop_regra, sped_cofins_cst)']:
    check(f"existe: {consulta}", consulta in FONTE)
check("o CT-e tambem usa o CFOP mapeado do frete",
      'cfop_de_referencia(sped_cfop, expected_cfop)' in FONTE)


# ============================================================================
print("\n=== 3. O agrupamento por item usa o mesmo criterio do laco ===")
# Se o filtro do agrupamento olhasse o CFOP do SPED e o laço o mapeado, a
# mesma linha entraria na soma de um lado e sairia glosada do outro.
_sped = [{"cfop": "1556", "vl_icms": 100.0, "cod_item": "A", "num_item": "1"}]
_xml = [{"cfop": "5102", "vl_icms": 100.0, "cod_item": "A",
         "descr": "PRODUTO A", "vl_unit": 10.0, "vl_item": 100.0}]
_regras = {"1556": False, "1653": True}

g1 = next(iter(agrupar_icms_por_item_identico(_sped, _xml, _regras).values()))
check("sem mapa, o CFOP do SPED manda e a linha fica de fora", g1["sped_icms"] == 0.0, g1)

g2 = next(iter(agrupar_icms_por_item_identico(
    _sped, _xml, _regras, cfop_por_linha={0: "1653"}).values()))
check("com mapa para 1653, a linha entra na soma", g2["sped_icms"] == 100.0, g2)

g3 = next(iter(agrupar_icms_por_item_identico(
    _sped, _xml, {"1556": True, "1653": False}, cfop_por_linha={0: "1653"}).values()))
check("mapa manda mesmo quando o SPED permitiria", g3["sped_icms"] == 0.0, g3)


# ============================================================================
print("\n=== 4. Ponta a ponta ===")

NS = "http://www.portalfiscal.inf.br/nfe"
CHAVE = "3" * 44
EMPRESA = "11222333000181"
FORNECEDOR = "99888777000100"
COD = "PROD01"


def montar_xml(cfop_xml, v_icms, v_ipi, v_pis, v_cofins):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="{NS}"><NFe><infNFe Id="NFe{CHAVE}">
 <ide><nNF>777</nNF><serie>1</serie></ide>
 <emit><CNPJ>{FORNECEDOR}</CNPJ><xNome>FORNECEDOR TESTE LTDA</xNome>
   <enderEmit><UF>ES</UF></enderEmit></emit>
 <dest><CNPJ>{EMPRESA}</CNPJ><enderDest><UF>ES</UF></enderDest></dest>
 <det nItem="1"><prod><cProd>{COD}</cProd><xProd>PRODUTO TESTE</xProd>
   <CFOP>{cfop_xml}</CFOP><NCM>84713012</NCM><vProd>1000.00</vProd>
   <qCom>1.0000</qCom><vUnCom>1000.00</vUnCom></prod>
   <imposto>
     <ICMS><ICMS00><CST>00</CST><vBC>1000.00</vBC><pICMS>18.00</pICMS>
       <vICMS>{v_icms:.2f}</vICMS></ICMS00></ICMS>
     <IPI><cEnq>999</cEnq><IPITrib><CST>50</CST><vBC>1000.00</vBC>
       <pIPI>5.00</pIPI><vIPI>{v_ipi:.2f}</vIPI></IPITrib></IPI>
     <PIS><PISAliq><CST>50</CST><vBC>1000.00</vBC><pPIS>1.65</pPIS>
       <vPIS>{v_pis:.2f}</vPIS></PISAliq></PIS>
     <COFINS><COFINSAliq><CST>50</CST><vBC>1000.00</vBC><pCOFINS>7.60</pCOFINS>
       <vCOFINS>{v_cofins:.2f}</vCOFINS></COFINSAliq></COFINS>
   </imposto>
 </det>
 <total><ICMSTot><vNF>1000.00</vNF></ICMSTot></total>
</infNFe></NFe></nfeProc>"""


def montar_sped(cfop_sped, vl_icms, vl_ipi, vl_pis, vl_cofins):
    fd, p = tempfile.mkstemp(suffix=".txt"); os.close(fd)
    v = lambda x: f"{x:.2f}".replace(".", ",")  # noqa: E731
    linhas = [
        f"|0000|017|0|01072026|31072026|EMPRESA TESTE LTDA|{EMPRESA}|ES|||",
        f"|C100|0|1|9999|55|00|1|777|{CHAVE}|18072026|18072026|"
        f"1000,00|0|0,00|0,00|1000,00|0|0,00|0,00|0,00|0,00|0,00|0,00|0,00|0,00|",
    ]
    # Layout oficial do C170 (posicoes 1..38). O split come um campo vazio antes
    # do "C170" por causa do pipe inicial, entao indice da lista = posicao - 1.
    campos = [
        "C170", "1", COD, "PRODUTO TESTE", "1,00", "UN", "1000,00", "0,00",  # 1-8
        "0", "00", cfop_sped, "",                                            # 9-12
        "1000,00", "18,00", v(vl_icms),                                      # 13-15 ICMS
        "0,00", "0,00", "0,00",                                              # 16-18 ST
        "0",                                                                 # 19 IND_APUR
        "50", "999", "1000,00", "5,00", v(vl_ipi),                           # 20-24 IPI
        "50", "1000,00", "1,65", "", "", v(vl_pis),                          # 25-30 PIS
        "50", "1000,00", "7,60", "", "", v(vl_cofins),                       # 31-36 COFINS
        "", "0,00",                                                          # 37-38
    ]
    linhas.append("|" + "|".join(campos) + "|")
    io.open(p, "w", encoding="latin1").write("\n".join(linhas) + "\n")
    return p


def rodar(cfop_sped, cfop_xml, mapeado, icms_rules,
          vl_icms=180.0, vl_ipi=50.0, vl_pis=16.5, vl_cofins=76.0,
          xml_icms=180.0, xml_ipi=50.0, xml_pis=16.5, xml_cofins=76.0,
          ipi_rules=None, pis_rules=None):
    """Uma auditoria completa. `mapeado` = entry_cfop da regra, ou None."""
    xml_dir = tempfile.mkdtemp(prefix="synax_cfopmap_")
    io.open(os.path.join(xml_dir, f"{CHAVE}.xml"), "w", encoding="utf-8").write(
        montar_xml(cfop_xml, xml_icms, xml_ipi, xml_pis, xml_cofins))
    sped = montar_sped(cfop_sped, vl_icms, vl_ipi, vl_pis, vl_cofins)

    db = SessionLocal()
    tid, task_id = str(uuid.uuid4()), str(uuid.uuid4())
    repo = RulesRepository(db, tid)
    repo.audit_tasks.add(
        AuditTask(id=task_id, status="pendente", user_id="teste", file_name="sped.txt"))

    for cfop, permite in (icms_rules or {}).items():
        repo.credit_rules.add(CfopCreditRule(
            cfop=cfop, tenant_id=tid, allow_credit=permite,
            description="teste", status="aprovado", is_global=False))

    for (cfop, cst), permite in (ipi_rules or {}).items():
        repo.ipi_credit_rules.add(IpiCreditRule(
            cfop=cfop, cst_expected=cst, tenant_id=tid, allow_credit=permite,
            status="aprovado", is_global=False))

    for (cfop, cst), permite in (pis_rules or {}).items():
        repo.pis_cofins_credit_rules.add(PisCofinsCreditRule(
            cfop=cfop, cst_expected=cst, tenant_id=tid, allow_credit=permite,
            status="aprovado", is_global=False))

    if mapeado:
        # Chave exata de quatro campos, como o motor consulta. Nada de curinga.
        repo.cfop_classifications.add(CfopClassification(
            company_cnpj=EMPRESA, supplier_cnpj=FORNECEDOR, supplier_cfop=cfop_xml,
            product_code=COD, tenant_id=tid, entry_cfop=mapeado,
            product_description="PRODUTO TESTE", status="aprovado", is_global=False))

    execute_audit(task_id, tid, sped, xml_dir, db, sped_only_mode=False,
                  ignore_saidas=True, considerar_icms_mono=False)
    task = RulesRepository(db, tid).audit_tasks.get_by_id(task_id)
    status, dados = task.status, json.loads(task.result_data or "{}")
    db.close()

    shutil.rmtree(xml_dir, ignore_errors=True)
    if os.path.exists(sped):
        os.remove(sped)
    return status, dados


print("\n--- 4a. Escriturado 1556 (sem credito), mapeado 1653 (com credito) ---")
st, d = rodar("1556", "5102", "1653", {"1556": False, "1653": True})
check("auditoria concluiu", st == "concluido", f"status={st}")
razoes = [i["reason"] for i in d.get("icms_issues", [])]
check("NAO glosa mais um credito que o CFOP mapeado autoriza",
      not any("nao permite credito" in sem_acento(r) for r in razoes), razoes)
check("o CFOP errado no SPED continua reportado na aba de CFOP",
      any(i["expected_cfop"] == "1653" for i in d.get("cfop_classifications_issues", [])),
      d.get("cfop_classifications_issues"))

print("\n--- 4b. Mesmo caso, mas o cliente NAO aproveitou o credito ---")
st, d = rodar("1556", "5102", "1653", {"1556": False, "1653": True}, vl_icms=0.0)
razoes = [i["reason"] for i in d.get("icms_issues", [])]
check("acusa o credito que faltou aproveitar", len(razoes) == 1, razoes)
check("a mensagem fala em ICMS nao aproveitado",
      razoes and "nao aproveitado" in sem_acento(razoes[0]), razoes)
check("a mensagem mostra os DOIS CFOPs",
      razoes and "1653" in razoes[0] and "1556" in razoes[0], razoes)
check("a linha sai etiquetada com o CFOP mapeado",
      d.get("icms_issues") and d["icms_issues"][0]["cfop"] == "1653", d.get("icms_issues"))

print("\n--- 4c. Sem regra de classificacao, nada muda ---")
st, d = rodar("1556", "5102", None, {"1556": False})
razoes = [i["reason"] for i in d.get("icms_issues", [])]
check("o CFOP do SPED volta a mandar e a glosa sai",
      any("1556" in r and "nao permite credito" in sem_acento(r) for r in razoes), razoes)

print("\n--- 4d. Sem regra de ICMS para o CFOP mapeado: pendencia ---")
st, d = rodar("1556", "5102", "1653", {"1556": False})
check("nao alega nada sobre ICMS", d.get("icms_issues") == [], d.get("icms_issues"))
check("a pendencia pede regra para o CFOP MAPEADO",
      d.get("unmapped_icms_cfops") == ["1653"], d.get("unmapped_icms_cfops"))

print("\n--- 4e. IPI segue o mesmo criterio ---")
st, d = rodar("1556", "5102", "1653", {"1653": True},
              ipi_rules={("1556", "50"): False, ("1653", "50"): True})
razoes = [i["reason"] for i in d.get("ipi_issues", [])]
check("IPI creditado nao e glosado pelo CFOP do SPED",
      not any("nao permite credito" in sem_acento(r) for r in razoes), razoes)

st, d = rodar("1556", "5102", "1653", {"1653": True}, vl_ipi=0.0,
              ipi_rules={("1556", "50"): False, ("1653", "50"): True})
razoes = [i["reason"] for i in d.get("ipi_issues", [])]
check("IPI nao aproveitado sai com mensagem propria",
      any("nao aproveitado" in sem_acento(r) for r in razoes), razoes)

print("\n--- 4f. PIS/COFINS segue o mesmo criterio ---")
st, d = rodar("1556", "5102", "1653", {"1653": True},
              pis_rules={("1556", "50"): False, ("1653", "50"): True})
razoes = [i["reason"] for i in d.get("pis_cofins_issues", [])]
check("PIS/COFINS creditado nao e glosado pelo CFOP do SPED",
      not any("sem direito a credito" in sem_acento(r) for r in razoes), razoes)

st, d = rodar("1556", "5102", None, {"1653": True},
              pis_rules={("1556", "50"): False, ("1653", "50"): True})
razoes = [i["reason"] for i in d.get("pis_cofins_issues", [])]
check("sem mapeamento, PIS/COFINS volta a glosar pelo CFOP do SPED",
      any("sem direito a credito" in sem_acento(r) for r in razoes), razoes)


print("\n" + "=" * 60)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if FALHAS:
    print("FALHAS:")
    for m in _falhou:
        print("   ", m)
    print("FALHA")
    sys.exit(1)
print("OK")
