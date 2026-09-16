# -*- coding: utf-8 -*-
"""Casamento SPED x XML por IA, só para o que o motor NÃO conseguiu parear.

Pedido do Sandro em 28/08/2026.

O que este módulo NÃO faz, e é o mais importante: ele não toca em item que o
motor já casou. Os sinais determinísticos (código do produto, unitário +
quantidade, unitário, posição com conferência) continuam mandando, e só o que
sobra deles — hoje a aba "Itens Não Conferidos" — é oferecido à IA. Assim o
pior caso da IA é errar onde hoje não se confere nada; ela não tem como piorar
o que já funciona.

POR QUE UMA IA RESOLVE ISTO. O COD_ITEM do C170 é o código interno da empresa e
o cProd do XML é o do fornecedor: quase nunca batem. Quantidade e valor
unitário divergem quando a empresa fraciona a compra para revenda — caixa de 24
vira 24 unidades, jogo de parafusos vira 18 peças. Nesses casos o VALOR TOTAL do
item sobrevive à conversão e é o sinal forte. Quando duas linhas da mesma nota
têm o mesmo total, o desempate é a DESCRIÇÃO — e é aí que texto puro falha e
significado resolve: "SERVO EMBR. MB 1118/1315/1317 MBF015-" e "SERVO MBF-015 -
1E" são o mesmo produto, "REFRESCO EM PÓ" e "SUCO CONCENTRADO" também.

Por isso o payload é enxuto de propósito: DESCRIÇÃO e VALOR TOTAL dos dois
lados, nada mais. Mandar a nota inteira só daria ao modelo mais chance de se
confundir.

"Não sei" é resposta VÁLIDA e esperada (`idx_xml: null`). O princípio é o mesmo
que já rege `casar_itens_da_nota`: alegar divergência sem saber a que item ela
pertence é pior do que não alegar. Um modelo obrigado a escolher escolhe — e
erra calado.

Nada aqui é aplicado sem uma pessoa confirmar na tela. Ver a rota de
confirmação em api/routes/uploads.py.
"""
import json
import logging
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# O modelo sai do ambiente para poder ser trocado sem mexer no código — é a
# mesma escolha do auditor de PDF (PDF_IA_MODELO).
MODELO_PADRAO = os.environ.get("OPENAI_MODELO", "gpt-5.1")

# Tetos por auditoria. Um call por NOTA (não por item): a decisão de qual item
# vai com qual só faz sentido olhando a nota inteira de uma vez, e é o que
# permite o modelo resolver por eliminação.
MAX_NOTAS = 120
MAX_ITENS_POR_LADO = 40
TIMEOUT_S = 90

# Chamadas simultâneas à API. O SDK OpenAI é thread-safe (cada chamada cria sua
# sessão HTTP). O rate limit do tier pago comporta centenas de RPM; 8 workers já
# reduz ~5× o tempo de rede sem pressionar a cota.
MAX_WORKERS = 8

# Abaixo disso o par NÃO é sugerido. "media" entra porque foi exatamente a nota
# que o modelo deu ao par correto resolvido por eliminação num caso real
# (NF 1842, os dois SERVO de mesmo valor); exigir "alta" descartaria acertos.
CONFIANCAS_ACEITAS = ("alta", "media")

# Tolerância da poda trivial para valores "iguais" (centavos de arredondamento).
_TOLERANCIA_VALOR = 0.05

ESQUEMA_RESPOSTA = {
    "name": "pareamento_sped_xml",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["pares"],
        "properties": {
            "pares": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["idx_sped", "idx_xml", "confianca", "justificativa"],
                    "properties": {
                        "idx_sped": {"type": "integer"},
                        # null = não deu para parear com confiança. É resposta
                        # legítima, não falha.
                        "idx_xml": {"type": ["integer", "null"]},
                        "confianca": {"type": "string",
                                      "enum": ["alta", "media", "baixa"]},
                        "justificativa": {"type": "string"},
                    },
                },
            }
        },
    },
}

