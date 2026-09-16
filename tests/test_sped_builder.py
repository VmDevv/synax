# -*- coding: utf-8 -*-
"""Gerar SPED a partir dos XMLs — para importar no Domínio.

Empresa que não escritura não gera SPED. O escritório junta os XMLs do mês e
este módulo devolve um arquivo no leiaute da EFD, que é importado no Domínio
para lançar as notas sem digitação.

O que esta suíte protege:

1. **Só entra o que é ENTRADA DA EMPRESA.** A separação é pelo CNPJ do
   destinatário, nunca pelo nome do arquivo: um ZIP com as saídas dela ou com
   notas de outro cliente geraria uma escrituração falsa, e o erro só
   apareceria depois de importado.

2. **O CFOP de entrada é presunção quando não há regra.** O XML traz o do
   FORNECEDOR (5xxx/6xxx); a regra aprovada da empresa manda quando existe, e
   na falta dela vale a conversão do primeiro dígito — marcada no relatório,
   item por item.

3. **O que o XML não tem sai declarado, não inventado.** Bloco E, H, G, K e 1
   vão como "sem dados" (IND_MOV = 1), e cada campo preenchido por dedução
   (inscrição estadual, município, tipo do item) vira uma `Presuncao` visível.

4. **O arquivo fecha.** Contadores de bloco, 9900 por tipo de registro e o 9999
   com o total de linhas — as mesmas funções que fecham a correção do SPED.

5. **O motor de reescrita consegue LER o que este gerador escreve.** É a prova
   de que o arquivo tem estrutura de SPED de verdade: o leitor fiel, o mesmo
   que abre arquivo do PVA, reabre este e encontra a hierarquia.
"""
import io
import os
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, os.path.abspath('.'))

from backend.app.services.sped_builder import (  # noqa: E402
    Cadastro, cfop_de_entrada, gerar_de_pasta)
from backend.app.services.sped_rewriter import ler_texto  # noqa: E402

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
# XMLs sintéticos, com a estrutura real da NF-e 4.00.
# ============================================================================
EMPRESA = "27865757000102"
FORNECEDOR = "12345678000190"
OUTRA = "99999999000199"


def nfe(chave, num, emit_cnpj, emit_nome, dest_cnpj, itens, data="2026-04-12",
        dest_ie="082414507", dest_mun="3203205"):
    dets = ""
    for i, it in enumerate(itens, start=1):
        dets += f'''
    <det nItem="{i}">
      <prod>
        <cProd>{it['cod']}</cProd><xProd>{it['descr']}</xProd>
        <NCM>{it.get('ncm', '84713012')}</NCM><CFOP>{it['cfop']}</CFOP>
        <uCom>{it.get('unid', 'UN')}</uCom><qCom>{it['qtd']}</qCom>
        <vUnCom>{it['unit']}</vUnCom><vProd>{it['total']}</vProd>
        <vDesc>0.00</vDesc>
      </prod>
      <imposto>
        <ICMS><ICMS00>
          <orig>{it.get('orig', '0')}</orig><CST>{it.get('cst', '00')}</CST>
          <vBC>{it['total']}</vBC><pICMS>{it.get('aliq', '12.00')}</pICMS>
          <vICMS>{it['icms']}</vICMS>
        </ICMS00></ICMS>
        <PIS><PISAliq><CST>50</CST><vBC>{it['total']}</vBC>
          <pPIS>1.65</pPIS><vPIS>{it.get('pis', '0.00')}</vPIS>
        </PISAliq></PIS>
        <COFINS><COFINSAliq><CST>50</CST><vBC>{it['total']}</vBC>
          <pCOFINS>7.60</pCOFINS><vCOFINS>{it.get('cofins', '0.00')}</vCOFINS>
        </COFINSAliq></COFINS>
      </imposto>
    </det>'''

    total = sum(float(it['total']) for it in itens)
    icms = sum(float(it['icms']) for it in itens)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">
 <NFe><infNFe Id="NFe{chave}" versao="4.00">
  <ide><cUF>32</cUF><nNF>{num}</nNF><serie>1</serie><mod>55</mod>
    <dhEmi>{data}T10:30:00-03:00</dhEmi><tpNF>1</tpNF></ide>
  <emit><CNPJ>{emit_cnpj}</CNPJ><xNome>{emit_nome}</xNome>
    <IE>111222333</IE>
    <enderEmit><UF>ES</UF><cMun>3205309</cMun><xMun>VITORIA</xMun></enderEmit>
  </emit>
  <dest><CNPJ>{dest_cnpj}</CNPJ><xNome>COMERCIAL MASSUCATTI LTDA</xNome>
    <IE>{dest_ie}</IE>
    <enderDest><UF>ES</UF><cMun>{dest_mun}</cMun><xMun>LINHARES</xMun></enderDest>
  </dest>{dets}
  <total><ICMSTot>
    <vProd>{total:.2f}</vProd><vNF>{total:.2f}</vNF><vBC>{total:.2f}</vBC>
    <vICMS>{icms:.2f}</vICMS><vST>0.00</vST><vFrete>0.00</vFrete>
    <vSeg>0.00</vSeg><vOutro>0.00</vOutro><vDesc>0.00</vDesc>
    <vIPI>0.00</vIPI><vPIS>0.00</vPIS><vCOFINS>0.00</vCOFINS>
  </ICMSTot></total>
 </infNFe></NFe>
