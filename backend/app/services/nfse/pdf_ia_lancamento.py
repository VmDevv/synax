"""Lançamento das notas lidas do PDF: arquivo do Domínio e planilha de auditoria.

A regra que este módulo existe para acertar é uma só:

    CFOP 1933  -> prestador e tomador na MESMA UF (operação interna)
    CFOP 2933  -> UFs diferentes (interestadual)

**As duas UFs vêm da própria nota**, lidas do PDF pela IA. O processo é
autônomo: não consulta cadastro de empresa, não exige escolher tomador na tela,
não trava se o banco estiver incompleto.

Duas versões anteriores erraram justamente aqui, e por isso a UF não volta a
sair de lugar nenhum que não seja o documento:

1. `UF_EMPRESA = "ES"` fixo no módulo, escrito quando toda empresa da carteira
   era capixaba. Entrou um cliente do RJ e TODA nota RJ×RJ dele saiu 2933. No
   PDF real de abril, 19 das 59 notas mudavam de CFOP por causa disso.
2. A UF passou a vir do `Tenant.uf`, o que corrigiu o cálculo mas exigia a
   empresa cadastrada — e as 170 empresas estavam sem UF, então nada rodava.

Quando a IA não consegue ler alguma das UFs, o CFOP é PRESUMIDO como 1933 e a
nota sai marcada (`cfop_presumido`). Decisão do escritório (26/08/2026): nada
bloqueia, tudo entra no arquivo. Mas presumido aparece na tela e numa coluna da
planilha — CFOP escrito sem base que ninguém vê é como o bug 1 passou meses.
"""
import io
import logging

logger = logging.getLogger(__name__)

CFOP_INTERNO = "1933"
CFOP_INTERESTADUAL = "2933"

# Usado quando falta alguma das UFs. Ver o cabeçalho: é presunção, não leitura.
CFOP_PRESUMIDO = CFOP_INTERNO

CODIGO_SERVICO_PADRAO = "176"

# Cadastro de fornecedores: completa o que a nota não traz (cidade, endereço,
# código de serviço) e confirma a UF pelo CNPJ. Dado cadastral, não regra de
# imposto por empresa — a UF de um CNPJ é a mesma para todo mundo que compra
# dele. Já `cod_serv` é classificação, e o dia em que dois clientes precisarem
# de códigos diferentes para o mesmo fornecedor isto vira tabela por tenant.
#
# O cadastro é OPCIONAL: fornecedor que não está aqui é lançado do mesmo jeito,
# com o que veio da nota. Ele não pode voltar a ser um pré-requisito.
BASE_CNPJ = {
    "00105063000102": {"razao": "SOLVIS INDUSTRIA E COMERCIO", "uf": "PR", "cidade": "CURITIBA", "endereco": "R DESEMBARGADOR WESTPHALEN", "cod_serv": "179"},
    "34177449000141": {"razao": "INDEXMED TECNOLOGIA LTDA", "uf": "SP", "cidade": "SÃO PAULO", "endereco": "AV ENG LUIZ CARLOS BERRINI", "cod_serv": "179"},
    "54090954000161": {"razao": "CAFFEU E THOMAZ DISTRIBUIDORA", "uf": "ES", "cidade": "SÃO MATEUS", "endereco": "R BOLIVIA", "cod_serv": "176"},
}

CIDADE_AUSENTE = "CIDADE NAO INFORMADA"
ENDERECO_AUSENTE = "ENDERECO NAO INFORMADO"


def _so_digitos(valor) -> str:
    return "".join(filter(str.isdigit, str(valor or "")))


def cnpj_valido(valor) -> bool:
    """Confere os dois dígitos verificadores do CNPJ.

    É a única checagem objetiva que temos sobre a qualidade da leitura: o PDF é
    digitalizado, e um 8 lido como 3 passa despercebido em qualquer conferência
    visual, mas quebra o DV. CNPJ errado no arquivo do Domínio lança a nota no
    fornecedor errado — pior que não lançar.
    """
    cnpj = _so_digitos(valor)
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        return False
    for posicao, pesos in ((12, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]),
                           (13, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])):
        soma = sum(int(cnpj[i]) * peso for i, peso in enumerate(pesos))
        resto = 11 - soma % 11
        if int(cnpj[posicao]) != (0 if resto >= 10 else resto):
            return False
    return True


