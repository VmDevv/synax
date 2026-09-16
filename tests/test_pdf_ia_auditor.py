"""Auditoria de notas em PDF pela IA da Anthropic (Claude).

Sem rede: o cliente da Anthropic é substituído por um dublê que registra a
requisição montada. O que se verifica aqui é o CONTRATO da chamada — modelo,
PDF anexado como documento base64, prompt literal, saída travada por JSON
Schema — e o tratamento de cada jeito que a resposta pode dar errado.

O comportamento do modelo em si (achar a divergência entre resumo e nota) foi
conferido contra a API real com PDFs sintéticos; não dá para travar isso numa
suíte offline, e mockar a resposta e depois afirmar que "a IA acertou" seria
testar o próprio mock.
"""
import os, sys, io, json, types

sys.path.insert(0, os.path.abspath('.'))

import dotenv
dotenv.load_dotenv = lambda *a, **k: False
import dotenv.main
dotenv.main.load_dotenv = lambda *a, **k: False

os.environ["ANTHROPIC_API_KEY"] = "sk-ant-teste-nao-e-chave-real"

import anthropic
from backend.app.services.nfse import pdf_ia_auditor as mod

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("  OK  " if cond else " FALHA") + f" | {name}" + (f"  [{detail}]" if detail and not cond else ""))


PDF_MINIMO = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"

RESPOSTA_OK = {
    "notas_validadas": [{
        "cnpj_prestador": "11222333000181", "fornecedor": "ALFA SERVICOS LTDA",
        "uf_prestador": "ES", "cnpj_tomador": "34449753000109", "uf_tomador": "ES",
        "data_entrada": "08/07/2026", "data_emissao": "05/07/2026",
        "num_nota": "501", "valor": 1500.50,
        "retencao_ir": 22.51, "retencao_pis": 9.75,
        "retencao_cofins": 45.02, "retencao_csll": 15.01,
    }],
    "divergencias": [{
        "cnpj_prestador": "44555666000199", "num_nota": "502",
        "data_emissao": "12/07/2026",
        "motivo_divergencia": "Resumo diz 2000,00 e a nota diz 2350,00.",
    }],
}


class _Bloco:
    def __init__(self, tipo, texto=""):
        self.type = tipo
        self.text = texto


class _Resposta:
    def __init__(self, blocos, stop_reason="end_turn", stop_details=None):
        self.content = blocos
        self.stop_reason = stop_reason
        self.stop_details = stop_details
        self.usage = types.SimpleNamespace(input_tokens=1234, output_tokens=567)


class _Stream:
    def __init__(self, resposta):
        self._resposta = resposta
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def get_final_message(self):
        return self._resposta


class _Messages:
    """Guarda os kwargs da chamada e devolve o que o teste mandar devolver."""
    def __init__(self, dono, via_beta=False):
        self.dono = dono
        self.via_beta = via_beta
    def stream(self, **kwargs):
        self.dono.chamadas.append({**kwargs, "_via_beta": self.via_beta})
        if isinstance(self.dono.retorno, Exception):
            raise self.dono.retorno
        return _Stream(self.dono.retorno)


class _Files:
    """Files API dublada: registra o que subiu e o que foi apagado."""
    def __init__(self, dono):
        self.dono = dono
    def upload(self, **kwargs):
        self.dono.uploads.append(kwargs)
        return types.SimpleNamespace(id="file_teste_123", size_bytes=0)
    def delete(self, file_id, **kwargs):
        self.dono.deletados.append(file_id)
        return types.SimpleNamespace(id=file_id)


class _Beta:
    def __init__(self, dono):
        self.files = _Files(dono)
        self.messages = _Messages(dono, via_beta=True)


class ClienteFalso:
    def __init__(self, retorno):
        self.retorno = retorno
        self.chamadas = []
        self.uploads = []
        self.deletados = []
        self.messages = _Messages(self)
        self.beta = _Beta(self)


def rodar_com(retorno, pdf=PDF_MINIMO):
    """Executa auditar_pdf com o cliente dublê; devolve (resultado_ou_erro, cliente)."""
    cliente = ClienteFalso(retorno)
    original = mod.anthropic.Anthropic
    mod.anthropic.Anthropic = lambda *a, **k: cliente
    try:
        try:
            return mod.auditar_pdf(pdf), cliente
        except Exception as e:
            return e, cliente
    finally:
        mod.anthropic.Anthropic = original


