# -*- coding: utf-8 -*-
"""O segundo leiaute: EFD Contribuições (PIS/COFINS), ligado por caixa na tela.

A mesma empresa entrega DOIS arquivos por mês. O EFD ICMS/IPI traz ICMS e IPI;
o EFD Contribuições traz a base de PIS/COFINS que o primeiro quase não preenche.
Os dois começam com `|0000|` e só diferem de posição, então o arquivo trocado
NÃO dá erro sozinho: o CNPJ da empresa sai com oito dígitos, porque o leitor
pega a data final; o nome sai como uma data; a competência sai nula. E a
auditoria roda inteira assim.

O que esta suíte protege:

1. **O leiaute é detectado pelo próprio 0000**, e o arquivo trocado é recusado
   no envio, com o motivo em português.
2. **O leitor de Contribuições lê o cabeçalho certo** — competência, CNPJ de 14
   dígitos e razão social.
3. **O leitor Fiscal não mudou nada.** É a regressão que importa: o percurso
   passou a ser compartilhado, e o de sempre tem de sair igual.
4. **As mesmas regras nos dois.** COD_SIT, duplicidade, participantes, itens e
   PIS/COFINS de frete se comportam igual; só o cabeçalho muda de lugar.
5. **Sem C190 e sem D190, nada é inventado.** O arquivo de Contribuições não
   tem esses registros, e o motor não pode acusar divergência interna nem
   perder o valor do CT-e por causa disso.
6. **Cada documento sabe de qual filial é.** O arquivo de Contribuições é por
   pessoa jurídica e cobre todas elas; o Fiscal é de um estabelecimento só.
7. **A caixa existe na tela e chega ao motor.**
"""
import io
import os
import sys
import tempfile
import types
import uuid as _uuid

sys.path.insert(0, os.path.abspath('.'))

from backend.app.services.audit_engine.sped_parser import (  # noqa: E402
    CONTRIBUICOES, FISCAL, LeiauteInesperado, detectar_leiaute, ler_sped,
    parse_sped)
from backend.app.services.audit_engine.sped_contribuicoes import (  # noqa: E402
    parse_sped_contribuicoes)

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


def linha(reg, campos, total):
    valores = [""] * total
    valores[0] = reg
    for numero, valor in campos.items():
        valores[numero - 1] = valor
    return "|" + "|".join(valores) + "|"


CNPJ = "27865757000102"
FILIAL = "27865757000285"
NOME = "COMERCIAL MASSUCATTI LTDA"
CHAVE = "35260812345678000190550010000027771000002777"
CHAVE2 = "35260812345678000190550010000027781000002778"
CHAVE_CTE = "35260834992472000270570010001498311000149831"

# Um item igual nos dois arquivos, a menos do número de campos do registro.
ITEM = {2: "1", 3: "CAFE500", 5: "10,00000", 6: "UN", 7: "1000,00", 8: "0,00",
        9: "0", 10: "000", 11: "1102", 13: "1000,00", 14: "12,00",
        15: "120,00", 16: "0,00", 18: "0,00",
        25: "50", 26: "880,00", 27: "1,65", 30: "14,52",
        31: "50", 32: "880,00", 33: "7,60", 36: "66,88"}

C100 = {2: "0", 3: "1", 4: "F1", 5: "55", 6: "00", 7: "1", 8: "2777",
        9: CHAVE, 10: "12042026", 11: "12042026", 12: "1000,00", 13: "1",
        14: "0,00", 16: "1000,00", 17: "9", 21: "1000,00", 22: "120,00"}

D100 = {2: "0", 3: "1", 4: "T1", 5: "57", 6: "00", 7: "1", 9: "149831",
        10: CHAVE_CTE, 11: "15042026", 12: "15042026", 13: "0",
        15: "130,75", 17: "1", 18: "130,75", 19: "130,75", 20: "15,69"}


