"""Gerador de e-mail de apontamentos: catálogo completo de divergências.

Pedido do usuário (20/08/2026): a aba de e-mail tinha só 5 opções, enquanto o
motor de auditoria produz 20 tipos de apontamento. Faltava poder comunicar ao
cliente notas não lançadas, duplicadas, DIFAL, ST, antecipação, CFOP e CT-e —
e cada categoria precisa de um texto padrão próprio.

Dois bugs reais foram encontrados e corrigidos no caminho:

1. ERRO 500 na tela: `unmapped_icms_cfops` é uma lista de STRINGS no motor
   (`results["unmapped_icms_cfops"].append(sped_cfop)`), mas o montador antigo
   chamava `.get()` em cada item. Qualquer auditoria com CFOP de ICMS não
   mapeado — o caso mais comum — derrubava a geração do e-mail.

2. Seção de CT-e SEMPRE vazia: o montador lia `result_data["cte_issues"]`,
   chave que o motor nunca produz. As chaves reais são `cte_tomador_issues`,
   `cte_cfop_classifications_issues` e `unmapped_cte_items`.

Além disso, o corpo HTML é montado em f-string (fora do autoescape do Jinja) e
embute descrição de produto vinda do XML do fornecedor — o vetor de XSS
armazenado que core/templating.py descreve. Agora passa por html.escape.
"""
import os, sys, re, uuid, types, json, io
sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_email_catalogo_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
os.environ["SIEG_API_KEY"] = "teste"
os.environ["SIEG_EMAIL"] = "teste@x.com"
os.environ.pop("ANTHROPIC_API_KEY", None)

if os.path.exists("_email_catalogo_test.db"):
    os.remove("_email_catalogo_test.db")

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
TOKENS = {}

class _Auth:
    def get_user(self, token):
        u = TOKENS.get(token)
        if not u:
            raise Exception("invalid token")
        return types.SimpleNamespace(user=u)

class _Client:
    auth = _Auth()

sb.get_supabase_client = lambda: _Client()
sb.get_supabase_admin_client = lambda: _Client()
import backend.app.api.dependencies.auth as dep_auth
dep_auth.get_supabase_client = lambda: _Client()

from backend.app.infrastructure.database.session import engine as _eng
if _eng.url.get_backend_name() != "sqlite":
    raise SystemExit(f"ABORTADO: engine aponta para {_eng.url.get_backend_name()}, não SQLite.")

import backend.app.main as m
from backend.app.infrastructure.database.session import SessionLocal, engine
from backend.app.domain.models.base import Base
from backend.app.domain.models.auth_models import Tenant, UserProfile, UserTenant
from backend.app.domain.models.audit import AuditTask
from backend.app.services.audit_engine import email_catalog as ec
from backend.app.api.routes.uploads import (
    _montar_secoes_email, _texto_da_linha, _html_da_linha, _build_audit_email_contents,
)
from fastapi.testclient import TestClient

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


CHAVE_A = "3" * 44
CHAVE_B = "4" * 44

