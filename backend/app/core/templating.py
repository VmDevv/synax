"""Ambiente Jinja2 único da aplicação.

MOTIVO DE EXISTIR: cada módulo de rota criava seu próprio `Environment(...)`.
O construtor puro do Jinja2 vem com `autoescape=False` — ao contrário do padrão
do FastAPI — então TODO valor renderizado saía sem escape de HTML.

Isso é XSS armazenado: a descrição de produto vem do XML da nota emitida por um
fornecedor (terceiro), e é exibida nas telas de Curadoria e de Regras. Um
`<script>` no campo `xProd` executaria no navegador do admin.

Use `build_templates()` em qualquer rota nova. Onde o HTML for intencional
(ex.: corpo do e-mail montado no servidor), marque explicitamente com `| safe`
no template.
"""
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .fuso import hora_br

TEMPLATES_DIR = "frontend/templates"


def numero_br(valor) -> str:
    """1234567 -> "1.234.567". No Brasil o separador de milhar é o ponto.

    Jinja não tem formatação por idioma, e `{{ n }}` cru imprime 1234567 — que
    num painel de contagem é justamente o número que ninguém lê de relance.
    """
    try:
        return f"{int(valor):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "0"


def moeda_br(valor) -> str:
    """1234567.8 -> "1.234.567,80". Ponto separa milhar, vírgula separa centavo.

    `"%.2f"|format(v)` do Jinja imprime 37734.35 — que num relatório fiscal
    brasileiro lê-se errado à primeira vista, porque o ponto ali parece milhar.
    """
    try:
        inteiro = "{:,.2f}".format(float(valor or 0))
    except (TypeError, ValueError):
        return "0,00"
    return inteiro.replace(",", "@").replace(".", ",").replace("@", ".")


def build_env() -> Environment:
    # Ambiente único da aplicação; o autoescape está ligado logo abaixo.
    env = Environment(  # nosemgrep
        loader=FileSystemLoader(TEMPLATES_DIR, encoding="utf-8"),
        autoescape=select_autoescape(
            enabled_extensions=("html", "htm", "xml", "jinja"),
            default_for_string=True,
            default=True,
        ),
    )
    env.filters["num_br"] = numero_br
    env.filters["moeda_br"] = moeda_br
    # `created_at` é UTC no banco inteiro; sem este filtro a tela mostra três
    # horas a mais do que o relógio de quem está olhando.
    env.filters["hora_br"] = hora_br
    return env


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    templates.env = build_env()
    return templates