def _uf_valida(valor) -> str:
    """UF normalizada, ou "" se não for uma sigla de 2 letras."""
    uf = str(valor or "").upper().strip()
    return uf if len(uf) == 2 and uf.isalpha() else ""


def formatar_moeda(valor) -> str:
    try:
        return f"{float(valor):.2f}".replace(".", ",")
    except (TypeError, ValueError):
        return "0,00"


def total_retencoes(nota: dict) -> float:
    """IR + PIS + COFINS + CSLL. As quatro, sempre — a planilha antiga somava
    só três (esquecia a CSLL) e o total não batia com o do TXT."""
    total = 0.0
    for campo in ("retencao_ir", "retencao_pis", "retencao_cofins", "retencao_csll"):
        try:
            total += float(nota.get(campo, 0.0) or 0.0)
        except (TypeError, ValueError):
            pass
    return round(total, 2)


def cfop_da_nota(uf_prestador: str, uf_tomador: str) -> tuple[str, bool]:
    """Devolve (cfop, presumido).

    `presumido=True` significa que faltou alguma das UFs e o 1933 é chute, não
    leitura. Quem chama tem que propagar essa marca até a tela e a planilha.
    """
    uf_p = _uf_valida(uf_prestador)
    uf_t = _uf_valida(uf_tomador)
    if not uf_p or not uf_t:
        return CFOP_PRESUMIDO, True
    return (CFOP_INTERNO if uf_p == uf_t else CFOP_INTERESTADUAL), False


def resolver_fornecedor(nota: dict) -> dict:
    """Completa a nota com o cadastro do fornecedor (por CNPJ, senão por razão).

    Devolve {cnpj, razao, uf, cidade, endereco, cod_serv}. Fornecedor fora do
    cadastro é lançado com o que veio da nota — o cadastro só acrescenta.
    """
    # `cnpj_prestador` é o campo do formato novo; `cnpj` fica aceito porque
    # resultado de análise antiga, salvo antes da mudança, ainda usa o nome
    # velho e não deve virar linha sem CNPJ no arquivo do Domínio.
    cnpj = _so_digitos(nota.get("cnpj_prestador") or nota.get("cnpj"))
    razao_nota = str(nota.get("fornecedor") or "").upper().strip()
    uf = _uf_valida(nota.get("uf_prestador"))

    cadastro = BASE_CNPJ.get(cnpj)
    if cadastro is None and razao_nota:
        # A IA lê a razão social da nota; o cadastro tem a forma abreviada. O
        # casamento é por continência nos dois sentidos, como no código antigo.
        for cnpj_cadastro, dados in BASE_CNPJ.items():
            razao_cadastro = dados["razao"].upper()
            if razao_cadastro in razao_nota or razao_nota in razao_cadastro:
                cadastro = dados
                cnpj = cnpj_cadastro
                break

    if cadastro:
        return {
            "cnpj": cnpj,
            "razao": str(cadastro.get("razao") or razao_nota).upper(),
            # O cadastro tem prioridade sobre a UF lida do PDF: é conferido, a
            # leitura é OCR. Se o cadastro não tiver UF, fica a da nota.
            "uf": _uf_valida(cadastro.get("uf")) or uf,
            "cidade": str(cadastro.get("cidade") or CIDADE_AUSENTE).upper(),
            "endereco": str(cadastro.get("endereco") or ENDERECO_AUSENTE).upper(),
            "cod_serv": str(cadastro.get("cod_serv") or CODIGO_SERVICO_PADRAO),
        }

    return {
        "cnpj": cnpj,
        "razao": razao_nota or "NOME NAO ENCONTRADO",
        "uf": uf,
        "cidade": CIDADE_AUSENTE,
        "endereco": ENDERECO_AUSENTE,
        "cod_serv": CODIGO_SERVICO_PADRAO,
    }


