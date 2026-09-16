# -*- coding: utf-8 -*-
"""Crédito de PIS/COFINS sobre o frete (CT-e).

Pedido do Sandro (26/08/2026): o motor só lia PIS/COFINS dos produtos (C170) e
o frete ficava de fora — crédito legítimo que ninguém conferia.

A base é CALCULADA, não copiada: valor contábil do CT-e menos o ICMS destacado.
Não há de onde copiar — o CT-e modelo 57 não tem grupo de PIS/COFINS no XML, e
o D100/D190 do SPED só traz ICMS. O que o cliente escriturou vem do D101 (PIS)
e do D105 (COFINS).

O CONFRONTO É DE VALOR, não de base: o SPED Fiscal não escritura base de
PIS/COFINS, só o valor do crédito. A base calculada vira valor pelas alíquotas
(1,65% e 7,6%) e é isso que se compara com o que o cliente lançou.

Armadilha coberta aqui: "D101" existe nos DOIS arquivos e são registros
DIFERENTES. No SPED Fiscal é o complemento de DIFAL da EC 87/15, e lê-lo como
PIS transformaria o ICMS do remetente em "CST".

A trava que este teste protege: SPED sem NENHUM D101/D105 não escriturou nada —
o layout não tem o dado. Tratar isso como "não aproveitado" acusaria todo CT-e
do mês de uma vez, por causa do arquivo e não de erro do cliente. A aba do
aproveitamento sai do mesmo jeito, com o crédito calculado; a alegação, não.
"""
import os, sys, uuid, json, tempfile, io, shutil, unicodedata
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_pis_cofins_cte_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="

