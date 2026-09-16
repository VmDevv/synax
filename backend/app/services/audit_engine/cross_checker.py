import re
import os
import json
import logging
import shutil
from collections import defaultdict
from sqlalchemy.orm import Session
from ...core import competencia
from ...domain.models.audit import AuditTask

# NCMs sujeitos a Antecipação de ICMS (Revenda 2102)
from ...domain.constants.ncms_antecipacao import NCMS_ANTECIPACAO, NCMS_ANTECIPACAO_MAP

# NÃO existe lista padrão de CFOP sem crédito. Havia uma aqui, e ela decidia
# glosa de ICMS para CFOP sem regra cadastrada — acusação que nenhum admin
# aprovou, em toda empresa do sistema. Sem regra, o CFOP vai para a curadoria e
# o motor não alega nada sobre ele.

RE_DIGITS = re.compile(r"\D")

# CFOPs de entrada que entram no cálculo de ICMS ST (aba "ICMS ST").
#
# São as operações em que a mercadoria vai ser REVENDIDA e, por isso, comportam
# substituição tributária a recolher quando o fornecedor não reteve. Bonificação,
# doação e brinde (1910/2910) entram pelo mesmo motivo das compras: a mercadoria
# entra no estoque para revenda, e o ST é devido mesmo sem ter havido pagamento
# ao fornecedor. Incluídos a pedido do Sandro em 01/09/2026.
#
# A lista é o critério de ENTRADA no cálculo, não de crédito: o que decide se há
# ST a recolher é a regra de CEST/MVA (Portaria 16) consultada logo adiante.
# Acrescentar CFOP aqui faz notas novas aparecerem na aba — confira o efeito
# antes de mexer.
CFOPS_REVENDA_ST = ("1102", "2102", "1403", "2403", "1910", "2910")

# CSTs de PIS/COFINS que, por definição do próprio CST, não geram crédito.
# Independem de regra cadastrada: o CST já diz que a operação não é creditável.
CSTS_PIS_COFINS_SEM_CREDITO = {
    "70", "71", "72", "73", "74", "75", "98", "99", "49", "07", "08", "09",
}

# Alíquotas do regime NÃO-CUMULATIVO. O SPED Fiscal não traz base de cálculo de
# PIS/COFINS — só o valor do crédito — então o confronto é feito aplicando estas
# alíquotas sobre a base que o motor calculou. Decisão do Sandro em 26/08/2026.
#
# ATENÇÃO: no regime cumulativo (lucro presumido) são 0,65% e 3%. Isto está fixo
# no código e vale para TODA empresa — é o mesmo tipo de generalização que já
# mordeu no `UF_EMPRESA = "ES"`. No dia em que entrar cliente do presumido, a
# alíquota tem de virar cadastro por empresa, não um `if` aqui dentro.
ALIQ_PIS = 1.65
ALIQ_COFINS = 7.6


def pis_cofins_da_base(base: float) -> tuple[float, float]:
    """(PIS, COFINS) devidos sobre uma base, pelas alíquotas não-cumulativas."""
    b = _num(base)
    return round(b * ALIQ_PIS / 100.0, 2), round(b * ALIQ_COFINS / 100.0, 2)

logger = logging.getLogger(__name__)

from ...domain.models.integrations import MissingInvoiceLog

from .sped_parser import parse_sped
from .xml_parser import parse_nfe_xml, parse_cte_xml
from ...infrastructure.repositories.rules_repository import RulesRepository
from ..integrations.cest_mva_service import get_cest_mva_rule, get_mva_for_aliquota


def apply_specific_cfop_rules(cfop_origem: str, cst_icms: str, current_suggestion: str) -> str:
    """
    Interceptador de regras de negócio específicas para sugestão de CFOP.
    """
    return current_suggestion


def _norm_cod(valor) -> str:
    return str(valor or "").strip().upper()


