"""Auditoria de notas em PDF pela IA da Anthropic (Claude).

O PDF cru vai inteiro para o modelo multimodal — nada de PyPDF/pdfplumber. Um
extrator de texto devolve a página achatada numa string e perde a estrutura
visual: qual número está em qual coluna, qual linha da tabela de resumo
corresponde a qual nota detalhada. É exatamente essa correspondência que a
auditoria precisa enxergar, então quem lê o documento é o modelo, olhando a
página como ela é.

A saída é travada por `output_config.format` (JSON Schema): a API garante um
JSON válido e aderente ao schema, sem cerca de markdown para limpar e sem
campo faltando. É o equivalente ao `response_mime_type="application/json"` do
Gemini, só que com validação do formato inteiro, não só do tipo.

O processo é AUTÔNOMO: a IA lê do próprio PDF os dois lados da operação —
prestador e TOMADOR, com as duas UFs. O CFOP (1933 interno / 2933
interestadual) sai da comparação entre elas, sem consultar cadastro nenhum.
Antes a UF do tomador vinha do banco (`Tenant.uf`), o que exigia escolher a
empresa na tela e travava em 170 empresas sem UF cadastrada.

Sem `ANTHROPIC_API_KEY` configurada o módulo levanta `ConfiguracaoAusente` na
chamada — o resto do sistema não importa nada daqui na inicialização.
"""
import base64
import io
import json
import logging
import os

import anthropic

logger = logging.getLogger(__name__)

# Sonnet 5 e nao Opus 5: um PDF escaneado de 60 paginas custa perto de 150 mil
# tokens de entrada, e a diferenca de preco (US$ 2/M contra US$ 5/M na entrada,
# US$ 10/M contra US$ 25/M na saida) pesa em cada auditoria. Escolha do Sandro
# em 26/08/2026, por custo. Haiku 4.5 seria mais barato ainda, mas o contexto e
# de 200K -- um PDF grande nao cabe -- e errar um valor lido de digitalizacao
# custa mais que o token economizado.
MODELO_PADRAO = os.environ.get("PDF_IA_MODELO", "claude-sonnet-5")

# A chave da Anthropic sai do AMBIENTE, e só de lá.
#
# De 26/08 a 10/09/2026 existiu aqui uma constante com a chave em texto puro,
# posta para destravar o teste da tela com o combinado de remover depois. Ela
# valia para quem tivesse o arquivo — commit, backup, cópia de pasta, print — e
# foi apagada junto com a rotação da chave. Se voltar a faltar configuração, o
# certo é a auditoria de PDF recusar com `ConfiguracaoAusente`: parar é
# recuperável, chave em código não.

# Dois caminhos para o mesmo PDF, escolhidos pelo tamanho:
#
# - Inline (base64 no corpo da requisição): uma chamada só, nada a limpar
#   depois. Mas o corpo da requisição é limitado a 32 MB e o base64 infla ~33%,
#   então só serve para PDF pequeno.
# - Files API: sobe o arquivo antes e a mensagem referencia o `file_id`. Aceita
#   até 500 MB. Custa uma volta a mais e o arquivo FICA hospedado na Anthropic
#   até ser apagado — por isso `_chamar_modelo` apaga no `finally`. PDF de nota
#   fiscal é documento de cliente; não fica guardado fora daqui.
#
# Os PDFs reais do escritório são digitalizações (uma imagem por página) e
# passam fácil dos 30 MB — o caminho normal é o da Files API.
LIMITE_INLINE_BYTES = 20 * 1024 * 1024
MAX_PDF_BYTES = 450 * 1024 * 1024

# Limite de páginas da API. Não dá para contar página com confiança sem uma
# biblioteca de PDF, então quem recusa é a própria API — o 400 vira mensagem
# legível em `auditar_pdf`.
MAX_PAGINAS = 600

BETA_FILES_API = "files-api-2025-04-14"