# ============================================================================
print("\n=== 1. Validação do arquivo, antes de gastar chamada de API ===")
for entrada, rotulo in [(b"", "arquivo vazio"), (b"PK\x03\x04", "ZIP disfarçado de PDF"),
                        (b"<html>", "HTML disfarçado de PDF")]:
    r, c = rodar_com(_Resposta([]), pdf=entrada)
    check(f"recusa {rotulo}", isinstance(r, mod.PdfInvalido), f"{type(r).__name__}")
    check(f"e nem chega a chamar a API para {rotulo}", c.chamadas == [])

# Rebaixa o teto so para o teste: alocar 450 MB de "x" na suite seria absurdo.
_teto = mod.MAX_PDF_BYTES
try:
    mod.MAX_PDF_BYTES = 1024
    r, c = rodar_com(_Resposta([]), pdf=b"%PDF-1.4\n" + b"x" * 2048)
    check("recusa PDF acima do limite de tamanho", isinstance(r, mod.PdfInvalido))
    check("a mensagem diz o tamanho e manda separar",
          "MB" in str(r) and "partes menores" in str(r), str(r))
    check("e nem chega a chamar a API", c.chamadas == [])
finally:
    mod.MAX_PDF_BYTES = _teto

check("o teto cabe na Files API (500 MB)", mod.MAX_PDF_BYTES < 500 * 1024 * 1024,
      mod.MAX_PDF_BYTES)
check("o limite do inline cabe no corpo da requisição (32 MB com base64)",
      mod.LIMITE_INLINE_BYTES * 4 / 3 < 32 * 1024 * 1024, mod.LIMITE_INLINE_BYTES)

# A chave sai do AMBIENTE, e so de la. Ate 10/09/2026 havia uma constante com a
# chave em texto puro no modulo, como provisorio; ela foi rotacionada e apagada.
_chave = os.environ.pop("ANTHROPIC_API_KEY")
try:
    check("com ANTHROPIC_API_KEY no ambiente, e ela que vale",
          (os.environ.__setitem__("ANTHROPIC_API_KEY", "sk-do-ambiente") or
           mod.chave_api()) == "sk-do-ambiente", mod.chave_api())
    os.environ.pop("ANTHROPIC_API_KEY")

    check("sem a variavel de ambiente, nao ha chave nenhuma",
          mod.chave_api() == "", mod.chave_api())
    r, c = rodar_com(_Resposta([]))
    check("e a auditoria RECUSA em vez de rodar",
          isinstance(r, mod.ConfiguracaoAusente), f"{type(r).__name__}")
    check("sem nem chegar a chamar a API", c.chamadas == [])
finally:
    os.environ["ANTHROPIC_API_KEY"] = _chave

# A trava que impede a constante de voltar: chave de API e' segredo de ambiente,
# e uma vez no arquivo ela vale para quem tiver o arquivo — commit, backup,
# copia de pasta, print de tela.
_FONTE = io.open(mod.__file__, encoding="utf-8").read()
check("nenhuma chave da Anthropic escrita no codigo",
      "sk-ant-api" not in _FONTE)
check("e nenhum fallback de chave sobrou",
      "CHAVE_PROVISORIA" not in _FONTE)


# ============================================================================
print("\n=== 2. Contrato da requisição ===")
resultado, cliente = rodar_com(_Resposta([_Bloco("text", json.dumps(RESPOSTA_OK))]))
check("a chamada acontece", len(cliente.chamadas) == 1, len(cliente.chamadas))
req = cliente.chamadas[0]

# Sonnet 5 e nao Opus: escolha do Sandro por custo (26/08/2026). Um PDF
# escaneado de 60 paginas custa ~150 mil tokens de entrada por auditoria.
check("modelo é claude-sonnet-5", req["model"] == "claude-sonnet-5", req.get("model"))
check("esforço baixo: extração de digitalização é trabalho mecânico",
      req["output_config"].get("effort") == "low", req["output_config"].get("effort"))
check("orçamento de saída no máximo do modelo (o raciocínio sai daqui também)",
      mod.MAX_TOKENS == 128000, mod.MAX_TOKENS)
check("usa streaming (max_tokens alto estoura o timeout sem ele)",
      mod.MAX_TOKENS >= 64000, mod.MAX_TOKENS)