def preparar_notas(notas: list) -> list:
    """Resolve fornecedor e CFOP de cada nota. NENHUMA nota é descartada.

    Cada item devolvido é a nota original acrescida de `_fornecedor`, `_cfop`,
    `_presumido` e `_uf_tomador`.
    """
    preparadas = []
    for nota in notas or []:
        fornecedor = resolver_fornecedor(nota)
        uf_tomador = _uf_valida(nota.get("uf_tomador"))
        cfop, presumido = cfop_da_nota(fornecedor["uf"], uf_tomador)
        preparadas.append({
            **nota,
            "_fornecedor": fornecedor,
            "_uf_tomador": uf_tomador,
            "_cfop": cfop,
            "_presumido": presumido,
            # Fornecedor confirmado pelo cadastro dispensa a checagem de DV: o
            # CNPJ passou a vir de lá, não do OCR.
            "_cnpj_suspeito": (fornecedor["cnpj"] not in BASE_CNPJ
                               and not cnpj_valido(fornecedor["cnpj"])),
        })
    return preparadas


def motivo_presuncao(nota_preparada: dict) -> str:
    """O que há de conferir nesta nota. "" quando está tudo lido e coerente."""
    avisos = []

    if nota_preparada.get("_presumido"):
        faltando = []
        if not nota_preparada["_fornecedor"]["uf"]:
            faltando.append("UF do prestador")
        if not nota_preparada["_uf_tomador"]:
            faltando.append("UF do tomador")
        avisos.append(f"CFOP presumido {CFOP_PRESUMIDO}: a IA não leu "
                      f"{' nem '.join(faltando)} no documento.")

    if nota_preparada.get("_cnpj_suspeito"):
        avisos.append(
            f"CNPJ {nota_preparada['_fornecedor']['cnpj'] or '(vazio)'} não passa no "
            "dígito verificador — provável erro de leitura da digitalização.")

    return (" ".join(avisos) + " Confira antes de importar.") if avisos else ""


def gerar_txt_importacao(notas: list) -> str:
    """Arquivo de importação do Domínio. TODAS as notas entram."""
    preparadas = preparar_notas(notas)

    linhas = []
    for nota in preparadas:
        f = nota["_fornecedor"]
        emissao = nota.get("data_emissao", "")
        # Sem data de entrada, a de emissão é a melhor aproximação e é o que a
        # escrituração usa — diferente da UF, este campo não muda o imposto.
        entrada = nota.get("data_entrada") or emissao

        try:
            valor_bruto = float(nota.get("valor", 0.0) or 0.0)
        except (TypeError, ValueError):
            valor_bruto = 0.0

        valor_str = formatar_moeda(valor_bruto)
        retencoes_str = formatar_moeda(total_retencoes(nota))

        linhas.append(
            f"{f['cnpj']};{f['razao']};{f['uf']};{f['cidade']};{f['endereco']};"
            f"{nota.get('num_nota', '')};;{emissao};{entrada};0;{f['cod_serv']};"
            f"{nota['_cfop']};{valor_str};0;{valor_str};0;0;0;0;0;0;0;0;0;"
            f"{retencoes_str};;;"
        )

    presumidos = sum(1 for n in preparadas if n["_presumido"])
    logger.info("TXT de lançamento: %s linhas, %s com CFOP presumido",
                len(linhas), presumidos)
    return "\n".join(linhas)