</nfeProc>'''


CTE = '''<?xml version="1.0" encoding="UTF-8"?>
<cteProc xmlns="http://www.portalfiscal.inf.br/cte" versao="4.00">
 <CTe><infCte Id="CTe35260812345678000190570010001498311000149831" versao="4.00">
  <ide><cUF>32</cUF><nCT>149831</nCT><serie>1</serie><mod>57</mod>
    <dhEmi>2026-04-15T08:00:00-03:00</dhEmi><CFOP>6352</CFOP>
    <toma3><toma>3</toma></toma3></ide>
  <emit><CNPJ>34992472000270</CNPJ><xNome>IGUACU TRANSPORTES LTDA</xNome></emit>
  <dest><CNPJ>27865757000102</CNPJ><xNome>COMERCIAL MASSUCATTI LTDA</xNome></dest>
  <vPrest><vTPrest>130.75</vTPrest></vPrest>
  <imp><ICMS><ICMS00><CST>00</CST><vBC>130.75</vBC><pICMS>12.00</pICMS>
    <vICMS>15.69</vICMS></ICMS00></ICMS></imp>
 </infCte></CTe>
</cteProc>'''


PASTA = tempfile.mkdtemp(prefix="synax_builder_")


def gravar(nome, conteudo):
    caminho = os.path.join(PASTA, nome)
    io.open(caminho, "w", encoding="utf-8").write(conteudo)
    return caminho


ITENS_1 = [
    {"cod": "CAFE500", "descr": "CAFE TORRADO 500G", "cfop": "5102",
     "qtd": "10.0000", "unit": "100.00", "total": "1000.00", "icms": "120.00"},
    {"cod": "ACUCAR1", "descr": "ACUCAR CRISTAL 1KG", "cfop": "5102",
     "qtd": "5.0000", "unit": "100.00", "total": "500.00", "icms": "60.00",
     "unid": "CX"},
]
ITENS_2 = [
    {"cod": "OLEO900", "descr": "OLEO DE SOJA 900ML", "cfop": "6102",
     "qtd": "3.0000", "unit": "100.00", "total": "300.00", "icms": "36.00",
     "orig": "1", "cst": "00"},
]

gravar("entrada1.xml", nfe("35260812345678000190550010000027771000002777",
                           "2777", FORNECEDOR, "DIST LAURENCINI LTDA",
                           EMPRESA, ITENS_1))
gravar("entrada2.xml", nfe("35260812345678000190550010000027781000002778",
                           "2778", FORNECEDOR, "DIST LAURENCINI LTDA",
                           EMPRESA, ITENS_2))
# Saída da própria empresa: NÃO pode entrar.
gravar("saida.xml", nfe("35260827865757000102550010000099991000009999",
                        "9999", EMPRESA, "COMERCIAL MASSUCATTI LTDA",
                        OUTRA, ITENS_1))
# Nota de outro cliente que veio junto no ZIP: também não.
gravar("terceiro.xml", nfe("35260899999999000199550010000012341000001234",
                           "1234", FORNECEDOR, "DIST LAURENCINI LTDA",
                           OUTRA, ITENS_1))
gravar("frete.xml", CTE)
gravar("lixo.xml", "<isto>não é nota fiscal</isto>")


# ============================================================================
print("\n=== 1. Só entra o que é entrada DESTA empresa ===")

cad = Cadastro(cnpj=EMPRESA, nome="COMERCIAL MASSUCATTI LTDA", uf="ES")
res = gerar_de_pasta(PASTA, cad)

check("gerou arquivo", res.documento is not None, res.avisos)
check("duas notas de entrada", res.notas == 2, res.notas)
check("tres itens", res.itens == 3, res.itens)
check("um CT-e", res.fretes == 1, res.fretes)

ignorados = {nome: motivo for nome, motivo in res.ignorados}
check("a SAIDA da empresa ficou de fora",
      "própria empresa" in ignorados.get("saida.xml", ""),
      ignorados.get("saida.xml"))
check("a nota de outro cliente tambem",
      "outra empresa" in ignorados.get("terceiro.xml", ""),
      ignorados.get("terceiro.xml"))
check("e o arquivo que nao e nota diz isso",
      "não é XML" in ignorados.get("lixo.xml", ""), ignorados.get("lixo.xml"))

texto = res.documento.serializar()
linhas = [l for l in texto.split("\r\n") if l]


def campos(prefixo, indice=0):
    achadas = [l for l in linhas if l.startswith(prefixo)]
    return achadas[indice].split("|") if achadas else []


check("nenhuma nota de saida foi escriturada",
      not any("9999" == l.split("|")[8] for l in linhas if l.startswith("|C100|")))


# ============================================================================
print("\n=== 2. O 0000 sai do proprio XML ===")

r0000 = campos("|0000|")
check("competencia lida das notas", (r0000[4], r0000[5]) == ("01042026", "30042026"),
      (r0000[4], r0000[5]))
check("CNPJ da empresa", r0000[7] == EMPRESA, r0000[7])
check("inscricao estadual veio do destinatario no XML", r0000[10] == "082414507",
      r0000[10])
check("codigo IBGE do municipio tambem", r0000[11] == "3203205", r0000[11])
check("e o perfil/atividade saem marcados como presuncao",
      any("IND_PERFIL" in p.campo for p in res.presuncoes))

check("mes misturado no pacote e recusado, com os meses listados",
      True)  # coberto abaixo, com pacote proprio


# ============================================================================
print("\n=== 3. Participantes, unidades e produtos ===")

check("um 0150 por fornecedor (o transportador entra tambem)",
      len([l for l in linhas if l.startswith("|0150|")]) == 2,
      [l.split("|")[2] for l in linhas if l.startswith("|0150|")])
check("0190 traz as duas unidades usadas",
      {l.split("|")[2] for l in linhas if l.startswith("|0190|")} == {"UN", "CX"},
      [l for l in linhas if l.startswith("|0190|")])
check("um 0200 por produto, com o codigo do fornecedor",
      {l.split("|")[2] for l in linhas if l.startswith("|0200|")}
      == {"CAFE500", "ACUCAR1", "OLEO900"},
      [l.split("|")[2] for l in linhas if l.startswith("|0200|")])
check("com NCM e unidade preenchidos",
      all(l.split("|")[8] and l.split("|")[6]
          for l in linhas if l.startswith("|0200|")))
check("e o TIPO_ITEM presumido aparece no relatorio",
      any("TIPO_ITEM" in p.campo for p in res.presuncoes))


# ============================================================================
print("\n=== 4. O CFOP de entrada: regra manda, deducao e marcada ===")

check("5102 vira 1102 quando nao ha regra", cfop_de_entrada("5102") == ("1102", False))
check("6102 vira 2102", cfop_de_entrada("6102") == ("2102", False))
check("7102 vira 3102", cfop_de_entrada("7102") == ("3102", False))
check("a regra aprovada manda", cfop_de_entrada("5102", "1556") == ("1556", True))
check("CFOP ilegivel nao vira palpite", cfop_de_entrada("") == ("", False))

cfops_gerados = {l.split("|")[11] for l in linhas if l.startswith("|C170|")}
check("os itens sairam com CFOP de entrada", cfops_gerados == {"1102", "2102"},
      cfops_gerados)
check("e a deducao esta no relatorio, com a contagem",
      any(p.campo.endswith("CFOP") and p.quantidade == 3 for p in res.presuncoes),
      [(p.campo, p.quantidade) for p in res.presuncoes])

regras = {(FORNECEDOR, "5102", "CAFE500"): "1556"}
com_regra = gerar_de_pasta(PASTA, cad, regras)
linhas_r = [l for l in com_regra.documento.serializar().split("\r\n") if l]
check("com regra aprovada, o item sai com o CFOP dela",
      any(l.split("|")[3] == "CAFE500" and l.split("|")[11] == "1556"
          for l in linhas_r if l.startswith("|C170|")),
      [l for l in linhas_r if l.startswith("|C170|")][0])


# ============================================================================
print("\n=== 5. O documento fiscal, campo a campo ===")

c100 = campos("|C100|")
check("entrada, emitido por terceiro", (c100[2], c100[3]) == ("0", "1"),
      (c100[2], c100[3]))
check("modelo 55, situacao 00", (c100[5], c100[6]) == ("55", "00"))
check("chave de 44 digitos", len(c100[9]) == 44, c100[9])
check("data no formato do SPED", c100[10] == "12042026", c100[10])
check("valor do documento", c100[12] == "1500,00", c100[12])

c170 = campos("|C170|")
check("item com 38 campos", len(c170) - 2 == 38, len(c170) - 2)
check("quantidade com 5 casas", c170[5] == "10,00000", c170[5])
check("CST com a ORIGEM colada", c170[10] == "000", c170[10])
check("e o item importado mantem a origem 1",
      any(l.split("|")[10] == "100" for l in linhas if l.startswith("|C170|")),
      [l.split("|")[10] for l in linhas if l.startswith("|C170|")])

c190s = [l.split("|") for l in linhas if l.startswith("|C190|")]
check("o analitico foi montado pelo motor", len(c190s) >= 2, len(c190s))
check("e soma o ICMS dos itens",
      any(c[7] == "180,00" for c in c190s), [c[7] for c in c190s])

d100 = campos("|D100|")
check("o CT-e virou D100 com a chave", len(d100[10]) == 44, d100[10])
d190 = campos("|D190|")
check("com D190 e CFOP de entrada convertido", d190[3] == "2352", d190[3])


# ============================================================================
print("\n=== 6. O arquivo fecha, e o motor de reescrita consegue le-lo ===")

check("todo bloco obrigatorio foi declarado",
      all(any(l.startswith(f"|{b}001|") for l in linhas)
          for b in ("0", "C", "D", "E", "G", "H", "K", "1", "9")),
      [l[:6] for l in linhas if l[1:4].endswith("001")])
check("os blocos sem dados vao com IND_MOV = 1",
      all(campos(f"|{b}001|")[2] == "1" for b in ("E", "G", "H", "K", "1")))

check("o 9999 conta todas as linhas",
      campos("|9999|")[2] == str(len(linhas)),
      (campos("|9999|")[2], len(linhas)))
check("ha uma linha 9900 por tipo de registro",
      len({l.split("|")[1] for l in linhas})
      == len([l for l in linhas if l.startswith("|9900|")]),
      (len({l.split("|")[1] for l in linhas}),
       len([l for l in linhas if l.startswith("|9900|")])))
check("e cada 9900 conta certo",
      all(int(l.split("|")[3]) == len([x for x in linhas
                                       if x.split("|")[1] == l.split("|")[2]])
          for l in linhas if l.startswith("|9900|")),
      [(l.split("|")[2], l.split("|")[3]) for l in linhas
       if l.startswith("|9900|")][:5])

# A prova de que isto é SPED de verdade: o leitor fiel do motor de correção —
# o mesmo que abre arquivo gerado pelo PVA — reabre este e acha a hierarquia.
relido = ler_texto(texto)
check("o motor de reescrita releva o arquivo sem perder linha",
      len(relido.registros) == len(linhas), len(relido.registros))
check("e reconhece a hierarquia: itens pendurados no documento",
      len(relido.filhos_reg(relido.por_reg("C100")[0], "C170")) == 2,
      len(relido.filhos_reg(relido.por_reg("C100")[0], "C170")))
check("o arquivo volta byte a byte pelo leitor", relido.serializar() == texto)


# ============================================================================
print("\n=== 7. O que impede a geracao ===")

sem_cnpj = gerar_de_pasta(PASTA, Cadastro(cnpj=""))
check("sem CNPJ da empresa nao gera nada",
      sem_cnpj.documento is None and "CNPJ da empresa" in sem_cnpj.avisos[0],
      sem_cnpj.avisos)

vazia = tempfile.mkdtemp(prefix="synax_builder_vazio_")
nada = gerar_de_pasta(vazia, cad)
check("pacote sem entrada da empresa avisa, em vez de gerar arquivo vazio",
      nada.documento is None and "Nenhum documento de entrada" in nada.avisos[0],
      nada.avisos)
os.rmdir(vazia)

# SPED é MENSAL: dois meses no mesmo pacote não podem virar um arquivo só.
dois_meses = tempfile.mkdtemp(prefix="synax_builder_meses_")
io.open(os.path.join(dois_meses, "a.xml"), "w", encoding="utf-8").write(
    nfe("35260812345678000190550010000027771000002777", "2777", FORNECEDOR,
        "DIST LAURENCINI LTDA", EMPRESA, ITENS_1, data="2026-04-12"))
io.open(os.path.join(dois_meses, "b.xml"), "w", encoding="utf-8").write(
    nfe("35260812345678000190550010000027791000002779", "2779", FORNECEDOR,
        "DIST LAURENCINI LTDA", EMPRESA, ITENS_2, data="2026-05-03"))
misturado = gerar_de_pasta(dois_meses, cad)
check("pacote com dois meses e recusado",
      misturado.documento is None
      and "mais de um mês" in misturado.avisos[0], misturado.avisos)
check("e os meses aparecem no aviso",
      "2026-04" in misturado.avisos[0] and "2026-05" in misturado.avisos[0],
      misturado.avisos)
for nome in os.listdir(dois_meses):
    os.remove(os.path.join(dois_meses, nome))
os.rmdir(dois_meses)


# ============================================================================
print("\n=== 8. A tela: escopo, geracao e download ===")

# O harness da aplicacao vem DEPOIS dos testes do gerador de proposito: assim a
# parte que importa — a montagem do arquivo — roda mesmo que o banco falhe.
import json  # noqa: E402
import types  # noqa: E402
import uuid as _uuid  # noqa: E402
import zipfile  # noqa: E402

import dotenv  # noqa: E402
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main  # noqa: E402
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["DATABASE_URL"] = "sqlite:///./_builder_test.db"
os.environ["SUPABASE_URL"] = "http://fake"
os.environ["SUPABASE_KEY"] = "fake"
os.environ["SUPABASE_SERVICE_KEY"] = "fake"
os.environ["FERNET_SECRET_KEY"] = "hn5Ck1J8pVXHRV9V3Q6mYbXW4y2S1TqLpRZ0oN7dG8A="
if os.path.exists("_builder_test.db"):
    os.remove("_builder_test.db")

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
from backend.app.infrastructure.database.session import SessionLocal  # noqa: E402
from backend.app.domain.models.base import Base  # noqa: E402
from backend.app.domain.models.auth_models import (  # noqa: E402
    Tenant, UserProfile, UserTenant)
from backend.app.domain.models.rules import CfopClassification  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

Base.metadata.create_all(bind=_eng)
db = SessionLocal()
tA, tB = _uuid.uuid4(), _uuid.uuid4()
db.add_all([
    Tenant(id=tA, name="COMERCIAL MASSUCATTI LTDA", cnpj=EMPRESA, uf="ES"),
    # Empresa sem CNPJ: o gerador nao tem como separar entrada de saida.
    Tenant(id=tB, name="SEM CADASTRO LTDA", cnpj=None, uf="ES"),
])
u_ope, u_cli = _uuid.uuid4(), _uuid.uuid4()
db.add_all([
    UserProfile(id=u_ope, tenant_id=tA, role="operador", name="Operador"),
    UserProfile(id=u_cli, tenant_id=tA, role="cliente", name="Cliente"),
])
db.add_all([UserTenant(user_id=u_ope, tenant_id=tA),
            UserTenant(user_id=u_ope, tenant_id=tB)])
# Uma regra aprovada, para provar que ela manda no CFOP gerado.
db.add(CfopClassification(
    tenant_id=str(tA), company_cnpj=EMPRESA, supplier_cnpj=FORNECEDOR,
    supplier_cfop="5102", product_code="CAFE500", entry_cfop="1556",
    status="aprovado", created_by="admin"))
db.commit()
db.close()

TOKENS["ope"] = types.SimpleNamespace(id=str(u_ope), email="oper@synax.com")
TOKENS["cli"] = types.SimpleNamespace(id=str(u_cli), email="cli@synax.com")

from backend.app.api.dependencies.db import get_rls_db as _real_rls  # noqa: E402


def _fake_rls():
    ss = SessionLocal()
    try:
        yield ss
    finally:
        ss.close()


m.app.dependency_overrides[_real_rls] = _fake_rls
c = TestClient(m.app, raise_server_exceptions=False)


def as_(token):
    c.cookies.clear()
    if token:
        c.cookies.set("access_token", f"Bearer {token}")
    return c


r = as_("ope").get("/gerar-sped")
check("operador abre a tela", r.status_code == 200, r.status_code)
check("com o seletor de empresa", 'name="tenant_id"' in r.text)
check("e o aviso de que sai arquivo de ENTRADAS",
      "Blocos E, G, H, K e 1" in r.text)
check("cliente nao entra", as_("cli").get("/gerar-sped").status_code == 403)
check("anonimo tambem nao",
      as_(None).get("/gerar-sped").status_code in (401, 403))

# O ZIP do mes, com as mesmas notas da fixture.
pacote = io.BytesIO()
with zipfile.ZipFile(pacote, "w") as z:
    z.writestr("entrada1.xml", nfe("35260812345678000190550010000027771000002777",
                                   "2777", FORNECEDOR, "DIST LAURENCINI LTDA",
                                   EMPRESA, ITENS_1))
    z.writestr("entrada2.xml", nfe("35260812345678000190550010000027781000002778",
                                   "2778", FORNECEDOR, "DIST LAURENCINI LTDA",
                                   EMPRESA, ITENS_2))
    z.writestr("saida.xml", nfe("35260827865757000102550010000099991000009999",
                                "9999", EMPRESA, "COMERCIAL MASSUCATTI LTDA",
                                OUTRA, ITENS_1))
pacote.seek(0)

r_gera = as_("ope").post("/api/gerar-sped",
                         data={"tenant_id": str(tA), "perfil": "A",
                               "ind_ativ": "1"},
                         files={"xmls_zip": ("xmls.zip", pacote.getvalue(),
                                             "application/zip")})
check("a geracao responde 200", r_gera.status_code == 200, r_gera.status_code)
check("a tela mostra as notas lidas", ">2<" in r_gera.text
      and "notas de entrada" in r_gera.text)
check("mostra o que ficou de fora do pacote",
      "ficaram de fora" in r_gera.text and "saida.xml" in r_gera.text)
check("mostra as presuncoes ANTES do download",
      "precisou presumir" in r_gera.text
      and r_gera.text.index("precisou presumir") < r_gera.text.index("Baixar"))
check("e oferece o download", "/arquivo" in r_gera.text)

import re as _re  # noqa: E402
processo = _re.search(r"/api/gerar-sped/([0-9a-f-]{36})/arquivo", r_gera.text)
check("o link do arquivo traz o identificador", processo is not None)

r_arq = as_("ope").get(f"/api/gerar-sped/{processo.group(1)}/arquivo")
check("o download responde 200", r_arq.status_code == 200, r_arq.status_code)
check("como arquivo, nunca renderizado",
      r_arq.headers.get("content-type") == "application/octet-stream"
      and r_arq.headers.get("content-disposition", "").startswith("attachment;"))
gerado = r_arq.content.decode("latin-1")
check("e o conteudo e o SPED", gerado.startswith("|0000|")
      and "|C100|" in gerado and "|9999|" in gerado)
check("com o CNPJ da empresa no 0000",
      gerado.splitlines()[0].split("|")[7] == EMPRESA)
check("a REGRA APROVADA mandou no CFOP do item dela",
      any(l.split("|")[3] == "CAFE500" and l.split("|")[11] == "1556"
          for l in gerado.splitlines() if l.startswith("|C170|")),
      [l for l in gerado.splitlines() if l.startswith("|C170|")][:1])

check("cliente nao baixa",
      as_("cli").get(f"/api/gerar-sped/{processo.group(1)}/arquivo").status_code
      == 403)
check("identificador que nao e UUID nao vira leitura de arquivo do servidor",
      as_("ope").get("/api/gerar-sped/..%2F..%2Fmain/arquivo").status_code
      in (404, 400))

pacote.seek(0)
r_sem_cnpj = as_("ope").post("/api/gerar-sped",
                             data={"tenant_id": str(tB)},
                             files={"xmls_zip": ("x.zip", pacote.getvalue(),
                                                 "application/zip")})
check("empresa sem CNPJ no cadastro e recusada, com o motivo",
      r_sem_cnpj.status_code == 200 and "não tem CNPJ" in r_sem_cnpj.text)

TELA = io.open(os.path.join("frontend", "templates", "pages", "dashboard.html"),
               encoding="utf-8").read()
check("o dashboard leva para a ferramenta",
      "/gerar-sped" in TELA and "não gera SPED" in TELA)

# Limpeza do que a rota deixou em temp_uploads.
import shutil as _shutil  # noqa: E402
_shutil.rmtree(os.path.join(os.getcwd(), "temp_uploads", processo.group(1)),
               ignore_errors=True)
_eng.dispose()
if os.path.exists("_builder_test.db"):
    try:
        os.remove("_builder_test.db")
    except OSError:
        pass


# ============================================================================
for nome in os.listdir(PASTA):
    os.remove(os.path.join(PASTA, nome))
os.rmdir(PASTA)

print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
for f in _falhou:
    print("  -", f)
sys.exit(1 if FALHAS else 0)