# Saída longa: um PDF com centenas de notas gera um JSON grande, e os tokens de
# raciocínio saem do MESMO orçamento. Com 64000 o PDF real de 62 páginas bateu
# no teto e a resposta veio truncada (o motor recusou, em vez de entregar meia
# lista). 128000 é o máximo do modelo e só é cobrado o que for usado.
# Requisição não-streaming com max_tokens alto estoura o timeout HTTP do SDK,
# então a chamada é sempre por streaming (ver `_chamar_modelo`).
MAX_TOKENS = 128000

# Extrair número de uma digitalização é trabalho mecânico, não raciocínio: com
# esforço alto o modelo gastava o orçamento pensando antes de escrever o JSON.
# `low` corta esse gasto — que é o que mais pesa na conta — sem mexer na
# leitura em si.
ESFORCO = os.environ.get("PDF_IA_ESFORCO", "low")


PROMPT_AUDITORIA = """Você é um assistente de auditoria fiscal corporativa. Analise este documento PDF contendo Notas Fiscais de Serviço.

Extraia os dados de todas as notas fiscais presentes no documento. Se houver apenas uma tabela de resumo, aceite-a como verdade.

Para CADA nota, leia os dois lados da operação:
- PRESTADOR (quem emitiu a nota / fornecedor do serviço): CNPJ, nome e UF.
- TOMADOR (quem contratou o serviço, no cabeçalho da nota ou do resumo): CNPJ e UF do endereço.

A UF do tomador é o que define se a operação é interna ou interestadual, então leia-a do documento com atenção. Se um campo não estiver no documento, devolva "" (ou 0.0 nos valores) em vez de inventar.

Se você tiver o Resumo E a Nota Detalhada da mesma operação e os valores ou números NÃO baterem, coloque essa nota em "divergencias" em vez de "notas_validadas", descrevendo exatamente o que não bateu.

Retorne EXATAMENTE E APENAS um objeto JSON válido com esta estrutura (sem markdown):
{
  "notas_validadas": [
    {
      "cnpj_prestador": "string (apenas numeros)",
      "fornecedor": "string (nome do prestador)",
      "uf_prestador": "string (ex: ES, RJ, SP)",
      "cnpj_tomador": "string (apenas numeros - extraído do cabeçalho da nota/resumo)",
      "uf_tomador": "string (ex: ES, RJ, SP - extraído do endereço do tomador)",
      "data_entrada": "DD/MM/AAAA",
      "data_emissao": "DD/MM/AAAA",
      "num_nota": "string",
      "valor": 1500.50,
      "retencao_ir": 0.0,
      "retencao_pis": 0.0,
      "retencao_cofins": 0.0,
      "retencao_csll": 0.0
    }
  ],
  "divergencias": [
    {
      "cnpj_prestador": "string (apenas numeros)",
      "num_nota": "string (ou null)",
      "data_emissao": "DD/MM/AAAA",
      "motivo_divergencia": "O que não bateu entre o resumo e a nota detalhada"
    }
  ]
}"""


