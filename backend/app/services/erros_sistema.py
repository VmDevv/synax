"""Os erros do sistema, lidos para dentro do Synax.

Até aqui, ver um erro exigia abrir `logs/erros_ia/` no Desktop da máquina onde o
servidor roda. Isso não é lugar: não funciona de outro computador, some se a
máquina morrer, e num VPS não existe "abrir a pasta".

Este módulo lê o que está em disco e entrega pronto para a tela. Duas fontes,
porque nenhuma sozinha conta a história inteira:

  1. **Os relatórios `.md`** — um por erro, escritos pelo diagnóstico de IA,
     com resumo em uma frase, gravidade e o que fazer. São os bons.
  2. **O `erros_ia.log` cru** — recebe TUDO, inclusive o que não virou
     relatório. E muita coisa não vira: o diagnóstico tem limite de uma chamada
     por erro a cada 15 minutos, e fica desligado inteiro quando não há chave de
     IA no `.env` (foi o caso entre 02 e 03/09/2026). Sem esta segunda fonte, a
     tela mostraria "nenhum erro" justamente nos períodos em que ninguém estava
     olhando.

DUAS TRAVAS QUE NÃO DÃO PARA DEDUZIR DO CÓDIGO
-----------------------------------------------
* **O nome do arquivo vem da URL e NUNCA é concatenado no caminho.** A tela abre
  um relatório por nome; um `..\..\.env` ali dentro leria o arquivo de segredos.
  O nome é conferido contra o padrão exato que o relator gera E contra a lista
  do que existe na pasta — as duas coisas, não uma.

* **O conteúdo é escapado ANTES de virar HTML.** O relatório carrega o texto da
  exceção, e esse texto pode trazer descrição de produto vinda do XML de um
  fornecedor — que é exatamente o vetor de XSS armazenado que este sistema já
  levou uma vez. O renderizador escapa primeiro e só depois aplica negrito,
  título e lista.
"""
from __future__ import annotations

import html
import os
import re
from datetime import datetime

# 2026-09-04_22-10-21__envio-de-arquivos__HTTPException.md
RE_NOME = re.compile(
    r"^(?P<data>\d{4}-\d{2}-\d{2})_(?P<hora>\d{2}-\d{2}-\d{2})"
    r"__(?P<origem>[A-Za-z0-9_-]+)__(?P<tipo>[A-Za-z0-9_]+)\.md$")

# 2026-09-03 14:46:30,830 ERROR backend.app.x — mensagem
RE_LINHA_LOG = re.compile(
    r"^(?P<quando>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})[,.]?\d*\s+"
    r"(?P<nivel>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<origem>[\w.]+)[:\s—-]+(?P<mensagem>.*)$")

NIVEIS_DE_ERRO = ("ERROR", "CRITICAL")

# Gravidade que o diagnóstico escreve, e a cor com que a tela a mostra. A cor
# nunca vai sozinha: o texto da gravidade aparece sempre ao lado.
CORES_GRAVIDADE = {
    "alta": "bg-red-100 text-red-700 border-red-200",
    "crítica": "bg-red-100 text-red-700 border-red-200",
    "critica": "bg-red-100 text-red-700 border-red-200",
    "média": "bg-amber-100 text-amber-700 border-amber-200",
    "media": "bg-amber-100 text-amber-700 border-amber-200",
    "baixa": "bg-slate-100 text-slate-600 border-slate-200",
}


def _diretorio() -> str:
    from ..core.ai_error_reporter import DIRETORIO_ERROS
    return DIRETORIO_ERROS


def _arquivo_log() -> str:
    from ..core.ai_error_reporter import RELATORIO_PATH
    return RELATORIO_PATH


def _secao(texto: str, titulo: str) -> str:
    """Conteúdo de uma seção `## Titulo` do relatório, sem o cabeçalho."""
    marca = f"\n## {titulo}\n"
    ini = texto.find(marca)
    if ini < 0:
        return ""
    resto = texto[ini + len(marca):]
    fim = resto.find("\n## ")
    return (resto if fim < 0 else resto[:fim]).strip()


