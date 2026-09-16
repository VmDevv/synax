"""O resumo executivo da correção — a IA ESCREVE, ela não calcula.

Quem lê o relatório de uma retificação normalmente não acompanhou a tela: recebe
uma planilha com 40 linhas de "campo X: de A para B" e precisa entender, em
trinta segundos, o que foi feito e quanto isso mexeu no crédito do mês. É esse
parágrafo que falta.

**A separação que sustenta o módulo:** os números saem de `fatos()`, que só lê o
`Relatorio` já apurado pelo motor; o modelo recebe esses números prontos e tem
uma tarefa só, redigir. Pedir para ele somar seria trocar aritmética conferível
por texto plausível — e num relatório fiscal o erro passaria despercebido
justamente por estar bem escrito.

E ainda assim não se confia: `_so_usa_os_numeros_dados()` confere que todo
número no texto devolvido é um dos que foram enviados. Apareceu um que ninguém
mandou — porcentagem inventada, valor "arredondado" —, o texto do modelo é
descartado e vale o resumo determinístico. O sistema nunca fica sem resumo.

Sem `OPENAI_API_KEY`, com a API fora do ar ou com resposta truncada, o
`resumo_local()` responde e o relatório sai igual. Mesma regra do resto do
sistema: fechar a apuração do mês não pode depender de um serviço externo.

**Por que GPT e não Claude aqui:** é onde o escritório tem crédito. A primeira
versão chamava a Anthropic e, no primeiro uso real (09/09/2026), devolveu 400 —
saldo zerado. O resumo é a parte mais dispensável do relatório e não vale uma
segunda conta para manter; as integrações Anthropic do sistema (auditor de PDF
de NFS-e e reporter de erros) continuam onde estão. Trocar de volta é a variável
`CORRECAO_IA_MODELO`, mas o cliente aqui é o da OpenAI: mudar de provedor é
mudar este módulo, não só o nome do modelo.
"""
from __future__ import annotations

import logging
import os
import re
from decimal import Decimal

logger = logging.getLogger(__name__)

# O mesmo modelo do casamento de itens por IA (`ia_matcher`), pela mesma razão:
# é a conta que o escritório mantém com crédito. Trocável por ambiente.
MODELO = os.environ.get("CORRECAO_IA_MODELO",
                        os.environ.get("OPENAI_MODELO", "gpt-5.1"))

# Três frases não justificam esperar mais que isso. O relatório não fica
# bloqueado: estourou, sai o resumo local.
TIMEOUT_S = 60

INSTRUCAO = (
    "Você escreve o resumo executivo de uma retificação de SPED Fiscal para um "
    "contador brasileiro.\n\n"
    "REGRAS:\n"
    "- Use EXCLUSIVAMENTE os números que estão nos dados. Não some, não calcule, "
    "não arredonde e não crie percentuais.\n"
    "- Escreva de 2 a 4 frases corridas, em português do Brasil, tom técnico e "
    "direto. Sem saudação, sem markdown, sem lista, sem título.\n"
    "- Diga o que foi corrigido (por tipo), quanto o crédito de cada imposto "
    "mudou e, se houver, quantas correções foram recusadas.\n"
    "- Não afirme que a apuração (Bloco E) foi alterada: ela não é reescrita "
    "pelo sistema."
)


def _moeda(valor) -> str:
    """1234.5 -> "1.234,50" — o mesmo formato que a tela e a planilha mostram."""
    try:
        bruto = "{:,.2f}".format(float(valor or 0))
    except (TypeError, ValueError):
        return "0,00"
    return bruto.replace(",", "@").replace(".", ",").replace("@", ".")


