# -*- coding: utf-8 -*-
"""CFOPs que puxam no relatório de diferencial de alíquota (DIFAL).

O DIFAL de entrada é informativo (não é divergência fiscal). A lista de CFOPs
que caem na aba é UMA constante por classe — interno (1xxx) e interestadual
(2xxx) — porque o motor consulta o CFOP escriturado E o mapeado, e manter dois
literais na mão fazia um casar e o outro não.

Pedido do escritório em 08/10/2026: **incluir o CFOP 2552** (transferência de
bem do ativo imobilizado, interestadual) no relatório de DIFAL, junto de
2551/2556. Esta suíte prova, de ponta a ponta, que uma nota com 2552 aparece na
aba — e que um CFOP fora da lista não aparece.
"""
import os, sys, uuid, json, tempfile, io
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_difal_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

if os.path.exists("_difal_test.db"):
    os.remove("_difal_test.db")

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
    raise SystemExit("ABORTADO: engine nao e SQLite")

from backend.app.infrastructure.database.session import SessionLocal, engine
from backend.app.domain.models.base import Base
from backend.app.domain.models import rules, audit  # noqa: F401
from backend.app.domain.models.audit import AuditTask
from backend.app.infrastructure.repositories.rules_repository import RulesRepository
from backend.app.services.audit_engine.cross_checker import (
    execute_audit, CFOPS_DIFAL_INTERESTADUAL, CFOPS_DIFAL_INTERNO)

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
print("\n=== 1. A constante do DIFAL inclui o 2552 (e mantém os irmãos) ===")
check("2552 está na lista de DIFAL interestadual",
      "2552" in CFOPS_DIFAL_INTERESTADUAL, CFOPS_DIFAL_INTERESTADUAL)
check("2551 e 2556 continuam lá (não substituí, incluí)",
      "2551" in CFOPS_DIFAL_INTERESTADUAL and "2556" in CFOPS_DIFAL_INTERESTADUAL,
      CFOPS_DIFAL_INTERESTADUAL)
check("a lista interna não foi mexida",
      set(CFOPS_DIFAL_INTERNO) == {"1556", "1551", "1910"}, CFOPS_DIFAL_INTERNO)


# ============================================================================
# Ponta a ponta: uma nota com CFOP 2552 tem de aparecer na aba de DIFAL.
# ============================================================================
CHAVE = "7" * 44
EMPRESA = "11222333000181"
FORN = "99888777000166"


def xml_interestadual(cfop_item):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe{CHAVE}">
 <ide><nNF>700</nNF><serie>1</serie></ide>
 <emit><CNPJ>{FORN}</CNPJ><xNome>FORNECEDOR SP LTDA</xNome>
   <enderEmit><UF>SP</UF></enderEmit></emit>
 <dest><CNPJ>{EMPRESA}</CNPJ><xNome>EMPRESA TESTE LTDA</xNome>
   <enderDest><UF>ES</UF></enderDest></dest>
 <det nItem="1"><prod><cProd>ATIVO01</cProd><xProd>EMPILHADEIRA ELETRICA</xProd>
   <CFOP>{cfop_item}</CFOP><NCM>84271000</NCM><vProd>10000.00</vProd><vUnCom>10000.00</vUnCom>
   <qCom>1.0000</qCom></prod>
   <imposto><ICMS><ICMS00>
     <CST>00</CST><vBC>10000.00</vBC><pICMS>7.00</pICMS><vICMS>700.00</vICMS>
   </ICMS00></ICMS></imposto></det>
 <total><ICMSTot><vNF>10000.00</vNF><vST>0.00</vST></ICMSTot></total>