def resumo(notas: list) -> dict:
    """Contagens para a tela: quantas internas, interestaduais e presumidas."""
    preparadas = preparar_notas(notas)
    return {
        "total": len(preparadas),
        "internas": sum(1 for n in preparadas
                        if n["_cfop"] == CFOP_INTERNO and not n["_presumido"]),
        "interestaduais": sum(1 for n in preparadas if n["_cfop"] == CFOP_INTERESTADUAL),
        "presumidas": sum(1 for n in preparadas if n["_presumido"]),
        "cnpj_suspeito": sum(1 for n in preparadas if n["_cnpj_suspeito"]),
        "a_conferir": sum(1 for n in preparadas
                          if n["_presumido"] or n["_cnpj_suspeito"]),
        "valor_total": round(sum(
            float(n.get("valor", 0.0) or 0.0) for n in preparadas), 2),
        "tomadores": sorted({n["_uf_tomador"] for n in preparadas if n["_uf_tomador"]}),
    }


CABECALHO_VALIDADAS = ["Data Emissão", "CNPJ Prestador", "Fornecedor", "UF Prest.",
                       "CNPJ Tomador", "UF Tom.", "Nº Nota", "Valor (R$)",
                       "Retenções (R$)", "CFOP Calculado", "Observação"]
CABECALHO_DIVERGENCIAS = ["CNPJ Prestador", "Nº Nota", "Data Emissão",
                          "Motivo do Erro/Divergência"]


def gerar_excel_auditoria(notas_validadas: list, divergencias: list) -> io.BytesIO:
    """Planilha de conferência: notas com o CFOP, e as divergências.

    A coluna "Observação" carrega o aviso de CFOP presumido, e a linha inteira
    fica destacada — é o que impede uma presunção de passar despercebida.
    """
    import openpyxl
    from openpyxl.styles import PatternFill

    from ..planilha_estilo import AMBAR, VERDE, VERMELHO, estilizar_planilha

    ambar_claro = PatternFill(start_color=AMBAR, end_color=AMBAR, fill_type="solid")

    wb = openpyxl.Workbook()

    ws = wb.active
    ws.title = "Notas Validadas"
    for coluna, titulo in enumerate(CABECALHO_VALIDADAS, start=1):
        ws.cell(row=1, column=coluna, value=titulo)

    for i, nota in enumerate(preparar_notas(notas_validadas), start=2):
        f = nota["_fornecedor"]
        aviso = motivo_presuncao(nota)
        valores = [
            nota.get("data_emissao", ""), f["cnpj"], f["razao"], f["uf"],
            _so_digitos(nota.get("cnpj_tomador")), nota["_uf_tomador"],
            nota.get("num_nota", ""), nota.get("valor", 0.0),
            total_retencoes(nota), nota["_cfop"], aviso,
        ]
        for coluna, valor in enumerate(valores, start=1):
            celula = ws.cell(row=i, column=coluna, value=valor)
            if aviso:
                celula.fill = ambar_claro

    ws_div = wb.create_sheet(title="Divergências")
    for coluna, titulo in enumerate(CABECALHO_DIVERGENCIAS, start=1):
        ws_div.cell(row=1, column=coluna, value=titulo)

    for i, d in enumerate(divergencias or [], start=2):
        valores = [
            _so_digitos(d.get("cnpj_prestador") or d.get("cnpj")),
            d.get("num_nota", ""),
            d.get("data_emissao", ""),
            # O motor grava `motivo_divergencia`; o código antigo lia `motivo` e
            # a coluna saía vazia em toda linha.
            d.get("motivo_divergencia") or d.get("motivo", ""),
        ]
        for coluna, valor in enumerate(valores, start=1):
            ws_div.cell(row=i, column=coluna, value=valor)

    # O estilo do escritório (faixa azul, cabeçalho fixo, filtro, largura das
    # colunas e formato de moeda/CNPJ) vem do módulo comum. O destaque âmbar das
    # linhas com CFOP presumido é aplicado ANTES e sobrevive: `estilizar_aba` não
    # mexe no preenchimento das células de dados, só no cabeçalho e na listra.
    estilizar_planilha(wb, cores={"Notas Validadas": VERDE,
                                  "Divergências": VERMELHO},
                       sem_zebra=("Notas Validadas",))

    saida = io.BytesIO()
    wb.save(saida)
    saida.seek(0)
    return saida
