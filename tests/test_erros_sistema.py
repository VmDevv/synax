# -*- coding: utf-8 -*-
"""Erros do sistema visíveis DENTRO do Synax — e as duas travas que isso exige.

Até 04/09/2026, ver um erro exigia abrir `logs/erros_ia/` no Desktop da máquina
onde o servidor roda. Não serve de outro computador, some se a máquina morrer, e
num VPS não existe "abrir a pasta". Agora há uma aba em Administração.

Ler arquivo do disco e mostrá-lo numa página traz dois riscos reais, e são eles
que esta suíte protege:

1. **Travessia de caminho.** O nome do arquivo vem da URL. Um `../../.env` ali
   dentro leria o arquivo de segredos e o mostraria na tela. A conferência é
   dupla: o nome tem de casar com o padrão exato que o relator gera E estar na
   listagem do que existe na pasta.

2. **XSS armazenado.** O relatório carrega o texto da exceção, e esse texto já
   veio de XML de fornecedor — descrição de produto é dado de terceiro. Este
   sistema já levou um XSS por esse caminho (ver core/templating.py). O
   renderizador de Markdown escapa ANTES de formatar; se a ordem inverter, um
   `<script>` na descrição de um produto chega inteiro ao navegador do admin.

A terceira trava é de honestidade, não de segurança:

3. **A tela mostra o log cru, não só os relatórios.** O diagnóstico por IA só
   escreve um relatório por erro a cada 15 minutos, e nenhum quando falta chave
   no `.env` — ficou desligado entre 02 e 03/09/2026. Se a tela mostrasse apenas
   os relatórios, diria "nenhum erro" justamente nos períodos sem vigilância.
"""
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('backend'))

from app.services import erros_sistema as es  # noqa: E402

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
# Pasta de mentira, com um relatório no formato real e um arquivo de segredo ao
# lado — que é o que a travessia tentaria alcançar.
# ============================================================================
BASE = tempfile.mkdtemp(prefix="synax_erros_")
PASTA = os.path.join(BASE, "erros_ia")
os.makedirs(PASTA)
io.open(os.path.join(BASE, ".env"), "w", encoding="utf-8").write(
    "SUPABASE_SERVICE_KEY=segredo-que-nao-pode-vazar\n")

RELATORIO = """# envio-de-arquivos — HTTPException

**Quando:** 04/09/2026 às 22:10:21
**Onde:** backend.app.api.routes.uploads
**Nível:** ERROR

---

## Em uma frase
O sistema bloqueou a gravação do mapeamento porque o usuário não estava logado.

## Gravidade
**Média** — deve ser resolvido, mas não é emergência.

## Como corrigir
- Refazer o login
- Tentar de novo

## Mensagem original do sistema

```
HTTPException: 401
```
"""
io.open(os.path.join(PASTA, "2026-09-04_22-10-21__envio-de-arquivos__HTTPException.md"),
        "w", encoding="utf-8").write(RELATORIO)
io.open(os.path.join(PASTA, "2026-09-01_15-21-32__main__OperationalError.md"),
        "w", encoding="utf-8").write(
    RELATORIO.replace("## Gravidade\n**Média**", "## Gravidade\n**Alta**")
             .replace("Em uma frase\nO sistema bloqueou",
                      "Em uma frase\nO banco de dados não respondeu"))
# Arquivo que NÃO segue o padrão do relator: não pode ser listado nem lido.
io.open(os.path.join(PASTA, "anotacao.md"), "w", encoding="utf-8").write("# nota solta")

LOG = os.path.join(BASE, "erros_ia.log")
io.open(LOG, "w", encoding="utf-8").write(
    "2026-09-03 14:46:30,830 ERROR backend.app.api.routes.uploads — Falha ao gravar mapeamento\n"
    "Traceback (most recent call last):\n"
    "  File \"x.py\", line 1, in y\n"
    "2026-09-03 15:00:00,000 ERROR backend.app.api.routes.uploads — Falha ao gravar mapeamento\n"
    "2026-09-01 15:21:32,100 ERROR backend.app.main — Não foi possível preparar o esquema\n"
    "2026-09-01 10:00:00,000 INFO backend.app.main — subiu normalmente\n"
    "2026-09-01 11:00:00,000 WARNING backend.app.core.rate_limit — Rate limit atingido\n"
)

es._diretorio = lambda: PASTA
es._arquivo_log = lambda: LOG


# ============================================================================
print("\n=== 1. Travessia de caminho: o nome vem da URL ===")

ATAQUES = [
    "../.env", "..\\.env", "../../.env",
    "%2e%2e%2f.env", "....//.env",
    "/etc/passwd", "C:\\Windows\\win.ini",
    "2026-09-04_22-10-21__envio-de-arquivos__HTTPException.md/../../.env",
    "../erros_ia/../.env",
    "anotacao.md",                      # existe, mas não é do relator
    "", None,
]
for tentativa in ATAQUES:
    check(f"recusa {tentativa!r}", es.ler(tentativa) is None,
          "LEU um arquivo que não devia")

check("o .env de teste realmente está lá (senão o teste não prova nada)",
      os.path.exists(os.path.join(BASE, ".env")))
check("e o arquivo fora do padrão não aparece nem na listagem",
      "anotacao.md" not in [e["arquivo"] for e in es.listar()])

bom = "2026-09-04_22-10-21__envio-de-arquivos__HTTPException.md"
check("o relatório legítimo É lido", es.ler(bom) is not None)


# ============================================================================
print("\n=== 2. XSS: escapa ANTES de formatar ===")