# Payload cobrindo TODAS as categorias do catálogo.
PAYLOAD = {
    "resumo": {"notas_processadas": 40, "inconsistencias": 13},
    "xmls_faltantes": [{"chave": CHAVE_A, "fornecedor": "0 - ACME DISTRIBUIDORA LTDA",
                        "ind_oper": "0", "tipo_operacao": "Entrada"}],
    "unrecorded_invoices": [{"key": CHAVE_B, "tipo": "NFE", "emit_name": "OMEGA COMERCIO SA",
                             "vl_total": "1530.75"}],
    "notas_duplicadas": [{"chave": CHAVE_A, "num": "1001", "ind_oper": "0", "vl_doc": 890.10,
                          "tipo": "NF-e", "tipo_operacao": "Entrada"}],
    "value_discrepancies": [{"type": "NF-e", "key": CHAVE_A, "num": "1002",
                             "emit_name": "ACME DISTRIBUIDORA", "sped_val": 1000.0,
                             "xml_val": 950.0, "diff": 50.0,
                             "reason": "Total da NF-e divergente."}],
    "cnpj_emitente_issues": [{"type": "NF-e", "key": CHAVE_A, "num": "1002",
                              "cod_part": "F2", "sped_cnpj": "99888777000166",
                              "sped_name": "OUTRA EMPRESA", "xml_cnpj": "86527934000100",
                              "emit_name": "ACME DISTRIBUIDORA",
                              "tipo_divergencia": "divergente",
                              "reason": "Emitente divergente: o SPED lançou a nota "
                                        "no participante F2."}],
    "icms_issues": [{"key": CHAVE_A, "num": "1003", "emit_name": "ACME DISTRIBUIDORA",
                     "num_item": "1", "descr": "PARAFUSO SEXTAVADO", "cfop": "1556",
                     "reason": "CFOP 1556 não permite crédito de ICMS.",
                     "sped_icms": 42.0, "xml_icms": 0.0}],
    # STRINGS puras — o formato que derrubava o gerador antigo.
    "unmapped_icms_cfops": ["1556", "2949"],
    "ipi_issues": [{"key": CHAVE_A, "num": "1004", "emit_name": "INDUSTRIA BETA",
                    "num_item": "2", "descr": "CHAPA ACO", "cfop": "1101", "cst_ipi": "00",
                    "reason": "Divergência de IPI.", "sped_val": 10.0, "xml_val": 12.0}],
    "unmapped_ipi_rules": [{"cfop": "1949", "cst_ipi": "*", "descr": "MATERIAL DIVERSO"}],
    "pis_cofins_issues": [{"key": CHAVE_A, "num": "1005", "emit_name": "GAMA ATACADO",
                           "num_item": "1", "descr": "CAIXA PAPELAO", "tipo": "PIS/COFINS",
                           "cfop": "1102", "cst": "50", "reason": "Base divergente.",
                           "sped_val": 33.0, "xml_val": 30.0}],
    "unmapped_pis_cofins_rules": [{"cfop": "1556", "cst_expected": "70", "descr": "USO E CONSUMO"}],
    "icms_st_notes": [{"key": CHAVE_A, "num": "1006", "emit_name": "DELTA BEBIDAS",
                       "items": [{"num_item": 1, "descr": "REFRI 2L", "vl_item": 100.0,
                                  "vl_bc_icms_st": 130.0, "vl_icms_st": 12.5},
                                 {"num_item": 2, "descr": "CERVEJA", "vl_item": 200.0,
                                  "vl_bc_icms_st": 260.0, "vl_icms_st": 25.0}]}],
    "icms_st_calculation": [{"key": CHAVE_A, "num_nf": "1007", "fornecedor": "DELTA BEBIDAS",
                             "descricao_item": "REFRI 2L", "cest": "0300700",
                             "mva_str": "56.00%", "icms_st": 18.42}],
    "antecipacao_icms": [{"num_nf": "1008", "cnpj_emitente": "11222333000181",
                          "descr": "AGUA MINERAL", "ncm": "22011000", "cest": "0300100",
                          "categoria_ncm": "Bebidas", "vl_item": 250.0,
                          "vl_bc_icms": 250.0, "p_icms": 12.0, "vl_icms": 30.0}],
    "anticipation_results": [],
    # Shapes fiéis ao motor (cross_checker.py): a tela de resultados renderiza
    # estas tabelas campo a campo, então faltar uma chave aqui quebraria o
    # template — e o teste deixaria de valer como espelho da realidade.
    "difal_interno": [{"emit_cnpj": "11222333000181", "num_nf": "1009", "descr": "CADEIRA",
                       "ncm": "94013000", "cest": "", "total_item_difal": 800.0,
                       "vl_bc": 800.0, "p_icms": 12.0, "vl_icms": 96.0, "c_benef": "",
                       "cfop_sped": "1556", "eff_pct": 12.0, "status": "Informativo"}],
    "difal_interestadual": [{"emit_cnpj": "99888777000166", "emit_uf": "SP", "num_nf": "1010",
                             "descr": "NOTEBOOK", "ncm": "84713012", "cest": "",
                             "vl_item": 3500.0, "vl_bc_icms": 3500.0, "p_icms": 7.0,
                             "vl_icms": 245.0, "p_icms_inter": 7.0,
                             "vl_icms_uf_dest": 175.0, "vl_icms_uf_remet": 0.0,
                             "cfop_sped": "2556", "status": "Informativo"}],
    "cfop_classifications_issues": [{"key": CHAVE_A, "num": "1011",
                                     "emit_name": "ACME DISTRIBUIDORA", "num_item": "1",
                                     "product_code": "P100",
                                     "product_description": "PARAFUSO SEXTAVADO",
                                     "supplier_cfop": "5102", "sped_cfop": "1556",
                                     "expected_cfop": "1102"}],
    "cfops_desconhecidos": [{"company_cnpj": "11222333000181", "cfop": "5405",
                             "descricao": "PRODUTO NOVO", "cfop_sugerido": "1403",
                             "cnpj_emitente": "77666555000144", "product_code": "P999"}],
    "cte_tomador_issues": [{"key": CHAVE_B, "num": "5001",
                            "emit_name": "TRANSPORTADORA RAPIDA",
                            "tomador_role": "REMETENTE"}],
    "cte_cfop_classifications_issues": [{"key": CHAVE_B, "num": "5002",
                                         "emit_name": "TRANSPORTES SUL", "emit_uf": "PR",
                                         "supplier_cfop": "6352", "sped_cfop": "2353",
                                         "expected_cfop": "1353"}],
    "unmapped_cte_items": [{"supplier_cfop": "6932", "emitter_uf": "MG",
                            "current_sped_cfop": "2932"}],
}