def sped_contribuicoes(cod_sit="00", repetida=False, com_filial=True):
    """EFD Contribuições: 0000 com 14 campos, datas em 06/07, CNPJ em 09."""
    ls = [
        linha("0000", {2: "006", 3: "0", 6: "01042026", 7: "30042026",
                       8: NOME, 9: CNPJ, 10: "ES", 11: "3203205",
                       13: "00", 14: "1"}, 14),
        linha("0001", {2: "0"}, 2),
    ]
    if com_filial:
        ls += [
            linha("0140", {2: "001", 3: NOME, 4: CNPJ, 5: "ES",
                           6: "082414507", 7: "3203205"}, 9),
            linha("0140", {2: "002", 3: NOME + " FILIAL", 4: FILIAL, 5: "ES",
                           6: "082414590", 7: "3205309"}, 9),
        ]
    ls += [
        linha("0150", {2: "F1", 3: "FORNECEDOR LTDA", 4: "1058",
                       5: "12345678000190"}, 13),
        linha("0150", {2: "T1", 3: "IGUACU TRANSPORTES LTDA", 4: "1058",
                       5: "34992472000270"}, 13),
        linha("0200", {2: "CAFE500", 3: "CAFE TORRADO 500G"}, 12),
        linha("0990", {2: "8"}, 2),
        linha("C001", {2: "0"}, 2),
        linha("C010", {2: CNPJ, 3: "0"}, 3),
        linha("C100", {**C100, 6: cod_sit}, 29),
        linha("C170", ITEM, 37),
    ]
    if repetida:
        ls += [linha("C100", {**C100, 6: cod_sit}, 29),
               linha("C170", ITEM, 37)]
    if com_filial:
        # Segunda filial: mesma pessoa jurídica, outro estabelecimento.
        ls += [linha("C010", {2: FILIAL, 3: "0"}, 3),
               linha("C100", {**C100, 8: "2778", 9: CHAVE2}, 29),
               linha("C170", ITEM, 37)]
    ls += [
        linha("C990", {2: str(len(ls))}, 2),
        linha("D001", {2: "0"}, 2),
        linha("D010", {2: CNPJ}, 2),
        linha("D100", D100, 23),
        linha("D101", {2: "0", 3: "130,75", 4: "50", 5: "01", 6: "115,06",
                       7: "1,65", 8: "1,90"}, 9),
        linha("D105", {2: "0", 3: "130,75", 4: "50", 5: "01", 6: "115,06",
                       7: "7,60", 8: "8,74"}, 9),
        linha("D990", {2: "6"}, 2),
        linha("M001", {2: "0"}, 2),
        linha("M200", {2: "14,52"}, 13),
        linha("M990", {2: "3"}, 2),
        linha("9001", {2: "0"}, 2),
        linha("9990", {2: "3"}, 2),
        linha("9999", {2: "30"}, 2),
    ]
    return "\r\n".join(ls) + "\r\n"


def sped_fiscal(cod_sit="00"):
    """EFD ICMS/IPI: 0000 com 15 campos, datas em 04/05, CNPJ em 07."""
    ls = [
        linha("0000", {2: "020", 3: "0", 4: "01042026", 5: "30042026",
                       6: NOME, 7: CNPJ, 9: "ES", 10: "082414507",
                       11: "3203205", 14: "A", 15: "1"}, 15),
        linha("0001", {2: "0"}, 2),
        linha("0150", {2: "F1", 3: "FORNECEDOR LTDA", 4: "1058",
                       5: "12345678000190"}, 13),
        linha("0150", {2: "T1", 3: "IGUACU TRANSPORTES LTDA", 4: "1058",
                       5: "34992472000270"}, 13),
        linha("0200", {2: "CAFE500", 3: "CAFE TORRADO 500G"}, 13),
        linha("0990", {2: "6"}, 2),
        linha("C001", {2: "0"}, 2),
        linha("C100", {**C100, 6: cod_sit}, 29),
        linha("C170", ITEM, 38),
        linha("C190", {2: "000", 3: "1102", 4: "12,00", 5: "1000,00",
                       6: "1000,00", 7: "120,00"}, 12),
        linha("C990", {2: "4"}, 2),
        linha("D001", {2: "0"}, 2),
        linha("D100", D100, 25),
        linha("D190", {2: "000", 3: "1353", 4: "12,00", 5: "130,75",
                       6: "130,75", 7: "15,69"}, 9),
        linha("D990", {2: "4"}, 2),
        linha("9001", {2: "0"}, 2),
        linha("9990", {2: "3"}, 2),
        linha("9999", {2: "18"}, 2),
    ]
    return "\r\n".join(ls) + "\r\n"


PASTA = tempfile.mkdtemp(prefix="synax_contrib_")


def gravar(nome, texto):
    caminho = os.path.join(PASTA, nome)
    io.open(caminho, "w", encoding="latin-1", newline="").write(texto)
    return caminho


ARQ_CONTRIB = gravar("contribuicoes.txt", sped_contribuicoes())
ARQ_FISCAL = gravar("fiscal.txt", sped_fiscal())


# ============================================================================
print("\n=== 1. O leiaute e detectado pelo proprio 0000 ===")

check("o EFD Contribuicoes e reconhecido",
      detectar_leiaute(ARQ_CONTRIB) == CONTRIBUICOES, detectar_leiaute(ARQ_CONTRIB))
check("o EFD ICMS/IPI tambem",
      detectar_leiaute(ARQ_FISCAL) == FISCAL, detectar_leiaute(ARQ_FISCAL))
check("arquivo sem 0000 legivel nao chuta leiaute",
      detectar_leiaute(gravar("sem.txt", "|C100|0|1|\r\n")) == "")
check("arquivo inexistente devolve vazio em vez de estourar",
      detectar_leiaute(os.path.join(PASTA, "nao_existe.txt")) == "")
# IND_SIT_ESP e NUM_REC_ANTERIOR preenchidos: as datas continuam em 06/07.
com_recibo = sped_contribuicoes().replace(
    "|0000|006|0|||01042026|", "|0000|006|0|0|41100001234567890123456789012345678901|01042026|", 1)
check("Contribuicoes com situacao especial e recibo anterior ainda e reconhecido",
      detectar_leiaute(gravar("recibo.txt", com_recibo)) == CONTRIBUICOES)