VENENO = """# Erro

## Em uma frase
Produto <script>alert('xss')</script> deu erro.

## Detalhe
Descrição vinda do XML: <img src=x onerror=alert(1)> e **negrito** de verdade.

```
<script>alert('no bloco de codigo')</script>
```
"""
saida = es.markdown_seguro(VENENO)
check("a tag <script> não sai crua",
      "<script>" not in saida, "XSS: script cru na saída")
check("ela aparece escapada", "&lt;script&gt;" in saida)
check("o onerror também não passa",
      "<img src=x onerror" not in saida, "XSS por atributo")
check("dentro de bloco de código também é escapado",
      saida.count("&lt;script&gt;") >= 2, saida.count("&lt;script&gt;"))
check("mas o negrito legítimo continua funcionando",
      "<strong>negrito</strong>" in saida, "formatação quebrou")
check("e o título vira <h2>", "<h2" in saida)

check("aspas e & também são escapados",
      "&amp;" in es.markdown_seguro("a & b") and
      "&#x27;" in es.markdown_seguro("a 'b'"))


# ============================================================================
print("\n=== 3. A listagem lê o que interessa do relatório ===")

lista = es.listar()
check("os dois relatórios do padrão aparecem", len(lista) == 2, len(lista))
check("do mais recente para o mais antigo",
      lista[0]["quando"] > lista[1]["quando"])

e = lista[0]
check("a data sai do nome do arquivo",
      e["quando"].strftime("%d/%m/%Y %H:%M:%S") == "04/09/2026 22:10:21",
      e["quando"])
check("a origem também, sem os hífens",
      e["origem"] == "envio de arquivos", e["origem"])
check("e o tipo da exceção", e["tipo"] == "HTTPException", e["tipo"])
check("o resumo de uma frase é extraído",
      e["resumo"].startswith("O sistema bloqueou"), e["resumo"][:40])
check("a gravidade também", e["gravidade"] == "Média", e["gravidade"])
check("gravidade alta tem cor própria",
      "red" in lista[1]["cor_gravidade"], lista[1]["cor_gravidade"])
check("e média não usa a mesma cor de alta",
      e["cor_gravidade"] != lista[1]["cor_gravidade"])


# ============================================================================
print("\n=== 4. O log cru: o que NÃO virou relatório ===")

crus = es.erros_do_log()
check("só ERROR e CRITICAL entram", len(crus) == 3, len(crus))
check("INFO fica de fora", not any("subiu normalmente" in c["mensagem"] for c in crus))
check("WARNING também", not any("Rate limit" in c["mensagem"] for c in crus))
check("as linhas de traceback não viram erro solto",
      not any("Traceback" in c["mensagem"] for c in crus))
check("do mais recente para o mais antigo",
      crus[0]["quando"] > crus[-1]["quando"])
check("guarda o módulo de origem",
      crus[0]["origem"].startswith("backend.app"), crus[0]["origem"])


# ============================================================================
print("\n=== 5. O resumo do cabeçalho ===")

r = es.resumo()
check("conta os relatórios", r["relatorios"] == 2, r["relatorios"])
check("e os erros do log", r["no_log"] == 3, r["no_log"])
check("aponta o erro que mais se repete",
      r["mais_repetido"] and r["mais_repetido"]["vezes"] == 2,
      r["mais_repetido"])
check("erro que aconteceu uma vez só não é 'repetido'",
      es.resumo()["mais_repetido"]["vezes"] > 1)
check("diz onde os arquivos ficam", r["pasta"] == PASTA)


# ============================================================================
print("\n=== 6. Nada disso derruba a tela ===")

es._diretorio = lambda: os.path.join(BASE, "nao-existe")
es._arquivo_log = lambda: os.path.join(BASE, "nao-existe.log")
check("pasta inexistente devolve lista vazia", es.listar() == [])
check("log inexistente também", es.erros_do_log() == [])
check("e o resumo não levanta", es.resumo()["relatorios"] == 0)
check("ler de pasta inexistente devolve None", es.ler(bom) is None)
check("markdown vazio não quebra", es.markdown_seguro("") == "")
check("markdown None não quebra", es.markdown_seguro(None) == "")


# ============================================================================
print("\n=== 7. Ligação com a tela ===")

ADMIN = io.open(os.path.join("backend", "app", "api", "routes", "admin.py"),
                encoding="utf-8").read()
check("a rota da lista existe", '@router.get("/erros"' in ADMIN)
check("e a do detalhe", '@router.get("/erros/{arquivo}"' in ADMIN)
check("o detalhe devolve 404 para nome inválido, não 500",
      "status_code=404" in ADMIN)

MAIN = io.open(os.path.join("backend", "app", "main.py"), encoding="utf-8").read()
check("o /admin inteiro (e portanto /admin/erros) é só de admin",
      'prefix="/admin", dependencies=[Depends(require_admin)]' in MAIN)

PAGINA = io.open(os.path.join("frontend", "templates", "pages", "admin.html"),
                 encoding="utf-8").read()
check("a aba aparece em Administração", 'hx-get="/admin/erros"' in PAGINA)

LISTA = io.open(os.path.join("frontend", "templates", "partials", "admin",
                             "erros_list.html"), encoding="utf-8").read()
check("a tela mostra as DUAS fontes",
      "Com diagnóstico" in LISTA and "Registro completo" in LISTA)
check("e explica por que o log cru existe",
      "15 minutos" in LISTA and "chave de IA" in LISTA)

DET = io.open(os.path.join("frontend", "templates", "partials", "admin",
                           "erro_detalhe.html"), encoding="utf-8").read()
check("o modal usa | safe (o HTML já vem escapado da origem)",
      "erro.html | safe" in DET)
check("e o comentário diz POR QUE isso é seguro",
      "escapa ANTES" in DET or "já vem escapado" in DET)


shutil.rmtree(BASE, ignore_errors=True)

# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

sys.exit(1 if FALHAS else 0)