INSTRUCAO = """Você pareia itens de uma nota fiscal brasileira entre dois documentos da MESMA nota:

- SPED: os itens como a EMPRESA COMPRADORA os escriturou (registro C170/0200).
- XML: os itens como o FORNECEDOR os emitiu (NF-e).

É a mesma nota, então cada item do SPED corresponde a algum item do XML — mas os
documentos descrevem os produtos de formas diferentes:

1. Os CÓDIGOS quase nunca batem: o do SPED é interno da empresa, o do XML é do
   fornecedor. Não tente casar por código.
2. A UNIDADE muda. A empresa costuma desmembrar a compra para revenda: uma caixa
   com 24 vira 24 unidades, um jogo vira as peças avulsas. Por isso o VALOR
   TOTAL é o sinal mais forte — ele sobrevive à conversão, porque quantidade e
   preço unitário mudam de forma inversa.
3. As DESCRIÇÕES são abreviadas de formas diferentes, e às vezes usam palavras
   distintas para o mesmo produto ("REFRESCO EM PÓ" e "SUCO CONCENTRADO").
   Códigos de fabricante embutidos na descrição são pistas fortes: "MBF015" no
   SPED corresponde a "MBF-015" no XML.

REGRAS:

- Cada item do XML pode ser usado NO MÁXIMO UMA VEZ. Se dois itens do SPED
  disputam o mesmo item do XML, fique com o mais provável e resolva o outro por
  eliminação, ou devolva null.
- Quando os valores totais empatam, a DESCRIÇÃO é que decide. Nunca escolha por
  ordem de aparição.
- Se o mesmo produto aparece repetido em várias linhas idênticas, qualquer
  atribuição serve — use "alta" e diga que as linhas são equivalentes.
- Responda para TODOS os itens do SPED listados, um objeto por item.
- Quando NÃO tiver convicção, devolva idx_xml: null. Isso é uma resposta certa,
  não uma falha. Um par errado gera acusação fiscal indevida contra o cliente;
  um par ausente só deixa o item para conferência manual. Prefira sempre o
  segundo.
- A justificativa é lida por um contador que vai confirmar ou recusar o seu
  par. Escreva UMA frase, dizendo o que fez você casar: o valor, a descrição,
  o código embutido, ou a eliminação."""


def ia_disponivel() -> bool:
    """Sem chave configurada, o sistema segue sem a etapa de IA."""
    return bool(os.environ.get("OPENAI_API_KEY"))


def _cliente(timeout: float = TIMEOUT_S):
    from openai import OpenAI  # import tardio: sem chave, ninguém paga o import
    chave = os.environ.get("OPENAI_API_KEY")
    if not chave:
        raise RuntimeError("OPENAI_API_KEY não configurada.")
    return OpenAI(api_key=chave, timeout=timeout)


def _linha(idx: int, item: dict) -> str:
    descr = str(item.get("descr") or "").strip() or "(sem descrição)"
    return f"[{idx}] {descr} | total {float(item.get('valor') or 0.0):.2f}"


def montar_prompt(nota: dict) -> str:
    """O payload: só DESCRIÇÃO e VALOR TOTAL dos dois lados."""
    sped = "\n".join(_linha(i, it) for i, it in enumerate(nota["sped_itens"]))
    xml = "\n".join(_linha(i, it) for i, it in enumerate(nota["xml_itens"]))
    return (f"NOTA FISCAL {nota.get('num', '')} — {nota.get('emit_name', '')}\n\n"
            f"ITENS DO SPED (não pareados):\n{sped}\n\n"
            f"ITENS DO XML (ainda livres):\n{xml}")


def _parear_nota(cliente, nota: dict, modelo: str) -> list[dict]:
    resposta = cliente.chat.completions.create(
        model=modelo,
        response_format={"type": "json_schema", "json_schema": ESQUEMA_RESPOSTA},
        messages=[
            {"role": "system", "content": INSTRUCAO},
            {"role": "user", "content": montar_prompt(nota)},
        ],
    )

    escolha = resposta.choices[0]
    # Resposta cortada devolve JSON parcial: pares a menos passariam por
    # "não deu para casar", que é mentira diferente de não saber.
    if escolha.finish_reason == "length":
        raise RuntimeError("A resposta do modelo foi truncada (max_tokens).")

    bruto = json.loads(escolha.message.content or "{}")
    return bruto.get("pares", [])


def _val(item: dict) -> float:
    return round(float(item.get("valor") or 0.0), 2)