def fatos(relatorio) -> dict:
    """Os números da correção, já apurados pelo motor. Nada é calculado aqui.

    Contar por CAMPO, e não por linha, é o que faz o resumo dizer algo: "12
    alterações" não distingue doze CFOPs de doze centavos de IPI.
    """
    por_campo: dict[str, int] = {}
    aceitas = cascata = 0
    for doc in relatorio.documentos:
        for a in doc.aceitas:
            por_campo[a.campo] = por_campo.get(a.campo, 0) + 1
            aceitas += 1
        cascata += len(doc.cascata)

    imp = relatorio.impacto
    return {
        "documentos": len(relatorio.documentos),
        "correcoes_aceitas": aceitas,
        "alteracoes_em_cascata": cascata,
        "alteracoes_por_campo": por_campo,
        "recusas": len(relatorio.recusas),
        "competencia": relatorio.competencia or "",
        "icms_credito_antes": _moeda(imp.icms_creditos_antes),
        "icms_credito_depois": _moeda(imp.icms_creditos_depois),
        "icms_diferenca": _moeda(imp.icms_diferenca),
        "ipi_credito_antes": _moeda(imp.ipi_creditos_antes),
        "ipi_credito_depois": _moeda(imp.ipi_creditos_depois),
        "ipi_diferenca": _moeda(imp.ipi_diferenca),
    }


def resumo_local(f: dict) -> str:
    """O resumo sem IA. Existe para o relatório nunca sair sem um.

    É também a rede de segurança do texto do modelo: quando o que ele escreve
    não passa na conferência dos números, é este que vai para o relatório.
    """
    campos = f.get("alteracoes_por_campo") or {}
    partes = [f"{qtd} em {campo}" for campo, qtd in sorted(campos.items())]
    corpo = (f"Foram aplicadas {f['correcoes_aceitas']} correção(ões) em "
             f"{f['documentos']} documento(s)"
             + (f" ({', '.join(partes)})" if partes else "")
             + f", além de {f['alteracoes_em_cascata']} alteração(ões) feitas "
             "pelo próprio leiaute.")

    icms = (f" O crédito de ICMS passou de R$ {f['icms_credito_antes']} para "
            f"R$ {f['icms_credito_depois']} (diferença de R$ {f['icms_diferenca']}).")
    ipi = ""
    if f["ipi_diferenca"] != "0,00":
        ipi = (f" O de IPI passou de R$ {f['ipi_credito_antes']} para "
               f"R$ {f['ipi_credito_depois']} (diferença de R$ {f['ipi_diferenca']}).")
    recusa = (f" {f['recusas']} documento(s) tiveram a correção recusada."
              if f["recusas"] else "")
    return corpo + icms + ipi + recusa


def _numeros(texto: str) -> set:
    """Todo número do texto, normalizado para comparação."""
    return {n.strip(".,") for n in re.findall(r"\d[\d.,]*", texto or "")}


def _permitidos(f: dict) -> set:
    """Os números que o modelo PODE escrever: os que foram enviados a ele."""
    permitidos = set()
    for chave, valor in f.items():
        if chave == "alteracoes_por_campo":
            permitidos |= {str(v) for v in valor.values()}
        elif isinstance(valor, int):
            permitidos.add(str(valor))
        elif isinstance(valor, str) and valor:
            permitidos |= _numeros(valor)
    # Zero é sempre aceitável: "nenhuma recusa", "sem alteração de IPI".
    permitidos |= {"0", "0,00"}
    return permitidos


def _so_usa_os_numeros_dados(texto: str, f: dict) -> bool:
    return _numeros(texto) <= _permitidos(f)


class ResumoIndisponivel(Exception):
    """A IA não escreveu, e o motivo é conhecido e explicável.

    Existe para separar o esperado do inesperado: conta sem crédito, chave
    ausente e limite de uso são condições de configuração, não defeitos — vão
    para o log em uma linha, com o que fazer a respeito, e não como traceback.
    O traceback fica reservado para o que ninguém previu.
    """