def _gravidade(texto: str) -> tuple[str, str]:
    """('Média', classe de cor). Vazio quando o relatório não diz."""
    bruto = _secao(texto, "Gravidade")
    if not bruto:
        return "", CORES_GRAVIDADE["baixa"]
    # A linha começa com **Alta**, **Média** ou **Baixa**.
    achado = re.search(r"\*\*([^*]+)\*\*", bruto)
    rotulo = (achado.group(1) if achado else bruto.split("—")[0]).strip()
    chave = rotulo.split()[0].lower().strip(".:,") if rotulo else ""
    return rotulo[:40], CORES_GRAVIDADE.get(chave, CORES_GRAVIDADE["baixa"])


def listar(limite: int = 200) -> list[dict]:
    """Os relatórios de erro, do mais recente para o mais antigo."""
    pasta = _diretorio()
    if not os.path.isdir(pasta):
        return []

    saida = []
    for nome in os.listdir(pasta):
        achado = RE_NOME.match(nome)
        if not achado:
            continue
        p = achado.groupdict()
        try:
            quando = datetime.strptime(p["data"] + " " + p["hora"].replace("-", ":"),
                                       "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue

        caminho = os.path.join(pasta, nome)
        try:
            with open(caminho, encoding="utf-8", errors="replace") as f:
                texto = f.read()
            tamanho = os.path.getsize(caminho)
        except OSError:
            continue

        rotulo, cor = _gravidade(texto)
        saida.append({
            "arquivo": nome,
            "quando": quando,
            "origem": p["origem"].replace("-", " "),
            "tipo": p["tipo"],
            "resumo": _secao(texto, "Em uma frase") or "(sem resumo)",
            "gravidade": rotulo,
            "cor_gravidade": cor,
            "bytes": tamanho,
        })

    saida.sort(key=lambda e: e["quando"], reverse=True)
    return saida[:limite]


def ler(nome: str) -> dict | None:
    """Um relatório, já em HTML seguro. None se o nome não for legítimo.

    A conferência é dupla de propósito: o padrão do nome barra `../`, e a
    presença na listagem garante que só se lê o que o relator escreveu — nem um
    `.md` que alguém tenha deixado na pasta por outro caminho.
    """
    if not RE_NOME.match(nome or ""):
        return None
    disponiveis = {e["arquivo"]: e for e in listar(limite=10000)}
    if nome not in disponiveis:
        return None

    caminho = os.path.join(_diretorio(), nome)
    try:
        with open(caminho, encoding="utf-8", errors="replace") as f:
            texto = f.read()
    except OSError:
        return None

    dados = dict(disponiveis[nome])
    dados["html"] = markdown_seguro(texto)
    return dados


def erros_do_log(limite: int = 60) -> list[dict]:
    """Erros do log cru, do mais recente para o mais antigo.

    É o que preenche o buraco dos relatórios: o diagnóstico só escreve `.md`
    quando há chave de IA e quando o mesmo erro não apareceu nos últimos 15
    minutos. Aqui aparece tudo que foi registrado como ERROR ou CRITICAL.
    """
    caminho = _arquivo_log()
    if not os.path.isfile(caminho):
        return []
    try:
        with open(caminho, encoding="utf-8", errors="replace") as f:
            linhas = f.readlines()
    except OSError:
        return []

    achados = []
    for linha in linhas:
        m = RE_LINHA_LOG.match(linha.rstrip("\n"))
        if not m or m.group("nivel") not in NIVEIS_DE_ERRO:
            continue
        try:
            quando = datetime.strptime(m.group("quando"), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        achados.append({
            "quando": quando,
            "nivel": m.group("nivel"),
            "origem": m.group("origem"),
            "mensagem": m.group("mensagem").strip()[:240],
        })

    achados.sort(key=lambda e: e["quando"], reverse=True)
    return achados[:limite]


def resumo() -> dict:
    """Contagens para o cabeçalho da tela."""
    relatorios = listar()
    crus = erros_do_log(limite=10000)
    hoje = datetime.now().date()

    # Erros iguais repetidos: mesma origem + mesmo tipo. É o que transforma
    # "19 linhas iguais" em "este erro aconteceu 19 vezes".
    repetidos: dict[tuple, int] = {}
    for e in crus:
        chave = (e["origem"], e["mensagem"][:60])
        repetidos[chave] = repetidos.get(chave, 0) + 1
    campeao = max(repetidos.items(), key=lambda kv: kv[1], default=(None, 0))

    return {
        "relatorios": len(relatorios),
        "no_log": len(crus),
        "hoje": sum(1 for e in crus if e["quando"].date() == hoje),
        "ultimo": relatorios[0]["quando"] if relatorios else (
            crus[0]["quando"] if crus else None),
        "mais_repetido": ({"origem": campeao[0][0], "mensagem": campeao[0][1],
                           "vezes": campeao[1]} if campeao[0] and campeao[1] > 1
                          else None),
        "pasta": _diretorio(),
    }


# ------------------------------------------------------------------ markdown

_RE_NEGRITO = re.compile(r"\*\*(.+?)\*\*")
_RE_CODIGO = re.compile(r"`([^`]+)`")


def markdown_seguro(texto: str) -> str:
    """Markdown -> HTML, com o texto ESCAPADO antes de qualquer formatação.

    Não usa biblioteca: os relatórios seguem um gabarito fixo (título, negrito,
    lista, bloco de código) e trazer um pacote novo para isso seria mais uma
    coisa a instalar no servidor.

    A ORDEM importa e é a trava de segurança: escapa-se primeiro, formata-se
    depois. O relatório carrega o texto da exceção, e esse texto já veio de XML
    de fornecedor — um `<script>` na descrição de um produto chegaria aqui
    inteiro se a formatação viesse antes.
    """
    linhas = (texto or "").replace("\r\n", "\n").split("\n")
    saida = []
    em_codigo = False
    em_lista = False

    def formatar(t: str) -> str:
        t = html.escape(t)
        t = _RE_CODIGO.sub(
            r'<code class="px-1 py-0.5 rounded bg-slate-100 text-[12px] '
            r'font-mono text-slate-700">\1</code>', t)
        return _RE_NEGRITO.sub(r"<strong>\1</strong>", t)

    for linha in linhas:
        crua = linha.rstrip()

        if crua.strip().startswith("```"):
            if em_lista:
                saida.append("</ul>")
                em_lista = False
            saida.append("</pre>" if em_codigo else
                         '<pre class="p-3 rounded-lg bg-slate-900 text-slate-100 '
                         'text-[12px] font-mono overflow-x-auto whitespace-pre-wrap">')
            em_codigo = not em_codigo
            continue

        if em_codigo:
            saida.append(html.escape(crua))
            continue

        if not crua.strip():
            if em_lista:
                saida.append("</ul>")
                em_lista = False
            continue

        if crua.startswith("### "):
            saida.append(f'<h4 class="font-bold text-slate-700 mt-4 mb-1">'
                         f'{formatar(crua[4:])}</h4>')
        elif crua.startswith("## "):
            saida.append(f'<h3 class="font-bold text-[15px] mt-5 mb-2" '
                         f'style="color: var(--syn-900);">{formatar(crua[3:])}</h3>')
        elif crua.startswith("# "):
            saida.append(f'<h2 class="font-black text-lg mb-3 text-slate-800">'
                         f'{formatar(crua[2:])}</h2>')
        elif crua.strip() in ("---", "***"):
            saida.append('<hr class="my-4 border-slate-200">')
        elif re.match(r"^\s*[-*]\s+", crua) or re.match(r"^\s*\d+[.)]\s+", crua):
            if not em_lista:
                saida.append('<ul class="list-disc pl-5 space-y-1 my-2">')
                em_lista = True
            item = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", crua)
            saida.append(f'<li class="text-slate-600">{formatar(item)}</li>')
        else:
            if em_lista:
                saida.append("</ul>")
                em_lista = False
            saida.append(f'<p class="text-slate-600 mb-2 leading-relaxed">'
                         f'{formatar(crua)}</p>')

    if em_lista:
        saida.append("</ul>")
    if em_codigo:
        saida.append("</pre>")
    return "\n".join(saida)