# O schema é a mesma estrutura do prompt, agora como contrato executável. Com
# `additionalProperties: false` e `required` completo, a API recusa devolver
# nota sem campo — o "deixe vazio/zerado" da REGRA 4 vira garantia, não pedido.
SCHEMA_RESPOSTA = {
    "type": "object",
    "properties": {
        "notas_validadas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cnpj_prestador": {"type": "string", "description": "apenas numeros"},
                    "fornecedor": {"type": "string", "description": "nome do prestador"},
                    "uf_prestador": {"type": "string", "description": "sigla de 2 letras, ex: ES, RJ, SP"},
                    "cnpj_tomador": {"type": "string", "description": "apenas numeros, do cabecalho da nota"},
                    "uf_tomador": {"type": "string", "description": "sigla de 2 letras, do endereco do tomador"},
                    "data_entrada": {"type": "string", "description": "DD/MM/AAAA"},
                    "data_emissao": {"type": "string", "description": "DD/MM/AAAA"},
                    "num_nota": {"type": "string"},
                    "valor": {"type": "number"},
                    "retencao_ir": {"type": "number"},
                    "retencao_pis": {"type": "number"},
                    "retencao_cofins": {"type": "number"},
                    "retencao_csll": {"type": "number"},
                },
                "required": [
                    "cnpj_prestador", "fornecedor", "uf_prestador",
                    "cnpj_tomador", "uf_tomador",
                    "data_entrada", "data_emissao", "num_nota", "valor",
                    "retencao_ir", "retencao_pis", "retencao_cofins", "retencao_csll",
                ],
                "additionalProperties": False,
            },
        },
        "divergencias": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cnpj_prestador": {"type": "string", "description": "apenas numeros"},
                    "num_nota": {"type": ["string", "null"]},
                    "data_emissao": {"type": "string", "description": "DD/MM/AAAA"},
                    "motivo_divergencia": {
                        "type": "string",
                        "description": "O que nao bateu entre o resumo e a nota detalhada",
                    },
                },
                "required": ["cnpj_prestador", "num_nota", "data_emissao", "motivo_divergencia"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["notas_validadas", "divergencias"],
    "additionalProperties": False,
}


class ErroAuditoriaPdf(Exception):
    """Falha da auditoria por IA que o chamador precisa mostrar ao usuário."""


class ConfiguracaoAusente(ErroAuditoriaPdf):
    pass


class PdfInvalido(ErroAuditoriaPdf):
    pass


def chave_api() -> str:
    """A chave da Anthropic, do ambiente. Não há fallback — ver a nota acima."""
    return os.environ.get("ANTHROPIC_API_KEY", "")


def _cliente() -> anthropic.Anthropic:
    chave = chave_api()
    if not chave:
        raise ConfiguracaoAusente(
            "ANTHROPIC_API_KEY não configurada. A auditoria de PDF por IA "
            "precisa da chave da Anthropic no .env."
        )
    return anthropic.Anthropic(api_key=chave)


def _validar_pdf(pdf_bytes: bytes) -> None:
    if not pdf_bytes:
        raise PdfInvalido("Arquivo vazio.")
    # Assinatura do formato. Um .pdf que na verdade é ZIP/HTML chegaria à API e
    # voltaria como erro de parsing sem dizer que o problema é o arquivo.
    if not pdf_bytes.lstrip()[:5].startswith(b"%PDF-"):
        raise PdfInvalido("O arquivo não é um PDF (assinatura %PDF- ausente).")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        mb = len(pdf_bytes) / (1024 * 1024)
        raise PdfInvalido(
            f"PDF de {mb:.0f} MB excede o limite de {MAX_PDF_BYTES // (1024 * 1024)} MB. "
            "Separe o arquivo em partes menores."
        )


def _mensagem(fonte_documento: dict) -> list:
    """A mensagem é a mesma nos dois caminhos; só muda a fonte do documento.

    O bloco do documento vem ANTES do texto: é a ordem recomendada e a que o
    modelo lê melhor.
    """
    return [{
        "role": "user",
        "content": [
            {"type": "document", "source": fonte_documento},
            {"type": "text", "text": PROMPT_AUDITORIA},
        ],
    }]