</infNFe></NFe></nfeProc>"""


def montar_sped(cfop_sped):
    """Um C100 + um C170 com o CFOP dado, ICMS do crédito zerado (ativo
    imobilizado não credita ICMS no lançamento)."""
    fd, p = tempfile.mkstemp(suffix=".txt"); os.close(fd)
    linhas = [
        f"|0000|017|0|01072026|31072026|EMPRESA TESTE LTDA|{EMPRESA}|ES|||",
        f"|C100|0|1|9999|55|00|1|700|{CHAVE}|18072026|18072026|"
        "10000,00|0|0,00|0,00|10000,00|0|0,00|0,00|0,00|0,00|0,00|0,00|0,00|0,00|",
    ]
    campos = ["C170", "1", "ATIVO01", "Desc", "1,00000", "UN",
              "10000,00", "0,00", "0", "00", cfop_sped, "",
              "0,00", "0,00", "0,00", "0,00", "0,00", "0,00", "0", "", "",
              "0,00", "0,00", "0,00", "", "0,00", "0,00", "0,00", "0,00",
              "0,00", "", "0,00", "0,00", "0,00", "0,00", "0,00"]
    linhas.append("|" + "|".join(campos) + "|")
    with io.open(p, "w", encoding="latin1") as f:
        f.write("\n".join(linhas) + "\n")
    return p


def rodar(cfop_sped, cfop_item_xml):
    xml_dir = tempfile.mkdtemp(prefix="synax_difal_")
    io.open(os.path.join(xml_dir, f"{CHAVE}.xml"), "w", encoding="utf-8").write(
        xml_interestadual(cfop_item_xml))
    sped = montar_sped(cfop_sped)

    db = SessionLocal()
    tid, task_id = str(uuid.uuid4()), str(uuid.uuid4())
    RulesRepository(db, tid).audit_tasks.add(
        AuditTask(id=task_id, status="pendente", user_id="teste", file_name="sped.txt"))
    execute_audit(task_id, tid, sped, xml_dir, db, sped_only_mode=False,
                  ignore_saidas=True)
    task = RulesRepository(db, tid).audit_tasks.get_by_id(task_id)
    status, dados = task.status, json.loads(task.result_data or "{}")
    db.close()

    import shutil
    shutil.rmtree(xml_dir, ignore_errors=True)
    if os.path.exists(sped):
        os.remove(sped)
    return status, dados


# ============================================================================
print("\n=== 2. Nota com CFOP 2552 aparece na aba de DIFAL interestadual ===")
status, dados = rodar("2552", "6551")
check("auditoria concluiu", status == "concluido", f"status={status}")

difal = dados.get("difal_interestadual", [])
check("a aba de DIFAL interestadual tem 1 item", len(difal) == 1, f"={len(difal)}")
if difal:
    item = difal[0]
    check("o item é o do CFOP 2552", item.get("cfop_sped") == "2552",
          item.get("cfop_sped"))
    check("trouxe a descrição do produto", item.get("descr") == "EMPILHADEIRA ELETRICA",
          item.get("descr"))
    check("trouxe a base e o ICMS do XML (R$ 10.000,00 / R$ 700,00)",
          abs(item.get("vl_bc_icms", 0) - 10000.0) < 0.01
          and abs(item.get("vl_icms", 0) - 700.0) < 0.01,
          (item.get("vl_bc_icms"), item.get("vl_icms")))
check("não foi parar na aba de DIFAL interno",
      len(dados.get("difal_interno", [])) == 0, dados.get("difal_interno"))


# ============================================================================
print("\n=== 3. CFOP fora da lista NÃO cai na aba de DIFAL ===")
status2, dados2 = rodar("2102", "6102")
check("auditoria concluiu", status2 == "concluido", f"status={status2}")
check("CFOP 2102 (revenda) não entra no DIFAL interestadual",
      len(dados2.get("difal_interestadual", [])) == 0,
      dados2.get("difal_interestadual"))


# ============================================================================
print("\n" + "=" * 60)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("FALHARAM:", ", ".join(_falhou))
print("=" * 60)
sys.exit(1 if FALHAS else 0)