TODAS = [c["chave"] for c in ec.CATEGORIAS]


# ============================================================================
print("\n=== 1. Catálogo cobre o que o motor produz ===")
check("catálogo tem 20 categorias (antes a tela tinha 5)", len(ec.CATEGORIAS) == 20,
      f"={len(ec.CATEGORIAS)}")

src_motor = io.open(os.path.join("backend", "app", "services", "audit_engine",
                                 "cross_checker.py"), encoding="utf-8").read()
chaves_motor = set(re.findall(r'results\["([a-z_]+)"\]', src_motor))
chaves_motor.add("notas_duplicadas")   # vem de sped_data no dict inicial

fontes = set()
for c in ec.CATEGORIAS:
    fontes.update(c["fontes"])
orfas = sorted(fontes - chaves_motor)
check("nenhuma fonte do catálogo é órfã (categoria que nunca teria conteúdo)",
      orfas == [], f"órfãs={orfas}")

check("toda categoria tem texto padrão (intro) próprio",
      all(c.get("intro", "").strip() for c in ec.CATEGORIAS))
check("toda categoria tem descrição para a tela",
      all(c.get("descricao", "").strip() for c in ec.CATEGORIAS))
check("toda categoria tem formatador", all(callable(c["formatador"]) for c in ec.CATEGORIAS))
check("chaves do catálogo são únicas", len({c["chave"] for c in ec.CATEGORIAS}) == len(ec.CATEGORIAS))
check("toda categoria pertence a um grupo conhecido",
      all(c["grupo"] in ec.GRUPOS for c in ec.CATEGORIAS))
check("toda cor existe na paleta literal", all(c["cor"] in ec.PALETA for c in ec.CATEGORIAS))


# ============================================================================
print("\n=== 2. Bug corrigido: unmapped_icms_cfops é lista de STRINGS ===")
# O montador antigo fazia item.get(...) num str -> AttributeError -> 500 na tela.
try:
    secoes = _montar_secoes_email({"unmapped_icms_cfops": ["1556", "2949"]}, ["icms_sem_regra"])
    quebrou = False
except AttributeError as err:
    quebrou, secoes = True, []
check("lista de strings não quebra mais o gerador", not quebrou,
      "AttributeError: 'str' object has no attribute 'get'")
check("gerou a seção com os 2 CFOPs", secoes and len(secoes[0]["linhas"]) == 2,
      f"secoes={secoes}")
if secoes:
    check("o código do CFOP aparece no texto", "1556" in secoes[0]["linhas"][0]["detalhe"],
          secoes[0]["linhas"][0]["detalhe"])


# ============================================================================
print("\n=== 3. Bug corrigido: seção de CT-e não era mais vazia ===")
check("o motor realmente NÃO produz a chave 'cte_issues' que o código antigo lia",
      "cte_issues" not in chaves_motor)

secoes_cte = _montar_secoes_email(PAYLOAD, ["cte_tomador", "cte_cfop", "cte_nao_mapeado"])
check("as 3 categorias de CT-e produzem seção", len(secoes_cte) == 3, f"={len(secoes_cte)}")
check("linhas de CT-e usam o rótulo 'Nº CT-e' (e não 'Nº NF')",
      _texto_da_linha(secoes_cte[0]["linhas"][0], "cte").startswith("Nº CT-e"),
      _texto_da_linha(secoes_cte[0]["linhas"][0], "cte"))


# ============================================================================
print("\n=== 4. Todas as 20 categorias geram conteúdo ===")
secoes = _montar_secoes_email(PAYLOAD, TODAS)
check("20 seções geradas a partir do payload completo", len(secoes) == 20, f"={len(secoes)}")