# ============================================================================
print("\n=== 2. O leitor de Contribuicoes le o cabecalho certo ===")

c = parse_sped_contribuicoes(ARQ_CONTRIB)
check("CNPJ com 14 digitos", c["cnpj"] == CNPJ, c["cnpj"])
check("razao social, nao uma data", c["name"] == NOME, c["name"])
check("competencia lida", (c["periodo_inicio"], c["periodo_fim"])
      == ("2026-04-01", "2026-04-30"), (c["periodo_inicio"], c["periodo_fim"]))
check("o resultado diz qual leiaute leu",
      c["leiaute"] == CONTRIBUICOES and "Contribuições" in c["leiaute_rotulo"],
      (c["leiaute"], c["leiaute_rotulo"]))

# E o que acontecia antes desta suite existir: o leitor Fiscal no arquivo errado.
errado = parse_sped(ARQ_CONTRIB)
check("o leitor Fiscal no arquivo de Contribuicoes le o CNPJ como data (o bug)",
      len(errado["cnpj"]) == 8 and errado["periodo_inicio"] is None,
      (errado["cnpj"], errado["periodo_inicio"]))
check("e mesmo assim processaria as notas — por isso existe a trava",
      len(errado["c100"]) > 0, len(errado["c100"]))


# ============================================================================
print("\n=== 3. O leitor Fiscal nao mudou (regressao) ===")

f = parse_sped(ARQ_FISCAL)
check("CNPJ", f["cnpj"] == CNPJ, f["cnpj"])
check("nome", f["name"] == NOME, f["name"])
check("competencia", (f["periodo_inicio"], f["periodo_fim"])
      == ("2026-04-01", "2026-04-30"))
check("marca o leiaute como fiscal", f["leiaute"] == FISCAL, f["leiaute"])
check("o C190 continua sendo somado", f["c100"][CHAVE]["vl_opr_c190_sum"] == 1000.0,
      f["c100"][CHAVE]["vl_opr_c190_sum"])
check("o D190 continua mandando no CT-e",
      f["d100"][CHAVE_CTE]["tem_d190"] and f["d100"][CHAVE_CTE]["vl_icms"] == 15.69,
      f["d100"][CHAVE_CTE]["vl_icms"])
check("sem 0140, o estabelecimento do documento e a propria empresa",
      f["c100"][CHAVE]["estabelecimento"] == CNPJ and f["estabelecimentos"] == {},
      f["estabelecimentos"])


# ============================================================================
print("\n=== 4. As mesmas regras nos dois leiautes ===")

item_c = c["c100"][CHAVE]["items"][0]
item_f = f["c100"][CHAVE]["items"][0]
check("o item sai igual nos dois (37 e 38 campos, mesmas posicoes)",
      item_c == item_f, [(k, item_c[k], item_f[k]) for k in item_c
                         if item_c[k] != item_f[k]])
check("a base de PIS veio do C170", item_c["vl_bc_pis"] == 880.0, item_c["vl_bc_pis"])
check("e o CST tambem", (item_c["cst_pis"], item_c["cst_cofins"]) == ("50", "50"))
check("participantes lidos igual", c["participants"]["F1"]["cnpj"]
      == f["participants"]["F1"]["cnpj"] == "12345678000190")
check("descricao do produto lida igual",
      c["products"]["CAFE500"] == f["products"]["CAFE500"] == "CAFE TORRADO 500G")

for rotulo, leitor, montar in (("Contribuicoes", parse_sped_contribuicoes, sped_contribuicoes),
                               ("Fiscal", parse_sped, sped_fiscal)):
    cancelada = leitor(gravar(f"cancel_{rotulo}.txt", montar(cod_sit="02")))
    check(f"COD_SIT 02 fica fora da apuracao ({rotulo})",
          CHAVE not in cancelada["c100"], list(cancelada["c100"]))
    complementar = leitor(gravar(f"compl_{rotulo}.txt", montar(cod_sit="06")))
    check(f"COD_SIT 06 (complementar) entra ({rotulo})", CHAVE in complementar["c100"])

dup = parse_sped_contribuicoes(gravar("dup.txt", sped_contribuicoes(repetida=True)))
check("nota repetida e apontada como duplicidade",
      len(dup["duplicate_nfs"]) == 1 and dup["duplicate_nfs"][0]["chave"] == CHAVE,
      dup["duplicate_nfs"])

cte = c["d100"][CHAVE_CTE]
check("PIS/COFINS do frete lidos do D101/D105",
      (cte["vl_bc_pis"], cte["vl_pis"], cte["vl_cofins"]) == (115.06, 1.90, 8.74),
      (cte["vl_bc_pis"], cte["vl_pis"], cte["vl_cofins"]))
check("e o arquivo e marcado como tendo PIS/COFINS de frete",
      c["bloco_d_tem_pis_cofins"] and cte["tem_pis_cofins"])


# ============================================================================
print("\n=== 5. Sem C190 e sem D190, nada e inventado ===")