def _chamar_modelo(client: anthropic.Anthropic, pdf_bytes: bytes, modelo: str):
    """Uma chamada ao modelo com o PDF anexado e o formato de saída travado.

    Streaming sempre: `max_tokens` alto em requisição não-streaming estoura o
    timeout HTTP do SDK. `get_final_message()` devolve a mensagem completa.
    """
    comum = {
        "model": modelo,
        "max_tokens": MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": ESFORCO,
            "format": {"type": "json_schema", "schema": SCHEMA_RESPOSTA},
        },
    }

    if len(pdf_bytes) <= LIMITE_INLINE_BYTES:
        pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
        fonte = {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}
        with client.messages.stream(messages=_mensagem(fonte), **comum) as stream:
            return stream.get_final_message()

    enviado = client.beta.files.upload(
        file=("notas.pdf", io.BytesIO(pdf_bytes), "application/pdf"),
        betas=[BETA_FILES_API],
    )
    logger.info("PDF de %.0f MB enviado pela Files API (%s)",
                len(pdf_bytes) / (1024 * 1024), enviado.id)
    try:
        with client.beta.messages.stream(
            messages=_mensagem({"type": "file", "file_id": enviado.id}),
            betas=[BETA_FILES_API],
            **comum,
        ) as stream:
            return stream.get_final_message()
    finally:
        # Documento de cliente não fica hospedado na Anthropic depois da
        # análise. Falha ao apagar não derruba a auditoria já feita, mas vai
        # para o log com o id, para dar para limpar à mão.
        try:
            client.beta.files.delete(enviado.id, betas=[BETA_FILES_API])
        except Exception:
            logger.warning("Não foi possível apagar o arquivo %s da Files API", enviado.id)


def _texto_da_resposta(resposta) -> str:
    """O JSON vem no primeiro bloco de texto; blocos de raciocínio vêm antes."""
    for bloco in resposta.content:
        if bloco.type == "text":
            return bloco.text
    raise ErroAuditoriaPdf(
        "A IA não devolveu conteúdo de texto "
        f"(stop_reason={getattr(resposta, 'stop_reason', '?')})."
    )


def _limpar_cerca_markdown(texto: str) -> str:
    """Rede de segurança: com output_config o JSON já vem limpo.

    Fica aqui porque o dia em que alguém trocar o modelo por um que não suporte
    saída estruturada, a cerca ```json volta e o json.loads quebra sem explicar.
    """
    limpo = texto.strip()
    if limpo.startswith("```"):
        limpo = limpo.split("\n", 1)[-1] if "\n" in limpo else limpo[3:]
        if limpo.rstrip().endswith("```"):
            limpo = limpo.rstrip()[:-3]
    return limpo.strip()