check("thinking adaptativo", req.get("thinking") == {"type": "adaptive"}, req.get("thinking"))

conteudo = req["messages"][0]["content"]
doc = next((b for b in conteudo if b.get("type") == "document"), None)
check("o PDF vai como bloco document", doc is not None)
check("mime type application/pdf", doc and doc["source"]["media_type"] == "application/pdf",
      doc and doc["source"].get("media_type"))
check("fonte base64", doc and doc["source"]["type"] == "base64")
import base64 as _b64
check("o base64 é o PDF cru, sem quebra de linha",
      doc and _b64.standard_b64decode(doc["source"]["data"]) == PDF_MINIMO
      and "\n" not in doc["source"]["data"])
check("o documento vem ANTES do texto (ordem recomendada)",
      [b["type"] for b in conteudo] == ["document", "text"],
      [b["type"] for b in conteudo])

texto_prompt = next(b["text"] for b in conteudo if b["type"] == "text")
check("o prompt é o combinado, literal", texto_prompt == mod.PROMPT_AUDITORIA)
for trecho in ["assistente de auditoria fiscal corporativa", "PRESTADOR",
               "TOMADOR", "notas_validadas", "divergencias",
               "cnpj_tomador", "uf_tomador", "cnpj_prestador",
               "aceite-a como verdade"]:
    check(f"prompt contém {trecho!r}", trecho in texto_prompt)

fmt = req.get("output_config", {}).get("format", {})
check("saída travada por JSON Schema", fmt.get("type") == "json_schema", fmt.get("type"))
esquema = fmt.get("schema", {})
check("o schema exige as duas listas",
      set(esquema.get("required", [])) == {"notas_validadas", "divergencias"},
      esquema.get("required"))
campos_nota = esquema["properties"]["notas_validadas"]["items"]
check("nota exige os 13 campos", len(campos_nota["required"]) == 13, campos_nota.get("required"))
check("nota não aceita campo extra", campos_nota.get("additionalProperties") is False)
check("num_nota da divergência aceita null (o prompt prevê isso)",
      esquema["properties"]["divergencias"]["items"]["properties"]["num_nota"]["type"]
      == ["string", "null"])
check("todo campo do prompt está no schema da nota",
      set(campos_nota["properties"]) == {
          "cnpj_prestador", "fornecedor", "uf_prestador",
          "cnpj_tomador", "uf_tomador",
          "data_entrada", "data_emissao", "num_nota", "valor",
          "retencao_ir", "retencao_pis", "retencao_cofins", "retencao_csll"},
      sorted(campos_nota["properties"]))

# O ponto da reformulacao: sem os dois lados no schema, o CFOP volta a depender
# de cadastro externo e a tela volta a exigir escolher a empresa.
check("o schema OBRIGA os dois lados da operação",
      {"cnpj_prestador", "uf_prestador", "cnpj_tomador", "uf_tomador"}
      <= set(campos_nota["required"]), campos_nota["required"])
check("a UF do tomador é descrita como sigla de 2 letras",
      "2 letras" in campos_nota["properties"]["uf_tomador"]["description"],
      campos_nota["properties"]["uf_tomador"])


# ============================================================================
print("\n=== 2b. PDF pequeno vai inline; grande vai pela Files API ===")
check("PDF pequeno NAO usa a Files API", cliente.uploads == [], cliente.uploads)
check("PDF pequeno usa o endpoint normal, sem beta", req["_via_beta"] is False)