check("o somatorio do C190 fica em zero (o registro nao existe no leiaute)",
      c["c100"][CHAVE]["vl_opr_c190_sum"] == 0.0,
      c["c100"][CHAVE]["vl_opr_c190_sum"])
check("zero e o que faz o motor PULAR o confronto interno C100 x C190",
      not (c["c100"][CHAVE]["vl_opr_c190_sum"] > 0.01))
check("o CT-e sem D190 cai no cabecalho, e nao em zero",
      not cte["tem_d190"] and cte["vl_doc"] == 130.75 and cte["vl_icms"] == 15.69,
      (cte["tem_d190"], cte["vl_doc"], cte["vl_icms"]))
check("o valor contabil do frete tambem sai do cabecalho",
      cte["vl_contabil"] == 130.75, cte["vl_contabil"])


# ============================================================================
print("\n=== 6. Cada documento sabe de qual filial e ===")

check("as duas filiais do 0140 foram lidas",
      set(c["estabelecimentos"]) == {CNPJ, FILIAL}, list(c["estabelecimentos"]))
check("a nota depois do primeiro C010 e da matriz",
      c["c100"][CHAVE]["estabelecimento"] == CNPJ)
check("a nota depois do segundo C010 e da filial",
      c["c100"][CHAVE2]["estabelecimento"] == FILIAL,
      c["c100"][CHAVE2]["estabelecimento"])
check("o frete segue o D010", c["d100"][CHAVE_CTE]["estabelecimento"] == CNPJ)
sem_filial = parse_sped_contribuicoes(
    gravar("sem_filial.txt", sped_contribuicoes(com_filial=False)))
check("arquivo de um estabelecimento so nao inventa lista",
      sem_filial["estabelecimentos"] == {})


# ============================================================================
print("\n=== 7. A trava: arquivo trocado e recusado, com o motivo ===")

check("ler_sped abre o Contribuicoes quando a caixa esta marcada",
      ler_sped(ARQ_CONTRIB, True)["cnpj"] == CNPJ)
check("e o Fiscal quando nao esta",
      ler_sped(ARQ_FISCAL, False)["cnpj"] == CNPJ)

erros = {}
for rotulo, caminho, contrib in (("fiscal_marcado", ARQ_FISCAL, True),
                                 ("contrib_desmarcado", ARQ_CONTRIB, False)):
    try:
        ler_sped(caminho, contrib)
        erros[rotulo] = None
    except LeiauteInesperado as e:
        erros[rotulo] = e

check("arquivo Fiscal com a caixa marcada e recusado",
      erros["fiscal_marcado"] is not None)
check("e a mensagem diz o que fazer",
      "Desmarque a opção" in (erros["fiscal_marcado"].mensagem or ""),
      getattr(erros["fiscal_marcado"], "mensagem", ""))
check("arquivo de Contribuicoes com a caixa desmarcada tambem",
      erros["contrib_desmarcado"] is not None)
check("com a mensagem inversa",
      "Marque a opção" in (erros["contrib_desmarcado"].mensagem or ""),
      getattr(erros["contrib_desmarcado"], "mensagem", ""))
check("a mensagem nao vaza caminho de arquivo nem traceback",
      all(termo not in erros["fiscal_marcado"].mensagem
          for termo in ("\\", "Traceback", ".py", "temp_uploads")),
      erros["fiscal_marcado"].mensagem)

# Cabeçalho ilegível não bloqueia: quem trata a ausência é o resto do leitor.
indeciso = gravar("indeciso.txt", "|C100|0|1|F1|55|00|1|1|" + CHAVE + "|\r\n")
check("arquivo sem 0000 nao e recusado — segue com o leiaute pedido",
      ler_sped(indeciso, True)["periodo_inicio"] is None)


# ============================================================================
print("\n=== 8. A tela e a rota ===")

TELA = io.open(os.path.join("frontend", "templates", "pages", "dashboard.html"),
               encoding="utf-8").read()
check("a caixa existe na tela de auditoria",
      'name="sped_contribuicoes"' in TELA)
check("com rotulo que diz qual arquivo e",
      "SPED Contribuições" in TELA and "PIS/COFINS" in TELA)
check("e explica que as regras da auditoria nao mudam",
      "regras da auditoria não mudam" in TELA)

import dotenv  # noqa: E402
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main  # noqa: E402
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_contrib_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
os.environ.pop("OPENAI_API_KEY", None)
if os.path.exists("_contrib_test.db"):
    os.remove("_contrib_test.db")

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
import backend.app.api.routes.uploads as rota_uploads  # noqa: E402
from backend.app.infrastructure.database.session import SessionLocal  # noqa: E402
from backend.app.domain.models.base import Base  # noqa: E402
from backend.app.domain.models.auth_models import (  # noqa: E402
    Tenant, UserProfile, UserTenant)
from fastapi.testclient import TestClient  # noqa: E402

