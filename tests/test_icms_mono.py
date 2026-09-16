"""ICMS Monofásico de combustíveis (pedido do usuário em 21/08/2026).

Regra definida pelo escritório: na tela inicial há a opção "Considerar ICMS
Mono (combustíveis)". Marcada, o ICMS monofásico do XML soma ao ICMS normal e
ao ICMS do Simples Nacional na apuração do crédito. O relatório ganha a aba
"ICMS Mono".

DECISÃO FISCAL (confirmada pelo usuário): as QUATRO tags do grupo monofásico
somam como crédito — próprio (`vICMSMono`), retido para a etapa seguinte
(`vICMSMonoReten`), cobrado anteriormente (`vICMSMonoRet`) e diferido
(`vICMSMonoDif`). A aba mostra as quatro em colunas separadas justamente para
que essa decisão possa ser revista sem precisar abrir o XML.

Armadilha coberta aqui: `vICMSMonoRet` e `vICMSMonoReten` são tags DIFERENTES
com nomes quase iguais. Confundi-las trocaria "cobrado anteriormente" por
"retido para frente" — valores distintos, sob CSTs distintos.
"""
import os, sys, uuid, json, tempfile, io
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_icms_mono_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

if os.path.exists("_icms_mono_test.db"):
    os.remove("_icms_mono_test.db")

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
from backend.app.domain.models.rules import CfopCreditRule
from backend.app.services.audit_engine.xml_parser import parse_nfe_xml
from backend.app.services.audit_engine.cross_checker import execute_audit, icms_creditavel_xml

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


NS = "http://www.portalfiscal.inf.br/nfe"
CHAVE = "7" * 44

# Uma nota com os quatro CSTs monofásicos + um item de ICMS normal.
XML_COMBUSTIVEL = f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="{NS}"><NFe><infNFe Id="NFe{CHAVE}">
 <ide><nNF>500</nNF><serie>1</serie></ide>
 <emit><CNPJ>99888777000166</CNPJ><xNome>DISTRIBUIDORA COMBUSTIVEIS SA</xNome>
   <enderEmit><UF>ES</UF></enderEmit></emit>
 <det nItem="1"><prod><cProd>GAS001</cProd><xProd>GASOLINA COMUM</xProd>
   <CFOP>1102</CFOP><NCM>27101259</NCM><vProd>5000.00</vProd><vUnCom>5.00</vUnCom></prod>
   <imposto><ICMS><ICMS61>
     <CST>61</CST><qBCMonoRet>1000.0000</qBCMonoRet>
     <adRemICMSRet>1.4210</adRemICMSRet><vICMSMonoRet>1421.00</vICMSMonoRet>
   </ICMS61></ICMS></imposto></det>
 <det nItem="2"><prod><cProd>DIE001</cProd><xProd>OLEO DIESEL S10</xProd>
   <CFOP>1102</CFOP><NCM>27101921</NCM><vProd>3000.00</vProd><vUnCom>6.00</vUnCom></prod>
   <imposto><ICMS><ICMS02>
     <CST>02</CST><qBCMono>500.0000</qBCMono>
     <adRemICMS>1.0630</adRemICMS><vICMSMono>531.50</vICMSMono>
   </ICMS02></ICMS></imposto></det>
 <det nItem="3"><prod><cProd>ETA001</cProd><xProd>ETANOL HIDRATADO</xProd>
   <CFOP>1102</CFOP><NCM>22072010</NCM><vProd>2000.00</vProd><vUnCom>4.00</vUnCom></prod>
   <imposto><ICMS><ICMS15>
     <CST>15</CST><qBCMono>300.0000</qBCMono><adRemICMS>1.2200</adRemICMS>
     <vICMSMono>366.00</vICMSMono>
     <qBCMonoReten>100.0000</qBCMonoReten><adRemICMSReten>1.2200</adRemICMSReten>
     <vICMSMonoReten>122.00</vICMSMonoReten>
   </ICMS15></ICMS></imposto></det>
 <det nItem="4"><prod><cProd>GLP001</cProd><xProd>GLP P13</xProd>
   <CFOP>1102</CFOP><NCM>27111910</NCM><vProd>1000.00</vProd><vUnCom>100.00</vUnCom></prod>
   <imposto><ICMS><ICMS53>
     <CST>53</CST><qBCMono>200.0000</qBCMono><adRemICMS>1.1000</adRemICMS>
     <vICMSMonoOp>220.00</vICMSMonoOp><pDif>100.00</pDif>
     <vICMSMonoDif>220.00</vICMSMonoDif><vICMSMono>0.00</vICMSMono>
   </ICMS53></ICMS></imposto></det>
 <det nItem="5"><prod><cProd>LUB001</cProd><xProd>LUBRIFICANTE 1L</xProd>
   <CFOP>1102</CFOP><NCM>27101932</NCM><vProd>500.00</vProd><vUnCom>50.00</vUnCom></prod>
   <imposto><ICMS><ICMS00>
     <CST>00</CST><vBC>500.00</vBC><pICMS>17.00</pICMS><vICMS>85.00</vICMS>
   </ICMS00></ICMS></imposto></det>
 <total><ICMSTot><vNF>11500.00</vNF><vST>0.00</vST></ICMSTot></total>