# Rebaixa o limite do inline para forcar o caminho da Files API sem alocar 20 MB.
_limite = mod.LIMITE_INLINE_BYTES
try:
    mod.LIMITE_INLINE_BYTES = 10
    grande = b"%PDF-1.4\n" + b"x" * 500
    r_g, c_g = rodar_com(_Resposta([_Bloco("text", json.dumps(RESPOSTA_OK))]), pdf=grande)
    check("PDF grande sobe pela Files API", len(c_g.uploads) == 1, c_g.uploads)
    up = c_g.uploads[0]
    check("o upload leva o beta files-api", mod.BETA_FILES_API in (up.get("betas") or []),
          up.get("betas"))
    check("sobe como application/pdf", up["file"][2] == "application/pdf", up["file"])
    check("sobe os bytes do PDF", up["file"][1].getvalue() == grande)

    req_g = c_g.chamadas[0]
    check("a mensagem usa o endpoint beta", req_g["_via_beta"] is True)
    doc_g = next(b for b in req_g["messages"][0]["content"] if b["type"] == "document")
    check("o documento vira referencia por file_id",
          doc_g["source"] == {"type": "file", "file_id": "file_teste_123"}, doc_g["source"])
    check("nenhum base64 vai no corpo", "data" not in doc_g["source"])
    check("o prompt continua o mesmo nos dois caminhos",
          next(b["text"] for b in req_g["messages"][0]["content"] if b["type"] == "text")
          == mod.PROMPT_AUDITORIA)
    check("o schema continua travado no caminho grande",
          req_g["output_config"]["format"]["schema"] == mod.SCHEMA_RESPOSTA)
    check("o resultado e o mesmo", r_g == RESPOSTA_OK, r_g)

    # Documento de cliente nao fica hospedado na Anthropic depois da analise.
    check("o arquivo e apagado da Files API depois", c_g.deletados == ["file_teste_123"],
          c_g.deletados)

    # Mesmo quando a analise falha, o arquivo tem que ser apagado.
    _, c_erro = rodar_com(_Resposta([_Bloco("text", "nao e json")]), pdf=grande)
    check("apaga o arquivo mesmo quando o parse falha",
          c_erro.deletados == ["file_teste_123"], c_erro.deletados)
finally:
    mod.LIMITE_INLINE_BYTES = _limite


# ============================================================================
print("\n=== 3. Leitura da resposta ===")
check("devolve as duas listas separadas",
      resultado == RESPOSTA_OK, resultado)
check("nota validada com valor float", resultado["notas_validadas"][0]["valor"] == 1500.50)
check("a nota traz os dois lados",
      resultado["notas_validadas"][0]["uf_tomador"] == "ES"
      and resultado["notas_validadas"][0]["cnpj_tomador"] == "34449753000109")
check("divergência com o motivo", "2350,00" in resultado["divergencias"][0]["motivo_divergencia"])

# Bloco de raciocínio vem antes do texto quando thinking está ligado.
r, _ = rodar_com(_Resposta([_Bloco("thinking"), _Bloco("text", json.dumps(RESPOSTA_OK))]))
check("ignora o bloco de raciocínio e acha o JSON", r == RESPOSTA_OK, r)

# Rede de segurança: se alguém trocar por um modelo sem saída estruturada.
cercado = "```json\n" + json.dumps(RESPOSTA_OK) + "\n```"
r, _ = rodar_com(_Resposta([_Bloco("text", cercado)]))
check("cerca de markdown não quebra o parse", r == RESPOSTA_OK, r)

# `null` explícito é JSON válido e passaria pelo schema.
r, _ = rodar_com(_Resposta([_Bloco("text", '{"notas_validadas": null, "divergencias": null}')]))
check("null nas listas vira lista vazia, não None",
      r == {"notas_validadas": [], "divergencias": []}, r)


# ============================================================================
print("\n=== 4. Falhas: nenhuma vira resultado vazio silencioso ===")
r, _ = rodar_com(_Resposta([_Bloco("text", "isto nao e json")]))
check("JSON inválido levanta erro", isinstance(r, mod.ErroAuditoriaPdf), f"{type(r).__name__}")
check("e NÃO devolve zero notas como se fosse sucesso", not isinstance(r, dict))

r, _ = rodar_com(_Resposta([_Bloco("text", '{"notas_validadas": [')], stop_reason="max_tokens"))
check("resposta truncada (max_tokens) levanta erro",
      isinstance(r, mod.ErroAuditoriaPdf), f"{type(r).__name__}")
check("a mensagem manda separar o PDF", "partes menores" in str(r), str(r))

r, _ = rodar_com(_Resposta(
    [_Bloco("text", "")], stop_reason="refusal",
    stop_details=types.SimpleNamespace(category="cyber", explanation="motivo qualquer")))
check("recusa do modelo levanta erro", isinstance(r, mod.ErroAuditoriaPdf), f"{type(r).__name__}")
check("e repassa a explicação da recusa", "motivo qualquer" in str(r), str(r))

r, _ = rodar_com(_Resposta([_Bloco("thinking")]))
check("resposta sem bloco de texto levanta erro", isinstance(r, mod.ErroAuditoriaPdf))