Base.metadata.create_all(bind=_eng)
db = SessionLocal()
tA = _uuid.uuid4()
u_ope = _uuid.uuid4()
db.add(Tenant(id=tA, name=NOME, cnpj=CNPJ, uf="ES"))
db.add(UserProfile(id=u_ope, tenant_id=tA, role="operador", name="Operador"))
db.add(UserTenant(user_id=u_ope, tenant_id=tA))
db.commit()
db.close()
TOKENS["ope"] = types.SimpleNamespace(id=str(u_ope), email="oper@synax.com")

c_http = TestClient(m.app, raise_server_exceptions=False)
c_http.cookies.set("access_token", "Bearer ope")

# A auditoria de verdade não roda aqui: o que se testa é a OPÇÃO chegando.
chamadas = []
rota_uploads.execute_audit = lambda *a, **kw: chamadas.append(kw)


def pastas_de_upload():
    raiz = "temp_uploads"
    return set(os.listdir(raiz)) if os.path.isdir(raiz) else set()


def enviar(texto, marcada):
    return c_http.post(
        "/api/upload",
        data={"tenant_id": str(tA), "sped_only_mode": "true",
              **({"sped_contribuicoes": "true"} if marcada else {})},
        files={"sped_file": ("sped.txt", texto.encode("latin-1"), "text/plain")})


antes_do_envio = pastas_de_upload()
r = enviar(sped_fiscal(), marcada=True)
check("a rota RECUSA o arquivo Fiscal com a caixa marcada",
      r.status_code == 400, r.status_code)
check("e devolve o motivo em portugues, sem traceback",
      "Desmarque a opção" in r.text and "Traceback" not in r.text, r.text[:160])

r = enviar(sped_contribuicoes(), marcada=False)
check("e o de Contribuicoes com a caixa desmarcada",
      r.status_code == 400 and "Marque a opção" in r.text,
      (r.status_code, r.text[:120]))

check("upload recusado nao deixa pasta orfa em temp_uploads",
      pastas_de_upload() == antes_do_envio, pastas_de_upload() - antes_do_envio)

r = enviar(sped_contribuicoes(), marcada=True)
check("o par certo passa", r.status_code == 200, r.status_code)
check("e a opcao chega ao motor",
      chamadas and chamadas[-1].get("sped_contribuicoes") is True, chamadas[-1:])

r = enviar(sped_fiscal(), marcada=False)
check("o par de sempre continua passando", r.status_code == 200, r.status_code)
check("com a opcao desligada",
      chamadas[-1].get("sped_contribuicoes") is False, chamadas[-1:])

# PERCAL, 23/10/2026: o arquivo foi salvo com um "F" antes do |0000|. Sem
# leiaute nenhum o envio passava, e a auditoria rodava sem o CNPJ da empresa:
# 137 itens "sem regra" numa empresa com 290 regras aprovadas.
_antes_f = (len(chamadas), pastas_de_upload())
r = enviar("F" + sped_fiscal(), marcada=False)
check("0000 ilegivel: o envio e RECUSADO, nao auditado calado",
      r.status_code == 400 and len(chamadas) == _antes_f[0], (r.status_code, len(chamadas)))
check("e o motivo mostra o trecho que sobra antes do |0000|",
      '"F" antes do |0000|' in r.json().get("detail", "") and "apague-o" in r.text, r.text[:200])
check("sem pasta orfa em temp_uploads", pastas_de_upload() == _antes_f[1])

from backend.app.services.audit_engine.sped_parser import motivo_sem_leiaute  # noqa: E402


def _motivo(texto):
    caminho = os.path.join(PASTA, "motivo.txt")
    with open(caminho, "w", encoding="latin-1", newline="") as f:
        f.write(texto)
    return motivo_sem_leiaute(caminho)


check("espaco antes do |0000| e dito como espaco",
      "espaços em branco antes do |0000|" in _motivo("  " + sped_fiscal()))
check("arquivo sem 0000: diz que nao e SPED",
      "não começa pelo registro |0000|" in _motivo("|0001|0|\r\n"))
check("o |9900|0000| do Bloco 9 nao passa por 0000 com sobra",
      "não começa pelo registro |0000|" in _motivo("|0001|0|\r\n|9900|0000|1|\r\n"))
check("0000 sem as datas: aponta os campos do leiaute",
      "campos 04 e 05" in _motivo("|0000|020|0|||EMPRESA|41216114000312|\r\n"))

import shutil  # noqa: E402
for pasta in (os.listdir("temp_uploads") if os.path.isdir("temp_uploads") else []):
    caminho = os.path.join("temp_uploads", pasta)
    if os.path.isdir(caminho) and os.path.exists(os.path.join(caminho, "sped.txt")):
        shutil.rmtree(caminho, ignore_errors=True)
_eng.dispose()
if os.path.exists("_contrib_test.db"):
    try:
        os.remove("_contrib_test.db")
    except OSError:
        pass



# ============================================================================
print("\n=== 9. A correcao de PIS/COFINS: outro leiaute, outra cascata ===")

from decimal import Decimal  # noqa: E402
from backend.app.services.sped_rewriter import (  # noqa: E402
    aplicar, aplicar_contribuicoes, ler_texto, montar_patch, prever,
    propor_contribuicoes, texto_da_previa)
