"""Leitura do SPED para a AUDITORIA — os dois leiautes que o escritório recebe.

São dois arquivos diferentes, da mesma empresa e do mesmo mês:

- **EFD ICMS/IPI (SPED Fiscal)**, o de sempre. Traz ICMS e IPI, tem o analítico
  C190 por CST/CFOP/alíquota e apura no Bloco E.
- **EFD Contribuições (PIS/COFINS)**. Traz a base e o valor de PIS/COFINS que o
  Fiscal quase nunca preenche, NÃO tem C190 nem D190, e apura no Bloco M.

O que muda entre os dois, medido nos arquivos reais da MEGA (agosto/2026):

    registro   EFD ICMS/IPI            EFD Contribuições
    0000       DT_INI no campo 04      DT_INI no campo 06  (2 campos a mais antes)
               CNPJ no campo 07        CNPJ no campo 09
    C100       29 campos               29 campos, iguais
    C170       38 campos               37 campos (sem VL_ABAT_NT no fim)
    D100       25 campos               23 campos (sem os dois COD_MUN no fim)
    C190       existe                  NÃO existe
    D190       existe                  NÃO existe
    D101/D105  DIFAL da EC 87/15       PIS e COFINS do frete
    0140/C010  não existem             cadastro e troca de estabelecimento

**Só o 0000 muda de posição.** Todos os campos que a auditoria lê no C100, no
C170 e no D100 estão no MESMO lugar nos dois leiautes — o que difere são campos
no FIM do registro, que este leitor não usa. Por isso existe um percurso só,
parametrizado pela tabela do 0000, em vez de dois leitores copiados: duplicar
150 linhas de posição de campo criaria duas verdades sobre o mesmo C170, e a
segunda estaria errada no dia em que alguém corrigisse só uma delas.

O leiaute é ESCOLHIDO por quem sobe o arquivo (a caixa "SPED Contribuições" na
tela de auditoria), e conferido contra o próprio 0000 antes de ler — ver
`ler_sped`. Sem essa conferência, subir o arquivo trocado não dá erro: o CNPJ
da empresa sai com 8 dígitos (é a data final lida fora de posição), a
competência sai nula, e a auditoria roda inteira e entrega relatório.
"""
import io
import os
from collections import namedtuple

BOM_UTF8 = b"\xef\xbb\xbf"

# Os dois leiautes, pelo nome que viaja no banco e nas opções da auditoria.
FISCAL = "fiscal"
CONTRIBUICOES = "contribuicoes"

# Onde cada informação do registro 0000 fica, por leiaute. São os ÚNICOS campos
# que mudam de posição entre os dois arquivos.
Leiaute = namedtuple("Leiaute", "nome rotulo dt_ini dt_fin nome_empresa cnpj")

LEIAUTE_FISCAL = Leiaute(
    nome=FISCAL, rotulo="EFD ICMS/IPI (SPED Fiscal)",
    dt_ini=4, dt_fin=5, nome_empresa=6, cnpj=7)

# Registros que trocam o estabelecimento corrente. Só existem no EFD
# Contribuições, que é por PESSOA JURÍDICA e cobre todas as filiais num arquivo
# só — o EFD ICMS/IPI é por estabelecimento. Lidos sempre: no arquivo Fiscal
# eles não aparecem, então o ramo nunca dispara.
TROCA_ESTABELECIMENTO = ("A010", "C010", "D010", "F010")


class LeiauteInesperado(Exception):
    """O arquivo não é do leiaute que foi pedido na tela.

    Carrega a mensagem pronta para o usuário: quem sobe o arquivo errado
    precisa saber QUAL arquivo mandou e qual a tela esperava, não um código.
    """

    def __init__(self, esperado: str, encontrado: str):
        self.esperado = esperado
        self.encontrado = encontrado
        rotulos = {FISCAL: LEIAUTE_FISCAL.rotulo,
                   CONTRIBUICOES: "EFD Contribuições (PIS/COFINS)"}
        if encontrado == CONTRIBUICOES:
            texto = (
                "O arquivo enviado é um EFD Contribuições (PIS/COFINS), mas a "
                "opção \"SPED Contribuições\" não está marcada. Marque a opção "
                "ou envie o arquivo do SPED Fiscal (EFD ICMS/IPI).")
        else:
            texto = (
                "O arquivo enviado é um EFD ICMS/IPI (SPED Fiscal), mas a "
                "opção \"SPED Contribuições\" está marcada. Desmarque a opção "
                "ou envie o arquivo do EFD Contribuições.")
        self.mensagem = texto
        super().__init__(
            f"{texto} (esperado: {rotulos.get(esperado, esperado)}; "
            f"encontrado: {rotulos.get(encontrado, encontrado)})")