</infNFe></NFe></nfeProc>"""

MONO_ESPERADO = {"GASOLINA COMUM": 1421.00, "OLEO DIESEL S10": 531.50,
                 "ETANOL HIDRATADO": 488.00, "GLP P13": 220.00, "LUBRIFICANTE 1L": 0.0}
TOTAL_MONO = 2660.50
TOTAL_NORMAL = 85.00


# ============================================================================
print("\n=== 1. Parser: as quatro tags monofásicas, cada uma no seu campo ===")
fd, caminho = tempfile.mkstemp(suffix=".xml"); os.close(fd)
io.open(caminho, "w", encoding="utf-8").write(XML_COMBUSTIVEL)
dados = parse_nfe_xml(caminho)
os.remove(caminho)

check("XML de combustível foi lido sem erro", "error" not in dados, dados.get("error"))
itens = {i["descr"]: i for i in dados["items"]}

check("CST 61 -> vICMSMonoRet = 1421,00 (cobrado anteriormente)",
      itens["GASOLINA COMUM"]["vl_icms_mono_ret"] == 1421.00,
      f"={itens['GASOLINA COMUM']['vl_icms_mono_ret']}")
check("CST 61 NÃO contaminou 'retido para frente'",
      itens["GASOLINA COMUM"]["vl_icms_mono_reten"] == 0.0,
      "vICMSMonoRet foi lido como vICMSMonoReten")
check("CST 02 -> vICMSMono = 531,50 (próprio)",
      itens["OLEO DIESEL S10"]["vl_icms_mono_proprio"] == 531.50,
      f"={itens['OLEO DIESEL S10']['vl_icms_mono_proprio']}")
check("CST 15 -> próprio 366,00 E retido p/ frente 122,00 (as duas tags)",
      itens["ETANOL HIDRATADO"]["vl_icms_mono_proprio"] == 366.00
      and itens["ETANOL HIDRATADO"]["vl_icms_mono_reten"] == 122.00,
      f"={itens['ETANOL HIDRATADO']}")
check("CST 53 -> vICMSMonoDif = 220,00 (diferido)",
      itens["GLP P13"]["vl_icms_mono_dif"] == 220.00,
      f"={itens['GLP P13']['vl_icms_mono_dif']}")
check("CST 53: vICMSMonoOp NÃO é somado (evita contagem dupla)",
      itens["GLP P13"]["vl_icms_mono"] == 220.00,
      f"={itens['GLP P13']['vl_icms_mono']} (se fosse 440 houve dupla contagem)")

for descr, esperado in MONO_ESPERADO.items():
    check(f"total mono de '{descr}' = {esperado:.2f}",
          abs(itens[descr]["vl_icms_mono"] - esperado) < 0.01,
          f"={itens[descr]['vl_icms_mono']}")

check("item de ICMS normal fica com mono zerado",
      itens["LUBRIFICANTE 1L"]["vl_icms_mono"] == 0.0)
check("vl_icms (normal+SN) NÃO inclui o mono por conta própria",
      abs(sum(i["vl_icms"] for i in dados["items"]) - TOTAL_NORMAL) < 0.01,
      f"={sum(i['vl_icms'] for i in dados['items'])}")


# ============================================================================
print("\n=== 2. icms_creditavel_xml: a flag decide se o mono entra ===")
gasolina = itens["GASOLINA COMUM"]
check("sem a opção, a gasolina credita 0,00 (só mono na nota)",
      icms_creditavel_xml(gasolina, considerar_mono=False) == 0.0,
      f"={icms_creditavel_xml(gasolina, False)}")
check("com a opção, a gasolina credita 1421,00",
      icms_creditavel_xml(gasolina, considerar_mono=True) == 1421.00,
      f"={icms_creditavel_xml(gasolina, True)}")

lubrificante = itens["LUBRIFICANTE 1L"]
check("item sem mono credita igual nos dois modos (85,00)",
      icms_creditavel_xml(lubrificante, False) == icms_creditavel_xml(lubrificante, True) == 85.00)

etanol = itens["ETANOL HIDRATADO"]
check("mono soma as QUATRO parcelas: etanol credita 488,00 (366 + 122)",
      icms_creditavel_xml(etanol, True) == 488.00, f"={icms_creditavel_xml(etanol, True)}")


# ============================================================================
print("\n=== 3. Ponta a ponta: execute_audit com e sem a opção ===")

def montar_sped(itens_sped):
    """C100 + C170. `itens_sped` é lista de (cod, qtd, vl_item, vl_icms)."""
    fd, p = tempfile.mkstemp(suffix=".txt"); os.close(fd)
    vl_doc = round(sum(i[2] for i in itens_sped), 2)
    linhas = ["|0000|017|0|01072026|31072026|POSTO TESTE LTDA|11222333000181|ES|||",
              f"|C100|0|1|9999|55|00|1|500|{CHAVE}|18072026|18072026|"
              f"{str(vl_doc).replace('.', ',')}|0|0,00|0,00|{str(vl_doc).replace('.', ',')}|"
              f"0|0,00|0,00|0,00|0,00|0,00|0,00|0,00|0,00|"]
    for idx, (cod, qtd, vl_item, vl_icms) in enumerate(itens_sped, start=1):
        campos = ["C170", str(idx), cod, "Desc", str(qtd).replace(".", ","), "UN",
                  str(vl_item).replace(".", ","), "0,00", "0", "00", "1102", "",
                  str(vl_item).replace(".", ","), "17,00", str(vl_icms).replace(".", ","),
                  "0,00", "0,00", "0,00", "0", "", "", "0,00", "0,00", "0,00",
                  "", "0,00", "0,00", "0,00", "0,00", "0,00", "", "0,00", "0,00", "0,00", "0,00", "0,00"]
        linhas.append("|" + "|".join(campos) + "|")
    with io.open(p, "w", encoding="latin1") as f:
        f.write("\n".join(linhas) + "\n")
    return p


def rodar(considerar_mono, itens_sped):
    """Roda uma auditoria completa e devolve o result_data."""
    xml_dir = tempfile.mkdtemp(prefix="synax_mono_")
    io.open(os.path.join(xml_dir, f"{CHAVE}.xml"), "w", encoding="utf-8").write(XML_COMBUSTIVEL)
    sped = montar_sped(itens_sped)

    db = SessionLocal()
    tid, task_id = str(uuid.uuid4()), str(uuid.uuid4())
    repo = RulesRepository(db, tid)
    repo.audit_tasks.add(
        AuditTask(id=task_id, status="pendente", user_id="teste", file_name="sped.txt"))
    # Sem regra cadastrada para o CFOP o motor não compara nada: manda para a
    # curadoria e cala. A regra é deste tenant — não existe regra global.
    repo.credit_rules.add(CfopCreditRule(cfop="1102", tenant_id=tid, allow_credit=True,
                                         description="teste", status="aprovado", is_global=False))
    execute_audit(task_id, tid, sped, xml_dir, db, sped_only_mode=False,
                  ignore_saidas=True, considerar_icms_mono=considerar_mono)
    task = RulesRepository(db, tid).audit_tasks.get_by_id(task_id)
    status, dados = task.status, json.loads(task.result_data or "{}")
    db.close()

    import shutil
    shutil.rmtree(xml_dir, ignore_errors=True)
    if os.path.exists(sped):
        os.remove(sped)
    return status, dados


# O SPED escriturou o crédito COM o monofásico (é o que o posto faria).
SPED_COM_MONO = [("GAS001", 1000, 5000.0, 1421.00), ("DIE001", 500, 3000.0, 531.50),
                 ("ETA001", 500, 2000.0, 488.00), ("GLP001", 10, 1000.0, 220.00),
                 ("LUB001", 10, 500.0, 85.00)]

print("\n--- 3a. Opção MARCADA: o SPED que creditou mono fica sem divergência ---")
status, dados = rodar(True, SPED_COM_MONO)
check("auditoria concluiu", status == "concluido", f"status={status}")
check("marcada -> icms_mono_considerado = True", dados.get("icms_mono_considerado") is True)
check("nenhuma divergência de ICMS (o mono foi somado dos dois lados)",
      dados.get("icms_issues") == [], f"issues={dados.get('icms_issues')}")

print("\n--- 3b. Opção DESMARCADA: o mesmo SPED acusa o mono como crédito a mais ---")
status2, dados2 = rodar(False, SPED_COM_MONO)
check("auditoria concluiu", status2 == "concluido", f"status={status2}")
check("desmarcada -> icms_mono_considerado = False", dados2.get("icms_mono_considerado") is False)
check("aparecem divergências (o XML sem mono credita menos que o SPED)",
      len(dados2.get("icms_issues", [])) > 0,
      "sem a opção, o crédito do XML deveria ficar menor que o do SPED")


# ============================================================================
print("\n=== 4. A aba 'ICMS Mono' é alimentada nos dois modos ===")
for rotulo, d in (("marcada", dados), ("desmarcada", dados2)):
    linhas_mono = d.get("icms_mono", [])
    check(f"[{rotulo}] a aba lista os 4 itens com monofásico", len(linhas_mono) == 4,
          f"={len(linhas_mono)}")
    check(f"[{rotulo}] total do mono = {TOTAL_MONO:.2f}",
          abs(d.get("icms_mono_total", 0) - TOTAL_MONO) < 0.01,
          f"={d.get('icms_mono_total')}")
    check(f"[{rotulo}] item sem mono não entra na aba",
          all("LUBRIFICANTE" not in x["descr_item"] for x in linhas_mono))

linha_gas = next(x for x in dados["icms_mono"] if "GASOLINA" in x["descr_item"])
check("a aba traz as quatro parcelas separadas",
      all(k in linha_gas for k in ("mono_proprio", "mono_reten", "mono_ret", "mono_dif")),
      list(linha_gas))
check("gasolina na aba: cobrado antes 1421,00, demais zerados",
      linha_gas["mono_ret"] == 1421.00 and linha_gas["mono_proprio"] == 0.0
      and linha_gas["mono_reten"] == 0.0 and linha_gas["mono_dif"] == 0.0, linha_gas)
check("a aba registra se a auditoria somou ou não",
      dados["icms_mono"][0]["considerado"] is True
      and dados2["icms_mono"][0]["considerado"] is False)
check("a aba traz CST, NCM e a base por quantidade",
      linha_gas["cst"] == "61" and linha_gas["ncm"] == "27101259"
      and linha_gas["q_bc_mono"] == 1000.0, linha_gas)


# ============================================================================
print("\n=== 5. Fiação: botão na tela inicial e aba no relatório ===")
dash = io.open(os.path.join("frontend", "templates", "pages", "dashboard.html"),
               encoding="utf-8").read()
check("tela inicial tem o botão com o texto pedido",
      "Considerar ICMS Mono (combustíveis)" in dash)
check("o botão envia o campo considerar_icms_mono",
      'name="considerar_icms_mono"' in dash)

res_tpl = io.open(os.path.join("frontend", "templates", "partials", "curation",
                               "curation_results.html"), encoding="utf-8").read()
check("relatório tem a aba 'ICMS Mono'", "ICMS Mono" in res_tpl)
check("a aba tem painel próprio", 'id="panel-tab-icms-mono"' in res_tpl)
check("o painel mostra as quatro parcelas",
      all(c in res_tpl for c in ("mono_proprio", "mono_reten", "mono_ret", "mono_dif")))

up_src = io.open(os.path.join("backend", "app", "api", "routes", "uploads.py"),
                 encoding="utf-8").read()
check("a rota de upload recebe a opção", "considerar_icms_mono: bool = Form(False)" in up_src)
check("a rota repassa a opção ao motor", "considerar_icms_mono=considerar_icms_mono" in up_src)

xls_src = io.open(os.path.join("backend", "app", "services", "audit_engine",
                               "excel_exporter.py"), encoding="utf-8").read()
check("o Excel também exporta a aba", 'add_sheet_if_exists("icms_mono", "ICMS Mono")' in xls_src)

engine.dispose()
if os.path.exists("_icms_mono_test.db"):
    try:
        os.remove("_icms_mono_test.db")
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