# ============================================================================
print("\n=== 5. Erros da API viram mensagem legível ===")
class _Resp:
    def __init__(self, status=400, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.request = None

def _erro(classe, **kw):
    """Instancia a exceção do SDK sem depender da assinatura exata do __init__."""
    e = classe.__new__(classe)
    Exception.__init__(e, kw.get("message", "erro"))
    for k, v in kw.items():
        setattr(e, k, v)
    return e

casos = [
    (_erro(anthropic.BadRequestError, message="PDF com 900 paginas", status_code=400),
     mod.PdfInvalido, "recusou o documento"),
    # Conta sem saldo tambem volta 400. Se cair em PdfInvalido, o usuario vai
    # procurar defeito num PDF que esta perfeito.
    (_erro(anthropic.BadRequestError, status_code=400,
           message="Your credit balance is too low to access the Anthropic API."),
     mod.ErroAuditoriaPdf, "sem saldo"),
    (_erro(anthropic.AuthenticationError, message="invalid x-api-key", status_code=401),
     mod.ConfiguracaoAusente, "inválida ou revogada"),
    (_erro(anthropic.RateLimitError, message="rate limited", status_code=429,
           response=_Resp(429, {"retry-after": "30"})),
     mod.ErroAuditoriaPdf, "30s"),
    (_erro(anthropic.APIStatusError, message="upstream", status_code=503),
     mod.ErroAuditoriaPdf, "indisponível"),
    (_erro(anthropic.APIConnectionError, message="conn"),
     mod.ErroAuditoriaPdf, "Sem conexão"),
]
for excecao, esperada, trecho in casos:
    r, _ = rodar_com(excecao)
    nome = type(excecao).__name__
    check(f"{nome} vira {esperada.__name__}", isinstance(r, esperada), f"{type(r).__name__}: {r}")
    check(f"{nome}: a mensagem explica o que fazer", trecho in str(r), str(r))


# ============================================================================
print("\n=== 6. Relatório em Excel ===")
import openpyxl
wb = openpyxl.load_workbook(mod.gerar_excel(RESPOSTA_OK))
check("duas abas", wb.sheetnames == ["Notas Validadas", "Divergências"], wb.sheetnames)
aba = wb["Notas Validadas"]
check("cabeçalho em português", [c.value for c in aba[1]][:4] == ["Nº Nota", "Emissão", "Entrada", "CNPJ"],
      [c.value for c in aba[1]][:4])
check("uma linha por nota validada", aba.max_row == 2, aba.max_row)
check("o valor foi para a planilha como número", aba.cell(2, 7).value == 1500.50, aba.cell(2, 7).value)
check("uma linha por divergência", wb["Divergências"].max_row == 2)

wb_vazio = openpyxl.load_workbook(mod.gerar_excel({"notas_validadas": [], "divergencias": []}))
check("resultado vazio ainda cria as duas abas",
      wb_vazio.sheetnames == ["Notas Validadas", "Divergências"], wb_vazio.sheetnames)
check("aba vazia mantém o cabeçalho (o usuário lê 'nenhuma', não procura o que não existe)",
      wb_vazio["Divergências"].max_row == 1 and wb_vazio["Divergências"].cell(1, 1).value == "Nº Nota")


# ============================================================================
print("\n=== 7. É Claude, não outro provedor ===")
fonte = io.open(mod.__file__, encoding="utf-8").read()

# Só o CÓDIGO. A docstring do módulo cita Gemini e response_mime_type de
# propósito, explicando o que output_config substitui — isso é documentação útil
# para quem chega vindo da especificação antiga, não chamada a outro provedor.
import ast as _ast
_doc = _ast.get_docstring(_ast.parse(fonte)) or ""
codigo = "\n".join(
    l for l in fonte.replace(_doc, "", 1).splitlines() if not l.strip().startswith("#")
).lower()

for proibido in ["google", "genai", "gemini", "vertex", "openai", "response_mime_type"]:
    check(f"nenhuma chamada a {proibido} no código", proibido not in codigo)
check("importa o SDK oficial da Anthropic", "import anthropic" in fonte)
check("não usa requests/httpx à mão",
      "requests" not in fonte and "httpx" not in fonte)


# ============================================================================
fails = [n for n, ok, _ in results if not ok]
print("\n" + "=" * 60)
print(f"RESULTADO: {len(results)-len(fails)}/{len(results)} passaram")
if fails:
    print("FALHAS:")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("OK")