def clean_float(val):
    """"1234,56" -> 1234.56. Vazio e lixo viram 0.0, sem derrubar a leitura.

    Com vírgula, o ponto é separador de milhar e sai. SEM vírgula, um ponto
    é decimal: "1234.56" (arquivo fora do leiaute) virava 123456.0 aqui
    enquanto o leitor da reescrita (`texto_para_decimal`) lia 1234.56 — os
    dois motores discordavam sobre o mesmo campo por um fator de cem.
    """
    if not val:
        return 0.0
    val = val.strip()
    if "," in val:
        val = val.replace(".", "").replace(",", ".")
    try:
        return float(val)
    except ValueError:
        return 0.0


def _decodificar(bruto: bytes) -> str:
    """UTF-8 (com ou sem BOM) quando o arquivo é UTF-8 válido; senão latin-1.

    O leiaute manda latin-1, e é assim que o Domínio grava. Mas arquivo
    salvo por editor de texto sai em UTF-8, às vezes com BOM — e o BOM lido
    como latin-1 vira "ï»¿" grudado no "|0000|": a linha deixa de começar
    com pipe, e CNPJ, nome e competência sumiam sem aviso nenhum.
    """
    if bruto.startswith(BOM_UTF8):
        return bruto[len(BOM_UTF8):].decode("utf-8", errors="replace")
    try:
        return bruto.decode("utf-8")
    except UnicodeDecodeError:
        return bruto.decode("latin1")


def _abrir(path):
    """O arquivo inteiro como texto, na codificação certa."""
    with open(path, "rb") as f:
        return io.StringIO(_decodificar(f.read()))


def _e_data(valor: str) -> bool:
    """DDMMAAAA — o formato de data do SPED, em qualquer registro."""
    return len(valor) == 8 and valor.isdigit()


def detectar_leiaute(path) -> str:
    """Qual leiaute o arquivo É, lido do próprio 0000. "" quando não dá para dizer.

    A pergunta se responde com duas posições: no EFD ICMS/IPI os campos 04 e 05
    são as datas do período; no EFD Contribuições eles são IND_SIT_ESP e
    NUM_REC_ANTERIOR, e as datas estão em 06 e 07. Não há como confundir, porque
    no Fiscal os campos 06 e 07 são o nome da empresa e o CNPJ — nenhum dos dois
    tem a forma de uma data de oito dígitos.

    Lê só o cabeçalho: a detecção roda no upload, antes de a auditoria começar.
    """
    try:
        with open(path, "rb") as f:
            bruto = f.read(4096)
    except OSError:
        return ""
    for linha in _decodificar(bruto).splitlines():
        if not linha.startswith("|0000|"):
            continue
        campos = linha.strip().split("|")

        def campo(n):
            return campos[n].strip() if len(campos) > n else ""

        if _e_data(campo(4)) and _e_data(campo(5)):
            return FISCAL
        if _e_data(campo(6)) and _e_data(campo(7)):
            return CONTRIBUICOES
        return ""
    return ""