def _poda_triviais(notas: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separa notas que podem ser casadas sem IA daquelas que precisam dela.

    Dois filtros, todos seguros (sem ambiguidade possível):

    1. **1:1 óbvio** — exatamente 1 item SPED e 1 item XML, com o valor total
       dentro da tolerância. Com um só de cada lado não há ambiguidade: o item do
       SPED só pode corresponder ao único item do XML.

    2. **Valores únicos distintos** — para cada item SPED existe exatamente um
       item XML com valor total igual (dentro da tolerância), e vice-versa. É a
       versão em lote do filtro 1:1: quando não há empate de valores, o
       casamento é determinístico por eliminação, sem precisar de descrição.

    Devolve (triviais, ambiguas): triviais já vêm com os pares embutidos como
    se tivessem vindo do modelo; ambíguas vão para a API.
    """
    triviais = []
    ambiguas = []

    for nota in notas:
        sped = nota["sped_itens"]
        xml = nota["xml_itens"]

        # Filtro 1: 1:1 óbvio
        if len(sped) == 1 and len(xml) == 1:
            vs, vx = _val(sped[0]), _val(xml[0])
            if vx <= 0 or abs(vs - vx) / vx <= 0.05:
                triviais.append({
                    **nota,
                    "_pares_poda": [{
                        "idx_sped": 0,
                        "idx_xml": 0,
                        "confianca": "alta",
                        "justificativa": "Único item de cada lado — casamento direto.",
                    }],
                })
                continue

        # Filtro 2: valores únicos distintos — cada valor do SPED tem exatamente
        # um candidato do XML e vice-versa (bijeção por valor).
        valores_xml = defaultdict(list)
        for j, x in enumerate(xml):
            valores_xml[_val(x)].append(j)

        pares_valor = []
        xml_usados = set()
        ok = True
        for i, s in enumerate(sped):
            vs = _val(s)
            # Busca candidatos com valor igual (tolerância)
            candidatos = [j for j in range(len(xml))
                          if j not in xml_usados
                          and abs(_val(xml[j]) - vs) <= _TOLERANCIA_VALOR]
            if len(candidatos) != 1:
                ok = False
                break
            j = candidatos[0]
            xml_usados.add(j)
            pares_valor.append({
                "idx_sped": i,
                "idx_xml": j,
                "confianca": "alta",
                "justificativa": f"Valor total R$ {vs:.2f} único — casamento por eliminação.",
            })

        if ok and len(pares_valor) == len(sped):
            triviais.append({**nota, "_pares_poda": pares_valor})
        else:
            ambiguas.append(nota)

    if triviais:
        logger.info("Poda trivial: %d notas resolvidas sem IA, %d seguem para a API.",
                     len(triviais), len(ambiguas))
    return triviais, ambiguas


def _extrair_sugestoes_poda(nota: dict) -> list[dict]:
    """Converte os pares da poda trivial no formato de sugestão do resultado."""
    sped_itens = nota["sped_itens"]
    xml_itens = nota["xml_itens"]
    sugestoes = []
    for par in nota.get("_pares_poda", []):
        i_sped, i_xml = par["idx_sped"], par["idx_xml"]
        if not (0 <= i_sped < len(sped_itens) and 0 <= i_xml < len(xml_itens)):
            continue
        lado_sped, lado_xml = sped_itens[i_sped], xml_itens[i_xml]
        sugestoes.append({
            "chave": nota.get("chave", ""),
            "num": str(nota.get("num", "")),
            "emit_name": nota.get("emit_name", ""),
            # Chave da regra gravada quando a pessoa aprova o par.
            "company_cnpj": nota.get("company_cnpj", ""),
            "supplier_cnpj": nota.get("supplier_cnpj", ""),
            "origem": "poda",
            "idx_sped": lado_sped["idx"],
            "idx_xml": lado_xml["idx"],
            "sped_cod": lado_sped.get("cod", ""),
            "sped_descr": lado_sped.get("descr", ""),
            "sped_valor": round(float(lado_sped.get("valor") or 0.0), 2),
            "xml_cod": lado_xml.get("cod", ""),
            "xml_descr": lado_xml.get("descr", ""),
            "xml_valor": round(float(lado_xml.get("valor") or 0.0), 2),
            "confianca": par.get("confianca", "alta"),
            "justificativa": par.get("justificativa", ""),
        })
    return sugestoes


def _processar_nota_api(args: tuple) -> tuple[str, list | None, str | None]:
    """Worker para o ThreadPoolExecutor: chama a API para uma nota.

    Devolve (num_nota, pares_ou_None, erro_ou_None). Nunca levanta exceção:
    falha de rede vira erro no resultado e a auditoria segue sem aquela nota.
    """
    cliente, recorte, modelo = args
    num = str(recorte.get("num", ""))
    try:
        pares = _parear_nota(cliente, recorte, modelo)
        return (num, pares, None)
    except Exception as err:  # noqa: BLE001
        logger.warning("IA não pareou a nota %s: %s", num, err)
        return (num, None, f"{type(err).__name__}: {err}"[:200])


def casar_nao_conferidos(notas: list[dict], modelo: str = MODELO_PADRAO) -> dict:
    """Sugere pares para os itens que o motor não conseguiu casar.

    `notas` = [{chave, num, emit_name,
                sped_itens: [{idx, cod, descr, valor}],
                xml_itens:  [{idx, cod, descr, valor}]}]

    Devolve {"sugestoes": [...], "erros": [...], "tokens": int}. Cada sugestão
    traz a nota, os dois lados por extenso e a justificativa — é o que a tela de
    confirmação mostra para a pessoa decidir.

    Otimizações (31/08/2026):
    - **Poda trivial**: notas com casamento óbvio (1:1 ou valores únicos) são
      resolvidas localmente, sem call à API.
    - **Paralelismo**: as notas ambíguas vão para a API em paralelo (até
      MAX_WORKERS chamadas simultâneas), reduzindo ~5× o tempo de rede.

    Falha de rede ou de cota NÃO derruba a auditoria: a nota entra em `erros`, o
    item continua não conferido e a vida segue. Depender da OpenAI estar no ar
    para fechar a apuração do mês seria trocar um problema por outro.
    """
    resultado = {"sugestoes": [], "erros": [], "tokens": 0}
    if not notas:
        return resultado
    if not ia_disponivel():
        resultado["erros"].append({"nota": "", "erro": "OPENAI_API_KEY não configurada."})
        return resultado

    # --- Fase 1: poda trivial (sem rede) ---
    triviais, ambiguas = _poda_triviais(notas[:MAX_NOTAS])
    for nota in triviais:
        resultado["sugestoes"].extend(_extrair_sugestoes_poda(nota))

    if not ambiguas:
        return resultado

    # --- Fase 2: notas ambíguas vão para a API, em paralelo ---
    cliente = _cliente()

    # Monta a lista de jobs: cada job é (cliente, recorte, modelo).
    jobs = []
    # Mapeia num → nota completa (para extrair sugestões depois)
    recortes_por_num = {}
    for nota in ambiguas:
        sped_itens = nota["sped_itens"][:MAX_ITENS_POR_LADO]
        xml_itens = nota["xml_itens"][:MAX_ITENS_POR_LADO]
        if not sped_itens or not xml_itens:
            continue
        recorte = {**nota, "sped_itens": sped_itens, "xml_itens": xml_itens}
        jobs.append((cliente, recorte, modelo))
        recortes_por_num[str(nota.get("num", ""))] = recorte

    workers = min(MAX_WORKERS, len(jobs))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ia-match") as executor:
        futures = {executor.submit(_processar_nota_api, job): job for job in jobs}
        for future in as_completed(futures):
            num, pares, erro = future.result()
            if erro is not None:
                resultado["erros"].append({"nota": num, "erro": erro})
                continue
            if pares is None:
                continue

            recorte = recortes_por_num.get(num)
            if recorte is None:
                continue
            sped_itens = recorte["sped_itens"]
            xml_itens = recorte["xml_itens"]

            # Cada item do XML só pode ser usado uma vez — a instrução pede, mas a
            # trava é aqui: dois itens apontando para o mesmo XML gerariam duas
            # divergências sobre o mesmo valor.
            xml_usados = set()
            for par in pares:
                i_sped, i_xml = par.get("idx_sped"), par.get("idx_xml")
                if i_xml is None or par.get("confianca") not in CONFIANCAS_ACEITAS:
                    continue
                if not (isinstance(i_sped, int) and 0 <= i_sped < len(sped_itens)):
                    continue
                if not (isinstance(i_xml, int) and 0 <= i_xml < len(xml_itens)):
                    continue
                if i_xml in xml_usados:
                    continue
                xml_usados.add(i_xml)

                lado_sped, lado_xml = sped_itens[i_sped], xml_itens[i_xml]
                resultado["sugestoes"].append({
                    "chave": recorte.get("chave", ""),
                    "num": str(recorte.get("num", "")),
                    "emit_name": recorte.get("emit_name", ""),
                    # Chave da regra gravada quando a pessoa aprova o par.
                    "company_cnpj": recorte.get("company_cnpj", ""),
                    "supplier_cnpj": recorte.get("supplier_cnpj", ""),
                    "origem": "ia",
                    # Índices na nota ORIGINAL, que é o que o motor usa para aplicar.
                    "idx_sped": lado_sped["idx"],
                    "idx_xml": lado_xml["idx"],
                    "sped_cod": lado_sped.get("cod", ""),
                    "sped_descr": lado_sped.get("descr", ""),
                    "sped_valor": round(float(lado_sped.get("valor") or 0.0), 2),
                    "xml_cod": lado_xml.get("cod", ""),
                    "xml_descr": lado_xml.get("descr", ""),
                    "xml_valor": round(float(lado_xml.get("valor") or 0.0), 2),
                    "confianca": par.get("confianca", ""),
                    "justificativa": str(par.get("justificativa", ""))[:400],
                })

    return resultado