if os.path.exists("_pis_cofins_cte_test.db"):
    os.remove("_pis_cofins_cte_test.db")

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
from backend.app.domain.models.rules import CfopCreditRule, PisCofinsCreditRule
from backend.app.services.audit_engine.sped_parser import parse_sped
from backend.app.services.audit_engine.cross_checker import (
    execute_audit, base_pis_cofins_frete, pis_cofins_da_base,
    ALIQ_PIS, ALIQ_COFINS,
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


CHAVE = "7" * 44
EMPRESA = "11222333000181"
TRANSPORTADORA = "55444333000122"
v = lambda x: f"{x:.2f}".replace(".", ",")  # noqa: E731


# ============================================================================
print("\n=== 1. A conta, isolada ===")
check("contabil menos ICMS", base_pis_cofins_frete(1000.0, 120.0) == 880.0)
check("sem ICMS, a base e o contabil inteiro", base_pis_cofins_frete(1000.0, 0.0) == 1000.0)
check("nunca negativa", base_pis_cofins_frete(100.0, 250.0) == 0.0)
check("aceita texto vindo do parser", base_pis_cofins_frete("1000.00", "120.00") == 880.0)

check("aliquotas nao-cumulativas", (ALIQ_PIS, ALIQ_COFINS) == (1.65, 7.6))
check("PIS e 1,65% da base", pis_cofins_da_base(880.0)[0] == 14.52, pis_cofins_da_base(880.0))
check("COFINS e 7,6% da base", pis_cofins_da_base(880.0)[1] == 66.88, pis_cofins_da_base(880.0))
check("base zerada nao gera credito", pis_cofins_da_base(0.0) == (0.0, 0.0))


# ============================================================================
print("\n=== 2. Parser: D101 (PIS) e D105 (COFINS) ===")


def montar_sped(linhas_d, cabecalho=True):
    fd, p = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    linhas = [f"|0000|017|0|01072026|31072026|EMPRESA TESTE LTDA|{EMPRESA}|ES|||",
              f"|0150|9001|TRANSPORTADORA TESTE LTDA|1058|{TRANSPORTADORA}||||||"]
    linhas += linhas_d
    io.open(p, "w", encoding="latin1").write("\n".join(linhas) + "\n")
    return p


# Layout D100: |REG|IND_OPER|IND_EMIT|COD_PART|COD_MOD|COD_SIT|SER|SUB|NUM_DOC|CHV|
#              DT_DOC|DT_A_P|TP_CT|CHV_REF|VL_DOC|VL_DESC|IND_FRT|VL_SERV|VL_BC|VL_ICMS|...
# Layout D190: |REG|CST_ICMS|CFOP|ALIQ|VL_OPR|VL_BC_ICMS|VL_ICMS|VL_RED_BC|COD_OBS|
# Layout D101: |REG|IND_NAT_FRT|VL_ITEM|CST_PIS|NAT_BC_CRED|VL_BC_PIS|ALIQ_PIS|VL_PIS|COD_CTA|
# Layout D105: |REG|IND_NAT_FRT|VL_ITEM|CST_COFINS|NAT_BC_CRED|VL_BC_COFINS|ALIQ_COFINS|VL_COFINS|COD_CTA|
def linha_d100(chave=CHAVE, contabil=1000.0, icms=120.0):
    return (f"|D100|0|1|9001|57|00|000||500|{chave}|18072026|18072026|0||"
            f"{v(contabil)}|0|1|{v(contabil)}|{v(contabil)}|{v(icms)}|||9999|")


def linha_d190(cfop="1353", contabil=1000.0, icms=120.0):
    return f"|D190|000|{cfop}|12,00|{v(contabil)}|{v(contabil)}|{v(icms)}|0,00||"


def linha_d101(cst="50", base=880.0, valor=14.52):
    return f"|D101|0|1000,00|{cst}|09|{v(base)}|1,6500|{v(valor)}||"


def linha_d105(cst="50", base=880.0, valor=66.88):
    return f"|D105|0|1000,00|{cst}|09|{v(base)}|7,6000|{v(valor)}||"


sped = montar_sped([linha_d100(), linha_d190(), linha_d101(), linha_d105()])
d = parse_sped(sped)
cte = d["d100"][CHAVE]
check("o arquivo escritura PIS/COFINS de frete", d["bloco_d_tem_pis_cofins"] is True)
check("CST do PIS veio do D101", cte["cst_pis"] == "50", cte["cst_pis"])
check("base do PIS veio do D101", cte["vl_bc_pis"] == 880.0, cte["vl_bc_pis"])
check("valor do PIS veio do D101", cte["vl_pis"] == 14.52, cte["vl_pis"])
check("CST da COFINS veio do D105", cte["cst_cofins"] == "50", cte["cst_cofins"])
check("base da COFINS veio do D105", cte["vl_bc_cofins"] == 880.0, cte["vl_bc_cofins"])
check("valor da COFINS veio do D105", cte["vl_cofins"] == 66.88, cte["vl_cofins"])
check("o CT-e sabe que foi escriturado", cte["tem_pis_cofins"] is True)
check("valor contabil sai do VL_OPR do D190", cte["vl_contabil"] == 1000.0, cte["vl_contabil"])
os.remove(sped)

# Mais de um D101 no mesmo documento: parcelas do mesmo total, somam (mesmo
# motivo do D190 somar em vez de sobrescrever).
sped = montar_sped([linha_d100(), linha_d190(),
                    linha_d101(base=500.0, valor=8.25),
                    linha_d101(base=380.0, valor=6.27)])
cte = parse_sped(sped)["d100"][CHAVE]
check("dois D101 somam a base", cte["vl_bc_pis"] == 880.0, cte["vl_bc_pis"])
check("dois D101 somam o valor", cte["vl_pis"] == 14.52, cte["vl_pis"])
os.remove(sped)

# Sem D101/D105 — o caso do SPED Fiscal (EFD ICMS/IPI), que não tem esses
# registros. Ausência do dado, não erro do cliente.
sped = montar_sped([linha_d100(), linha_d190()])
d = parse_sped(sped)
cte = d["d100"][CHAVE]
check("sem D101/D105 o arquivo nao escritura", d["bloco_d_tem_pis_cofins"] is False)
check("sem D101/D105 o CT-e fica marcado como nao escriturado",
      cte["tem_pis_cofins"] is False)
check("sem D101/D105 as bases ficam zeradas",
      cte["vl_bc_pis"] == 0.0 and cte["vl_bc_cofins"] == 0.0)
os.remove(sped)

# O D101 do SPED FISCAL e outro registro: complemento de DIFAL da EC 87/15,
# layout |REG|VL_FCP_UF_DEST|VL_ICMS_UF_DEST|VL_ICMS_UF_REM|COD_CTA|. Lido como
# se fosse PIS, o ICMS do remetente viraria "CST" e o documento sairia marcado
# como escriturado — o motor cobraria crédito de um arquivo que não tem o dado.
sped = montar_sped([linha_d100(), linha_d190(),
                    "|D101|0,00|12,00|18,00|9999|"])
d = parse_sped(sped)
cte = d["d100"][CHAVE]
check("D101 de DIFAL nao e confundido com PIS", d["bloco_d_tem_pis_cofins"] is False)
check("D101 de DIFAL nao marca o CT-e como escriturado", cte["tem_pis_cofins"] is False)
check("D101 de DIFAL nao vira CST", cte["cst_pis"] == "", cte["cst_pis"])
check("D101 de DIFAL nao vira valor de PIS", cte["vl_pis"] == 0.0, cte["vl_pis"])
os.remove(sped)

# Sem D190: o contabil cai para o VL_DOC do cabecalho.
sped = montar_sped([linha_d100(contabil=777.0, icms=90.0)])
cte = parse_sped(sped)["d100"][CHAVE]
check("sem D190 o contabil vem do cabecalho", cte["vl_contabil"] == 777.0, cte["vl_contabil"])
os.remove(sped)


# ============================================================================
print("\n=== 3. Ponta a ponta ===")

NS_CTE = "http://www.portalfiscal.inf.br/cte"


def montar_xml_cte(cfop="1353", v_prest=1000.0, v_icms=120.0):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<cteProc xmlns="{NS_CTE}"><CTe><infCte Id="CTe{CHAVE}">
 <ide><nCT>500</nCT><serie>0</serie><CFOP>{cfop}</CFOP>
   <toma3><toma>3</toma></toma3></ide>
 <emit><CNPJ>{TRANSPORTADORA}</CNPJ><xNome>TRANSPORTADORA TESTE LTDA</xNome>
   <enderEmit><UF>ES</UF></enderEmit></emit>
 <rem><CNPJ>99888777000100</CNPJ><xNome>REMETENTE LTDA</xNome></rem>
 <dest><CNPJ>{EMPRESA}</CNPJ><xNome>EMPRESA TESTE LTDA</xNome></dest>
 <vPrest><vTPrest>{v_prest:.2f}</vTPrest></vPrest>
 <imp><ICMS><ICMS00><CST>00</CST><vBC>{v_prest:.2f}</vBC><pICMS>12.00</pICMS>
   <vICMS>{v_icms:.2f}</vICMS></ICMS00></ICMS></imp>
</infCte></CTe></cteProc>"""


def rodar(linhas_extra, pis_rules=None, icms_rules=None, contabil=1000.0, icms=120.0):
    """Uma auditoria completa com um CT-e de entrada."""
    xml_dir = tempfile.mkdtemp(prefix="synax_cte_piscofins_")
    io.open(os.path.join(xml_dir, f"{CHAVE}.xml"), "w", encoding="utf-8").write(
        montar_xml_cte(v_prest=contabil, v_icms=icms))
    sped = montar_sped([linha_d100(contabil=contabil, icms=icms),
                        linha_d190(contabil=contabil, icms=icms)] + linhas_extra)

    db = SessionLocal()
    tid, task_id = str(uuid.uuid4()), str(uuid.uuid4())
    repo = RulesRepository(db, tid)
    repo.audit_tasks.add(
        AuditTask(id=task_id, status="pendente", user_id="teste", file_name="sped.txt"))

    for cfop, permite in (icms_rules or {}).items():
        repo.credit_rules.add(CfopCreditRule(
            cfop=cfop, tenant_id=tid, allow_credit=permite,
            description="teste", status="aprovado", is_global=False))

    for (cfop, cst), permite in (pis_rules or {}).items():
        repo.pis_cofins_credit_rules.add(PisCofinsCreditRule(
            cfop=cfop, cst_expected=cst, tenant_id=tid, allow_credit=permite,
            status="aprovado", is_global=False))

    execute_audit(task_id, tid, sped, xml_dir, db, sped_only_mode=False,
                  ignore_saidas=True, considerar_icms_mono=False)
    task = RulesRepository(db, tid).audit_tasks.get_by_id(task_id)
    status, dados = task.status, json.loads(task.result_data or "{}")
    db.close()

    shutil.rmtree(xml_dir, ignore_errors=True)
    if os.path.exists(sped):
        os.remove(sped)
    return status, dados


def frete_do_pis_cofins(dados):
    return [i for i in dados.get("pis_cofins_issues", [])
            if i.get("tipo") == "PIS/COFINS Frete"]


print("\n--- 3a. Escriturado certo: base do SPED = contabil - ICMS ---")
st, d = rodar([linha_d101(), linha_d105()],
              pis_rules={("1353", "50"): True}, icms_rules={"1353": True})
check("auditoria concluiu", st == "concluido", f"status={st}")
check("a aba do aproveitamento tem o CT-e", len(d.get("pis_cofins_frete", [])) == 1,
      d.get("pis_cofins_frete"))
linha = (d.get("pis_cofins_frete") or [{}])[0]
check("a base creditavel e contabil menos ICMS", linha.get("base_creditavel") == 880.0, linha)
check("a aba traz o PIS calculado", linha.get("pis_calculado") == 14.52, linha)
check("a aba traz a COFINS calculada", linha.get("cofins_calculado") == 66.88, linha)
check("a aba traz o credito total calculado", linha.get("credito_calculado") == 81.40, linha)
check("a aba traz o que o SPED creditou", linha.get("credito_sped") == 81.40, linha)
check("mostra o contabil que originou a conta", linha.get("vl_contabil") == 1000.0, linha)
check("mostra o ICMS deduzido", linha.get("vl_icms") == 120.0, linha)
check("marca que houve escrituracao", linha.get("escriturado") is True, linha)
check("o arquivo escritura PIS/COFINS de frete",
      d.get("sped_escritura_pis_cofins_frete") is True)
check("nada a apontar quando bate", frete_do_pis_cofins(d) == [], frete_do_pis_cofins(d))

print("\n--- 3b. Nao aproveitado: SPED sem base, CT-e com credito ---")
st, d = rodar([linha_d101(base=0.0, valor=0.0), linha_d105(base=0.0, valor=0.0)],
              pis_rules={("1353", "50"): True}, icms_rules={"1353": True})
frete = frete_do_pis_cofins(d)
check("acusa uma vez", len(frete) == 1, frete)
check("a mensagem fala em nao aproveitado",
      frete and "nao aproveitado" in sem_acento(frete[0]["reason"]), frete)
check("a mensagem mostra a memoria da conta",
      frete and "1000.00" in frete[0]["reason"] and "120.00" in frete[0]["reason"], frete)
check("a mensagem mostra o PIS e a COFINS devidos",
      frete and "14.52" in frete[0]["reason"] and "66.88" in frete[0]["reason"], frete)
check("a linha sai identificada como frete",
      frete and frete[0]["num_item"] == "CT-e", frete)
check("o valor esperado e o credito calculado, nao a base",
      frete and frete[0]["xml_val"] == 81.40, frete)
check("a aba marca a situacao",
      (d.get("pis_cofins_frete") or [{}])[0].get("situacao") == "Não aproveitado",
      d.get("pis_cofins_frete"))

print("\n--- 3c. Valor divergente: creditou sobre o contabil cheio ---")
# 1,65% e 7,6% sobre 1000 (sem excluir o ICMS) em vez de sobre 880.
st, d = rodar([linha_d101(base=1000.0, valor=16.5), linha_d105(base=1000.0, valor=76.0)],
              pis_rules={("1353", "50"): True}, icms_rules={"1353": True})
frete = frete_do_pis_cofins(d)
check("acusa o valor divergente", len(frete) == 1, frete)
check("a mensagem compara o creditado com o calculado",
      frete and "16.50" in frete[0]["reason"] and "14.52" in frete[0]["reason"], frete)
check("nao chama de nao aproveitado",
      frete and "nao aproveitado" not in sem_acento(frete[0]["reason"]), frete)
check("o SPED reportado e a soma do que foi creditado",
      frete and abs(frete[0]["sped_val"] - 92.50) < 0.01, frete)
check("a aba marca valor divergente",
      (d.get("pis_cofins_frete") or [{}])[0].get("situacao") == "Valor divergente",
      d.get("pis_cofins_frete"))

print("\n--- 3d. SPED sem D101/D105: calcula e MOSTRA, mas nao acusa ---")
st, d = rodar([], pis_rules={("1353", "50"): True}, icms_rules={"1353": True})
check("nao acusa nada de frete", frete_do_pis_cofins(d) == [], frete_do_pis_cofins(d))
check("o arquivo e marcado como sem escrituracao",
      d.get("sped_escritura_pis_cofins_frete") is False)
linha = (d.get("pis_cofins_frete") or [{}])[0]
check("mesmo assim a aba mostra o credito calculado",
      linha.get("base_creditavel") == 880.0, linha)
check("a linha diz que nao houve escrituracao", linha.get("escriturado") is False, linha)
check("a situacao fica informativa", linha.get("situacao") == "Informativo", linha)

print("\n--- 3e. CST sem direito a credito, mas o SPED creditou ---")
st, d = rodar([linha_d101(cst="70", base=880.0, valor=14.52),
               linha_d105(cst="70", base=880.0, valor=66.88)],
              icms_rules={"1353": True})
frete = frete_do_pis_cofins(d)
check("acusa credito indevido", len(frete) == 1, frete)
check("a mensagem fala em sem direito a credito",
      frete and "sem direito a credito" in sem_acento(frete[0]["reason"]), frete)
check("o valor acusado e o que foi creditado",
      frete and abs(frete[0]["sped_val"] - 81.40) < 0.01, frete)

print("\n--- 3f. Sem regra para (CFOP, CST): vira pendencia, com o CST real ---")
st, d = rodar([linha_d101(), linha_d105()], icms_rules={"1353": True})
pend = d.get("unmapped_pis_cofins_rules", [])
check("a pendencia foi criada", any(p["cfop"] == "1353" and p["cst_expected"] == "50"
                                    for p in pend), pend)
check("a pendencia identifica o frete",
      any("frete" in sem_acento(p.get("descr", "")) for p in pend), pend)

print("\n--- 3g. O aproveitamento nao infla a contagem de inconsistencias ---")
st, d = rodar([], pis_rules={("1353", "50"): True}, icms_rules={"1353": True})
# A unica pendencia deste cenario e o CFOP do CT-e sem regra de classificacao,
# que ja existia antes desta feature. A aba do aproveitamento nao acrescenta.
check("aba informativa nao entra no resumo",
      d["resumo"]["inconsistencias"] == len(d.get("unmapped_cte_items", [])),
      f"inconsistencias={d['resumo']['inconsistencias']} "
      f"cte_pendentes={len(d.get('unmapped_cte_items', []))}")
check("a aba tem linha e mesmo assim nao contou",
      len(d.get("pis_cofins_frete", [])) == 1 and d.get("pis_cofins_issues") == [],
      d.get("pis_cofins_frete"))

FONTE = io.open("backend/app/services/audit_engine/cross_checker.py", encoding="utf-8").read()
_erros = FONTE[FONTE.find("error_lists = ["):]
_erros = _erros[:_erros.find("]")]
check("pis_cofins_frete fica fora da soma de inconsistencias",
      "pis_cofins_frete" not in _erros)


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

try:
    engine.dispose()
    if os.path.exists("_pis_cofins_cte_test.db"):
        os.remove("_pis_cofins_cte_test.db")
except Exception:
    pass

sys.exit(1 if FALHAS else 0)