def motivo_sem_leiaute(path) -> str:
    """Por que `detectar_leiaute` não reconheceu o arquivo, dito para quem o enviou.

    Sem o 0000 o percurso pula o cabeçalho calado, e a auditoria rodava inteira
    com o CNPJ da empresa vazio: nenhuma regra de CFOP casa e nenhuma pode ser
    gravada (a chave exige a empresa), e a competência sai nula. Foi assim em
    23/10/2026 com um SPED da PERCAL salvo com um "F" antes do |0000|: 137 itens
    "sem regra" numa empresa com 290 regras aprovadas, e cada "Aprovar" recusado.
    """
    try:
        with open(path, "rb") as f:
            bruto = f.read(4096)
    except OSError:
        return "Não foi possível ler o arquivo SPED enviado."
    for linha in _decodificar(bruto).splitlines():
        if "|0000|" not in linha:
            continue
        if linha.startswith("|0000|"):
            return (
                "O registro 0000 do SPED não traz as datas do período nas posições "
                "do leiaute (campos 04 e 05 no SPED Fiscal, 06 e 07 no EFD "
                "Contribuições). Sem ele não há CNPJ da empresa nem competência "
                "para auditar.")
        sobra = linha[:linha.index("|0000|")]
        if "|" in sobra:
            # É outro registro que cita o 0000 (o |9900|0000| do Bloco 9).
            continue
        trecho = f'"{sobra[:20]}"' if sobra.strip() else "espaços em branco"
        return (
            f"A linha do registro 0000 tem {trecho} antes do |0000|. Esse trecho "
            "não faz parte do SPED (costuma ser uma tecla digitada sem querer ao "
            "abrir o arquivo num editor de texto): apague-o, salve e envie de "
            "novo. Sem o 0000 não há CNPJ da empresa nem competência, e nenhuma "
            "regra de CFOP casaria.")
    return (
        "O arquivo não começa pelo registro |0000|, que traz o CNPJ da empresa e "
        "o período. Confira se é mesmo um SPED (EFD ICMS/IPI ou EFD Contribuições).")


def ler_sped(path, contribuicoes: bool = False):
    """Lê o SPED pelo leiaute escolhido na tela, conferindo o arquivo antes.

    A escolha é de quem sobe o arquivo, não do sistema: os dois leiautes são
    legítimos e a mesma empresa entrega os dois no mesmo mês. Mas o arquivo
    trocado é recusado aqui, com o motivo, em vez de auditado com o cabeçalho
    lido fora de posição.

    Quando o 0000 não permite decidir (arquivo truncado, cabeçalho ilegível), a
    leitura segue com o leiaute pedido: quem trata a ausência é o resto do
    leitor, que já devolve competência nula em vez de inventar um mês.

    A importação do leitor de Contribuições é adiada de propósito: ele importa
    o percurso deste módulo, e um import no topo fecharia o ciclo.
    """
    esperado = CONTRIBUICOES if contribuicoes else FISCAL
    encontrado = detectar_leiaute(path)
    if encontrado and encontrado != esperado:
        raise LeiauteInesperado(esperado, encontrado)

    if contribuicoes:
        from .sped_contribuicoes import parse_sped_contribuicoes
        return parse_sped_contribuicoes(path)
    return parse_sped(path)


# COD_SIT (campo 6 do C100/D100) que tira o documento da apuração: cancelado,
# escrituração extemporânea de cancelado, denegado e numeração inutilizada. Não
# há crédito a conferir em nenhum deles.
#
# Todo o RESTO entra — e isso inclui os que ficavam de fora antes: 01
# (extemporâneo regular), 06 (COMPLEMENTAR), 07 (complementar extemporâneo) e 08
# (regime especial). O filtro era `sit == "00"`, e por isso o CT-e 211146 da
# Transportes Mobiline, escriturado com COD_SIT 06, sumia do motor inteiro: o
# documento nunca chegava a `d100_entries`, então o crédito de R$ 30,45 do XML
# contra os R$ 0,00 do D190 não tinha como ser comparado. Descartar calado é o
# pior dos mundos — o documento existe, tem imposto, e ninguém é avisado.
SITUACOES_SEM_EFEITO = {"02", "03", "04", "05"}