def auditar_pdf(pdf_bytes: bytes, modelo: str = MODELO_PADRAO) -> dict:
    """Manda o PDF para o Claude e devolve {"notas_validadas": [...], "divergencias": [...]}.

    Levanta `ErroAuditoriaPdf` (ou uma subclasse) em qualquer falha — a mensagem
    é escrita para ser mostrada ao usuário.
    """
    _validar_pdf(pdf_bytes)
    client = _cliente()

    try:
        resposta = _chamar_modelo(client, pdf_bytes, modelo)
    except anthropic.BadRequestError as e:
        # A conta sem saldo também volta como 400. Culpar o PDF aí manda o
        # usuário procurar defeito num arquivo que está perfeito — aconteceu
        # em 26/08/2026 e custou uma investigação inteira.
        if "credit balance" in (e.message or "").lower():
            raise ErroAuditoriaPdf(
                "A conta da Anthropic está sem saldo. O PDF não tem problema "
                "nenhum — é preciso comprar créditos em console.anthropic.com "
                "(Plans & Billing) e reenviar."
            ) from e
        # O resto: PDF com mais de 600 páginas, protegido por senha ou
        # corrompido — a API é quem sabe dizer qual dos três.
        raise PdfInvalido(
            f"A API recusou o documento: {e.message} "
            f"(o limite é de {MAX_PAGINAS} páginas por análise)."
        ) from e
    except anthropic.AuthenticationError as e:
        raise ConfiguracaoAusente("ANTHROPIC_API_KEY inválida ou revogada.") from e
    except anthropic.RateLimitError as e:
        espera = e.response.headers.get("retry-after", "60")
        raise ErroAuditoriaPdf(
            f"Limite de requisições da Anthropic atingido. Tente de novo em {espera}s."
        ) from e
    except anthropic.APIStatusError as e:
        if e.status_code >= 500:
            raise ErroAuditoriaPdf(
                f"A Anthropic está indisponível no momento (HTTP {e.status_code})."
            ) from e
        raise ErroAuditoriaPdf(f"Erro da API da Anthropic: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ErroAuditoriaPdf("Sem conexão com a API da Anthropic.") from e

    if resposta.stop_reason == "refusal":
        motivo = getattr(getattr(resposta, "stop_details", None), "explanation", "") or ""
        raise ErroAuditoriaPdf(f"A IA recusou analisar este documento. {motivo}".strip())
    if resposta.stop_reason == "max_tokens":
        # O JSON foi cortado no meio. Alegar "0 divergências" a partir de uma
        # resposta truncada seria pior do que falhar.
        raise ErroAuditoriaPdf(
            "A resposta da IA excedeu o limite de tamanho — o documento tem notas "
            "demais para uma passada só. Separe o PDF em partes menores."
        )

    texto = _limpar_cerca_markdown(_texto_da_resposta(resposta))
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError as e:
        logger.error("JSON inválido devolvido pela IA: %s", texto[:500])
        raise ErroAuditoriaPdf(f"A IA devolveu um JSON inválido: {e}") from e

    uso = resposta.usage
    logger.info(
        "Auditoria de PDF por IA: %s validadas, %s divergências (in=%s out=%s tokens)",
        len(dados.get("notas_validadas") or []),
        len(dados.get("divergencias") or []),
        uso.input_tokens, uso.output_tokens,
    )

    # O schema já garante as duas chaves; o `or []` protege o chamador de um
    # `null` explícito, que é JSON válido e passaria pela validação.
    return {
        "notas_validadas": dados.get("notas_validadas") or [],
        "divergencias": dados.get("divergencias") or [],
    }


def auditar_pdf_do_arquivo(caminho: str, modelo: str = MODELO_PADRAO) -> dict:
    with open(caminho, "rb") as f:
        return auditar_pdf(f.read(), modelo=modelo)


COLUNAS_VALIDADAS = [
    ("num_nota", "Nº Nota"),
    ("data_emissao", "Emissão"),
    ("data_entrada", "Entrada"),
    ("cnpj", "CNPJ"),
    ("fornecedor", "Fornecedor"),
    ("uf_prestador", "UF"),
    ("valor", "Valor"),
    ("retencao_ir", "IR"),
    ("retencao_pis", "PIS"),
    ("retencao_cofins", "COFINS"),
    ("retencao_csll", "CSLL"),
]

COLUNAS_DIVERGENCIAS = [
    ("num_nota", "Nº Nota"),
    ("data_emissao", "Emissão"),
    ("cnpj", "CNPJ"),
    ("motivo_divergencia", "Motivo da Divergência"),
]


def gerar_excel(resultado: dict) -> io.BytesIO:
    """Relatório em duas abas: notas validadas e divergências.

    Aba vazia é criada mesmo assim — planilha sem a aba "Divergências" faz o
    usuário procurar o que não existe em vez de ler "nenhuma divergência".
    """
    import pandas as pd

    def montar(itens, colunas):
        chaves = [k for k, _ in colunas]
        rotulos = [r for _, r in colunas]
        linhas = [[item.get(k, "") for k in chaves] for item in (itens or [])]
        return pd.DataFrame(linhas, columns=rotulos)

    from ..planilha_estilo import VERDE, VERMELHO, estilizar_planilha

    validadas = montar(resultado.get("notas_validadas"), COLUNAS_VALIDADAS)
    divergencias = montar(resultado.get("divergencias"), COLUNAS_DIVERGENCIAS)

    saida = io.BytesIO()
    with pd.ExcelWriter(saida, engine="openpyxl") as writer:
        validadas.to_excel(writer, sheet_name="Notas Validadas", index=False)
        divergencias.to_excel(writer, sheet_name="Divergências", index=False)
        estilizar_planilha(writer.book, cores={
            "Notas Validadas": VERDE,
            "Divergências": VERMELHO,
        })

    saida.seek(0)
    return saida