sem_secao = [c["chave"] for c in ec.CATEGORIAS
             if c["chave"] not in {s["categoria"]["chave"] for s in secoes}]
check("nenhuma categoria ficou sem seção", sem_secao == [], f"faltaram={sem_secao}")
check("toda seção tem ao menos uma linha", all(s["linhas"] for s in secoes))
check("nenhuma linha ficou com detalhe vazio",
      all(l["detalhe"].strip() for s in secoes for l in s["linhas"]))

# DIFAL interno usa vl_bc; o interestadual usa vl_bc_icms — os dois têm de sair com base.
difal = next(s for s in secoes if s["categoria"]["chave"] == "difal")
check("DIFAL interno e interestadual mostram a base correta (não R$ 0,00)",
      all("R$ 0,00" not in l["detalhe"].split("base")[1].split(",")[0] + ",00"
          for l in difal["linhas"]) and
      "800,00" in difal["linhas"][0]["detalhe"] and "3.500,00" in difal["linhas"][1]["detalhe"],
      [l["detalhe"] for l in difal["linhas"]])


# ============================================================================
print("\n=== 5. Só entra no e-mail o que foi marcado ===")
so_icms = _montar_secoes_email(PAYLOAD, ["icms"])
check("marcando só ICMS, sai 1 seção", len(so_icms) == 1, f"={len(so_icms)}")
check("a seção é a de ICMS", so_icms[0]["categoria"]["chave"] == "icms")

check("nada marcado -> nenhuma seção", _montar_secoes_email(PAYLOAD, []) == [])
check("chave inexistente é ignorada", _montar_secoes_email(PAYLOAD, ["categoria_inventada"]) == [])
check("categoria marcada mas SEM apontamento não vira seção vazia",
      _montar_secoes_email({"icms_issues": []}, ["icms"]) == [])


# ============================================================================
print("\n=== 6. XSS: descrição de produto vem do XML do fornecedor ===")
malicioso = {"icms_issues": [{
    "num": "9999", "emit_name": "FORNECEDOR X",
    "descr": '<script>alert("xss")</script>',
    "reason": '<img src=x onerror=alert(1)>',
}]}
sec_mal = _montar_secoes_email(malicioso, ["icms"])[0]
htm = _html_da_linha(sec_mal["linhas"][0], "nfe", "#dc2626")
tags = set(re.findall(r"<\s*([a-zA-Z][a-zA-Z0-9]*)", htm))
check("nenhuma tag injetada sobrevive (só li/b/span de layout)",
      tags <= {"li", "b", "span"}, f"tags={sorted(tags)}")
check("o script veio escapado como entidade", "&lt;script" in htm)


# ============================================================================
print("\n=== 7. E-mail completo monta texto e HTML ===")
Base.metadata.create_all(bind=engine)
db = SessionLocal()
tA = uuid.uuid4()
db.add(Tenant(id=tA, name="Empresa Teste Ltda", cnpj="11222333000181", ativo=True))
ua_oper = uuid.uuid4()
db.add(UserProfile(id=ua_oper, tenant_id=tA, role="operador", name="Operador"))
# Sem este vínculo, allowed_tenant_ids(operador) volta vazio e get_task_or_403
# responde 404 — é a regra de isolamento por empresa, não um detalhe do teste.
db.add(UserTenant(user_id=ua_oper, tenant_id=tA))
db.add(AuditTask(id="task-email", tenant_id=str(tA), status="concluido",
                 user_id=str(ua_oper), file_name="sped.txt",
                 result_data=json.dumps(PAYLOAD)))
db.commit()
db.close()
TOKENS["oper"] = types.SimpleNamespace(id=str(ua_oper), email="operador@synax.com")

db2 = SessionLocal()
assunto, corpo_html, corpo_txt, nome_empresa, periodo = _build_audit_email_contents(
    "task-email", db2, "Responsável Fiscal", "07/2026", True, TODAS, "Observação de teste.")
db2.close()

check("assunto traz o nome da empresa", "Empresa Teste Ltda" in assunto, assunto)
check("abertura cita empresa, CNPJ e período",
      "Empresa Teste Ltda" in corpo_txt and "11222333000181" in corpo_txt and "07/2026" in corpo_txt)