def _chamar_modelo(f: dict) -> str:
    """Uma chamada, texto puro. Levanta `ResumoIndisponivel` com o porquê."""
    import json

    from openai import OpenAI  # import tardio: sem chave, ninguém paga o import

    chave = os.environ.get("OPENAI_API_KEY")
    if not chave:
        raise ResumoIndisponivel(
            "a chave da OpenAI (OPENAI_API_KEY) não está configurada "
            "neste servidor")

    cliente = OpenAI(api_key=chave, timeout=TIMEOUT_S)
    resposta = cliente.chat.completions.create(
        model=MODELO,
        messages=[
            {"role": "system", "content": INSTRUCAO},
            {"role": "user",
             "content": ("Dados da correção:\n"
                         + json.dumps(f, ensure_ascii=False, indent=2))},
        ],
    )

    escolha = resposta.choices[0]
    # Resposta cortada no meio não é resumo: o parágrafo terminaria numa frase
    # pela metade dentro de um relatório fiscal. Mesmo critério do `ia_matcher`.
    if escolha.finish_reason == "length":
        raise ResumoIndisponivel("a resposta do modelo veio cortada pelo limite "
                                 "de tokens")
    return (escolha.message.content or "").strip()


def _motivo_da_falha(erro: Exception) -> str:
    """Traduz a falha da API para uma frase que diz o que fazer.

    A mais comum não é defeito nenhum: conta sem crédito. Na OpenAI ela chega
    como 429 com `insufficient_quota` — indistinguível, à primeira vista, de um
    pico de uso —, e a mensagem crua só aparece no traceback do servidor. Quem
    está na tela via um resumo genérico e não sabia por quê.
    """
    from openai import (APIConnectionError, APIStatusError,
                        AuthenticationError, BadRequestError, RateLimitError)

    texto = str(getattr(erro, "message", "") or erro).lower()

    if isinstance(erro, AuthenticationError):
        return "a chave da OpenAI é inválida ou foi revogada"
    if isinstance(erro, RateLimitError):
        if "quota" in texto or "billing" in texto:
            return ("a conta da OpenAI está sem créditos "
                    "(platform.openai.com > Billing)")
        return "o limite de uso da API da OpenAI foi atingido"
    if isinstance(erro, BadRequestError):
        return "a API da OpenAI recusou a requisição"
    if isinstance(erro, APIConnectionError):
        return "não foi possível falar com a API da OpenAI"
    if isinstance(erro, APIStatusError):
        return "a API da OpenAI respondeu com erro"
    return ""


def resumir(relatorio) -> tuple[str, bool, str]:
    """(texto, veio da IA, motivo de não ter vindo). Nunca levanta.

    O booleano existe para a tela poder MARCAR o que foi escrito por modelo —
    mesma regra do par casado por IA na curadoria: o que a máquina sugeriu tem
    de aparecer identificado, não se misturar ao que o motor apurou.

    O `motivo` existe porque "gerado pelo sistema" sozinho não diz nada a quem
    esperava o resumo: sem ele, descobrir que a conta está sem crédito exige
    abrir o log do servidor.
    """
    f = fatos(relatorio)
    local = resumo_local(f)

    try:
        texto = _chamar_modelo(f)
    except ResumoIndisponivel as e:
        logger.warning("Resumo por IA indisponível (%s); usando o local.", e)
        return local, False, str(e)
    except Exception as e:
        # Modelo fora do ar não pode impedir o download de um relatório fiscal.
        motivo = ""
        try:
            motivo = _motivo_da_falha(e)
        except Exception:
            pass
        if motivo:
            logger.warning("Resumo por IA indisponível (%s); usando o local.",
                           motivo)
        else:
            logger.warning("Não foi possível gerar o resumo por IA; usando o local.",
                           exc_info=True)
            motivo = "a API da Anthropic não respondeu"
        return local, False, motivo

    if not texto:
        return local, False, "o modelo devolveu uma resposta vazia"
    if not _so_usa_os_numeros_dados(texto, f):
        logger.warning("Resumo por IA descartado: trouxe número que não foi "
                       "enviado. Texto: %s", texto[:300])
        return local, False, ("o texto do modelo trazia número que o sistema "
                              "não enviou, e foi descartado")
    return texto, True, ""