from backend.app.services.sped_rewriter.patch import (  # noqa: E402
    LEIAUTE_CONTRIBUICOES, LEIAUTE_FISCAL, opcoes_do_leiaute)

TEXTO = sped_contribuicoes()

# A base do item no arquivo e 880,00; a tese do STF deu 900,00.
ISSUE_BASE = {
    "key": CHAVE, "num": "2777", "num_item": "1", "descr": "CAFE TORRADO 500G",
    "emit_name": "FORNECEDOR LTDA", "tipo": "PIS/COFINS",
    "reason": "Base de PIS/COFINS divergente", "comparacao": "base",
    "base_calculada": 900.00, "base_sped": 880.00,
    "sped_val": 880.00, "xml_val": 900.00,
    "xml_aliq_pis": 1.65, "xml_aliq_cofins": 7.60,
}
ISSUE_GLOSA = {
    "key": CHAVE2, "num": "2778", "num_item": "1", "descr": "CAFE TORRADO 500G",
    "emit_name": "FORNECEDOR LTDA", "tipo": "PIS/COFINS", "acao": "glosar",
    "reason": "CFOP/CST sem direito a credito", "sped_val": 81.40, "xml_val": 0.00,
}
ISSUE_FRETE = {
    "key": CHAVE_CTE, "num": "149831", "num_item": "CT-e",
    "descr": "Conhecimento de Transporte (Frete)", "tipo": "PIS/COFINS Frete",
    "reason": "Credito de PIS/COFINS do frete divergente",
    "sped_val": 10.64, "xml_val": 12.00,
}
ISSUE_SEM_ALIQ = {k: v for k, v in ISSUE_BASE.items()
                  if k not in ("xml_aliq_pis", "xml_aliq_cofins")}


def propor_de(issues):
    return propor_contribuicoes({"pis_cofins_issues": issues}, TEXTO)


def patch_de(issues):
    props = propor_de(issues)
    return props, montar_patch(props, [x.id for x in props])


# --- a proposta de base: os seis campos juntos
[p] = propor_de([ISSUE_BASE])
check("a proposta e de credito de PIS/COFINS", p.opcao == "credito_pis_cofins", p.opcao)
check("e e aplicavel", p.aplicavel, p.impedimento)
check("com os seis campos: base, aliquota e valor dos dois tributos",
      sorted(p.campos) == ["ALIQ_COFINS", "ALIQ_PIS", "VL_BC_COFINS",
                           "VL_BC_PIS", "VL_COFINS", "VL_PIS"],
      sorted(p.campos))
check("a base e a mesma para PIS e COFINS",
      p.campos["VL_BC_PIS"] == p.campos["VL_BC_COFINS"] == Decimal("900.00"),
      p.campos["VL_BC_PIS"])
check("o valor e DERIVADO da base pela aliquota fixa (900 x 1,65%)",
      p.campos["VL_PIS"] == Decimal("14.85"), p.campos["VL_PIS"])
check("idem COFINS (900 x 7,60%)", p.campos["VL_COFINS"] == Decimal("68.40"),
      p.campos["VL_COFINS"])
check("o de -> para sai do ARQUIVO, nao do relatorio",
      ("VL_BC_PIS", "880,00", "900,00") in p.mudancas, p.mudancas)
check("e o delta e o quanto o credito se move",
      p.delta == (Decimal("14.85") + Decimal("68.40")
                  - Decimal("14.52") - Decimal("66.88")), p.delta)

# --- a aliquota e FIXA: 1,65% e 7,60%, qualquer que seja a do XML.
# Pedido do Sandro em 23/09/2026. O XML traz a aliquota do FORNECEDOR: no
# cumulativo ele destaca 0,65/3,00, e a correcao gravava credito a menor; no
# Simples nao destaca nada, e a proposta saia impedida. O credito e de quem
# compra, e no nao-cumulativo segue a aliquota dele.
ISSUE_CUMULATIVO = {**ISSUE_BASE, "xml_aliq_pis": 0.65, "xml_aliq_cofins": 3.00}
[p_cum] = propor_de([ISSUE_CUMULATIVO])
check("fornecedor no cumulativo (XML 0,65/3,00): o credito sai a 1,65/7,60",
      p_cum.aplicavel
      and (p_cum.campos["ALIQ_PIS"], p_cum.campos["ALIQ_COFINS"]) == ("1,65", "7,60")
      and (p_cum.campos["VL_PIS"], p_cum.campos["VL_COFINS"])
      == (Decimal("14.85"), Decimal("68.40")),
      (p_cum.impedimento, p_cum.campos))

[p_sem] = propor_de([ISSUE_SEM_ALIQ])
check("XML sem aliquota (Simples) ou auditoria antiga: corrige mesmo assim",
      p_sem.aplicavel
      and (p_sem.campos["VL_PIS"], p_sem.campos["VL_COFINS"])
      == (Decimal("14.85"), Decimal("68.40")),
      (p_sem.impedimento, p_sem.campos))