check("resumo entra quando marcado", "Documentos verificados: 40" in corpo_txt, corpo_txt[:400])
check("observações adicionais entram", "Observação de teste." in corpo_txt)
check("fechamento padrão entra", ec.TEXTO_FECHAMENTO in corpo_txt)
check("assinatura padrão entra", ec.ASSINATURA in corpo_txt)

rotulos_ausentes = [c["rotulo"].upper() for c in ec.CATEGORIAS
                    if c["rotulo"].upper() not in corpo_txt]
check("todos os 20 títulos de categoria aparecem no texto", rotulos_ausentes == [],
      f"ausentes={rotulos_ausentes}")

intros_ausentes = [c["chave"] for c in ec.CATEGORIAS if c["intro"] not in corpo_txt]
check("todo texto padrão (intro) de categoria aparece no e-mail", intros_ausentes == [],
      f"ausentes={intros_ausentes}")

check("HTML foi gerado", len(corpo_html) > 500)
check("HTML tem a tag informativo nas categorias informativas", "informativo" in corpo_html)

# Sem nada marcado, o e-mail não pode sair prometendo divergências que não lista.
db3 = SessionLocal()
_, _, txt_vazio, _, _ = _build_audit_email_contents(
    "task-email", db3, "Responsável", "07/2026", False, [], "")
db3.close()
check("e-mail sem seleção avisa que nada foi selecionado",
      "Nenhum apontamento foi selecionado" in txt_vazio, txt_vazio[:300])


# ============================================================================
print("\n=== 8. Rota HTTP: a tela do gerador funciona de ponta a ponta ===")
from backend.app.api.dependencies.db import get_rls_db as _real_rls
def _fake_rls():
    s = SessionLocal()
    try: yield s
    finally: s.close()
m.app.dependency_overrides[_real_rls] = _fake_rls

c = TestClient(m.app, raise_server_exceptions=False)
c.cookies.set("access_token", "Bearer oper")

# O cenário exato do print do usuário: ICMS marcado, com CFOP sem regra no payload.
r = c.post("/api/audit/task-email/email-generate", data={
    "recipient_name": "Responsável Fiscal / Contabilidade",
    "period": "07/2026",
    "incluir_resumo": "true",
    "incluir": ["icms", "icms_sem_regra", "pis_cofins"],
    "custom_notes": "",
})
check("rota responde 200 (antes dava 500 com CFOP de ICMS sem regra)",
      r.status_code == 200, f"status={r.status_code}")
check("prévia traz a seção de ICMS", "DIVERGÊNCIAS DE ICMS" in r.text, r.text[:300])
check("prévia traz a seção de CFOPs sem regra (a que quebrava)",
      "CFOPS DE ICMS SEM REGRA" in r.text)
check("prévia NÃO traz categoria não marcada (DIFAL)", "DIFAL" not in r.text.upper())

r_todas = c.post("/api/audit/task-email/email-generate", data={
    "recipient_name": "Responsável", "period": "07/2026",
    "incluir_resumo": "true", "incluir": TODAS, "custom_notes": "",
})
check("marcando as 20 categorias, a rota responde 200",
      r_todas.status_code == 200, f"status={r_todas.status_code}")

# Valor forjado no formulário não pode virar erro nem seção fantasma.
r_forjado = c.post("/api/audit/task-email/email-generate", data={
    "recipient_name": "R", "period": "07/2026",
    "incluir": ["icms", "../../etc/passwd", "categoria_que_nao_existe"],
})
check("chave forjada no formulário é ignorada sem erro",
      r_forjado.status_code == 200, f"status={r_forjado.status_code}")

# A aba precisa renderizar o catálogo (é o que o print do usuário mostrava vazio).
r_tela = c.get("/api/status/task-email")
check("tela de resultados responde 200", r_tela.status_code == 200, f"status={r_tela.status_code}")
rotulos_na_tela = sum(1 for cat in ec.CATEGORIAS if cat["rotulo"] in r_tela.text)
check("a aba de e-mail lista as 20 categorias do catálogo", rotulos_na_tela == 20,
      f"apareceram {rotulos_na_tela} de 20")
check("os grupos aparecem na tela",
      all(g in r_tela.text for g in ec.GRUPOS),
      [g for g in ec.GRUPOS if g not in r_tela.text])
check("nenhuma classe Tailwind montada dinamicamente sobrou no HTML",
      "-{{ cat.cor }}-" not in r_tela.text and "text--" not in r_tela.text)

engine.dispose()
if os.path.exists("_email_catalogo_test.db"):
    try:
        os.remove("_email_catalogo_test.db")
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
