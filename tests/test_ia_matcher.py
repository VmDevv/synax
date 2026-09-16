# -*- coding: utf-8 -*-
"""Casamento SPED x XML por IA: só o não conferido, e só com confirmação humana.

Pedido do Sandro em 28/08/2026.

As três travas que este teste existe para proteger:

1. A IA NÃO TOCA NO QUE JÁ ESTÁ CASADO. Os sinais determinísticos (código,
   unitário + quantidade, unitário, posição) continuam mandando; só o que sobra
   deles é oferecido ao modelo. O pior caso da IA é errar onde hoje não se
   confere nada — ela não pode piorar o que funciona.

2. NADA É APLICADO SEM UMA PESSOA CONFIRMAR. Havendo sugestão, a auditoria para
   em `aguardando_ia` e espera a tela. Ignorar devolve o comportamento de
   sempre: os itens ficam em "Itens Não Conferidos".

3. O PAR APLICADO FICA MARCADO em toda divergência que nascer dele. O sistema já
   teve o caso do UF fixo no código, em que uma presunção silenciosa durou
   meses; aqui a marca é obrigatória e o teste trava isso.

Nenhum teste aqui chama a API da OpenAI — a resposta do modelo é dublada. O que
se testa é o contrato em volta dela, que é onde mora o risco.
"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('backend'))

import jinja2  # noqa: E402

from app.services.audit_engine import ia_matcher  # noqa: E402

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


NOTA = {
    "chave": "7" * 44,
    "num": "1842",
    "emit_name": "FORNECEDOR TESTE",
    "sped_itens": [
        {"idx": 3, "cod": "A1", "descr": "SERVO EMBR. 912/914 ONIBUS MB 610/708", "valor": 1970.00},
        {"idx": 5, "cod": "A2", "descr": "SERVO EMBR. MB 1118/1315/1317 MBF015-", "valor": 1970.00},
    ],
    "xml_itens": [
        {"idx": 0, "cod": "X1", "descr": "SERVO MBF-032 - 9A", "valor": 1970.00},
        {"idx": 2, "cod": "X2", "descr": "SERVO MBF-015 - 1E", "valor": 1970.00},
    ],
}


class _Resposta:
    """Dublê do retorno do SDK da OpenAI."""

    def __init__(self, pares, finish_reason="stop"):
        conteudo = json.dumps({"pares": pares})
        self.choices = [type("C", (), {
            "finish_reason": finish_reason,
            "message": type("M", (), {"content": conteudo})(),
        })()]


def _dublar(pares, finish_reason="stop"):
    """Troca o cliente por um que devolve `pares`, e guarda o prompt enviado."""
    enviados = []

    class _Completions:
        def create(self, **kw):
            enviados.append(kw)
            return _Resposta(pares, finish_reason)

    class _Chat:
        completions = _Completions()

    class _Cliente:
        chat = _Chat()

    ia_matcher._cliente = lambda *a, **k: _Cliente()
    os.environ["OPENAI_API_KEY"] = "sk-teste"
    return enviados


_cliente_real = ia_matcher._cliente
_chave_real = os.environ.get("OPENAI_API_KEY")


# ============================================================================
print("\n=== 1. O payload leva só DESCRIÇÃO e VALOR TOTAL ===")

prompt = ia_matcher.montar_prompt(NOTA)
check("a descrição do SPED vai no prompt", "SERVO EMBR. MB 1118/1315/1317" in prompt)
check("a descrição do XML vai no prompt", "SERVO MBF-015 - 1E" in prompt)
check("o valor total vai no prompt", "1970.00" in prompt)
check("o código do produto NÃO vai", "A1" not in prompt and "X1" not in prompt, prompt)
check("os índices do prompt são locais, começando em 0",
      "[0]" in prompt and "[1]" in prompt and "[3]" not in prompt, prompt)


# ============================================================================
print("\n=== 2. Só entra par com confiança e dentro do intervalo ===")

enviados = _dublar([
    {"idx_sped": 0, "idx_xml": 0, "confianca": "media", "justificativa": "por eliminação"},
    {"idx_sped": 1, "idx_xml": 1, "confianca": "alta", "justificativa": "MBF015 casa com MBF-015"},
])
r = ia_matcher.casar_nao_conferidos([NOTA])
check("os dois pares vieram", len(r["sugestoes"]) == 2, r["sugestoes"])
check("confiança 'media' é aceita (foi a nota do par certo por eliminação)",
      any(s["confianca"] == "media" for s in r["sugestoes"]))
check("o índice devolvido é o da nota ORIGINAL, não o do prompt",
      sorted(s["idx_sped"] for s in r["sugestoes"]) == [3, 5],
      [s["idx_sped"] for s in r["sugestoes"]])
check("o índice do XML também é o original",
      sorted(s["idx_xml"] for s in r["sugestoes"]) == [0, 2],
      [s["idx_xml"] for s in r["sugestoes"]])
check("a justificativa acompanha o par",
      any("MBF015" in s["justificativa"] for s in r["sugestoes"]))
check("o schema é enviado como json_schema strict",
      enviados[0]["response_format"]["json_schema"]["strict"] is True)

_dublar([{"idx_sped": 0, "idx_xml": 0, "confianca": "baixa", "justificativa": "chute"}])
check("confiança 'baixa' é descartada",
      ia_matcher.casar_nao_conferidos([NOTA])["sugestoes"] == [])

_dublar([{"idx_sped": 0, "idx_xml": None, "confianca": "alta", "justificativa": "não sei"}])
check("'não sei' (idx_xml null) não vira par",
      ia_matcher.casar_nao_conferidos([NOTA])["sugestoes"] == [])

_dublar([{"idx_sped": 99, "idx_xml": 0, "confianca": "alta", "justificativa": "fora"}])
check("índice fora do intervalo é ignorado",
      ia_matcher.casar_nao_conferidos([NOTA])["sugestoes"] == [])

# Dois itens do SPED disputando o MESMO item do XML: o segundo é descartado.
_dublar([
    {"idx_sped": 0, "idx_xml": 1, "confianca": "alta", "justificativa": "a"},
    {"idx_sped": 1, "idx_xml": 1, "confianca": "alta", "justificativa": "b"},
])
r = ia_matcher.casar_nao_conferidos([NOTA])
check("um item do XML não é usado duas vezes", len(r["sugestoes"]) == 1, r["sugestoes"])


# ============================================================================
print("\n=== 3. Falha da IA não derruba a auditoria ===")


class _ClienteQuebrado:
    class chat:
        class completions:
            @staticmethod
            def create(**kw):
                raise RuntimeError("timeout na API")


ia_matcher._cliente = lambda *a, **k: _ClienteQuebrado()
r = ia_matcher.casar_nao_conferidos([NOTA])
check("erro da API vira registro, não exceção", r["sugestoes"] == [] and len(r["erros"]) == 1,
      r)

_dublar([{"idx_sped": 0, "idx_xml": 0, "confianca": "alta", "justificativa": "x"}],
        finish_reason="length")
r = ia_matcher.casar_nao_conferidos([NOTA])
check("resposta truncada é recusada, não aceita pela metade",
      r["sugestoes"] == [] and len(r["erros"]) == 1, r)

os.environ.pop("OPENAI_API_KEY", None)
check("sem chave, não há sugestão e nada quebra",
      ia_matcher.casar_nao_conferidos([NOTA])["sugestoes"] == [])
check("ia_disponivel() responde não sem chave", ia_matcher.ia_disponivel() is False)
os.environ["OPENAI_API_KEY"] = "sk-teste"
check("ia_disponivel() responde sim com chave", ia_matcher.ia_disponivel() is True)

check("lista vazia não chama a API", ia_matcher.casar_nao_conferidos([])["sugestoes"] == [])

ia_matcher._cliente = _cliente_real
if _chave_real is None:
    os.environ.pop("OPENAI_API_KEY", None)
else:
    os.environ["OPENAI_API_KEY"] = _chave_real


# ============================================================================
print("\n=== 4. O motor só aceita par onde NÃO casou sozinho ===")

FONTE = io.open(os.path.join("backend", "app", "services", "audit_engine",
                             "cross_checker.py"), encoding="utf-8").read()
bloco = FONTE[FONTE.find("linhas_ia = {}"):]
bloco = bloco[:bloco.find("cfop_regra_por_linha")]

check("o par da IA entra depois do casamento determinístico",
      FONTE.find("casar_itens_da_nota(c100[\"items\"]") < FONTE.find("linhas_ia = {}"))
check("par da IA é recusado se o motor já casou aquela linha",
      "_is in pares_da_nota" in bloco, bloco[:600])
check("índice do XML fora do intervalo é recusado",
      "0 <= _ix < len(xml_items_list)" in bloco, bloco[:600])
check("a linha casada por IA fica marcada",
      '"casado_por_ia": True' in bloco)
check("a marca leva confiança e justificativa",
      '"ia_confianca"' in bloco and '"ia_justificativa"' in bloco)
check("execute_audit aceita os pares confirmados",
      "pares_ia: dict | None = None" in FONTE)
check("sem pares_ia o comportamento é o de sempre",
      "if not sped_only_mode and pares_ia:" in bloco)

# A marca precisa chegar nas divergências, não só na linha.
check("a marca é mesclada nas divergências", FONTE.count("**marca_ia,") >= 8,
      FONTE.count("**marca_ia,"))
check("a marca sai do mapa da nota", "marca_ia = linhas_ia.get(idx_item, {})" in FONTE)


# ============================================================================
print("\n=== 5. A pré-análise oferece só o que sobrou ===")

pre = FONTE[FONTE.find("def levantar_nao_conferidos"):]
pre = pre[:pre.find("def _chave_item_xml")]
check("usa o mesmo casamento do motor", "casar_itens_da_nota(" in pre)
check("só oferece item do SPED sem par",
      "sobrando = [i for i in range(len(c100[\"items\"])) if i not in pares]" in pre, pre[:900])
check("só oferece item do XML que ninguém consumiu",
      "usados = {id(x) for x in pares.values()}" in pre)
check("a descrição do SPED vem do 0200",
      "produtos.get(" in pre and 'sped_data.get("products"' in pre)


# ============================================================================
print("\n=== 6. A auditoria para e espera a confirmação ===")

ROTA = io.open(os.path.join("backend", "app", "api", "routes", "uploads.py"),
               encoding="utf-8").read()
check("havendo sugestão, a auditoria não roda ainda",
      'tarefa.status = "aguardando_ia"' in ROTA)
check("as opções da tela inicial são guardadas para a 2ª fase",
      '"considerar_icms_mono": considerar_icms_mono,' in ROTA)
check("a etapa de IA não roda em Modo Somente SPED",
      "if not sped_only_mode and ia_disponivel():" in ROTA)
check("falha na etapa de IA não impede a auditoria",
      "Etapa de IA falhou; auditoria segue sem ela" in ROTA)
check("existe a rota que aplica a decisão",
      '@router.post("/api/audit/{task_id}/aplicar-ia"' in ROTA)
check("'ignorar' descarta tudo sem olhar a seleção",
      'str(acao).strip().lower() == "ignorar"' in ROTA)
check("só o que foi marcado na tela é aplicado",
      "aprovados: list[str] = Form([])" in ROTA and "selecionados.add(i)" in ROTA)
check("índice forjado no formulário é descartado sem erro",
      "0 <= i < len(sugestoes)" in ROTA and "except (TypeError, ValueError)" in ROTA)
check("o campo do form é opcional (Form vazio não vira 422)",
      'acao: str = Form("")' in ROTA)
check("clique repetido não roda a auditoria duas vezes",
      'if task.status != "aguardando_ia":' in ROTA)

# O guard acima só vale se a janela entre ele e a troca de status for curta.
# Gravando as regras ANTES, a janela durava os segundos da escrita no Supabase e
# todo clique repetido passava por ele.
_aplicar = ROTA[ROTA.find("def aplicar_pares_ia("):]
_aplicar = _aplicar[:_aplicar.find("@router.get(\"/api/export")]
check("o status muda ANTES de gravar as regras (fecha a janela do duplo clique)",
      _aplicar.find('task.status = "processando"') < _aplicar.find("_gravar_regras_de_par("),
      f'status={_aplicar.find(chr(34)+"processando"+chr(34))} '
      f'gravar={_aplicar.find("_gravar_regras_de_par(")}')
check("e commita na hora, não no fim",
      "db_session.commit()" in _aplicar[:_aplicar.find("_gravar_regras_de_par(")])
check("o status aguardando_ia renderiza a tela de confirmação",
      "partials/curation/confirmar_ia.html" in ROTA)

# Enquanto espera a decisão, o SPED e os XMLs seguem em disco — quem os apaga é
# o `finally` do execute_audit, que ainda não rodou. Antes desta etapa todo
# upload terminava lá; agora existe um caminho que não termina, e sem prazo uma
# aba fechada deixaria arquivo fiscal parado para sempre.
espera = ROTA[ROTA.find('if task.status == "aguardando_ia":'):]
espera = espera[:espera.find("if task.status in [\"erro\"")]
check("a espera pela confirmação tem prazo", "LIMITE_ESPERA_IA_S" in espera, espera[:400])
check("o prazo é de 1 hora", "LIMITE_ESPERA_IA_S = 3600" in ROTA)
check("vencido o prazo, os arquivos são descartados",
      "shutil.rmtree(os.path.join(TEMP_DIR, task_id)" in espera, espera[:900])
check("vencido o prazo, a tarefa vira timeout", 'task.status = "timeout"' in espera)
check("a mensagem diz o que fazer (enviar de novo)",
      "Envie o SPED e os" in espera)
check("dentro do prazo a tela continua aparecendo",
      'if task.status == "aguardando_ia":' in espera[40:], espera[:900])


# ============================================================================
print("\n=== 7. A tela e o aviso ===")

env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.join("frontend", "templates")), autoescape=True)

tela = env.get_template("partials/curation/confirmar_ia.html").render(
    task_id="t1", sugestoes=[{
        "num": "1842", "emit_name": "FORNECEDOR", "sped_cod": "A2",
        "sped_descr": "SERVO EMBR. MB 1118 MBF015-", "sped_valor": 1970.00,
        "xml_cod": "X2", "xml_descr": "SERVO MBF-015 - 1E", "xml_valor": 1970.00,
        "confianca": "alta", "justificativa": "MBF015 corresponde a MBF-015."}])
check("a tela mostra os dois lados", "SERVO EMBR. MB 1118" in tela and "SERVO MBF-015" in tela)
check("a tela mostra a justificativa", "MBF015 corresponde" in tela)
check("a tela mostra a confiança", "alta" in tela)
check("a tela marca quando os totais batem", "totais iguais" in tela)
# O clique não dava retorno nenhum: a tela ficava idêntica enquanto as regras
# eram gravadas (segundos, contra o Supabase), o usuário clicava de novo, e cada
# clique gravava tudo outra vez. Relato do Sandro em 01/09/2026.
check("existe o botão de aprovar", '"acao": "confirmar"' in tela)
check("existe o botão de descartar tudo", '"acao": "ignorar"' in tela)
check("a ação vai por hx-vals, não pelo botão submit",
      tela.count("hx-vals=") == 2 and 'name="acao"' not in tela)
check("o form não posta sozinho (Enter não dispara sem ação)",
      'onsubmit="return false;"' in tela)
check("os checkboxes viajam por hx-include",
      tela.count('hx-include="#form-confirmar-ia"') == 2)
check("os botões se desabilitam durante a requisição",
      tela.count('hx-disabled-elt="#acoes-ia button"') == 2)
check("há painel de progresso apontado pelo indicador",
      'id="progresso-ia"' in tela and "htmx-indicator" in tela
      and tela.count('hx-indicator="#progresso-ia"') == 2)
check("cada sugestão tem seu próprio checkbox",
      'name="aprovados" value="0"' in tela)
check("o filtro de confiança está na tela", 'id="filtro-confianca"' in tela)
check("há paginação", 'id="paginacao-ia"' in tela and "POR_PAGINA" in tela)
check("esconder linha não tira o checkbox do form",
      "tr.style.display = 'none'" in tela and "input[type=checkbox]:checked" in tela)
check("a tela explica o que acontece ao ignorar", "Itens Não Conferidos" in tela)

TPL = io.open(os.path.join("frontend", "templates", "partials", "curation",
                           "curation_results.html"), encoding="utf-8").read()
check("existe o macro do aviso", "{% macro aviso_ia(item) -%}" in TPL)
check("o aviso tolera resultado antigo sem os campos",
      "item.casado_por_ia|default(false)" in TPL)
check("o aviso aparece em várias abas", TPL.count("{{ aviso_ia(item) }}") >= 6,
      TPL.count("{{ aviso_ia(item) }}"))
check("o aviso usa a nuvem fixed, que escapa do recorte da tabela",
      "tip-nuvem" in TPL and "tip-ancora" in TPL)

resultados = env.get_template("partials/curation/curation_results.html")
base = {"resumo": {"notas_processadas": 1, "inconsistencias": 1, "informativos": 0}}
linha = {"key": "9" * 44, "num": "777", "emit_name": "F", "num_item": "1", "descr": "P",
         "cfop": "1102", "cst_ipi": "50", "reason": "x", "sped_val": 1.0, "xml_val": 2.0}

try:
    html_antigo = resultados.render(result={**base, "ipi_issues": [dict(linha)]},
                                    task_id="t", tenant_id="x", request=None)
    ok_antigo, erro = True, ""
except Exception as exc:  # noqa: BLE001
    ok_antigo, erro = False, f"{type(exc).__name__}: {exc}"
check("resultado gravado ANTES desta feature ainda abre a tela", ok_antigo, erro)
check("e não mostra aviso nenhum",
      ok_antigo and "Item relacionado por IA" not in html_antigo)

com_ia = dict(linha, casado_por_ia=True, ia_confianca="alta",
              ia_justificativa="Totais iguais.", ia_sped_descr="OLEO HIDRAULICO",
              ia_xml_descr="UNI L 10300 OLEO")
html_ia = resultados.render(result={**base, "ipi_issues": [com_ia]},
                            task_id="t", tenant_id="x", request=None)
check("item casado por IA mostra o aviso", "Item relacionado por IA" in html_ia)
check("o aviso destrincha os dois lados",
      "OLEO HIDRAULICO" in html_ia and "UNI L 10300 OLEO" in html_ia)
check("o aviso traz a justificativa", "Totais iguais." in html_ia)


# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

sys.exit(1 if FALHAS else 0)