# O arquivo escriturou a aliquota do fornecedor, com quatro casas (N 008 04 no
# Guia): a correcao troca o numero e mantem as casas que o arquivo ja usa.
TEXTO_CUM = "".join(
    l.replace("|1,65|", "|0,6500|").replace("|7,60|", "|3,0000|")
    if l.startswith("|C170|") else l
    for l in TEXTO.splitlines(keepends=True))
p_arq = propor_contribuicoes({"pis_cofins_issues": [ISSUE_BASE]}, TEXTO_CUM)[0]
check("aliquota do fornecedor no arquivo vira 1,6500/7,6000, nas casas do arquivo",
      ("ALIQ_PIS", "0,6500", "1,6500") in p_arq.mudancas
      and ("ALIQ_COFINS", "3,0000", "7,6000") in p_arq.mudancas, p_arq.mudancas)
check("e a aliquota que ja esta certa nao vira mudanca so por formato",
      not [m for m in p.mudancas if m[0].startswith("ALIQ_")], p.mudancas)
_doc_cum = ler_texto(TEXTO_CUM)
aplicar_contribuicoes(_doc_cum, montar_patch([p_arq], [p_arq.id]))
_it_cum = _doc_cum.filhos_reg(_doc_cum.indice_de_documentos()[CHAVE], "C170")[0]
check("no arquivo: 900,00 x 1,6500% = 14,85 e 900,00 x 7,6000% = 68,40",
      (_it_cum.campo(26), _it_cum.campo(27), _it_cum.campo(30),
       _it_cum.campo(33), _it_cum.campo(36))
      == ("900,00", "1,6500", "14,85", "7,6000", "68,40"),
      (_it_cum.campo(26), _it_cum.campo(27), _it_cum.campo(30),
       _it_cum.campo(33), _it_cum.campo(36)))

# --- a glosa: zerar o valor basta
[p_glosa] = propor_de([ISSUE_GLOSA])
check("a glosa pede so os dois valores",
      sorted(p_glosa.campos) == ["VL_COFINS", "VL_PIS"], sorted(p_glosa.campos))
check("e o delta e negativo", p_glosa.delta < 0, p_glosa.delta)

# --- o frete aparece impedido, nunca some
[p_frete] = propor_de([ISSUE_FRETE])
check("o PIS/COFINS do frete sai IMPEDIDO, com o motivo",
      not p_frete.aplicavel and "D101" in p_frete.impedimento, p_frete.impedimento)


# --- a cascata grava o item e totaliza o documento
def aplicar_em(issues):
    _props, patch = patch_de(issues)
    doc = ler_texto(TEXTO)
    return doc, aplicar_contribuicoes(doc, patch)


doc, res = aplicar_em([ISSUE_BASE])
c100 = doc.indice_de_documentos()[CHAVE]
item = doc.filhos_reg(c100, "C170")[0]
check("sem recusas", not res.recusas, [r.motivo for r in res.recusas])
check("base, aliquota e valor de PIS gravados no item",
      (item.campo(26), item.campo(27), item.campo(30)) == ("900,00", "1,65", "14,85"),
      (item.campo(26), item.campo(27), item.campo(30)))
check("idem COFINS",
      (item.campo(32), item.campo(33), item.campo(36)) == ("900,00", "7,60", "68,40"),
      (item.campo(32), item.campo(33), item.campo(36)))
check("o documento totaliza PIS e COFINS dos itens (C100 campos 26 e 27)",
      (c100.campo(26), c100.campo(27)) == ("14,85", "68,40"),
      (c100.campo(26), c100.campo(27)))
check("NENHUM C190 foi criado — o leiaute nao os tem",
      len(doc.por_reg("C190")) == 0, len(doc.por_reg("C190")))
check("nem uma linha a mais no arquivo",
      len(doc.registros) == len(ler_texto(TEXTO).registros),
      (len(doc.registros), len(ler_texto(TEXTO).registros)))
check("o ICMS do item nao foi tocado",
      (item.campo(13), item.campo(15)) == ("1000,00", "120,00"),
      (item.campo(13), item.campo(15)))
check("e nada foi escrito em apuracao",
      not any(a.registro in ("E110", "M100", "M200") for a in res.alteracoes))

# --- a glosa leva base e aliquota junto
doc_g, res_g = aplicar_em([ISSUE_GLOSA])
item_g = doc_g.filhos_reg(doc_g.indice_de_documentos()[CHAVE2], "C170")[0]
check("glosa zera o valor dos dois tributos",
      (item_g.campo(30), item_g.campo(36)) == ("0,00", "0,00"),
      (item_g.campo(30), item_g.campo(36)))
check("e a base e a aliquota vao junto (o Guia manda os tres em conjunto)",
      (item_g.campo(26), item_g.campo(27), item_g.campo(32), item_g.campo(33))
      == ("0,00", "0,00", "0,00", "0,00"),
      (item_g.campo(26), item_g.campo(27), item_g.campo(32), item_g.campo(33)))