def percorrer(path, leiaute: Leiaute):
    """O percurso do arquivo, igual para os dois leiautes.

    Só o registro 0000 consulta a tabela `leiaute`. Todo o resto — 0150, 0200,
    C100, C170, C190, D100, D190, D101/D105 — lê as MESMAS posições nos dois
    arquivos, e é por isso que existe um percurso só.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Arquivo SPED não encontrado: {path}")

    company_cnpj = ""
    company_name = ""
    # NULO quando o 0000 não trouxer as datas. Aqui já esteve "2026-01-01"
    # fixo: arquivo com cabeçalho ilegível saía gravado como janeiro, e a
    # competência da auditoria — que decide se o mês da empresa está em aberto
    # — afirmava um mês que ninguém leu. Quem consome trata a ausência: a busca
    # no SIEG fica indisponível e a planilha omite a linha do período.
    periodo_inicio = None
    periodo_fim = None
    c100_entries = {}
    d100_entries = {}
    prod_descriptions = {}
    # {COD_PART: {"nome", "cnpj", "cpf", ...}} — ver o 0150 adiante. Era
    # {COD_PART: nome}; quem consome deve usar os helpers do cross_checker
    # (`nome_participante` / `id_participante`) em vez de indexar direto.
    participants = {}
    seen_keys = set()
    duplicate_nfs = []
    # O arquivo escritura PIS/COFINS de frete? Só é verdadeiro se algum D101 ou
    # D105 apareceu. Sem isso, "SPED zerado" significa que o layout não tem o
    # dado — não que o cliente deixou de creditar.
    bloco_d_tem_pis_cofins = False
    # {CNPJ: {...}} do registro 0140, e o estabelecimento corrente. Só o EFD
    # Contribuições os preenche; no Fiscal ficam vazios e cada documento herda
    # o CNPJ do 0000, que é o único que existe ali.
    estabelecimentos = {}
    estab_atual = ""

    with _abrir(path) as f:
        current_c100 = None
        current_d100 = None
        for line in f:
            if not line.startswith("|"):
                continue
            fields = line.strip().split("|")
            if len(fields) < 2:
                continue
            reg = fields[1]

            def campo(n, padrao=""):
                return fields[n] if len(fields) > n else padrao

            if reg == "0000":
                company_cnpj = campo(leiaute.cnpj)
                company_name = campo(leiaute.nome_empresa)
                dt_ini_raw = campo(leiaute.dt_ini)
                dt_fin_raw = campo(leiaute.dt_fin)
                if len(dt_ini_raw) == 8:
                    periodo_inicio = f"{dt_ini_raw[4:8]}-{dt_ini_raw[2:4]}-{dt_ini_raw[0:2]}"
                if len(dt_fin_raw) == 8:
                    periodo_fim = f"{dt_fin_raw[4:8]}-{dt_fin_raw[2:4]}-{dt_fin_raw[0:2]}"
            elif reg == "0140":
                # Cadastro de estabelecimento do EFD Contribuições:
                # |1 REG|2 COD_EST|3 NOME|4 CNPJ|5 UF|6 IE|7 COD_MUN|8 IM|9 SUFRAMA|
                cnpj_est = campo(4).strip()
                if cnpj_est:
                    estabelecimentos[cnpj_est] = {
                        "cod": campo(2),
                        "nome": campo(3),
                        "cnpj": cnpj_est,
                        "uf": campo(5),
                        "ie": campo(6),
                    }
            elif reg in TROCA_ESTABELECIMENTO:
                # |1 REG|2 CNPJ|... — daqui para a frente os documentos são
                # deste estabelecimento, até o próximo registro de troca.
                estab_atual = campo(2).strip()
            elif reg == "0150":
                # Layout 0150: |1 REG|2 COD_PART|3 NOME|4 COD_PAIS|5 CNPJ|6 CPF|
                #              |7 IE|8 COD_MUN|9 SUFRAMA|10 END|...
                #
                # Aqui guardava-se SÓ o nome. O CNPJ do participante é o único
                # lugar do SPED que diz de QUEM é a nota — o C100 traz apenas o
                # COD_PART, um código interno do cliente —, e sem ele não havia
                # como conferir contra o emitente do XML. Nota lançada no
                # fornecedor errado passava limpa: os valores batiam (são os do
                # XML certo), só o participante estava trocado.
                cod_part = campo(2)
                participants[cod_part] = {
                    "cod": cod_part,
                    "nome": campo(3),
                    "cod_pais": campo(4),
                    "cnpj": campo(5).strip(),
                    "cpf": campo(6).strip(),
                    "ie": campo(7),
                    "cod_mun": campo(8),
                }
            elif reg == "0200":
                prod_descriptions[campo(2)] = campo(3)
            elif reg == "C100":
                oper = campo(2)
                ind_emit = campo(3)
                emit = campo(4)
                mod = campo(5)
                sit = campo(6)
                num = campo(8)
                key = campo(9)
                vl_doc = clean_float(campo(12))
                vl_icms = clean_float(campo(22))
                vl_icms_st = clean_float(campo(24))

                if oper in ["0", "1"] and sit not in SITUACOES_SEM_EFEITO:
                    if key and key in seen_keys:
                        duplicate_nfs.append({
                            "chave": key,
                            "num": num,
                            "ind_oper": oper,
                            "vl_doc": vl_doc,
                            "tipo": "NF-e",
                            "tipo_operacao": "Entrada" if oper == "0" else "Saída"
                        })
                    elif key:
                        seen_keys.add(key)
                    current_c100 = key
                    c100_entries[key] = {
                        "num": num,
                        "key": key,
                        "mod": mod,
                        "ind_emit": ind_emit,
                        "emit": emit,
                        "ind_oper": oper,
                        "vl_doc": vl_doc,
                        "vl_icms": vl_icms,
                        "vl_icms_st": vl_icms_st,
                        "vl_opr_c190_sum": 0.0,
                        # De qual filial é o documento. No Fiscal é sempre a
                        # empresa do 0000; no Contribuições vem do C010.
                        "estabelecimento": estab_atual or company_cnpj,
                        "items": []
                    }
                else:
                    current_c100 = None
            elif reg == "C170" and current_c100:
                num_item = campo(2)
                cod_item = campo(3)
                qtd = clean_float(campo(5))
                vl_item = clean_float(campo(7))
                cst = campo(10)
                cfop = campo(11)
                vl_bc_icms = clean_float(campo(13))
                vl_icms = clean_float(campo(15))
                vl_bc_icms_st = clean_float(campo(16))
                vl_icms_st = clean_float(campo(18))

                # Novas Extrações - IPI
                cst_ipi = campo(20)
                vl_bc_ipi = clean_float(campo(22))
                vl_ipi = clean_float(campo(24))

                # Novas Extrações - PIS
                cst_pis = campo(25)
                vl_bc_pis = clean_float(campo(26))
                vl_pis = clean_float(campo(30))

                # Novas Extrações - COFINS
                cst_cofins = campo(31)
                vl_bc_cofins = clean_float(campo(32))
                vl_cofins = clean_float(campo(36))

                c100_entries[current_c100]["items"].append({
                    "num_item": num_item,
                    "cod_item": cod_item,
                    "qtd": qtd,
                    "vl_item": vl_item,
                    "cst": cst,
                    "cfop": cfop,
                    "vl_bc_icms": vl_bc_icms,
                    "vl_icms": vl_icms,
                    "vl_bc_icms_st": vl_bc_icms_st,
                    "vl_icms_st": vl_icms_st,
                    "cst_ipi": cst_ipi,
                    "vl_bc_ipi": vl_bc_ipi,
                    "vl_ipi": vl_ipi,
                    "cst_pis": cst_pis,
                    "vl_bc_pis": vl_bc_pis,
                    "vl_pis": vl_pis,
                    "cst_cofins": cst_cofins,
                    "vl_bc_cofins": vl_bc_cofins,
                    "vl_cofins": vl_cofins
                })
            elif reg == "C190" and current_c100:
                # Não existe no EFD Contribuições. Lá a soma fica em zero, e o
                # confronto interno C100 × C190 do motor é pulado sozinho —
                # ele só roda com soma acima de um centavo.
                vl_opr = clean_float(campo(5))
                if current_c100 in c100_entries:
                    c100_entries[current_c100]["vl_opr_c190_sum"] = round(
                        c100_entries[current_c100].get("vl_opr_c190_sum", 0.0) + vl_opr, 2
                    )
            elif reg == "D100":
                oper = campo(2)
                ind_emit = campo(3)
                emit = campo(4)
                mod = campo(5)
                sit = campo(6)
                num = campo(9)
                key = campo(10)
                # Layout D100: ...|14 CHV_REF|15 VL_DOC|16 VL_DESC|17 IND_FRT|
                #              18 VL_SERV|19 VL_BC_ICMS|20 VL_ICMS|21 VL_NT|22 COD_INF|
                # O índice 22 (COD_INF) vinha sendo lido como se fosse o ICMS — é um
                # código, quase sempre vazio, então o ICMS do CT-e chegava zerado.
                vl_doc = clean_float(campo(15))
                vl_serv = clean_float(campo(18))
                vl_bc_icms = clean_float(campo(19))
                vl_icms = clean_float(campo(20))

                if oper in ["0", "1"] and sit not in SITUACOES_SEM_EFEITO:
                    if key and key in seen_keys:
                        duplicate_nfs.append({
                            "chave": key,
                            "num": num,
                            "ind_oper": oper,
                            "vl_doc": vl_doc,
                            "tipo": "CT-e",
                            "tipo_operacao": "Entrada" if oper == "0" else "Saída"
                        })
                    elif key:
                        seen_keys.add(key)
                    current_d100 = key
                    d100_entries[key] = {
                        "num": num,
                        "key": key,
                        "mod": mod,
                        "ind_emit": ind_emit,
                        "emit": emit,
                        "ind_oper": oper,
                        "estabelecimento": estab_atual or company_cnpj,
                        # Valores do cabeçalho, usados só como fallback quando o
                        # documento não traz analítico (D190).
                        "vl_doc_cab": vl_doc,
                        "vl_serv_cab": vl_serv,
                        "vl_bc_cab": vl_bc_icms,
                        "vl_icms_cab": vl_icms,
                        # Somatório do analítico D190 — é a fonte oficial.
                        "tem_d190": False,
                        "vl_opr_d190": 0.0,
                        "vl_bc_d190": 0.0,
                        "vl_icms_d190": 0.0,
                        "cfops": [],
                        # PIS/COFINS do frete (D101 e D105). Ficam zerados quando o
                        # arquivo não traz esses registros — ver `tem_pis_cofins`.
                        "cst_pis": "",
                        "vl_bc_pis": 0.0,
                        "vl_pis": 0.0,
                        "cst_cofins": "",
                        "vl_bc_cofins": 0.0,
                        "vl_cofins": 0.0,
                        "tem_pis_cofins": False,
                    }
                else:
                    current_d100 = None
            elif reg == "D190" and current_d100:
                # Layout D190: |1 REG|2 CST_ICMS|3 CFOP|4 ALIQ_ICMS|5 VL_OPR|
                #              |6 VL_BC_ICMS|7 VL_ICMS|8 VL_RED_BC|9 COD_OBS|
                # Um CT-e pode ter vários D190 (CST/CFOP/alíquota diferentes);
                # por isso somamos em vez de sobrescrever. Também não existe no
                # EFD Contribuições: lá o CT-e cai no fallback do cabeçalho.
                cfop = campo(3)
                entrada = d100_entries[current_d100]
                if cfop and cfop not in entrada["cfops"]:
                    entrada["cfops"].append(cfop)
                entrada["tem_d190"] = True
                entrada["vl_opr_d190"] += clean_float(campo(5))
                entrada["vl_bc_d190"] += clean_float(campo(6))
                entrada["vl_icms_d190"] += clean_float(campo(7))
            elif reg in ("D101", "D105") and current_d100:
                # PIS/COFINS do serviço de transporte. Layout (EFD Contribuições):
                # |1 REG|2 IND_NAT_FRT|3 VL_ITEM|4 CST|5 NAT_BC_CRED|6 VL_BC|
                # |7 ALIQ|8 VL|9 COD_CTA|  — D101 é o PIS, D105 é a COFINS.
                #
                # CUIDADO: "D101" existe nos DOIS arquivos e são registros
                # DIFERENTES. No SPED Fiscal (EFD ICMS/IPI) o D101 é o
                # complemento de DIFAL da EC 87/15 —
                # |1 REG|2 VL_FCP_UF_DEST|3 VL_ICMS_UF_DEST|4 VL_ICMS_UF_REM|
                # |5 COD_CTA| — e lê-lo como se fosse PIS transforma o valor de
                # ICMS do remetente em "CST" e marca o documento como
                # escriturado, fazendo o motor cobrar crédito que não existe.
                # Por isso a linha só é aceita quando tem a forma do registro
                # de Contribuições: o campo 4 é um CST de dois dígitos.
                cst = campo(4).strip()
                if not (len(fields) > 8 and len(cst) == 2 and cst.isdigit()):
                    continue

                # NÃO existe PIS/COFINS de frete no D100 nem no D190: quem não
                # traz esses dois registros não escriturou nada, e o motor não
                # pode confundir ausência com erro. Daí `tem_pis_cofins`.
                #
                # Somamos em vez de sobrescrever pelo mesmo motivo do D190: o
                # documento pode trazer mais de um registro (naturezas de base
                # de crédito diferentes) e cada um é uma parcela do total.
                entrada = d100_entries[current_d100]
                vl_bc = clean_float(campo(6))
                vl = clean_float(campo(8))
                sufixo = "pis" if reg == "D101" else "cofins"
                if not entrada[f"cst_{sufixo}"]:
                    entrada[f"cst_{sufixo}"] = cst
                entrada[f"vl_bc_{sufixo}"] = round(entrada[f"vl_bc_{sufixo}"] + vl_bc, 2)
                entrada[f"vl_{sufixo}"] = round(entrada[f"vl_{sufixo}"] + vl, 2)
                entrada["tem_pis_cofins"] = True
                bloco_d_tem_pis_cofins = True

    # Consolida os valores oficiais de cada CT-e.
    # Regra: quando existe analítico (D190), ele manda — é onde ficam o valor da
    # prestação e o ICMS efetivamente escriturados por CFOP. O cabeçalho D100 só
    # entra quando o documento não traz D190 algum, que é SEMPRE o caso no EFD
    # Contribuições, onde o registro nem existe.
    for _d in d100_entries.values():
        if _d["tem_d190"]:
            _d["vl_doc"] = _d["vl_opr_d190"]
            _d["vl_bc_icms"] = _d["vl_bc_d190"]
            _d["vl_icms"] = _d["vl_icms_d190"]
        else:
            _d["vl_doc"] = _d["vl_serv_cab"] or _d["vl_doc_cab"]
            _d["vl_bc_icms"] = _d["vl_bc_cab"]
            _d["vl_icms"] = _d["vl_icms_cab"]
        # Valor contábil do frete — a base do crédito de PIS/COFINS sai daqui
        # (contábil menos o ICMS destacado). É o VL_OPR do analítico; sem D190,
        # o VL_DOC do cabeçalho, que é o total do documento. Campo separado de
        # `vl_doc` de propósito: ali o fallback prefere VL_SERV (prestação), que
        # não inclui as demais parcelas do documento.
        _d["vl_contabil"] = _d["vl_opr_d190"] if _d["tem_d190"] else (
            _d["vl_doc_cab"] or _d["vl_serv_cab"])

    return {
        "cnpj": company_cnpj,
        "name": company_name,
        "periodo_inicio": periodo_inicio,
        "periodo_fim": periodo_fim,
        "c100": c100_entries,
        "d100": d100_entries,
        "bloco_d_tem_pis_cofins": bloco_d_tem_pis_cofins,
        "products": prod_descriptions,
        "participants": participants,
        "duplicate_nfs": duplicate_nfs,
        # Quem leu o arquivo, e as filiais que ele cobre. No EFD ICMS/IPI a
        # lista é vazia: aquele arquivo é de um estabelecimento só.
        "leiaute": leiaute.nome,
        "leiaute_rotulo": leiaute.rotulo,
        "estabelecimentos": estabelecimentos,
    }


def parse_sped(path):
    """O SPED Fiscal (EFD ICMS/IPI) — o leitor de sempre, sem mudança nenhuma."""
    return percorrer(path, LEIAUTE_FISCAL)