def _num(valor) -> float:
    try:
        return float(valor or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _unit_sped(item_sped: dict) -> float:
    """Valor unitário da linha do SPED — o C170 traz QTD e VL_ITEM (total)."""
    qtd = _num(item_sped.get("qtd"))
    if qtd <= 0:
        return 0.0
    return round(_num(item_sped.get("vl_item")) / qtd, 2)


def _num_item(item_sped: dict) -> int:
    n = item_sped.get("num_item", 0)
    if isinstance(n, str):
        try:
            n = int(n)
        except (ValueError, TypeError):
            n = 0
    return n or 0


def _casa_por_codigo(it, x) -> bool:
    cod = _norm_cod(it.get("cod_item"))
    return bool(cod) and _norm_cod(x.get("cod_item")) == cod


def _casa_por_unit_qtd(it, x) -> bool:
    unit, qtd = _unit_sped(it), round(_num(it.get("qtd")), 4)
    if unit <= 0 or qtd <= 0:
        return False
    return (abs(round(_num(x.get("vl_unit")), 2) - unit) <= 0.01
            and abs(round(_num(x.get("qtd")), 4) - qtd) <= 0.0001)


def _casa_por_unit(it, x) -> bool:
    unit = _unit_sped(it)
    return unit > 0 and abs(round(_num(x.get("vl_unit")), 2) - unit) <= 0.01


def casar_itens_da_nota(sped_items: list, xml_items: list) -> dict:
    """Pareia cada linha do SPED com a linha do XML que lhe corresponde.

    Devolve {índice da linha do SPED: item do XML}. Linha que não deu para
    casar com confiança fica FORA do resultado — de propósito: alegar
    divergência sem saber a que item do XML a linha corresponde é pior do que
    não alegar nada.

    POR QUE NÃO CASAR SÓ POR POSIÇÃO: era o que o motor fazia quando o código
    do produto não batia. Numa nota real (58897, 21/08/2026) o SPED estava
    deslocado uma linha em relação ao XML e TODO item foi comparado com o
    vizinho — a tela encheu de divergência inventada, o ICMS do SPED de um
    produto contra o ICMS do XML de outro.

    Duas etapas, nesta ordem:

    1. CONSUMINDO — cada linha do XML é usada no máximo uma vez, por sinal
       decrescente de confiança: código do produto, valor unitário +
       quantidade, valor unitário, e por fim a posição (só quando o unitário
       ou a quantidade daquela posição confirmam). O consumo importa quando a
       nota traz itens gêmeos de códigos diferentes — quatro MOLA A GAS de
       mesmo nome, mesmo preço e mesma quantidade precisam ir para quatro
       linhas distintas, e não todas para a primeira.

    2. REAPROVEITANDO — o que sobrou tenta de novo, agora podendo apontar
       para uma linha do XML já usada. É o caso de o SPED fracionar em duas
       linhas o que o fornecedor emitiu em uma; a soma por produto
       (agrupar_icms_por_item_identico) fecha a conta e precisa das duas.
    """
    pares = {}
    livres = list(range(len(xml_items)))
    pendentes = list(range(len(sped_items)))

    def _fechar(i_sped: int, i_xml: int, consumir: bool) -> None:
        pares[i_sped] = xml_items[i_xml]
        pendentes.remove(i_sped)
        if consumir:
            livres.remove(i_xml)

    # --- Etapa 1: consumindo, do sinal mais forte para o mais fraco ---
    for casa in (_casa_por_codigo, _casa_por_unit_qtd, _casa_por_unit):
        for i in list(pendentes):
            for j in livres:
                if casa(sped_items[i], xml_items[j]):
                    _fechar(i, j, consumir=True)
                    break

    # Posição, com conferência. Sem ela, um deslocamento de uma linha vira
    # erro em massa; quando não há dado para conferir (XML antigo, sem vUnCom
    # nem qCom), a posição é aceita como o motor sempre fez.
    for i in list(pendentes):
        pos = _num_item(sped_items[i]) - 1
        if pos < 0 or pos >= len(xml_items) or pos not in livres:
            continue
        it, x = sped_items[i], xml_items[pos]
        unit, qtd = _unit_sped(it), round(_num(it.get("qtd")), 4)
        unit_x, qtd_x = round(_num(x.get("vl_unit")), 2), round(_num(x.get("qtd")), 4)

        tem_como_conferir = (unit > 0 or qtd > 0) and (unit_x > 0 or qtd_x > 0)
        confere = ((unit > 0 and unit_x > 0 and abs(unit - unit_x) <= 0.01)
                   or (qtd > 0 and qtd_x > 0 and abs(qtd - qtd_x) <= 0.0001))
        if not tem_como_conferir or confere:
            _fechar(i, pos, consumir=True)

    # --- Etapa 2: reaproveitando linha já usada ---
    for casa in (_casa_por_codigo, _casa_por_unit_qtd, _casa_por_unit):
        for i in list(pendentes):
            for j, x in enumerate(xml_items):
                if casa(sped_items[i], x):
                    _fechar(i, j, consumir=False)
                    break

    return pares


def chave_item_match(company_cnpj: str, supplier_cnpj: str,
                     sped_cod: str, xml_cod: str) -> tuple:
    """Chave de um par SPED×XML já aprovado. Exata, sem curinga.

    Os QUATRO campos são obrigatórios — é a mesma chave da tabela menos o
    tenant, que já vem embutido no escopo do repositório que montou o mapa.
    Sem o `company_cnpj` aqui, um tenant que audita mais de um CNPJ (matriz e
    filial) teria as duas regras colidindo no mesmo dicionário, e a de uma
    empresa passaria a casar item da outra.
    """
    return (RE_DIGITS.sub("", str(company_cnpj or "")),
            RE_DIGITS.sub("", str(supplier_cnpj or "")),
            _norm_cod(sped_cod), _norm_cod(xml_cod))


def aplicar_pares_conhecidos(sped_items: list, xml_items: list, pares: dict,
                             company_cnpj: str, supplier_cnpj: str,
                             pares_conhecidos: dict) -> dict:
    """Casa o que já foi aprovado em auditoria anterior. Devolve {idx_sped: regra}.

    Entra DEPOIS dos sinais determinísticos e ANTES da IA: um par que já passou
    por uma pessoa vale mais que palpite de modelo, mas menos que o casamento por
    código do próprio motor — por isso `i in pares` sai fora.

    Muta `pares` no lugar, do mesmo jeito que a aplicação da IA faz.
    """
    if not pares_conhecidos:
        return {}

    marcados = {}
    usados = {id(x) for x in pares.values()}
    for i, item in enumerate(sped_items):
        if i in pares:
            continue
        cod_sped = item.get("cod_item", "")
        for j, x in enumerate(xml_items):
            if id(x) in usados:
                continue
            regra = pares_conhecidos.get(
                chave_item_match(company_cnpj, supplier_cnpj, cod_sped,
                                 x.get("cod_item", "")))
            if regra is None:
                continue
            pares[i] = x
            usados.add(id(x))
            marcados[i] = regra
            break
    return marcados


def levantar_nao_conferidos(sped_path: str, xml_dir: str,
                            ignore_saidas: bool = True,
                            pares_conhecidos: dict | None = None) -> list[dict]:
    """Itens que o motor NÃO conseguiu parear, com os candidatos ainda livres.

    Roda ANTES da auditoria, só para montar o que vai ser oferecido à IA. Usa o
    mesmo `casar_itens_da_nota` do motor — o que sai daqui é, por definição,
    exatamente o que cairia em "Itens Não Conferidos".

    `pares_conhecidos` são os casamentos já aprovados em auditorias anteriores
    (`ItemMatchRule`). Eles são aplicados aqui também, e não pelo motor apenas,
    para que o item que já passou por uma pessoa NÃO volte à tela de confirmação
    — que é o ponto de gravar a regra.

    A descrição do lado do SPED vem do registro 0200: o C170 traz DESCR_COMPL
    quase sempre vazio (0 de 1156 num arquivo real do escritório), e sem o 0200
    não haveria texto nenhum para comparar.

    Devolve uma lista de notas no formato que `ia_matcher.casar_nao_conferidos`
    espera.
    """
    sped_data = parse_sped(sped_path)
    produtos = sped_data.get("products", {})

    xmls_map = {}
    if os.path.isdir(xml_dir):
        for raiz, _, arquivos in os.walk(xml_dir):
            for nome in arquivos:
                if not nome.endswith(".xml"):
                    continue
                caminho = os.path.join(raiz, nome)
                xmls_map[nome[:-4]] = caminho
                achado = re.search(r"\d{44}", nome)
                if achado:
                    xmls_map[achado.group(0)] = caminho

    notas = []
    for chave, c100 in sped_data.get("c100", {}).items():
        if c100.get("mod") != "55":
            continue
        if ignore_saidas and str(c100.get("ind_oper", "0")) == "1":
            continue
        caminho = xmls_map.get(chave)
        if not caminho or not c100.get("items"):
            continue
        try:
            xml_data = parse_nfe_xml(caminho)
        except Exception:  # noqa: BLE001 — XML ilegível já é tratado no motor
            continue

        xml_items = xml_data.get("items", [])
        pares = casar_itens_da_nota(c100["items"], xml_items)

        # O que já foi aprovado antes some daqui: não se confirma o mesmo par
        # duas vezes.
        aplicar_pares_conhecidos(c100["items"], xml_items, pares,
                                 sped_data.get("cnpj", ""), xml_data.get("emit_id", ""),
                                 pares_conhecidos or {})

        sobrando = [i for i in range(len(c100["items"])) if i not in pares]
        if not sobrando:
            continue

        # Só os itens do XML que NENHUM par consumiu. Oferecer um item já casado
        # deixaria a IA desfazer, por tabela, um casamento determinístico.
        usados = {id(x) for x in pares.values()}
        livres = [(i, x) for i, x in enumerate(xml_items) if id(x) not in usados]
        if not livres:
            continue

        notas.append({
            "chave": chave,
            "num": c100.get("num", ""),
            "emit_name": xml_data.get("emit_name", ""),
            # Vão para a sugestão e daí para a regra gravada: a chave do par é
            # (empresa, fornecedor, cód. SPED, cód. XML).
            "company_cnpj": sped_data.get("cnpj", ""),
            "supplier_cnpj": xml_data.get("emit_id", ""),
            "sped_itens": [{
                "idx": i,
                "cod": c100["items"][i].get("cod_item", ""),
                "descr": (produtos.get(c100["items"][i].get("cod_item", ""))
                          or c100["items"][i].get("descr", "")),
                "valor": _num(c100["items"][i].get("vl_item")),
            } for i in sobrando],
            "xml_itens": [{
                "idx": i,
                "cod": x.get("cod_item", ""),
                "descr": x.get("descr", ""),
                "valor": _num(x.get("vl_item")),
            } for i, x in livres],
        })

    return notas


def _chave_item_xml(xml_item: dict) -> tuple:
    """Identidade de um item DENTRO do XML: código + descrição + valor unitário.

    Os três campos saem da mesma fonte (cProd, xProd e vUnCom do próprio XML),
    então são comparáveis entre si. É a definição de "item igual" do negócio:
    duas linhas da mesma nota com o mesmo código, o mesmo nome e o mesmo valor
    unitário são o mesmo produto, ainda que a quantidade mude.

    Exemplo real (DANFE do fornecedor):
        26934 | ADITIVO RADIADOR 1L ROSA ORGANICO | 3,7000 | qtd 40 | ICMS 10,36
        26934 | ADITIVO RADIADOR 1L ROSA ORGANICO | 3,7000 | qtd 30 | ICMS  7,77
    As duas linhas caem na mesma chave e somam ICMS 18,13.

    NÃO use esta chave para casar com o SPED: lá a descrição vem do registro
    0200 e o unitário sai de VL_ITEM/QTD — textos e valores diferentes para o
    mesmo produto. O casamento SPED x XML é de casar_itens_da_nota.
    """
    descr = re.sub(r"\s+", " ", str(xml_item.get("descr") or "")).strip().upper()
    try:
        unit = round(float(xml_item.get("vl_unit") or 0.0), 4)
    except (TypeError, ValueError):
        unit = 0.0
    return (str(xml_item.get("cod_item") or "").strip().upper(), descr, unit)


# Alíquotas do regime não-cumulativo. Servem de RESERVA: a alíquota real sai
# do próprio XML (pPIS/pCOFINS), que é quem sabe o regime do fornecedor. Só
# quando o documento não traz a alíquota é que estas entram.
ALIQ_PIS_PADRAO = 1.65
ALIQ_COFINS_PADRAO = 7.60


def credito_pis_cofins_esperado(xml_item: dict) -> tuple[float, float, float, float]:
    """Crédito de PIS e de COFINS que a nota comporta, e as alíquotas usadas.

    FORA DE USO NO MOTOR desde 28/08/2026 — não é código esquecido. O Sandro
    pediu a volta do confronto de BASE contra BASE, e é ele que roda hoje em
    cross_checker (procure por `sped_bc_pis`). Esta função fica de pé, e
    testada, porque a decisão pode voltar atrás e o caminho por valor é sutil o
    bastante para não valer reescrever do zero.

    O que ela faz: pega a base recalculada pela tese do STF (valor do item menos
    ICMS, ICMS ST, FCP ST e IPI — feita em xml_parser) e aplica a alíquota. O
    resultado é o crédito que a escrituração deveria ter.

    A razão de ela ter existido, para quem for reavaliar: o SPED Fiscal (EFD
    ICMS/IPI) não tem base de PIS/COFINS no C170 — traz, quando traz, só o valor
    do crédito. Confrontar base contra base compara o número do XML com um campo
    que costuma vir vazio, e a linha sai "SPED R$ 0,00 vs Base Calculada
    R$ 820,00" em todo item. Isso é sabido e foi aceito na decisão de 28/08.

    Devolve (pis_esperado, cofins_esperado, aliq_pis, aliq_cofins).
    """
    base = float(xml_item.get("vl_bc_pis", 0.0) or 0.0)
    aliq_pis = float(xml_item.get("p_pis", 0.0) or 0.0) or ALIQ_PIS_PADRAO
    aliq_cofins = float(xml_item.get("p_cofins", 0.0) or 0.0) or ALIQ_COFINS_PADRAO
    return (
        round(base * aliq_pis / 100.0, 2),
        round(base * aliq_cofins / 100.0, 2),
        aliq_pis,
        aliq_cofins,
    )


def permite_credito_icms(cfop: str, icms_rules_map: dict) -> bool | None:
    """Se o CFOP dá direito a crédito de ICMS, segundo a regra cadastrada.

    O CFOP que entra aqui é o de REFERÊNCIA — o mapeado, quando existe regra
    de classificação para a operação; só na falta dela é o do SPED. Ver
    cfop_de_referencia().

    Devolve None quando NÃO há regra para o CFOP — e None não é False. Sem
    regra o motor não sabe, então não glosa e nem compara valor: o CFOP vai
    para a aba de pendências e alguém decide. Chutar aqui enchia a tela de
    divergência que ninguém aprovou.
    """
    return icms_rules_map.get(cfop)


def cfop_de_referencia(sped_cfop: str, mapped_cfop: str | None) -> str:
    """O CFOP que decide crédito: o mapeado quando há regra, senão o do SPED.

    O CFOP do SPED é o que o cliente escriturou — é justamente o que está sob
    auditoria, então não pode ser também o critério da auditoria. Quando existe
    regra de classificação para a operação, ela diz qual CFOP a entrada
    deveria ter, e é esse que determina se o imposto é creditável.

    Sem isso o motor se contradizia: escriturado 1556 (sem crédito) e mapeado
    1653 (com crédito) gerava ao mesmo tempo "o CFOP deveria ser 1653" e "1556
    não permite crédito, glose o ICMS" — mandava desfazer um crédito que, com
    o CFOP corrigido, é devido. Agora a divergência que sai é a certa: o
    crédito que faltou aproveitar.

    Sem regra mapeada não há o que corrigir e o CFOP do SPED continua valendo.
    """
    return mapped_cfop or sped_cfop


def _nota_cfop(sped_cfop: str, cfop_regra: str) -> str:
    """Sufixo que mostra de onde saiu o CFOP quando ele não é o do SPED.

    Sem essa marca o usuário lê "CFOP 1653 permite crédito" numa nota que ele
    escriturou como 1556 e não entende de onde veio o número.
    """
    if str(cfop_regra) != str(sped_cfop):
        return f" (CFOP mapeado {cfop_regra}; no SPED está {sped_cfop})"
    return ""


def icms_creditavel_xml(xml_item: dict, considerar_mono: bool = False) -> float:
    """ICMS do item do XML que conta como crédito.

    Base: ICMS normal + ICMS do Simples Nacional (já somados em `vl_icms` pelo
    parser). Com a opção "Considerar ICMS Mono (combustíveis)" ligada na tela
    inicial, entra também o monofásico — decisão do escritório (21/08/2026):
    as QUATRO tags do grupo (próprio, retido para a etapa seguinte, cobrado
    anteriormente e diferido) somam como crédito.

    A aba "ICMS Mono" do relatório mostra as quatro separadas, para conferir
    item a item o que entrou nessa soma.
    """
    total = float(xml_item.get("vl_icms", 0.0) or 0.0)
    if considerar_mono:
        total += float(xml_item.get("vl_icms_mono", 0.0) or 0.0)
    return round(total, 2)


def base_pis_cofins_frete(vl_contabil: float, vl_icms: float) -> float:
    """Base de crédito de PIS/COFINS sobre o frete: contábil menos o ICMS.

    Mesmo critério dos produtos (exclusão do ICMS da base — tese do STF), só que
    aqui não há de onde copiar: o CT-e não tem grupo de PIS/COFINS no XML e o
    D100/D190 do SPED só traz ICMS. Os dois números vêm do próprio SPED — valor
    contábil do analítico (VL_OPR) e ICMS destacado — e a base é calculada.

    Decisão do Sandro em 26/08/2026. Nunca negativa: CT-e com ICMS maior que o
    contábil é erro de escrituração, e uma base negativa viraria "crédito" com
    sinal trocado no relatório.
    """
    return round(max(0.0, _num(vl_contabil) - _num(vl_icms)), 2)


def agrupar_icms_por_item_identico(sped_items: list, xml_items: list,
                                   icms_rules_map: dict | None = None,
                                   considerar_mono: bool = False,
                                   pares: dict | None = None,
                                   cfop_por_linha: dict | None = None) -> dict:
    """Soma o crédito de ICMS das linhas do MESMO item, dos dois lados da nota.

    Problema que resolve: o motor casa cada linha do SPED com a PRIMEIRA linha
    do XML de mesmo código. Quando o mesmo produto aparece em duas linhas —
    mesmo código, mesmo nome, mesmo valor unitário, só a quantidade diferente —
    as duas linhas do SPED eram comparadas contra a MESMA linha do XML, e a
    segunda acusava divergência que não existe.

    Como funciona: os itens do XML são agrupados pela identidade deles
    (_chave_item_xml). Cada linha do SPED é casada com sua linha do XML pelo
    MESMO pareamento do laço principal (`pares`, de casar_itens_da_nota) e tem
    o ICMS somado no
    grupo daquela linha. No fim, compara-se soma contra soma.

    Só entram na soma do SPED as linhas cujo CFOP permite crédito: linha sem
    direito a crédito já é reportada pela regra de glosa, e somá-la aqui
    contaminaria o total do grupo. O CFOP consultado é o de REFERÊNCIA de cada
    linha (`cfop_por_linha`, índice do item do SPED -> CFOP mapeado); sem esse
    mapa, o do próprio SPED.

    Devolve {chave_xml: {"sped_icms", "xml_icms", "n_sped", "n_xml"}}.
    """
    grupos = defaultdict(lambda: {"sped_icms": 0.0, "xml_icms": 0.0, "n_sped": 0, "n_xml": 0})

    for x in xml_items:
        g = grupos[_chave_item_xml(x)]
        g["xml_icms"] = round(g["xml_icms"] + icms_creditavel_xml(x, considerar_mono), 2)
        g["n_xml"] += 1

    if pares is None:
        pares = casar_itens_da_nota(sped_items, xml_items)

    for i, it in enumerate(sped_items):
        cfop_ref = (cfop_por_linha or {}).get(i) or it.get("cfop", "")
        if icms_rules_map is not None and not permite_credito_icms(cfop_ref, icms_rules_map):
            continue
        casado = pares.get(i)
        if casado is None:
            continue
        g = grupos[_chave_item_xml(casado)]
        g["sped_icms"] = round(g["sped_icms"] + float(it.get("vl_icms", 0.0) or 0.0), 2)
        g["n_sped"] += 1

    return dict(grupos)


def diagnose_val_diff(sped_val: float, xml_val: float, xml_item: dict) -> str:
    """
    Investiga de forma inteligente a diferença entre o valor do SPED e o valor calculado do XML
    para identificar se algum desconto, frete, IPI, ST ou ICMS foi esquecido/somado indevidamente.
    """
    diff = abs(sped_val - xml_val)
    if diff < 0.05:
        return ""
        
    vl_desc = float(xml_item.get("v_desc", 0.0) or 0.0)
    vl_frete = float(xml_item.get("v_frete", 0.0) or 0.0)
    vl_ipi = float(xml_item.get("v_ipi", 0.0) or 0.0)
    vl_icms = float(xml_item.get("vl_icms_normal", xml_item.get("vl_icms", 0.0)) or 0.0)
    vl_icms_st = float(xml_item.get("vl_icms_st", 0.0) or 0.0)
    vl_seg_out = float(xml_item.get("v_seg", 0.0) or 0.0) + float(xml_item.get("v_outro", 0.0) or 0.0)
    
    # 1) Desconto individual
    if vl_desc > 0.05 and abs(diff - vl_desc) <= 0.05:
        return f"Diferença igual ao Desconto do item (R$ {vl_desc:.2f}). Possível esquecimento de deduzir o desconto no SPED."
    # 2) Frete individual
    if vl_frete > 0.05 and abs(diff - vl_frete) <= 0.05:
        return f"Diferença igual ao Frete do item (R$ {vl_frete:.2f}). Verifique se o frete não foi somado no SPED."
    # 3) IPI individual
    if vl_ipi > 0.05 and abs(diff - vl_ipi) <= 0.05:
        return f"Diferença igual ao valor do IPI (R$ {vl_ipi:.2f})."
    # 4) ICMS individual (muito comum em PIS/COFINS pela exclusão do ICMS da base - Tese STF)
    if vl_icms > 0.05 and abs(diff - vl_icms) <= 0.05:
        return f"Diferença igual ao ICMS destacado na nota (R$ {vl_icms:.2f}). Verifique a exclusão do ICMS da base (Tese STF)."
    # 5) ICMS ST individual
    if vl_icms_st > 0.05 and abs(diff - vl_icms_st) <= 0.05:
        return f"Diferença igual ao ICMS ST do item (R$ {vl_icms_st:.2f})."
    # 6) Seguro/Outras despesas
    if vl_seg_out > 0.05 and abs(diff - vl_seg_out) <= 0.05:
        return f"Diferença igual a Despesas Acessórias/Seguro (R$ {vl_seg_out:.2f})."
        
    # 7) Combinações de 2 componentes mais comuns
    if (vl_desc + vl_icms) > 0.05 and abs(diff - (vl_desc + vl_icms)) <= 0.05:
        return f"Diferença igual à soma do Desconto (R$ {vl_desc:.2f}) + ICMS (R$ {vl_icms:.2f})."
    if (vl_frete + vl_ipi) > 0.05 and abs(diff - (vl_frete + vl_ipi)) <= 0.05:
        return f"Diferença igual à soma do Frete (R$ {vl_frete:.2f}) + IPI (R$ {vl_ipi:.2f})."
    if (vl_desc + vl_frete) > 0.05 and abs(diff - (vl_desc + vl_frete)) <= 0.05:
        return f"Diferença igual à combinação de Desconto (R$ {vl_desc:.2f}) e Frete (R$ {vl_frete:.2f})."
    if (vl_ipi + vl_icms_st) > 0.05 and abs(diff - (vl_ipi + vl_icms_st)) <= 0.05:
        return f"Diferença igual à soma de IPI (R$ {vl_ipi:.2f}) + ICMS ST (R$ {vl_icms_st:.2f})."
        
    return ""


# ---------------------------------------------------------------------------
# Participante do SPED (0150) × emitente do XML
# ---------------------------------------------------------------------------

def _so_digitos(valor) -> str:
    return RE_DIGITS.sub("", str(valor or ""))


def nome_participante(participants: dict, cod: str) -> str:
    """Razão social do COD_PART, para exibição.

    `participants` passou a ser {COD_PART: dict} com o 0150 inteiro; antes era
    {COD_PART: nome}. Sem este helper, `participants.get(cod, cod)` devolveria o
    dict cru e a tela mostraria "{'nome': ...}" no lugar do fornecedor.
    """
    p = participants.get(cod)
    if isinstance(p, dict):
        return p.get("nome") or str(cod or "")
    return str(p) if p else str(cod or "")


def id_participante(participants: dict, cod: str) -> str:
    """CNPJ do COD_PART; CPF quando o participante é pessoa física."""
    p = participants.get(cod)
    if isinstance(p, dict):
        return (p.get("cnpj") or p.get("cpf") or "").strip()
    return ""


def conferir_cnpj_do_emitente(tipo: str, key: str, entrada: dict,
                              participants: dict, xml_data: dict,
                              company_cnpj: str):
    """O emitente escriturado no SPED é o mesmo que assinou o XML?

    O C100/D100 não traz CNPJ: traz o COD_PART, um código do cadastro do
    cliente que o 0150 resolve. Quando a nota é lançada no fornecedor errado
    — trocar de participante no Domínio é um clique — todo o resto continua
    batendo, porque valor, imposto e item vêm do XML certo. Este confronto
    existe porque é o único campo da nota que só o cadastro responde.

    Qual lado conferir depende do IND_EMIT (campo 3), não do IND_OPER:

    - IND_EMIT = 1 (emissão de terceiros — toda nota de compra): o COD_PART é
      o emitente, e é ele que se compara com o `emit` do XML. Só em ENTRADA:
      na saída o COD_PART é o DESTINATÁRIO, e compará-lo com o emitente
      inventaria divergência em toda nota.
    - IND_EMIT = 0 (emissão própria — nota de entrada, devolução): o emitente
      é a própria empresa, e o COD_PART é o remetente. O que dá para afirmar
      aí é que o `emit` do XML tem que ser o CNPJ do 0000.

    Devolve None quando não há o que apontar.
    """
    xml_cnpj = _so_digitos(xml_data.get("emit_id"))
    if not xml_cnpj:
        # XML sem emitente legível não é erro de cadastro do cliente — é XML
        # ilegível, e quem reclama disso é o parser, não esta conferência.
        return None

    ind_emit = str(entrada.get("ind_emit", "") or "")
    ind_oper = str(entrada.get("ind_oper", "0") or "0")
    cod_part = entrada.get("emit", "") or ""
    empresa = _so_digitos(company_cnpj)
    achado = {
        "type": tipo,
        "key": key,
        "num": entrada.get("num", ""),
        "cod_part": cod_part,
        "emit_name": xml_data.get("emit_name", ""),
        "xml_cnpj": xml_cnpj,
        "sped_name": nome_participante(participants, cod_part),
    }

    if ind_emit == "0":
        if empresa == xml_cnpj:
            return None
        return dict(
            achado,
            sped_cnpj=empresa,
            tipo_divergencia="emissao_propria_de_terceiro",
            reason=(f"Escriturada como emissão própria (IND_EMIT 0), mas o XML foi "
                    f"emitido por {achado['emit_name'] or 'terceiro'} ({xml_cnpj}), "
                    f"e não pela empresa ({empresa})."),
        )

    if ind_oper != "0":
        # Saída emitida por terceiro: o COD_PART é o destinatário, e o emitente
        # não tem contraparte no SPED. Não há o que confrontar.
        return None

    sped_cnpj = _so_digitos(id_participante(participants, cod_part))
    if not sped_cnpj:
        return dict(
            achado,
            sped_cnpj="",
            tipo_divergencia="sem_cadastro",
            reason=(f"O participante {cod_part} não tem CNPJ nem CPF no registro 0150: "
                    f"não há como confirmar o emitente. O XML foi emitido por "
                    f"{achado['emit_name'] or 'fornecedor sem nome'} ({xml_cnpj})."),
        )

    if sped_cnpj == xml_cnpj:
        return None

    # Mesma raiz, ordem diferente: matriz e filial são CNPJs distintos e a nota
    # está na empresa errada do grupo. O conserto é trocar o estabelecimento, não
    # o fornecedor — por isso o motivo e a mensagem são próprios.
    if len(sped_cnpj) == 14 and len(xml_cnpj) == 14 and sped_cnpj[:8] == xml_cnpj[:8]:
        return dict(
            achado,
            sped_cnpj=sped_cnpj,
            tipo_divergencia="filial_divergente",
            reason=(f"Mesma empresa, estabelecimento diferente: o SPED lançou a nota no "
                    f"participante {cod_part} ({sped_cnpj}) e o XML foi emitido por "
                    f"{xml_cnpj}."),
        )

    return dict(
        achado,
        sped_cnpj=sped_cnpj,
        tipo_divergencia="divergente",
        reason=(f"Emitente divergente: o SPED lançou a nota no participante {cod_part} "
                f"— {achado['sped_name']} ({sped_cnpj}) — e o XML foi emitido por "
                f"{achado['emit_name'] or 'fornecedor sem nome'} ({xml_cnpj})."),
    )


def execute_audit(task_id: str, tenant_id: str, sped_path: str, xml_dir: str, db_session: Session, sped_only_mode: bool = False, ignore_saidas: bool = True, considerar_icms_mono: bool = False, pares_ia: dict | None = None):
    """
    Função síncrona projetada para rodar em ThreadPool via BackgroundTasks.
    Faz o cruzamento pesado SPED x XML sem bloquear o GIL/Event Loop.
    """
    repo = RulesRepository(db_session, tenant_id)
    task = repo.audit_tasks.get_by_id(task_id)
    if not task:
        return
        
    # Maker-Checker: regras deduzidas por heurística NUNCA entram aprovadas, nem
    # quando quem roda a auditoria é admin. Elas são palpites da máquina e devem
    # passar pela tela de Curadoria antes de influenciar qualquer cálculo futuro.

    try:
        task.status = "processando"
        db_session.commit()
        
        sped_data = parse_sped(sped_path)
        company_cnpj = sped_data["cnpj"]
        participants = sped_data["participants"]
        
        # 1. Carregar Regras do Repositório
        credit_rules = repo.credit_rules.get_all()
        icms_rules_map = {r.cfop: r.allow_credit for r in credit_rules if getattr(r, 'status', 'aprovado') == 'aprovado'}
        
        ipi_credit_rules = repo.ipi_credit_rules.get_all()
        # Chave (CFOP, CST) — a mesma da PK. O mapa era só {cfop: allow}: duas
        # regras do mesmo CFOP com CSTs diferentes se sobrescreviam em silêncio,
        # e o CST virava curinga na prática.
        ipi_rules_map = {(r.cfop, str(r.cst_expected or "")): r.allow_credit for r in ipi_credit_rules if getattr(r, 'status', 'aprovado') == 'aprovado'}
        
        pis_cofins_credit_rules = repo.pis_cofins_credit_rules.get_all()
        pis_cofins_rules_map = {(r.cfop, str(r.cst_expected)): r.allow_credit for r in pis_cofins_credit_rules if getattr(r, 'status', 'aprovado') == 'aprovado'}
        
        anticipation_rules = repo.icms_anticipation_rules.get_all()
        # Chave (UF origem, UF destino, CFOP) — a mesma da PK. Sem uf_destino no
        # mapa, uma regra PR->SP casava com nota que veio do PR para qualquer
        # estado, e duas regras da mesma origem se sobrescreviam.
        anticipation_rules_map = {(r.uf_origem, r.uf_destino, r.cfop): r for r in anticipation_rules}
        
        cfop_classifications = repo.cfop_classifications.get_all()

        # Busca ÚNICA: chave completa (empresa + fornecedor + CFOP + produto).
        # NÃO existe fallback genérico. Cada regra é unitária com 4 filtros exatos.
        # Se não houver regra para o produto específico, ele aparece como "não mapeado".
        cfop_mappings = {(getattr(r, 'company_cnpj', ''), getattr(r, 'supplier_cnpj', ''), getattr(r, 'supplier_cfop', ''), getattr(r, 'product_code', '')): r.entry_cfop for r in cfop_classifications if getattr(r, 'status', 'aprovado') == 'aprovado'}
        
        # Pares SPED×XML aprovados em auditorias anteriores. Só os `aprovado`:
        # regra em `pendente_revisao` (a que o operador criou) espera o admin na
        # Curadoria, igual a toda outra regra do sistema.
        item_match_map = {
            chave_item_match(r.company_cnpj, r.supplier_cnpj, r.sped_cod, r.xml_cod): r
            for r in repo.item_match_rules.get_all()
            if getattr(r, "status", "aprovado") == "aprovado"
        }

        cte_classifications = repo.cte_classifications.get_all()
        cte_cfop_mappings = {(getattr(r, 'company_cnpj', ''), r.supplier_cfop, r.emitter_uf): r.entry_cfop for r in cte_classifications if getattr(r, 'status', 'aprovado') == 'aprovado'}
        
        # 1.1. Bulk Load para consulta 100% em RAM (O(1)) sem bater no banco de dados nos loops quentes (Hot Loop)
        from ...domain.models.integrations import MissingInvoiceLog
        existing_missing_keys = set(r[0] for r in db_session.query(MissingInvoiceLog.access_key).filter_by(tenant_id=tenant_id).all())

        # Único acumulador que ainda vira registro: log de notas não lançadas.
        # Regras deduzidas ficam apenas em `results`, para confirmação humana.
        pending_missing_logs = {}
        
        # 2. Mapear XMLs
        xmls_map = {}
        if os.path.isdir(xml_dir):
            for root, _, files in os.walk(xml_dir):
                for f in files:
                    if f.endswith(".xml"):
                        key = f[:-4]
                        full_path = os.path.join(root, f)
                        xmls_map[key] = full_path
                        import re
                        m = re.search(r'\d{44}', f)
                        if m:
                            xmls_map[m.group(0)] = full_path
                        
        # 3. Inicializar Estruturas de Resultado
        results = {
            "resumo": {
                "notas_processadas": 0,
                "inconsistencias": 0,
                "informativos": 0
            },
            "company_cnpj": company_cnpj,
            "company_name": sped_data["name"],
            "missing_xmls": [],
            "unrecorded_invoices": [],
            "value_discrepancies": [],
            "icms_issues": [],
            "icms_st_notes": [],
            "ipi_issues": [],
            "pis_cofins_issues": [],
            # Aproveitamento de PIS/COFINS sobre frete: uma linha por CT-e, com o
            # crédito calculado (contábil − ICMS). Informativa; as divergências
            # reais vão para pis_cofins_issues.
            "pis_cofins_frete": [],
            "sped_escritura_pis_cofins_frete": bool(sped_data.get("bloco_d_tem_pis_cofins")),
            "anticipation_results": [],
            # Emitente escriturado × emitente do XML. O SPED só guarda o
            # COD_PART; o CNPJ vem do 0150. Nota lançada no fornecedor errado
            # bate em tudo o mais, porque valor e imposto vêm do XML certo.
            "cnpj_emitente_issues": [],
            "cte_tomador_issues": [],
            "cfop_classifications_issues": [],
            "cte_cfop_classifications_issues": [],
            "unmapped_items": [],
            "unmapped_cte_items": [],
            "unmapped_icms_cfops": [],
            # Itens que o motor NÃO conseguiu conferir. Antes eles sumiam num
            # `continue` mudo: o produto era complementar ou tinha descrição
            # genérica, o casamento SPED×XML não achava par confiável e a nota
            # inteira saía da apuração sem aparecer em lugar nenhum — foi o que
            # houve com a NF 2777 da Dist. Laurencini. Não conferir é uma
            # resposta legítima; não avisar não é.
            "itens_nao_conferidos": [],
            "difal_interno": [],
            "difal_interestadual": [],
            "antecipacao_icms": [],
            "cte_xml_list": [],
            "nfe_xml_list": [],
            "unmapped_ipi_rules": [],
            "unmapped_pis_cofins_rules": [],
            "notas_duplicadas": sped_data.get("duplicate_nfs", []),
            "relatorio_geral": [],
            "icms_mono": [],
            "icms_st_calculation": [],
            "icms_rules_count": len(credit_rules),
            "ipi_rules_count": len(ipi_credit_rules),
            "pis_cofins_rules_count": len(pis_cofins_credit_rules),
            "cfop_rules_count": len(cfop_classifications)
        }
        
        seen_unmapped = set()
        seen_unmapped_cte = set()
        seen_unmapped_ipi = set()
        seen_unmapped_pis_cofins = set()
        seen_unmapped_icms = set()
        used_cfops = set()
        
        # 4. Auditoria NF-e (C100)
        for key, c100 in sped_data["c100"].items():
            if c100["mod"] != "55": continue
            if ignore_saidas and str(c100.get("ind_oper", "0")) == "1": continue
            
            results["resumo"]["notas_processadas"] += 1
                
            if not sped_only_mode:
                xml_path = xmls_map.get(key)
                if not xml_path:
                    ind_oper = c100.get("ind_oper", "0")
                    tipo_operacao = "Entrada" if ind_oper == "0" else "Saída" if ind_oper == "1" else "Desconhecido"
                    # O CFOP escriturado ajuda a achar a nota no sistema do
                    # cliente — é por ele que a pessoa procura, não pela chave
                    # de 44 dígitos. Uma nota pode ter vários (um por item),
                    # então vão todos, sem repetir e na ordem em que aparecem.
                    cfops_sped = []
                    for item_sped in c100.get("items") or []:
                        cfop_item = str(item_sped.get("cfop") or "").strip()
                        if cfop_item and cfop_item not in cfops_sped:
                            cfops_sped.append(cfop_item)
                    results["missing_xmls"].append({
                        "type": "NF-e", "key": key, "num": c100["num"], "mod": c100["mod"],
                        "ind_emit": c100.get("ind_emit", ""), "emit": nome_participante(participants, c100["emit"]),
                        "vl_doc": c100["vl_doc"],
                        "ind_oper": ind_oper,
                        "tipo_operacao": tipo_operacao,
                        "cfops": cfops_sped
                    })
                    continue
                    
                xml_data = parse_nfe_xml(xml_path)
                if "error" in xml_data: continue
            else:
                # Mock xml_data for sped_only_mode
                xml_data = {
                    "emit_id": id_participante(participants, c100["emit"]) or c100["emit"],
                    "emit_name": nome_participante(participants, c100["emit"]),
                    "vl_doc": c100["vl_doc"],
                    "vl_st": 0.0,
                    "items": []
                }
            
            if not sped_only_mode:
                # Sem XML lido não há confronto: no modo "só SPED" o `emit_id`
                # acima JÁ sai do próprio 0150, e comparar os dois daria sempre
                # igual — conferência que nunca acusa nada é pior que nenhuma.
                achado_cnpj = conferir_cnpj_do_emitente(
                    "NF-e", key, c100, participants, xml_data, company_cnpj)
                if achado_cnpj:
                    results["cnpj_emitente_issues"].append(achado_cnpj)

            # Adiciona ao relatório geral de XMLs lidos
            results["nfe_xml_list"].append({
                "num_nf": c100["num"],
                "emit_cnpj": xml_data.get("emit_id", ""),
                "emit_name": xml_data.get("emit_name", ""),
                "descr": "Diversos", # Simplificado
                "cfop_xml": "Vários",
                "cfop_sped": "Vários",
                "vl_item": xml_data["vl_doc"],
                "vl_bc": 0.0,
                "vl_icms": 0.0,
                "vl_icms_st": xml_data["vl_st"],
                "fcp_st": 0.0, "v_ipi": 0.0, "v_ipi_devol": 0.0
            })
                
            soma_c190 = c100.get("vl_opr_c190_sum", 0.0)
            vl_c100 = c100.get("vl_doc", 0.0)
            
            if soma_c190 > 0.01 and abs(soma_c190 - vl_c100) > 0.05:
                results["value_discrepancies"].append({
                    "type": "NF-e (SPED C100 vs C190)", "key": key, "num": c100["num"],
                    "emit_name": nome_participante(participants, c100["emit"]),
                    "sped_val": soma_c190, "xml_val": vl_c100, "diff": abs(soma_c190 - vl_c100),
                    "reason": f"Divergência interna no SPED: Soma do analítico C190 (R$ {soma_c190:.2f}) não bate com o Total C100 (R$ {vl_c100:.2f})."
                })

            if not sped_only_mode:
                vl_xml = xml_data["vl_doc"]
                
                # A soma do registro analítico C190 deve representar o total da NF-e (incluindo frete, despesas, ST, etc.)
                sped_total_ref = soma_c190 if soma_c190 > 0.01 else vl_c100
                diff_doc = abs(sped_total_ref - vl_xml)
                
                if diff_doc > 0.05:
                    reason_msg = (
                        f"Total da NF-e divergente (Soma C190: R$ {soma_c190:.2f} vs XML: R$ {vl_xml:.2f})."
                        if soma_c190 > 0.01 else
                        f"Total da NF-e divergente (C100: R$ {vl_c100:.2f} vs XML: R$ {vl_xml:.2f})."
                    )
                    results["value_discrepancies"].append({
                        "type": "NF-e", "key": key, "num": c100["num"],
                        "emit_name": xml_data.get("emit_name", nome_participante(participants, c100["emit"])),
                        "sped_val": sped_total_ref, "xml_val": vl_xml, "diff": diff_doc,
                        "reason": reason_msg
                    })
                
            # Lógica de match e ICMS simplificada para este exemplo. Na versão final,
            # você copiará a lógica Option C (híbrida de matching) integralmente.
            xml_items_list = xml_data["items"]

            # Crédito de ICMS por grupo de item idêntico nesta nota — ver docstring
            # de agrupar_icms_por_item_identico(). Calculado uma vez por nota; o
            # loop abaixo consome cada grupo só na primeira vez que o encontra.
            # UM pareamento por nota, usado tanto aqui quanto no agrupamento:
            # se cada um casasse por conta própria, os dois poderiam divergir.
            pares_da_nota = ({} if sped_only_mode
                             else casar_itens_da_nota(c100["items"], xml_items_list))

            linhas_ia = {}

            # Pares APROVADOS numa auditoria anterior (ItemMatchRule). Entram
            # sempre, sem passar pela tela: já houve conferência humana daquele
            # par, e repetir a pergunta a cada mês é o que esta tabela existe
            # para evitar. A marca continua, com origem própria, porque o
            # casamento segue não sendo determinístico — mudou só quem confirmou
            # e quando.
            if not sped_only_mode and item_match_map:
                for _idx, _regra in aplicar_pares_conhecidos(
                        c100["items"], xml_items_list, pares_da_nota,
                        company_cnpj, xml_data.get("emit_id", ""),
                        item_match_map).items():
                    linhas_ia[_idx] = {
                        "casado_por_ia": True,
                        "ia_confianca": "aprovado",
                        "ia_justificativa": (
                            "Par aprovado em auditoria anterior e gravado como regra"
                            + (f" — {_regra.justificativa}" if _regra.justificativa else "")),
                        "ia_sped_descr": _regra.sped_descr or "",
                        "ia_xml_descr": _regra.xml_descr or "",
                    }

            # Pares que uma PESSOA confirmou na tela, vindos da sugestão da IA.
            # Entram DEPOIS e só onde o motor não casou: a condição `not in
            # pares_da_nota` garante que nenhum casamento determinístico é
            # desfeito por sugestão de modelo. `linhas_ia` marca as linhas para
            # que toda divergência nascida delas saia com o aviso na tela.
            if not sped_only_mode and pares_ia:
                for _sug in pares_ia.get(key, []):
                    _is, _ix = _sug.get("idx_sped"), _sug.get("idx_xml")
                    if not isinstance(_is, int) or not isinstance(_ix, int):
                        continue
                    if _is in pares_da_nota or not (0 <= _ix < len(xml_items_list)):
                        continue
                    pares_da_nota[_is] = xml_items_list[_ix]
                    linhas_ia[_is] = {
                        "casado_por_ia": True,
                        "ia_confianca": _sug.get("confianca", ""),
                        "ia_justificativa": _sug.get("justificativa", ""),
                        "ia_sped_descr": _sug.get("sped_descr", ""),
                        "ia_xml_descr": _sug.get("xml_descr", ""),
                    }

            # CFOP de referência de cada linha do SPED — o mapeado quando há
            # regra de classificação, senão o do próprio SPED. O agrupamento
            # abaixo filtra por crédito e precisa do MESMO critério do laço,
            # ou uma linha entraria na soma aqui e sairia glosada lá.
            cfop_regra_por_linha = {}
            if not sped_only_mode:
                for _idx, _xml_it in pares_da_nota.items():
                    _chave = (company_cnpj, xml_data.get("emit_id", ""),
                              _xml_it.get("cfop", ""), _xml_it.get("cod_item", ""))
                    cfop_regra_por_linha[_idx] = cfop_de_referencia(
                        c100["items"][_idx].get("cfop", ""), cfop_mappings.get(_chave)
                    )

            grupos_icms_nota = (
                agrupar_icms_por_item_identico(c100["items"], xml_items_list, icms_rules_map,
                                               considerar_mono=considerar_icms_mono,
                                               pares=pares_da_nota,
                                               cfop_por_linha=cfop_regra_por_linha)
                if not sped_only_mode else {}
            )
            grupos_icms_reportados = set()

            for idx_item, item in enumerate(c100["items"]):
                sped_cfop = item["cfop"]
                # Vazio na esmagadora maioria das linhas. Quando não é, viaja
                # junto de toda divergência daquele item para que a tela possa
                # avisar que o par saiu de uma sugestão de IA confirmada — e não
                # de um casamento determinístico.
                marca_ia = linhas_ia.get(idx_item, {})
                if not sped_only_mode:
                    xml_item = pares_da_nota.get(idx_item)
                    if xml_item is None:
                        # Sem par confiável não dá para conferir imposto nenhum:
                        # alegar divergência sem saber a que item ela pertence é
                        # pior do que não alegar. Mas o item vai para a lista de
                        # não conferidos, com o que se sabe dele, para conferência
                        # manual — ver a chave "itens_nao_conferidos".
                        results["itens_nao_conferidos"].append({
                            "key": key,
                            "num": c100["num"],
                            "emit_name": xml_data.get(
                                "emit_name", nome_participante(participants, c100["emit"])),
                            "cnpj_emitente": xml_data.get("emit_id", ""),
                            "num_item": item.get("num_item", idx_item + 1),
                            "cod_item": item.get("cod_item", ""),
                            # Descrição do 0200 do próprio SPED: aqui não há item do XML
                            # de onde tirá-la, e é justamente o nome genérico que
                            # costuma estar por trás do não-casamento.
                            "descr": (sped_data.get("products", {}).get(item.get("cod_item", ""))
                                      or item.get("descr", "")),
                            "cfop_sped": sped_cfop,
                            "vl_item": item.get("vl_item", 0.0),
                            "vl_icms": item.get("vl_icms", 0.0),
                            "motivo": ("O item do SPED não casou com nenhum item do XML: "
                                       "código do produto diferente e valor/quantidade sem "
                                       "correspondência única na nota. Acontece com produto "
                                       "complementar ou de descrição genérica. Confira à mão."),
                        })
                        continue
                else:
                    # Fake xml_item for SPED only mode
                    xml_item = {
                        "cfop": item["cfop"],
                        "cod_item": item["cod_item"],
                        "descr": "Produto/Serviço (Modo Somente SPED)",
                        "vl_icms": item.get("vl_icms", 0.0),
                        "vl_item": item.get("vl_item", 0.0),
                        "vl_bc": item.get("vl_bc", 0.0),
                        "p_icms": item.get("p_icms", 0.0),
                        "v_ipi": item.get("vl_ipi", 0.0),
                        "ncm": "",
                        "cest": "",
                        "c_benef": ""
                    }
                
                results["relatorio_geral"].append({
                    **marca_ia,
                    "key": key,
                    "num_nf": c100["num"],
                    "cnpj_emitente": xml_data.get("emit_id", c100.get("emit", "")),
                    "uf_emitente": xml_data.get("emit_uf", ""),
                    "razao_social": xml_data.get("emit_name", nome_participante(participants, c100["emit"])),
                    "descr_item": xml_item.get("descr", ""),
                    "vl_total": xml_item.get("vl_item", 0.0),
                    "bc_icms": xml_item.get("vl_bc", 0.0),
                    "icms": xml_item.get("vl_icms_normal", xml_item.get("vl_icms", 0.0)),
                    "icms_sn": xml_item.get("vl_icms_sn", 0.0),
                    "ipi": xml_item.get("v_ipi", 0.0),
                    "ipi_devolvido": xml_item.get("v_ipi_devol", 0.0),
                    "st": xml_item.get("vl_icms_st", 0.0),
                    "fcp_st": xml_item.get("fcp_st", 0.0),
                    "cfop_xml": xml_item.get("cfop", ""),
                    "cfop_sped": sped_cfop,
                    "icms_mono": xml_item.get("vl_icms_mono", 0.0)
                })

                # --- Aba "ICMS Mono": itens de combustível com monofásico ---
                # Entram na aba SEMPRE que a nota trouxer valor monofásico,
                # esteja a opção marcada ou não: a aba existe justamente para
                # o operador conferir o que seria somado. `considerado` diz se
                # esta auditoria de fato somou o valor no crédito.
                vl_mono_item = float(xml_item.get("vl_icms_mono", 0.0) or 0.0)
                if vl_mono_item > 0:
                    results["icms_mono"].append({
                        "key": key,
                        "num_nf": c100["num"],
                        "cnpj_emitente": xml_data.get("emit_id", c100.get("emit", "")),
                        "razao_social": xml_data.get("emit_name", nome_participante(participants, c100["emit"])),
                        "descr_item": xml_item.get("descr", ""),
                        "ncm": xml_item.get("ncm", ""),
                        "cst": xml_item.get("cst", ""),
                        "cfop_xml": xml_item.get("cfop", ""),
                        "cfop_sped": sped_cfop,
                        "vl_total": xml_item.get("vl_item", 0.0),
                        "q_bc_mono": xml_item.get("q_bc_mono", 0.0),
                        "ad_rem_icms": xml_item.get("ad_rem_icms", 0.0),
                        "mono_proprio": xml_item.get("vl_icms_mono_proprio", 0.0),
                        "mono_reten": xml_item.get("vl_icms_mono_reten", 0.0),
                        "mono_ret": xml_item.get("vl_icms_mono_ret", 0.0),
                        "mono_dif": xml_item.get("vl_icms_mono_dif", 0.0),
                        "mono_total": vl_mono_item,
                        "icms_normal": xml_item.get("vl_icms_normal", 0.0),
                        "considerado": bool(considerar_icms_mono),
                    })

                # --- Cálculo do ICMS ST (Aba ICMS ST) ---
                # 1) Produtos que entram para revenda — ver CFOPS_REVENDA_ST.
                if str(sped_cfop).strip() in CFOPS_REVENDA_ST:
                    uf_forn = str(xml_data.get("emit_uf", "")).upper().strip()
                    vl_st_item = float(xml_item.get("vl_icms_st", 0.0) or 0.0)
                    cst_item = str(xml_item.get("cst", "")).strip()
                    
                    # 2) Regra estrita: Em NENHUM caso puxar notas fiscais que vieram com ICMS ST no campo próprio (vl_st_item == 0.0)
                    # Dentro do ES: não puxar CST 60 nem CSOSN 0500
                    is_elegivel = (vl_st_item == 0.0)
                    if uf_forn == "ES" and cst_item in ["60", "060", "500", "0500", "10500", "5000"]:
                        is_elegivel = False
                        
                    if is_elegivel:
                        cest_item = str(xml_item.get("cest", "")).strip()
                        mva_rule = get_cest_mva_rule(cest_item)
                        if mva_rule:
                            # Checar se emitente é Simples Nacional
                            is_simples_nacional = (len(cst_item) == 3 or float(xml_item.get("vl_icms_sn", 0.0) or 0.0) > 0.0)
                            vl_tot_item = float(xml_item.get("vl_item", 0.0) or 0.0)
                            
                            if is_simples_nacional:
                                if uf_forn == "ES":
                                    aliq_icms_efetiva = 17.0
                                elif uf_forn in ["RS", "SC", "PR", "SP", "RJ", "MG"]:
                                    aliq_icms_efetiva = 7.0
                                else:
                                    aliq_icms_efetiva = 12.0
                                vl_icms_nf = round(vl_tot_item * (aliq_icms_efetiva / 100.0), 2)
                            else:
                                aliq_icms_efetiva = float(xml_item.get("p_icms", 0.0) or 0.0)
                                vl_icms_nf = float(xml_item.get("vl_icms_normal", xml_item.get("vl_icms", 0.0)) or 0.0)
                                
                            mva_val, mva_coluna = get_mva_for_aliquota(mva_rule, aliq_icms_efetiva)
                            bc_icms_st = round(vl_tot_item * (1.0 + mva_val), 2)
                            icms_st_calc = round(max(0.0, (bc_icms_st * 0.17) - vl_icms_nf), 2)
                            
                            results["icms_st_calculation"].append({
                                "key": key,
                                "num_nf": c100["num"],
                                "fornecedor": xml_data.get("emit_name", nome_participante(participants, c100["emit"])),
                                "uf_fornecedor": uf_forn,
                                "descricao_item": xml_item.get("descr", ""),
                                "ncm": xml_item.get("ncm", ""),
                                "cest": cest_item,
                                "cfop_xml": xml_item.get("cfop", ""),
                                "cfop_sped": sped_cfop,
                                "vl_total": vl_tot_item,
                                "bc_icms": float(xml_item.get("vl_bc", 0.0) or 0.0),
                                "aliq_icms": aliq_icms_efetiva,
                                "vl_icms": vl_icms_nf,
                                "is_simples": is_simples_nacional,
                                "mva": mva_val,
                                "mva_str": f"{mva_val * 100:.2f}%",
                                "mva_coluna": mva_coluna,
                                "bc_icms_st": bc_icms_st,
                                "icms_st": icms_st_calc,
                                "valida_ncm": mva_rule.get("ncm", ""),
                                "valida_descricao": mva_rule.get("description", "")
                            })

                xml_cfop = xml_item["cfop"]
                supplier_cnpj = xml_data.get("emit_id", "")
                prod_code = xml_item["cod_item"]
                
                mapping_key = (company_cnpj, supplier_cnpj, xml_cfop, prod_code)
                cnpj_emitente = xml_data.get("cnpj_emitente", xml_data.get("emit_id", ""))
                chave_composta_xml = f"{company_cnpj}_{cnpj_emitente}_{xml_cfop}"
                chave_composta_sped = f"{company_cnpj}_{cnpj_emitente}_{sped_cfop}"
                
                # Busca ÚNICA: regra exata por (empresa + fornecedor + CFOP + produto).
                # Não existe fallback genérico. Se não achou, vai para "não mapeado".
                mapped_cfop = cfop_mappings.get(mapping_key)

                # A partir daqui, ICMS/IPI/PIS-COFINS decidem crédito por ESTE
                # CFOP, não pelo do SPED — ver cfop_de_referencia().
                cfop_regra = cfop_de_referencia(sped_cfop, mapped_cfop)
                origem_cfop = _nota_cfop(sped_cfop, cfop_regra)

                if mapped_cfop:
                    if sped_cfop != mapped_cfop:
                        results["cfop_classifications_issues"].append({
                            "key": key, "num": c100["num"], "emit_name": xml_data.get("emit_name", ""),
                            "cnpj_emitente": cnpj_emitente,
                            "num_item": item["num_item"], "product_code": prod_code, 
                            "product_description": xml_item["descr"],
                            "supplier_cfop": xml_cfop, "sped_cfop": sped_cfop, "expected_cfop": mapped_cfop
                        })
                else:
                    unmap_key = f"{supplier_cnpj}_{xml_cfop}_{prod_code}"
                    if unmap_key not in seen_unmapped:
                        seen_unmapped.add(unmap_key)
                        
                        # Aplica regra de interceptação para gerar a sugestão ideal
                        cst_icms_origem = str(xml_item.get("cst", ""))
                        sugestao_refinada = apply_specific_cfop_rules(xml_cfop, cst_icms_origem, sped_cfop)
                        
                        # Somente sugestão em memória. O motor NÃO grava regra no banco:
                        # a persistência só acontece quando um humano confirma na tela.
                        results["unmapped_items"].append({
                            "company_cnpj": company_cnpj,
                            "supplier_cnpj": supplier_cnpj, "supplier_cfop": xml_cfop,
                            "product_code": prod_code, "product_description": xml_item["descr"],
                            "current_sped_cfop": sugestao_refinada
                        })

                # -- Sprint 1: ICMS Compliance --
                # Só a regra cadastrada decide, e ela é consultada pelo CFOP de
                # referência. Sem regra, o CFOP entra na fila de pendências e o
                # item é pulado: nem glosa, nem comparação de valor. Sem saber
                # se o CFOP dá crédito, "SPED R$ X vs XML R$ Y" acusaria toda
                # nota legitimamente glosada.
                allow_credit = icms_rules_map.get(cfop_regra)

                if allow_credit is None and cfop_regra not in seen_unmapped_icms:
                    seen_unmapped_icms.add(cfop_regra)
                    results["unmapped_icms_cfops"].append(cfop_regra)

                # xml_icms é calculado de qualquer jeito: o bloco de DIFAL abaixo
                # usa esse valor e não depende de regra de crédito.
                xml_icms = icms_creditavel_xml(xml_item, considerar_icms_mono)
                sped_icms = item.get("vl_icms", 0.0)

                if allow_credit is None:
                    pass  # sem regra: já foi para a fila de pendências, nada a alegar
                elif not allow_credit:
                    if sped_icms > 0.01:
                        results["icms_issues"].append({
                            **marca_ia,
                            "key": key,
                            "num": c100["num"],
                            "emit_name": xml_data.get("emit_name", nome_participante(participants, c100["emit"])),
                            "num_item": item["num_item"],
                            "descr": xml_item["descr"],
                            "cfop": cfop_regra,
                            "reason": (f"CFOP {cfop_regra} não permite crédito de ICMS, "
                                       f"mas SPED creditou R$ {sped_icms:.2f}{origem_cfop}"),
                            "acao": "glosar",
                            "sped_icms": sped_icms,
                            "xml_icms": xml_icms
                        })
                elif sped_only_mode:
                    # Sem XML real para cruzar (xml_item é um espelho 1:1 do
                    # próprio SPED neste modo) — a comparação por grupo não tem
                    # o que somar de um segundo lado; mantém item a item.
                    if abs(sped_icms - xml_icms) > 0.05:
                        results["icms_issues"].append({
                            **marca_ia,
                            "key": key,
                            "num": c100["num"],
                            "emit_name": xml_data.get("emit_name", nome_participante(participants, c100["emit"])),
                            "num_item": item["num_item"],
                            "descr": xml_item["descr"],
                            "cfop": cfop_regra,
                            "reason": (f"Divergência de crédito de ICMS: SPED = R$ {sped_icms:.2f}, "
                                       f"XML = R$ {xml_icms:.2f}{origem_cfop}"),
                            "acao": "ajustar",
                            # Base e alíquota do XML viajam junto porque quem for
                            # corrigir o arquivo precisa dos TRÊS campos: o Guia manda
                            # informar valor, base e alíquota em conjunto, e crédito
                            # sem base é tão inconsistente quanto base sem crédito.
                            # Ver services/sped_rewriter/ponte.py.
                            "xml_bc_icms": xml_item.get("vl_bc", 0.0),
                            "xml_aliq_icms": xml_item.get("p_icms", 0.0),
                            "sped_icms": sped_icms,
                            "xml_icms": xml_icms
                        })
                else:
                    # Compara o TOTAL do produto NESTA NOTA (todas as linhas do
                    # SPED contra todas as do XML) em vez da linha isolada — ver
                    # agrupar_icms_por_item_identico(). Cada produto só é
                    # reportado uma vez, na primeira linha dele encontrada aqui;
                    # quando não há duplicidade, o grupo tem 1 item de cada lado
                    # e o resultado é idêntico à comparação linha a linha de antes.
                    chave_grupo = _chave_item_xml(xml_item)
                    if chave_grupo not in grupos_icms_reportados:
                        grupos_icms_reportados.add(chave_grupo)
                        grupo = grupos_icms_nota.get(
                            chave_grupo,
                            {"sped_icms": sped_icms, "xml_icms": xml_icms, "n_sped": 1, "n_xml": 1},
                        )
                        total_sped_icms = grupo["sped_icms"]
                        total_xml_icms = grupo["xml_icms"]

                        if abs(total_sped_icms - total_xml_icms) > 0.05:
                            n_linhas = max(grupo["n_sped"], grupo["n_xml"])
                            soma = ("" if n_linhas <= 1 else
                                    f" — soma das {n_linhas} linhas deste mesmo produto na nota")
                            if total_sped_icms <= 0.01 < total_xml_icms:
                                # Crédito devido e não aproveitado. Sai com texto
                                # próprio: "divergência de R$ 0,00 vs R$ X" faz o
                                # usuário procurar erro de valor onde o que houve
                                # foi crédito deixado na mesa.
                                reason = (
                                    f"ICMS não aproveitado: o CFOP {cfop_regra} permite crédito e o XML "
                                    f"tem R$ {total_xml_icms:.2f} creditável, mas o SPED não creditou "
                                    f"nada{soma}.{origem_cfop}"
                                )
                            else:
                                reason = (
                                    f"Divergência de crédito de ICMS{soma}: "
                                    f"SPED = R$ {total_sped_icms:.2f}, "
                                    f"XML = R$ {total_xml_icms:.2f}{origem_cfop}"
                                )
                            results["icms_issues"].append({
                                **marca_ia,
                                "key": key,
                                "num": c100["num"],
                                "emit_name": xml_data.get("emit_name", nome_participante(participants, c100["emit"])),
                                "num_item": item["num_item"],
                                "descr": xml_item["descr"],
                                "cfop": cfop_regra,
                                "reason": reason,
                                "acao": "ajustar",
                                "xml_bc_icms": xml_item.get("vl_bc", 0.0),
                                "xml_aliq_icms": xml_item.get("p_icms", 0.0),
                                "sped_icms": total_sped_icms,
                                "xml_icms": total_xml_icms,
                                "itens_agrupados": n_linhas
                            })

                # -- Sprint 1: DIFAL Interno --
                if sped_cfop in ["1556", "1551", "1910"] or mapped_cfop in ["1556", "1551", "1910"]:
                    total_item_difal = xml_item.get("total_item_difal", xml_item.get("vl_item", 0.0))
                    eff_pct = (xml_icms / total_item_difal * 100) if total_item_difal > 0 else 0.0
                    results["difal_interno"].append({
                        "emit_cnpj": supplier_cnpj,
                        "num_nf": c100["num"],
                        "descr": xml_item["descr"],
                        "ncm": xml_item.get("ncm", ""),
                        "cest": xml_item.get("cest", ""),
                        "total_item_difal": total_item_difal,
                        "vl_bc": xml_item.get("vl_bc", 0),
                        "p_icms": xml_item.get("p_icms", 0),
                        "vl_icms": xml_icms,
                        "c_benef": xml_item.get("c_benef", ""),
                        "cfop_sped": sped_cfop,
                        "eff_pct": eff_pct,
                        "status": "Informativo"
                    })
                    
                # -- Sprint 1: DIFAL Interestadual --
                elif sped_cfop in ["2556", "2910", "2551", "2949"] or mapped_cfop in ["2556", "2910", "2551", "2949"]:
                    results["difal_interestadual"].append({
                        "emit_cnpj": supplier_cnpj,
                        "emit_uf": xml_data.get("emit_uf", ""),
                        "num_nf": c100["num"],
                        "descr": xml_item["descr"],
                        "ncm": xml_item.get("ncm", ""),
                        "cest": xml_item.get("cest", ""),
                        "vl_item": xml_item.get("total_item_difal", xml_item.get("vl_item", 0.0)),
                        "vl_bc_icms": xml_item.get("vl_bc", 0),
                        "p_icms": xml_item.get("p_icms", 0),
                        "vl_icms": xml_icms,
                        "p_icms_inter": xml_item.get("p_icms_inter", 0),
                        "vl_icms_uf_dest": xml_item.get("vl_icms_uf_dest", 0),
                        "vl_icms_uf_remet": xml_item.get("vl_icms_uf_remet", 0),
                        "cfop_sped": sped_cfop,
                        "status": "Informativo"
                    })

                # -- Novo Módulo: Antecipação de ICMS --
                ncm_limpo = RE_DIGITS.sub("", str(xml_item.get("ncm", "")))
                ncm_prefix_found = None
                if ncm_limpo in NCMS_ANTECIPACAO_MAP:
                    ncm_prefix_found = ncm_limpo
                elif len(ncm_limpo) >= 8 and ncm_limpo[:8] in NCMS_ANTECIPACAO_MAP:
                    ncm_prefix_found = ncm_limpo[:8]
                elif len(ncm_limpo) >= 7 and ncm_limpo[:7] in NCMS_ANTECIPACAO_MAP:
                    ncm_prefix_found = ncm_limpo[:7]
                elif len(ncm_limpo) >= 6 and ncm_limpo[:6] in NCMS_ANTECIPACAO_MAP:
                    ncm_prefix_found = ncm_limpo[:6]
                elif len(ncm_limpo) >= 5 and ncm_limpo[:5] in NCMS_ANTECIPACAO_MAP:
                    ncm_prefix_found = ncm_limpo[:5]
                elif len(ncm_limpo) >= 4 and ncm_limpo[:4] in NCMS_ANTECIPACAO_MAP:
                    ncm_prefix_found = ncm_limpo[:4]
                
                if mapped_cfop == "2102" and ncm_prefix_found:
                    categoria_nome = NCMS_ANTECIPACAO_MAP.get(ncm_prefix_found, "Revenda Sujeita à Antecipação")
                    results["antecipacao_icms"].append({
                        "num_nf": c100["num"],
                        "cnpj_emitente": xml_data.get("cnpj_emitente", xml_data.get("emit_id", "")),
                        "descr": xml_item.get("descr", ""),
                        "ncm": xml_item.get("ncm", ""),
                        "cest": xml_item.get("cest", ""),
                        "categoria_ncm": categoria_nome,
                        "vl_item": xml_item.get("total_item_difal", xml_item.get("vl_item", 0.0)),
                        "vl_bc_icms": xml_item.get("vl_bc", 0.0),
                        "p_icms": xml_item.get("p_icms", 0.0),
                        "vl_icms": xml_icms
                    })
                    
                # -- Sprint 3: IPI Compliance --
                # O IPI do XML é a soma de DUAS tags de grupos diferentes:
                #   <imposto><IPI><vIPI>            IPI destacado na venda
                #   <impostoDevol><IPI><vIPIDevol>  IPI que volta na devolução
                # Lendo só `vIPI`, nota de devolução chegava com IPI ZERO: quem
                # creditou certo era acusado ("SPED R$ 50,00 vs XML R$ 0,00") e
                # quem não creditou não era avisado de nada. Mesma doença do
                # `vICMSOutraUF` no CT-e. Quem decide o crédito continua sendo a
                # regra do tenant (CFOP + CST) — aqui o motor só passa a ENXERGAR
                # o valor que está no documento.
                xml_ipi_proprio = _num(xml_item.get("v_ipi"))
                xml_ipi_devol = _num(xml_item.get("v_ipi_devol"))
                xml_ipi = round(xml_ipi_proprio + xml_ipi_devol, 2)
                sped_ipi = item.get("vl_ipi", 0.0)
                sped_ipi_cst = str(item.get("cst_ipi", "") or "").strip()

                # A conta aberta acompanha a linha sempre que houver devolvido:
                # sem ela o total não bate com o `vIPI` que a pessoa vê ao abrir
                # o XML, e o sistema é que parece errado.
                detalhe_ipi = (
                    f" (R$ {xml_ipi_proprio:.2f} de IPI destacado + "
                    f"R$ {xml_ipi_devol:.2f} de IPI devolvido)"
                    if xml_ipi_devol > 0.01 else ""
                )

                allow_ipi = ipi_rules_map.get((cfop_regra, sped_ipi_cst))

                if allow_ipi is None:
                    # Sem CST no SPED não há chave para cadastrar regra — mesmo
                    # critério do PIS/COFINS logo abaixo. A pendência leva o CST
                    # real: gravar "*" aqui criava regra curinga.
                    unmap_ipi_key = (cfop_regra, sped_ipi_cst)
                    if sped_ipi_cst and unmap_ipi_key not in seen_unmapped_ipi:
                        seen_unmapped_ipi.add(unmap_ipi_key)
                        results["unmapped_ipi_rules"].append({
                            "cfop": cfop_regra,
                            "cst_ipi": sped_ipi_cst,
                            "descr": xml_item.get("descr", "")
                        })
                else:
                    if not allow_ipi:
                        if sped_ipi > 0.01:
                            results["ipi_issues"].append({
                                **marca_ia,
                                "key": key, "num": c100["num"], "emit_name": xml_data.get("emit_name", ""),
                                "num_item": item["num_item"], "descr": xml_item.get("descr", ""),
                                "cfop": cfop_regra, "cst_ipi": sped_ipi_cst,
                                "reason": (f"CFOP {cfop_regra} com CST {sped_ipi_cst} não permite crédito "
                                           f"de IPI, mas creditou R$ {sped_ipi:.2f}{origem_cfop}"),
                                "acao": "glosar",
                                "sped_val": sped_ipi, "xml_val": xml_ipi,
                                "xml_ipi_proprio": xml_ipi_proprio,
                                "xml_ipi_devol": xml_ipi_devol
                            })
                    else:
                        if abs(sped_ipi - xml_ipi) > 0.05:
                            razao_ipi = (
                                f"IPI não aproveitado: o CFOP {cfop_regra} com CST {sped_ipi_cst} permite "
                                f"crédito e o XML tem R$ {xml_ipi:.2f}{detalhe_ipi}, mas o SPED não "
                                f"creditou nada.{origem_cfop}"
                                if sped_ipi <= 0.01 < xml_ipi else
                                f"Divergência: SPED R$ {sped_ipi:.2f} vs XML R$ {xml_ipi:.2f}"
                                f"{detalhe_ipi}{origem_cfop}"
                            )
                            results["ipi_issues"].append({
                                **marca_ia,
                                "key": key, "num": c100["num"], "emit_name": xml_data.get("emit_name", ""),
                                "num_item": item["num_item"], "descr": xml_item.get("descr", ""),
                                "cfop": cfop_regra, "cst_ipi": sped_ipi_cst,
                                "reason": razao_ipi,
                                "acao": "ajustar",
                                "sped_val": sped_ipi, "xml_val": xml_ipi,
                                "xml_bc_ipi": xml_item.get("vl_bc_ipi", 0.0),
                                "xml_ipi_proprio": xml_ipi_proprio,
                                "xml_ipi_devol": xml_ipi_devol
                            })
                        
                # -- PIS/COFINS: confronto de BASE contra BASE --
                # O valor do crédito (vl_pis/vl_cofins do SPED) ainda é lido: ele
                # aparece na linha da divergência e no ramo de crédito indevido.
                # O que NÃO se usa mais é o valor do XML nem a base de COFINS —
                # a conferência é a base do STF (xml_bc_pis) contra a escriturada
                # (sped_bc_pis), e as duas bases do XML são idênticas por
                # construção em xml_parser.
                sped_pis = item.get("vl_pis", 0.0)
                sped_pis_cst = str(item.get("cst_pis", ""))
                xml_bc_pis = xml_item.get("vl_bc_pis", 0.0)
                sped_bc_pis = item.get("vl_bc_pis", 0.0)

                sped_cofins = item.get("vl_cofins", 0.0)
                sped_cofins_cst = str(item.get("cst_cofins", ""))

                allow_pis = pis_cofins_rules_map.get((cfop_regra, sped_pis_cst), None)
                if sped_pis_cst and allow_pis is None:
                    unmap_pis_key = f"{cfop_regra}_{sped_pis_cst}"
                    if unmap_pis_key not in seen_unmapped_pis_cofins:
                        seen_unmapped_pis_cofins.add(unmap_pis_key)
                        results["unmapped_pis_cofins_rules"].append({
                            "cfop": cfop_regra,
                            "cst_expected": sped_pis_cst,
                            "descr": xml_item.get("descr", "")
                        })

                allow_cofins = pis_cofins_rules_map.get((cfop_regra, sped_cofins_cst), None)
                if sped_cofins_cst and allow_cofins is None:
                    unmap_cofins_key = f"{cfop_regra}_{sped_cofins_cst}"
                    if unmap_cofins_key not in seen_unmapped_pis_cofins:
                        seen_unmapped_pis_cofins.add(unmap_cofins_key)
                        results["unmapped_pis_cofins_rules"].append({
                            "cfop": cfop_regra,
                            "cst_expected": sped_cofins_cst,
                            "descr": xml_item.get("descr", "")
                        })
                
                # -- Verificação de Elegibilidade a Crédito PIS/COFINS --
                # PIS/COFINS não deve ser bloqueado por CFOPs de ICMS como 1407/1556 se o CST for de crédito (ex: 50 a 66).
                is_non_credit_cst = sped_pis_cst in CSTS_PIS_COFINS_SEM_CREDITO
                is_elegivel_credito = True
                if is_non_credit_cst or allow_pis is False or allow_cofins is False:
                    is_elegivel_credito = False

                # -- Unificação PIS/COFINS (Uma única linha de divergência pela Base Calculada STF) --
                if not is_elegivel_credito:
                    # Se o item não for elegível a crédito e o SPED está zerado (ou próximo de 0), está correto!
                    if (sped_pis > 0.01 or sped_cofins > 0.01):
                        results["pis_cofins_issues"].append({
                            **marca_ia,
                            "key": key, "num": c100["num"], "emit_name": xml_data.get("emit_name", ""),
                            "num_item": item["num_item"], "descr": xml_item["descr"], "tipo": "PIS/COFINS",
                            "cfop": cfop_regra, "cst": sped_pis_cst or sped_cofins_cst,
                            "reason": (f"CFOP/CST sem direito a crédito de PIS/COFINS, mas o SPED "
                                       f"creditou R$ {sped_pis + sped_cofins:.2f}{origem_cfop}"),
                            "sped_val": sped_pis + sped_cofins, "xml_val": 0.00,
                            "diagnostic": ""
                        })
                else:
                    # Confronto de BASE contra BASE: a base recalculada pela tese
                    # do STF (item menos ICMS, ICMS ST, FCP ST e IPI, feita em
                    # xml_parser) contra o VL_BC_PIS escriturado no C170.
                    #
                    # Pedido do Sandro em 28/08/2026, revertendo o confronto por
                    # VALOR que vigorou em 27/08. Fica registrado o efeito, que é
                    # conhecido e foi aceito: o SPED Fiscal quase nunca preenche
                    # VL_BC_PIS no C170 — PIS/COFINS é escriturado na EFD
                    # Contribuições — então `sped_bc_pis` chega zerado e a linha
                    # sai "SPED R$ 0,00 vs Base Calculada R$ 820,00" em todo item
                    # de nota que tenha base. Não é defeito do cálculo: é o campo
                    # de origem que vem vazio.
                    #
                    # O caminho por valor não foi apagado: credito_pis_cofins_
                    # esperado() continua no arquivo, testada, para quando/se a
                    # decisão voltar atrás.
                    if abs(sped_bc_pis - xml_bc_pis) > 0.05:
                        razao = (f"Base de PIS/COFINS divergente: SPED "
                                 f"R$ {sped_bc_pis:.2f} vs Base Calculada "
                                 f"R$ {xml_bc_pis:.2f}{origem_cfop}")

                        results["pis_cofins_issues"].append({
                            **marca_ia,
                            "key": key, "num": c100["num"], "emit_name": xml_data.get("emit_name", ""),
                            "num_item": item["num_item"], "descr": xml_item["descr"], "tipo": "PIS/COFINS",
                            "cfop": cfop_regra, "cst": sped_pis_cst or sped_cofins_cst,
                            "reason": razao,
                            "sped_val": sped_bc_pis, "xml_val": xml_bc_pis,
                            # Estes dois são BASE DE CÁLCULO, não crédito.
                            # Sem esta marca, quem soma a lista para dizer
                            # "quanto há a aproveitar" soma base como se
                            # fosse imposto — deu R$ 111 mil de crédito
                            # inventado na primeira tentativa. As outras
                            # linhas da mesma lista SÃO crédito.
                            "comparacao": "base",
                            "base_calculada": xml_bc_pis,
                            "base_sped": sped_bc_pis,
                            "sped_pis": sped_pis, "sped_cofins": sped_cofins,
                            "diagnostic": diagnose_val_diff(sped_bc_pis, xml_bc_pis, xml_item)
                        })


                # -- Sprint 4: Cálculo da Antecipação de ICMS --
                emit_uf = xml_data.get("emit_uf", "")
                dest_uf = xml_data.get("dest_uf", "")
                ant_rule = anticipation_rules_map.get((emit_uf, dest_uf, sped_cfop))
                if ant_rule:
                    base_calc = xml_item.get("vl_item", 0.0)
                    
                    if ant_rule.reducao_base_calculo_percentage > 0:
                        base_calc = base_calc * (1 - (ant_rule.reducao_base_calculo_percentage / 100))
                    
                    if ant_rule.mva_percentage > 0:
                        base_calc = base_calc * (1 + (ant_rule.mva_percentage / 100))
                        
                    icms_origem = base_calc * (ant_rule.aliquota_interestadual_percentage / 100)
                    icms_destino = base_calc * (ant_rule.aliquota_interna_percentage / 100)
                    imposto_devido = icms_destino - icms_origem
                    
                    if imposto_devido > 0.01:
                        results["anticipation_results"].append({
                            "key": key, "num": c100["num"], "emit_name": xml_data.get("emit_name", ""),
                            "emit_uf": emit_uf, "num_item": item["num_item"],
                            "descr": xml_item["descr"], "cfop": sped_cfop,
                            "vl_item": xml_item.get("vl_item", 0.0),
                            "base_calc": base_calc,
                            "mva": ant_rule.mva_percentage,
                            "aliq_int": ant_rule.aliquota_interna_percentage,
                            "aliq_inter": ant_rule.aliquota_interestadual_percentage,
                            "red_bc": ant_rule.reducao_base_calculo_percentage,
                            "imposto_devido": imposto_devido
                        })

            # -- Sprint 1 e 3: Destaques de ICMS ST e IPI (Nível da Nota/Produto) --
            vl_st_total = xml_data.get("vl_st", 0)
            if vl_st_total > 0 or any(i.get("vl_icms_st", 0) > 0 for i in xml_items_list):
                results["icms_st_notes"].append({
                    "key": key,
                    "num": c100["num"],
                    "emit_name": xml_data.get("emit_name", nome_participante(participants, c100["emit"])),
                    "items": [
                        {
                            "num_item": i.get("num_item", idx + 1),
                            "descr": i["descr"],
                            "vl_item": i.get("vl_item", 0),
                            "vl_bc_icms_st": i.get("vl_bc", 0), # Simplified for memory
                            "vl_icms_st": i.get("vl_icms_st", 0)
                        } for idx, i in enumerate(xml_items_list) if i.get("vl_icms_st", 0) > 0
                    ]
                })

        # -- Sprint 2: Auditoria CT-e (D100) --
        for key, d100 in sped_data.get("d100", {}).items():
            if ignore_saidas and str(d100.get("ind_oper", "0")) == "1": continue
            results["resumo"]["notas_processadas"] += 1
            
            if not sped_only_mode:
                xml_path = xmls_map.get(key)
                if not xml_path:
                    emit_name_cte = nome_participante(participants, d100.get("emit", ""))

                    ind_oper = d100.get("ind_oper", "0")
                    tipo_operacao = "Entrada" if ind_oper == "0" else "Saída" if ind_oper == "1" else "Desconhecido"
                    results["missing_xmls"].append({
                        "type": "CT-e", "key": key, "num": d100.get("num", ""), "mod": "57",
                        "ind_emit": d100.get("ind_emit", ""), "emit": emit_name_cte,
                        "vl_doc": d100.get("vl_doc", 0.0),
                        "ind_oper": ind_oper,
                        "tipo_operacao": tipo_operacao,
                        # No CT-e o CFOP vem do analítico (D190), não de item:
                        # o D100 não tem registro de item. CT-e sem D190 fica
                        # com a lista vazia, e a tela mostra travessão em vez
                        # de inventar um código.
                        "cfops": list(d100.get("cfops") or [])
                    })
                    continue
                    
                xml_data = parse_cte_xml(xml_path)
                if "error" in xml_data: continue
                
                diff_doc = abs(d100.get("vl_doc", 0.0) - xml_data.get("vl_doc", 0.0))
                if diff_doc > 0.05:
                    emit_name_cte = nome_participante(participants, d100.get("emit", ""))
                        
                    results["value_discrepancies"].append({
                        "type": "CT-e", "key": key, "num": d100.get("num", ""),
                        "emit_name": xml_data.get("emit_name", emit_name_cte),
                        "sped_val": d100.get("vl_doc", 0.0), "xml_val": xml_data.get("vl_doc", 0.0), "diff": diff_doc
                    })
            else:
                xml_data = {
                    "emit_id": (id_participante(participants, d100.get("emit", ""))
                                or d100.get("emit", "")),
                    "emit_name": nome_participante(participants, d100.get("emit", "")),
                    "emit_uf": "UF",
                    "toma_cnpj": company_cnpj,
                    "toma_name": sped_data.get("name", ""),
                    "cfop": "0000",
                    "vl_doc": d100.get("vl_doc", 0.0)
                }
                
            if not sped_only_mode:
                achado_cnpj = conferir_cnpj_do_emitente(
                    "CT-e", key, d100, participants, xml_data, company_cnpj)
                if achado_cnpj:
                    results["cnpj_emitente_issues"].append(achado_cnpj)

            # Regra 5: Validação de Tomador
            xml_toma = xml_data.get("tomador_id", "")
            if xml_toma and xml_toma != company_cnpj:
                results["cte_tomador_issues"].append({
                    "key": key,
                    "num": d100.get("num", ""),
                    "emit_name": xml_data.get("emit_name", ""),
                    "tomador_id": xml_toma,
                    "tomador_role": xml_data.get("tomador_role", "").upper(),
                    "company_cnpj": company_cnpj
                })

            # Regra 7: CT-e CFOP Audit
            xml_cfop = xml_data.get("cfop", "")
            emit_uf = xml_data.get("emit_uf", "")
            
            mapping_key = (company_cnpj, xml_cfop, emit_uf)
            expected_cfop = cte_cfop_mappings.get(mapping_key)
            
            sped_cfops = d100.get("cfops", [])
            # O PIS/COFINS do frete é do DOCUMENTO (D101/D105), não de cada CFOP
            # do analítico. Guardamos o CFOP de referência da primeira linha para
            # consultar a regra uma única vez, depois do laço.
            cfop_regra_frete = ""
            for sped_cfop in sped_cfops:
                used_cfops.add(sped_cfop)

                # -- CT-e Frete ICMS Credit Check --
                # Mesma regra do laço de NF-e: quem decide crédito é o CFOP
                # mapeado para o frete, não o escriturado; sem regra cadastrada,
                # pendência.
                cfop_regra = cfop_de_referencia(sped_cfop, expected_cfop)
                origem_cfop = _nota_cfop(sped_cfop, cfop_regra)
                cfop_regra_frete = cfop_regra_frete or cfop_regra

                allow_credit = icms_rules_map.get(cfop_regra)
                if allow_credit is None and cfop_regra not in seen_unmapped_icms:
                    seen_unmapped_icms.add(cfop_regra)
                    results["unmapped_icms_cfops"].append(cfop_regra)

                sped_icms = d100.get("vl_icms", 0.0)
                xml_icms = xml_data.get("vl_icms", 0.0)
                # Quando parte do crédito vem de ICMSOutraUF, a mensagem abre a
                # conta: o valor não bate com o "vICMS" que a pessoa vê ao abrir
                # o XML, e sem isso a divergência parece erro do sistema.
                _outra_uf = xml_data.get("vl_icms_outra_uf", 0.0)
                detalhe_icms = (
                    f" (do CT-e: R$ {xml_data.get('vl_icms_proprio', 0.0):.2f} próprio "
                    f"+ R$ {_outra_uf:.2f} de ICMSOutraUF)" if _outra_uf > 0.01 else ""
                )
                if allow_credit is False and sped_icms > 0.01:
                    results["icms_issues"].append({
                        "key": key,
                        "num": d100.get("num", ""),
                        "emit_name": xml_data.get("emit_name", ""),
                        "num_item": "CT-e",
                        "descr": "Conhecimento de Transporte (Frete)",
                        "cfop": cfop_regra,
                        "reason": (f"CFOP de Frete {cfop_regra} não permite crédito de ICMS, "
                                   f"mas SPED creditou R$ {sped_icms:.2f}{origem_cfop}"),
                        "acao": "glosar",
                        "sped_cfop": sped_cfop,
                        "sped_icms": sped_icms,
                        "xml_icms": xml_icms
                    })
                elif allow_credit:
                    if abs(sped_icms - xml_icms) > 0.05:
                        razao_cte = (
                            f"ICMS do frete não aproveitado: o CFOP {cfop_regra} permite crédito e o "
                            f"CT-e tem R$ {xml_icms:.2f}{detalhe_icms}, mas o SPED não creditou "
                            f"nada.{origem_cfop}"
                            if sped_icms <= 0.01 < xml_icms else
                            f"Divergência de ICMS no CT-e: SPED R$ {sped_icms:.2f} "
                            f"vs XML R$ {xml_icms:.2f}{detalhe_icms}{origem_cfop}"
                        )
                        results["icms_issues"].append({
                            "key": key,
                            "num": d100.get("num", ""),
                            "emit_name": xml_data.get("emit_name", ""),
                            "num_item": "CT-e",
                            "descr": "Conhecimento de Transporte (Frete)",
                            "cfop": cfop_regra,
                            "reason": razao_cte,
                            "acao": "ajustar",
                            # O CFOP escriturado identifica QUAL D190 corrigir: o
                            # laço roda por CFOP do analítico e o valor do D100 é
                            # do documento inteiro. Sem ele a correção do frete
                            # não sabe em qual linha gravar.
                            "sped_cfop": sped_cfop,
                            "sped_icms": sped_icms,
                            "xml_icms": xml_icms,
                            "xml_bc_icms": xml_data.get("vl_bc", 0.0)
                        })

                if expected_cfop:
                    if sped_cfop != expected_cfop:
                        results["cte_cfop_classifications_issues"].append({
                            "key": key,
                            "num": d100.get("num", ""),
                            "emit_name": xml_data.get("emit_name", ""),
                            "emit_uf": emit_uf,
                            "supplier_cfop": xml_cfop,
                            "sped_cfop": sped_cfop,
                            "expected_cfop": expected_cfop
                        })
                else:
                    unmapped_cte_key = (company_cnpj, xml_cfop, emit_uf)
                    if unmapped_cte_key not in seen_unmapped_cte:
                        seen_unmapped_cte.add(unmapped_cte_key)
                        # Somente sugestão em memória; gravação depende de confirmação humana.
                        results["unmapped_cte_items"].append({
                            "company_cnpj": company_cnpj,
                            "supplier_cfop": xml_cfop,
                            "emitter_uf": emit_uf,
                            "current_sped_cfop": sped_cfop
                        })

            # -- Aproveitamento de PIS/COFINS sobre o frete (CT-e) --
            # Até 26/08/2026 o motor só lia PIS/COFINS dos produtos (C170) e o
            # frete ficava de fora — crédito legítimo que ninguém conferia.
            #
            # A base é CALCULADA: valor contábil do CT-e menos o ICMS destacado.
            # Não há de onde copiar — o CT-e não tem grupo de PIS/COFINS no XML.
            # O que o cliente escriturou vem do D101 (PIS) e do D105 (COFINS).
            #
            # O CONFRONTO É DE VALOR, não de base: o SPED Fiscal não escritura
            # base de cálculo de PIS/COFINS, só o valor do crédito. Então a base
            # calculada vira valor pelas alíquotas (1,65% e 7,6%) e é isso que se
            # compara com o que o cliente lançou. A base continua na aba, como
            # memória da conta.
            #
            # A aba sai SEMPRE, com o crédito calculado de cada CT-e: é o
            # aproveitamento que o contador confere. A ALEGAÇÃO de divergência
            # só acontece quando o arquivo escritura PIS/COFINS de frete em
            # algum lugar — SPED sem D101/D105 nenhum não escriturou nada, e
            # tratar isso como "não aproveitado" acusaria todo CT-e do mês de
            # uma vez, pelo layout do arquivo e não por erro do cliente.
            vl_contabil_frete = d100.get("vl_contabil", d100.get("vl_doc", 0.0))
            icms_frete = d100.get("vl_icms", 0.0)
            base_calc_frete = base_pis_cofins_frete(vl_contabil_frete, icms_frete)

            pis_calc_frete, cofins_calc_frete = pis_cofins_da_base(base_calc_frete)

            cst_pis_frete = str(d100.get("cst_pis", "") or "").strip()
            cst_cofins_frete = str(d100.get("cst_cofins", "") or "").strip()
            base_sped_pis = _num(d100.get("vl_bc_pis"))
            base_sped_cofins = _num(d100.get("vl_bc_cofins"))
            pis_sped_frete = _num(d100.get("vl_pis"))
            cofins_sped_frete = _num(d100.get("vl_cofins"))
            creditado_frete = round(pis_sped_frete + cofins_sped_frete, 2)
            origem_frete = _nota_cfop(sped_cfops[0], cfop_regra_frete) if sped_cfops else ""

            allow_pis_frete = pis_cofins_rules_map.get((cfop_regra_frete, cst_pis_frete))
            allow_cofins_frete = pis_cofins_rules_map.get((cfop_regra_frete, cst_cofins_frete))

            # Pendência de regra, com o CST real — mesmo critério dos produtos.
            for _cst in {c for c in (cst_pis_frete, cst_cofins_frete) if c}:
                if pis_cofins_rules_map.get((cfop_regra_frete, _cst)) is None:
                    _chave_pend = f"{cfop_regra_frete}_{_cst}"
                    if _chave_pend not in seen_unmapped_pis_cofins:
                        seen_unmapped_pis_cofins.add(_chave_pend)
                        results["unmapped_pis_cofins_rules"].append({
                            "cfop": cfop_regra_frete,
                            "cst_expected": _cst,
                            "descr": "Conhecimento de Transporte (Frete)"
                        })

            elegivel_frete = not (
                cst_pis_frete in CSTS_PIS_COFINS_SEM_CREDITO
                or cst_cofins_frete in CSTS_PIS_COFINS_SEM_CREDITO
                or allow_pis_frete is False
                or allow_cofins_frete is False
            )

            situacao_frete = "Informativo"
            if sped_data.get("bloco_d_tem_pis_cofins"):
                if not elegivel_frete:
                    if creditado_frete > 0.01:
                        situacao_frete = "Crédito indevido"
                        results["pis_cofins_issues"].append({
                            "key": key, "num": d100.get("num", ""),
                            "emit_name": xml_data.get("emit_name", ""),
                            "num_item": "CT-e", "descr": "Conhecimento de Transporte (Frete)",
                            "tipo": "PIS/COFINS Frete",
                            "cfop": cfop_regra_frete, "cst": cst_pis_frete or cst_cofins_frete,
                            "reason": (f"CFOP/CST do frete sem direito a crédito de PIS/COFINS, "
                                       f"mas o SPED creditou R$ {creditado_frete:.2f}{origem_frete}"),
                            "sped_val": creditado_frete, "xml_val": 0.00,
                            "diagnostic": ""
                        })
                elif (abs(pis_sped_frete - pis_calc_frete) > 0.05
                      or abs(cofins_sped_frete - cofins_calc_frete) > 0.05):
                    # Confronto de VALOR: o SPED Fiscal não traz base de
                    # PIS/COFINS, só o crédito. A memória da conta vai junto na
                    # mensagem — sem ela o usuário vê dois números e não sabe de
                    # onde saiu o "correto".
                    memoria = (f"base R$ {base_calc_frete:.2f} = contábil R$ "
                               f"{vl_contabil_frete:.2f} − ICMS R$ {icms_frete:.2f}; "
                               f"{ALIQ_PIS:.2f}% e {ALIQ_COFINS:.2f}%")
                    if creditado_frete <= 0.01 < (pis_calc_frete + cofins_calc_frete):
                        situacao_frete = "Não aproveitado"
                        razao_frete = (
                            f"PIS/COFINS do frete não aproveitado: o CT-e dá direito a "
                            f"R$ {pis_calc_frete:.2f} de PIS e R$ {cofins_calc_frete:.2f} de "
                            f"COFINS ({memoria}), mas o SPED não creditou nada.{origem_frete}"
                        )
                    else:
                        situacao_frete = "Valor divergente"
                        razao_frete = (
                            f"Crédito de PIS/COFINS do frete divergente. SPED PIS R$ "
                            f"{pis_sped_frete:.2f} e COFINS R$ {cofins_sped_frete:.2f} vs "
                            f"calculado PIS R$ {pis_calc_frete:.2f} e COFINS R$ "
                            f"{cofins_calc_frete:.2f} ({memoria}).{origem_frete}"
                        )
                    results["pis_cofins_issues"].append({
                        "key": key, "num": d100.get("num", ""),
                        "emit_name": xml_data.get("emit_name", ""),
                        "num_item": "CT-e", "descr": "Conhecimento de Transporte (Frete)",
                        "tipo": "PIS/COFINS Frete",
                        "cfop": cfop_regra_frete, "cst": cst_pis_frete or cst_cofins_frete,
                        "reason": razao_frete,
                        "sped_val": creditado_frete,
                        "xml_val": round(pis_calc_frete + cofins_calc_frete, 2),
                        "diagnostic": ""
                    })

            results["pis_cofins_frete"].append({
                "key": key,
                "num": d100.get("num", ""),
                "emit_name": xml_data.get("emit_name", ""),
                "emit_uf": emit_uf,
                "cfop_sped": ", ".join(sped_cfops) if sped_cfops else "",
                "cfop": cfop_regra_frete,
                "vl_contabil": round(_num(vl_contabil_frete), 2),
                "vl_icms": round(_num(icms_frete), 2),
                "base_creditavel": base_calc_frete,
                "aliq_pis": ALIQ_PIS,
                "aliq_cofins": ALIQ_COFINS,
                "pis_calculado": pis_calc_frete,
                "cofins_calculado": cofins_calc_frete,
                "credito_calculado": round(pis_calc_frete + cofins_calc_frete, 2),
                "cst_pis": cst_pis_frete,
                "base_sped_pis": round(base_sped_pis, 2),
                "vl_pis_sped": round(pis_sped_frete, 2),
                "cst_cofins": cst_cofins_frete,
                "base_sped_cofins": round(base_sped_cofins, 2),
                "vl_cofins_sped": round(cofins_sped_frete, 2),
                "credito_sped": creditado_frete,
                "escriturado": bool(d100.get("tem_pis_cofins")),
                "situacao": situacao_frete,
                "status": "Informativo" if situacao_frete == "Informativo" else "",
                # Um CT-e com mais de um CFOP no analítico tem UM só D101/D105:
                # a conferência é do total do documento, e a linha diz isso.
                "obs": ("Documento com mais de um CFOP no D190 — base conferida "
                        "no total do documento") if len(sped_cfops) > 1 else ""
            })

        # Regra 11: Relatório Geral CT-e (COMPILATION)
        for xml_path in set(xmls_map.values()):
            if xml_path.endswith(".xml"):
                xml_data = parse_cte_xml(xml_path)
                if "error" not in xml_data and xml_data.get("cfop"):  # If valid CTE
                    sped_cfops_list = []
                    for k in xml_data.get("nfe_keys", []):
                        if k in sped_data.get("c100", {}):
                            for item in sped_data["c100"][k].get("items", []):
                                sped_cfops_list.append(item.get("cfop", ""))
                                
                    sped_cfops_str = ", ".join(sorted(list(set(sped_cfops_list)))) if sped_cfops_list else "Não Localizado"
                    
                    results["cte_xml_list"].append({
                        "num": xml_data.get("num", ""),
                        "emit_cnpj": xml_data.get("emit_id", ""),
                        "emit_uf": xml_data.get("emit_uf", ""),
                        "rem_name": xml_data.get("rem_name", ""),
                        "dest_name": xml_data.get("dest_name", ""),
                        "toma_name": xml_data.get("tomador_name", ""),
                        "nfe_nums": ", ".join(xml_data.get("nfe_nums", [])) if xml_data.get("nfe_nums") else "N/A",
                        "nfe_keys": ", ".join(xml_data.get("nfe_keys", [])) if xml_data.get("nfe_keys") else "N/A",
                        "nfe_cfop_sped": sped_cfops_str,
                        "vl_doc": xml_data.get("vl_doc", 0.0),
                        "vl_bc": xml_data.get("vl_bc", 0.0),
                        "vl_icms": xml_data.get("vl_icms", 0.0)
                    })

        # Nota de devolução vai para o TOPO da Conferência IPI. O valor do XML
        # ali é a soma de duas tags (vIPI + vIPIDevol) e não bate com o `vIPI`
        # que a pessoa vê ao abrir o arquivo — é a linha que precisa de conferência
        # humana antes de qualquer outra, e no fim de uma lista paginada ela
        # simplesmente não seria vista. Ordenação estável: dentro de cada grupo a
        # ordem de apuração é preservada.
        results["ipi_issues"].sort(
            key=lambda i: 0 if float(i.get("xml_ipi_devol", 0.0) or 0.0) > 0.01 else 1)

        # Computar total de avisos informativos (DIFAL Interno / Interestadual, etc.)
        results["resumo"]["informativos"] = (
            len(results.get("difal_interno", [])) +
            len(results.get("difal_interestadual", []))
        )

        # Filtro rigoroso: contabilizar APENAS registros de erro real na soma de inconsistências
        def count_real_errors(lst):
            count = 0
            for item in lst:
                if isinstance(item, dict):
                    status_val = str(item.get("status", "")).lower()
                    if status_val in ["informativo", "ok", "correto", "info"]:
                        continue
                    if item.get("is_error") is False:
                        continue
                count += 1
            return count

        # Somar apenas listas de divergência fiscal real (excluindo DIFAL/itens informativos)
        error_lists = [
            results.get("missing_xmls", []),
            results.get("value_discrepancies", []),
            results.get("icms_issues", []),
            results.get("ipi_issues", []),
            results.get("pis_cofins_issues", []),
            results.get("icms_st_notes", []),
            results.get("cnpj_emitente_issues", []),
            results.get("cte_tomador_issues", []),
            results.get("cfop_classifications_issues", []),
            results.get("cte_cfop_classifications_issues", []),
            results.get("unmapped_items", []),
            results.get("unmapped_cte_items", []),
            results.get("unmapped_icms_cfops", []),
            results.get("unrecorded_invoices", []),
            results.get("notas_duplicadas", [])
        ]
        results["resumo"]["inconsistencias"] = sum(count_real_errors(lst) for lst in error_lists)

        # -- Sprint 5: Malha Fina (SIEG/SEFAZ vs SPED) --
        # Encontrar chaves de XML baixados que não estão no SPED C100/D100
        sped_keys = set(sped_data["c100"].keys()).union(set(sped_data["d100"].keys()))
        results["icms_mono_considerado"] = bool(considerar_icms_mono)
        results["icms_mono_total"] = round(
            sum(float(i.get("mono_total", 0.0) or 0.0) for i in results["icms_mono"]), 2)
        results["sped_keys_list"] = list(sped_keys)
        # Sem data no 0000 o período viaja NULO até a tela, que desabilita a
        # consulta ao SIEG. Um padrão fixo aqui mandaria buscar janeiro no cofre
        # fiscal e devolveria "nenhuma nota faltando" sobre o mês errado.
        results["periodo_inicio"] = sped_data.get("periodo_inicio")
        results["periodo_fim"] = sped_data.get("periodo_fim")
        for xml_key, xml_path in xmls_map.items():
            if xml_key not in sped_keys:
                # Tentar ler como NFe ou CTe
                xml_data = parse_nfe_xml(xml_path)
                is_cte = False
                if "error" in xml_data:
                    xml_data = parse_cte_xml(xml_path)
                    is_cte = True
                    
                if "error" not in xml_data:
                    tipo = "CTE" if is_cte else "NFE"
                    emit_name = xml_data.get("emit_name", xml_data.get("emit_xNome", "Desconhecido"))
                    emit_cnpj = xml_data.get("emit_id", xml_data.get("emit_CNPJ", ""))
                    if ignore_saidas and str(emit_cnpj).strip() == str(company_cnpj).strip():
                        continue
                    vl_total = str(xml_data.get("vl_doc", xml_data.get("vTPrest", 0.0)))
                    
                    results["unrecorded_invoices"].append({
                        "key": xml_key,
                        "tipo": tipo,
                        "emit_name": emit_name,
                        "vl_total": vl_total
                    })

                    if not is_cte:
                        for xml_item in xml_data.get("items", []):
                            results["relatorio_geral"].append({
                                "key": xml_key,
                                "num_nf": xml_data.get("num", ""),
                                "cnpj_emitente": emit_cnpj,
                                "uf_emitente": xml_data.get("emit_uf", ""),
                                "razao_social": emit_name,
                                "descr_item": xml_item.get("descr", ""),
                                "vl_total": xml_item.get("vl_item", 0.0),
                                "bc_icms": xml_item.get("vl_bc", 0.0),
                                "icms": xml_item.get("vl_icms_normal", xml_item.get("vl_icms", 0.0)),
                                "icms_sn": xml_item.get("vl_icms_sn", 0.0),
                                "ipi": xml_item.get("v_ipi", 0.0),
                                "ipi_devolvido": xml_item.get("v_ipi_devol", 0.0),
                                "st": xml_item.get("vl_icms_st", 0.0),
                                "fcp_st": xml_item.get("fcp_st", 0.0),
                                "cfop_xml": xml_item.get("cfop", ""),
                                "cfop_sped": "Omitida no SPED"
                            })
                    
                    # Salva no banco de dados de malha fina via RAM (sem query no loop)
                    if xml_key not in existing_missing_keys and xml_key not in pending_missing_logs:
                        missing_log = MissingInvoiceLog(
                            access_key=xml_key, tenant_id=tenant_id,
                            invoice_type=tipo, emit_cnpj=emit_cnpj, emit_name=emit_name,
                            vl_total=vl_total, task_id=task_id, status="pendente_proximo_mes"
                        )
                        pending_missing_logs[xml_key] = missing_log

        # Mapeamento para o formato esperado pelo frontend (curation_results.html e Excel)
        results["xmls_faltantes"] = [
            {
                "chave": x["key"],
                "fornecedor": x["emit"],
                "ind_oper": x.get("ind_oper", "0"),
                "tipo_operacao": x.get("tipo_operacao", "Entrada"),
                # Já vem pronto para a tela: string vazia quando o documento
                # não tem CFOP legível, para o template decidir o travessão.
                "cfop": ", ".join(x.get("cfops") or [])
            }
            for x in results["missing_xmls"]
        ]
        
        results["cfops_desconhecidos"] = [
            {
                "company_cnpj": item.get("company_cnpj", ""),
                "cfop": item["supplier_cfop"], 
                "descricao": item["product_description"], 
                "cfop_sugerido": item["current_sped_cfop"],
                "cnpj_emitente": item["supplier_cnpj"],
                "product_code": item.get("product_code", "*")
            } for item in results["unmapped_items"]
        ]
        
        results["divergencias_valor"] = [
            {
                "chave": item["key"], 
                "valor_sped": item["sped_val"], 
                "valor_xml": item["xml_val"]
            } for item in results["value_discrepancies"]
        ]

        # 12. O motor NÃO persiste regras deduzidas por heurística.
        # As sugestões vão apenas no payload (results) e só viram registro no banco
        # quando um humano confirma na tela de auditoria. Isso evita encher a
        # curadoria de palpites que ninguém pediu.
        # O log de notas não lançadas continua sendo gravado: é fato apurado, não palpite.
        try:
            # Reativa/limpa a transação para garantir conexão limpa (evita timeout por inatividade AWS/Supabase)
            db_session.rollback()
        except Exception:
            pass

        for log in pending_missing_logs.values():
            db_session.add(log)

        # Ordenar relatório geral por número de NF
        results["relatorio_geral"].sort(key=lambda x: str(x.get("num_nf", "")).zfill(10))
        results["icms_st_calculation"].sort(key=lambda x: str(x.get("num_nf", "")).zfill(10))

        # Finalizar status
        task.status = "concluido"
        task.result_data = json.dumps(results)

        # Os dois números do resumo saem também para colunas próprias. O Radar
        # de Atividade soma isto de centenas de auditorias para montar o painel,
        # e result_data tem alguns MB por auditoria — lê-lo para contar duas
        # linhas seria baixar o relatório inteiro de todo mundo a cada tela.
        _resumo = results.get("resumo") or {}
        task.notas_processadas = int(_resumo.get("notas_processadas") or 0)
        task.inconsistencias = int(_resumo.get("inconsistencias") or 0)

        # A competência do 0000 vira coluna pelo mesmo motivo — é ela que
        # responde "o abril desta empresa já foi conferido?", e a data do
        # upload não responde: reprocessar abril em setembro é rotina. Fica
        # NULA quando o SPED não trouxe o período; ver core/competencia.py.
        task.competencia = competencia.de_data(results.get("periodo_inicio"))
        db_session.commit()
        
    except Exception:
        # O traceback fica SÓ no log. error_message é renderizado na tela do
        # cliente: gravá-lo aqui expunha caminho de arquivo, versão de biblioteca
        # e trecho de código — e, como a auditoria processa XML de fornecedor,
        # um texto de exceção com dado de terceiro virava XSS armazenado.
        # O task_id correlaciona o que o usuário vê com a linha do log.
        logger.exception("Falha na auditoria task_id=%s tenant=%s", task_id, tenant_id)
        try:
            db_session.rollback()
        except Exception:
            pass
        task.status = "erro"
        task.error_message = (
            "Não foi possível concluir a auditoria. Confira o SPED e os XMLs e "
            "tente novamente. Se persistir, informe o código da tarefa ao suporte."
        )
        try:
            db_session.commit()
        except Exception:
            db_session.rollback()
        
    finally:
        # Os XMLs vão embora: são o volume (chegaram a 1,04 GB em temp_uploads)
        # e nada depois da auditoria precisa deles.
        #
        # O SPED FICA. A tela de resultado oferece gerar o arquivo corrigido, e
        # esse arquivo é o ORIGINAL com as correções aprovadas aplicadas — sem
        # ele em disco, a única saída seria pedir o upload de novo do mesmo
        # arquivo que o sistema acabou de ler. São ~90 KB por auditoria contra
        # os megabytes dos XMLs, e ele não fica para sempre:
        # `scripts/limpar_uploads.py` varre temp_uploads com prazo de 7 dias, e
        # a auditoria em curso tem arquivo tocado há minutos.
        if os.path.exists(xml_dir):
            shutil.rmtree(xml_dir)