# O CST 70 entrou em 18/09/2026, decisao do Sandro. Era aviso ate aqui: os
# codigos de entrada sem direito sao varios (70 a 75 e 98) e escolher entre
# eles e decisao fiscal, nao conta. Sem a troca o arquivo sai se contradizendo
# na mesma linha — imposto zero com CST 50 dizendo que ha direito a credito —
# e quem recebe a contradicao e o PVA, na importacao.
check("o CST vai a 70 junto — aquisicao sem direito a credito (Tabela 4.3.3)",
      (item_g.campo(25), item_g.campo(31)) == ("70", "70"),
      (item_g.campo(25), item_g.campo(31)))
check("a troca de CST aparece na trilha, com origem cascata",
      sorted(a.campo for a in res_g.alteracoes
             if a.campo in ("CST_PIS", "CST_COFINS") and a.origem == "cascata")
      == ["CST_COFINS", "CST_PIS"],
      [(a.campo, a.de, a.para, a.origem) for a in res_g.alteracoes])
check("e o aviso diz o que foi gravado e quais eram os outros candidatos",
      any("70" in a and "71 a 75 e 98" in a for a in res_g.avisos), res_g.avisos)

# --- a cascata do Fiscal sobre este arquivo: o que NAO pode acontecer
_props, patch_base = patch_de([ISSUE_BASE])
doc_errado = ler_texto(TEXTO)
aplicar(doc_errado, patch_base)
check("a cascata do Fiscal INJETARIA C190 neste arquivo — por isso sao duas",
      len(doc_errado.por_reg("C190")) > 0, len(doc_errado.por_reg("C190")))

# --- a previa escolhe a cascata
previa_ok = prever(TEXTO, patch_base, contribuicoes=True)
check("a previa de Contribuicoes aceita a correcao",
      len(previa_ok.aceitas) == 1 and not previa_ok.recusadas,
      (len(previa_ok.aceitas), len(previa_ok.recusadas)))
check("a previa NAO cria C190", previa_ok.efeitos.c190_criados == 0,
      previa_ok.efeitos.c190_criados)
check("mas a previa do Fiscal sobre o mesmo arquivo cria",
      prever(TEXTO, patch_base).efeitos.c190_criados > 0)

_props_g, patch_glosa = patch_de([ISSUE_GLOSA])
previa_glosa = prever(TEXTO, patch_glosa, contribuicoes=True)
check("na glosa a previa conta os 4 campos que a cascata completou",
      previa_glosa.efeitos.pis_cofins_completados == 4,
      previa_glosa.efeitos.pis_cofins_completados)
check("e os 2 CST trocados contam como CST, nao como base e aliquota",
      previa_glosa.efeitos.cst_ajustados == 2,
      previa_glosa.efeitos.cst_ajustados)
check("a previa diz que o CST vai junto",
      "CST ajustado(s)" in texto_da_previa(previa_glosa),
      texto_da_previa(previa_glosa)[-500:])
check("e o texto da previa explica o que mudou junto",
      "base e alíquota de PIS/COFINS" in texto_da_previa(previa_glosa),
      texto_da_previa(previa_glosa)[-400:])


# ============================================================================
print("\n=== 10. A tela de correcao segue o leiaute da auditoria ===")

TPL_CORRECAO = io.open(os.path.join(
    "frontend", "templates", "partials", "curation", "_correcao_sped.html"),
    encoding="utf-8").read()
check("a caixa da apuracao e condicionada ao leiaute",
      "{% if grupos and not contribuicoes %}" in TPL_CORRECAO)
check("e o template explica por que ela some",
      "não existe E110" in TPL_CORRECAO and "Bloco M" in TPL_CORRECAO)

from backend.app.api.routes import correcao_sped as rota_correcao  # noqa: E402
check("a rota reconhece o leiaute gravado pelo motor",
      rota_correcao._leiaute_da_auditoria(
          {"resumo": {"leiaute": "contribuicoes"}}) == "contribuicoes")
check("auditoria antiga, sem a marca, continua sendo Fiscal",
      rota_correcao._leiaute_da_auditoria({}) == "fiscal"
      and rota_correcao._leiaute_da_auditoria({"resumo": {}}) == "fiscal")
# Desde 18/09/2026 a de PIS/COFINS vale nos DOIS: os campos 25 a 36 do C170
# existem nos dois leiautes. As sete de ICMS/IPI continuam so no Fiscal, que
# e o que esta suite existe para provar.
check("as opcoes do Fiscal sao oito, com a de PIS/COFINS",
      len(opcoes_do_leiaute(LEIAUTE_FISCAL)) == 8
      and any(o.codigo == "credito_pis_cofins"
              for o in opcoes_do_leiaute(LEIAUTE_FISCAL)),
      [o.codigo for o in opcoes_do_leiaute(LEIAUTE_FISCAL)])
check("e a do Contribuicoes continua so a de PIS/COFINS",
      [o.codigo for o in opcoes_do_leiaute(LEIAUTE_CONTRIBUICOES)]
      == ["credito_pis_cofins"])

# ============================================================================
for nome in os.listdir(PASTA):
    os.remove(os.path.join(PASTA, nome))
os.rmdir(PASTA)

print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
for f_ in _falhou:
    print("  -", f_)
sys.exit(1 if FALHAS else 0)
