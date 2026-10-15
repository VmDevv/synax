"""Validador de EFD ICMS IPI guiado pelo descritor do PVA.

Confere um arquivo antes da transmissão, sem a tela do PVA: o descritor do
leiaute (extraído do PVA instalado por `scripts/extrair_dados_pva.py`) diz
quais registros existem, em que ordem, com quantos campos, de que tipo e
tamanho, quais valores valem e quais regras se aplicam a cada campo; o motor
implementa as regras GENÉRICAS — as que valem em dezenas de campos — a partir
do Guia Prático. As ~650 regras específicas (uma por campo, com lógica de
negócio própria) ficam de fora desta versão e são CONTADAS: o resultado diz
quantas aplicações de regra o arquivo teria e não foram conferidas, para
ninguém confundir "sem inconsistência" com "o PVA vai aceitar".

Duas escolhas que valem para o motor inteiro:

- **Sem regra, sem acusação.** Tabela externa que não existe para a UF,
  modificador de obrigatoriedade cuja lógica não está implementada, campo
  cuja obrigatoriedade depende de operação que o arquivo não define: em todos
  esses casos o motor se cala. Acusar errado mandaria o cliente mexer em
  arquivo certo — pior que deixar o PVA achar.
- **Lê com o leitor FIEL** (`sped_rewriter/documento.py`), latin-1 byte a
  byte. É como o PVA lê: um campo com acento gravado em UTF-8 tem, para os
  dois, mais caracteres do que parece no editor.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field

from ..sped_rewriter.documento import BOMS, ler_texto
from . import campos as cp
from . import dados
from .contas import REGRAS_DE_CONTA
from .documentos import DE_CAMPO as REGRAS_DE_DOCUMENTO_CAMPO
from .documentos import DE_REGISTRO as REGRAS_DE_DOCUMENTO

# Regras que rodam depois de a árvore inteira estar montada: as de conta
# (somas) e as de documento (chave, operação, CFOP, CST).
REGRAS_POSTERIORES = {**REGRAS_DE_CONTA, **REGRAS_DE_DOCUMENTO}

ERRO = "erro"
ADVERTENCIA = "advertencia"

FISCAL = "efd_icms_ipi"
CONTRIBUICOES = "efd_contribuicoes"

# COD_SIT de documento sem efeito: cancelado (02, 03), denegado (04),
# inutilizado (05). O Guia manda informar só a identificação e nenhum filho.
SITUACOES_SEM_EFEITO = frozenset({"02", "03", "04", "05"})
# Complementar (06), complementar extemporâneo (07) e regime especial (08):
# o Guia reduz os campos obrigatórios à identificação do documento.
SITUACOES_PARCIAIS = frozenset({"06", "07", "08"})
CAMPOS_DE_IDENTIFICACAO = frozenset({
    "REG", "IND_OPER", "IND_EMIT", "COD_MOD", "COD_SIT", "NUM_DOC"})
CAMPOS_DOCUMENTO_PARCIAL = CAMPOS_DE_IDENTIFICACAO | {"DT_DOC", "COD_PART"}

# Modificadores de obrigatoriedade cuja lógica está no código do PVA. Os que
# só tiram a obrigatoriedade de um campo conhecido entram aqui: o motor deixa
# de exigir o campo, sem tentar reproduzir a condição.
CAMPOS_DE_MODIFICADOR = {
    "REGRA_NAO_INFORMAR_COD_PART_14002_V1": {"COD_PART"},
    "REGRA_NAO_INFORMAR_COD_PART_16001_V1": {"COD_PART"},
    "REGRA_NAO_INFORMAR_COD_PART_E113_MO": {"COD_PART"},
    "REGRA_NAO_INFORMAR_COD_PART_E_DT_A_P_D700": {"COD_PART", "DT_A_P"},
    "REGRA_OBRIGATORIO_COD_MOT_REST_COMPL_15001_V1": {"COD_MOT_REST_COMPL"},
    "REGRA_OBRIGATORIO_COD_MOT_REST_COMPL_17001_V1": {"COD_MOT_REST_COMPL"},
    "REGRA_OBRIGATORIEDADE_COD_MOT_RESP_COMPL_17001_V1": {"COD_MOT_REST_COMPL"},
}

# Fonte de valores válidos (data-source do PVA) -> tipos de Tabela Externa.
# É a parte "interpretação" do mapeamento: o configurador do PVA dá só o nome
# da classe Java de cada fonte, e o nome da tabela correspondente é o que o
# Guia (seção 4 e 5) e os arquivos em TabelasExternas mostram.
TABELA_DA_FONTE = {
    "CFOP": ("CFOP",),
    "COD_MOD": ("MOD_DOC",),
    "COD_MOD_ECF": ("MOD_DOC",),
    "COD_SIT": ("COD_SIT",),
    "CST_ICMS": ("SIT_TRIB_ICMS",),
    "CST_IPI": ("SIT_TRIB_IPI",),
    "CST_PIS": ("SIT_TRIB_PIS",),
    "CST_COFINS": ("SIT_TRIB_COFINS",),
    "COD_MUN": ("MUNICIPIOS",),
    "COD_PAIS": ("PAISES",),
    "SISCOMEX": ("PAIS_SISCOMEX",),
    "SISCOMEX_APARTIR_2010": ("PAIS_SISCOMEX",),
    "UF_SIGLA_NOME": ("UF_SIG_NOM",),
    "UF_CODIGO_SIGLA": ("UF_COD_SIG",),
    "VERSAO_LEIAUTE": ("VERSOES_LEIAUTE",),
    "CEST": ("COD_CEST",),
    "COD_GEN": ("GEN_ITEM_MERC_SERV",),
    "COD_COMB": ("ANP",),
    "TP_CTE": ("TP_CT_E",),
    "TP_CHC": ("TP_CONHEC_TRANS",),
    "COD_TOT_PAR": ("TOT_PARC_REDZ",),
    "COD_OR": ("OBR_ICMS_RECOL",),
    "COD_OR_E316": ("OBR_ICMS_RECOL",),
    "COD_REC": ("REC_REF_OBRIGACAO",),
    "COD_REC_E250": ("REC_REF_OBRIGACAO",),
    "COD_REC_E316": ("REC_REF_OBRIGACAO",),
    "COD_REC_V2_A10E4V5": ("REC_REF_OBRIGACAO",),
    "COD_REC_E250_V2_A10E4V5": ("REC_REF_OBRIGACAO",),
    "COD_REC_E316_V2_A10E4V5": ("REC_REF_OBRIGACAO",),
    "COD_REC_E250_V3": ("REC_REF_OBRIGACAO",),
    "COD_REC_E316_V3": ("REC_REF_OBRIGACAO",),
    "COD_AJ": ("AJ_BENEF_INCENTIVO",),
    "COD_AJ_IPI": ("AJ_APUR_IPI",),
    "COD_AJ_APURACAO": ("AJ_APUR_DED",),
    "COD_AJ_APURACAO_A7E1V1": ("AJ_APUR_DED",),
    "COD_AJ_APURACAO_ST": ("AJ_APUR_DED",),
    "COD_AJ_APURACAO_ST_A7E1V1": ("AJ_APUR_DED",),
    "COD_AJ_APURACAO_DIFAL": ("AJ_APUR_DED",),
    "COD_AJ_APURACAO_1200": ("AJ_APUR_DED",),
    "COD_INF_ADIC": ("INF_ADIC_APUR",),
    "TIP_UTIL_CREDITO": ("TP_UTIL_CRED_FISCAIS",),
    "COD_MOT_REST_COMPL": ("COD_MOT_REST_COMPL_ST",),
    "COD_PROD_USINAS": ("COD_PROD_USINAS",),
    "COD_INF_ITEM": ("COD_INF_ITEM",),
    "COD_ITEM_IPM": ("COD_ITEM_IPM",),
    "COD_SELO_IPI": ("SELO_CONT_IPI",),
    "CL_ENQ": ("CL_ENQ_IPI",),
    "COD_ENQ": ("ENQ_LEGAL_IPI",),
    "CLAS_ESTAB_IND": ("CLASS_CONTRIB_IPI",),
    "IND_AP": ("IND_SUBAP",),
    "COD_CLASS": ("CL_ITENS_ENG_COM_TELECOM",),
    # A classe de consumo depende do modelo (energia, água, gás, telecom);
    # a união das quatro deixa passar o que é válido em alguma delas.
    "COD_CONS": ("CL_CONS_AGUA", "CL_CONS_COMUN_TELECOM", "CL_CONS_GAS",
                 "FAIXA_CONS_ENG_ELET"),
}

# 3º caractere do código de ajuste da apuração (Tabela 5.1.1): qual apuração.
# 0 = ICMS próprio (E111, 1921), 1 = ICMS ST (E220), 2 = DIFAL e 3 = FCP (E311).
APURACAO_DA_FONTE = {
    "COD_AJ_APURACAO": "0",
    "COD_AJ_APURACAO_A7E1V1": "0",
    "COD_AJ_APURACAO_ST": "1",
    "COD_AJ_APURACAO_ST_A7E1V1": "1",
    "COD_AJ_APURACAO_DIFAL": "23",
}

# Registros cuja UF manda nas tabelas dos filhos: o ajuste e a guia da
# apuração do ICMS ST (E200) e do DIFAL/FCP (E300) são da UF ali informada,
# não da UF do contribuinte.
REGISTROS_COM_UF_PROPRIA = frozenset({"E200", "E300"})

# Regra de existência no cadastro do próprio arquivo -> registro do cadastro.
CADASTRO_DA_REGRA = {
    "REGRA_EXISTE_COD_ITEM_V2": ("0200", "MSG_EXISTE_COD_ITEM"),
    "REGRA_EXISTE_COD_PART_V2": ("0150", "MSG_EXISTE_COD_PART"),
    "REGRA_EXISTE_UNID_V2": ("0190", "MSG_EXISTE_UNID"),
    "REGRA_EXISTE_COD_OBS_V2": ("0460", "MSG_EXISTE_COD_OBS"),
    "REGRA_EXISTE_COD_INF_V2": ("0450", "MSG_EXISTE_COD_INF"),
    "REGRA_EXISTE_COD_NAT_V2": ("0400", "MSG_EXISTE_COD_NAT"),
}

# Regras que o motor confere por outro caminho: o tipo D já é a data válida,
# a duplicidade do 9900 é a duplicidade pela chave REG_BLC, e os
# totalizadores (x990, 9900, 9999) são conferidos de uma vez no fim.
JA_COBERTAS = frozenset({
    "REGRA_DATA_VALIDA_A4E5V1", "REGRA_REGISTRO_DUPLICADO_9900",
    "REGRA_BLOCO_COM_SEM_MOVIMENTO", "REGRA_QTD_LIN_ARQUIVO",
    "REGRA_QTD_REG_BLC_OBRIGATORIO",
} | {f"REGRA_QTD_LIN_BLOCO{b}" for b in "0BCDEGHK19"})

# Gravidade: tudo é erro, menos o que o próprio PVA escreve como "Atenção".
GRAVIDADE = {
    "REGRA_VL_DOC_GRANDE": ADVERTENCIA,
    "REGRA_CODIFICACAO": ADVERTENCIA,
    # Confirmado no gabarito do PVA (LIESA, jul/2026): "Total de Advertências".
    "REGRA_CST_ICMS_RED_BC": ADVERTENCIA,
    "REGRA_CST_ICMS_RED_BC_COM_COD_SIT_C190": ADVERTENCIA,
    "REGRA_CST_ICMS_RED_BC_COM_COD_SIT_C590": ADVERTENCIA,
    "REGRA_CST_ICMS_RED_BC_COM_COD_SIT_D190": ADVERTENCIA,
    "REGRA_CST_ICMS_RED_BC_COM_COD_SIT_D590": ADVERTENCIA,
}


# ------------------------------------------------------------------ saída

@dataclass
class Inconsistencia:
    linha: int | None
    registro: str
    campo: str
    regra: str
    mensagem: str
    gravidade: str = ERRO
    valor: str = ""

    def como_dict(self) -> dict:
        return asdict(self)


@dataclass
class Resultado:
    leiaute: str                      # COD_VER do descritor usado ("020")
    leiaute_rotulo: str
    cod_ver: str                      # COD_VER declarado no 0000
    empresa: dict
    linhas: int
    inconsistencias: list[Inconsistencia]
    total: int                        # pode passar de len(inconsistencias)
    por_regra: Counter
    por_gravidade: Counter
    nao_conferidas: Counter           # regra -> aplicações não conferidas
    pva: dict = field(default_factory=dict)

    @property
    def truncado(self) -> bool:
        return self.total > len(self.inconsistencias)

    @property
    def erros(self) -> int:
        return self.por_gravidade.get(ERRO, 0)

    @property
    def advertencias(self) -> int:
        return self.por_gravidade.get(ADVERTENCIA, 0)

    def como_dict(self) -> dict:
        return {
            "leiaute": self.leiaute, "leiaute_rotulo": self.leiaute_rotulo,
            "cod_ver": self.cod_ver, "empresa": self.empresa,
            "linhas": self.linhas, "total": self.total,
            "erros": self.erros, "advertencias": self.advertencias,
            "truncado": self.truncado,
            "por_regra": dict(self.por_regra.most_common()),
            "nao_conferidas": {
                "aplicacoes": sum(self.nao_conferidas.values()),
                "regras": len(self.nao_conferidas),
                "por_regra": dict(self.nao_conferidas.most_common()),
            },
            "pva": self.pva,
            "inconsistencias": [i.como_dict() for i in self.inconsistencias],
        }


class ArquivoNaoValidavel(Exception):
    """O arquivo não chega a ser validável. `mensagem` vai para a tela."""

    def __init__(self, mensagem: str):
        self.mensagem = mensagem
        super().__init__(mensagem)


class LeiauteInesperado(ArquivoNaoValidavel):
    """É EFD Contribuições, não EFD ICMS IPI."""


class LeiauteSemDescritor(ArquivoNaoValidavel):
    """O COD_VER existe, mas não há descritor dele nos dados extraídos."""


# --------------------------------------------------------------- leiaute

def detectar_leiaute(campos_0000: list[str]) -> str:
    """Qual EFD o 0000 descreve. "" quando não dá para dizer.

    No EFD ICMS IPI os campos 04 e 05 são as datas do período; no EFD
    Contribuições as datas estão em 06 e 07 (04 e 05 são IND_SIT_ESP e
    NUM_REC_ANTERIOR). No Fiscal, 06 e 07 são nome e CNPJ — nunca uma data de
    oito dígitos —, então as duas leituras não se confundem. Mesma regra de
    `audit_engine.sped_parser.detectar_leiaute`.
    """
    def c(n):
        return campos_0000[n - 1].strip() if len(campos_0000) >= n else ""

    if cp.data(c(4)) and cp.data(c(5)):
        return FISCAL
    if cp.data(c(6)) and cp.data(c(7)):
        return CONTRIBUICOES
    return ""


def _escolher_leiaute(cod_ver: str, dt_fin: str) -> dados.Leiaute:
    versoes = dados.versoes_leiaute()
    disponiveis = dados.atos_disponiveis()
    faixa = (f"{disponiveis[0]} a {disponiveis[-1]}" if disponiveis else "nenhum")
    if cod_ver in disponiveis:
        return dados.leiaute(cod_ver)
    if cod_ver in versoes:
        raise LeiauteSemDescritor(
            f"O arquivo declara o leiaute {cod_ver} (COD_VER), e o validador "
            f"tem os leiautes {faixa}. Atualize o PVA e rode "
            "scripts/extrair_dados_pva.py para incluir este leiaute.")
    # COD_VER que não existe: valida pelo leiaute do período, e o próprio
    # 0000 acusa a versão errada (REGRA_VERSAO_LEIAUTE).
    for cod, vig in sorted(versoes.items()):
        if dt_fin and vig.get("inicio", "") <= dt_fin <= (vig.get("fim") or "99999999"):
            if cod in disponiveis:
                return dados.leiaute(cod)
    raise ArquivoNaoValidavel(
        f"O COD_VER do 0000 ('{cod_ver}') não é um leiaute conhecido e o "
        "período não indica outro. Confira o registro 0000.")


# ------------------------------------------------------------------ motor

class _No:
    """Um registro do arquivo já posicionado na árvore do leiaute."""

    __slots__ = ("r", "d", "pai", "filhos", "doc", "uf", "sit", "campos_ok")

    def __init__(self, r, d, pai):
        self.r = r
        self.d = d
        self.pai = pai
        self.filhos: list[_No] = []
        # Documento de referência: o próprio registro ou o ancestral mais
        # próximo que define modelo ou operação (C100, D100, C300...).
        if d.define_modelo or d.define_operacao:
            self.doc = self
        else:
            self.doc = pai.doc if pai else None
        self.uf = None
        self.sit = None
        self.campos_ok = False

    def valor(self, campo_id: str) -> str:
        c = self.d.indice_campo.get(campo_id)
        return self.r.campo(c.n) if c else ""

    @property
    def operacao(self) -> str | None:
        if self.doc is None or "IND_OPER" not in self.doc.d.indice_campo:
            return None
        return self.doc.valor("IND_OPER") or None

    @property
    def modelo(self) -> str | None:
        if self.doc is None or "COD_MOD" not in self.doc.d.indice_campo:
            return None
        return self.doc.valor("COD_MOD") or None


class _Validacao:
    def __init__(self, texto: str, limite: int | None):
        self.texto = texto
        self.limite = limite
        self.saida: list[Inconsistencia] = []
        self.total = 0
        self.por_regra: Counter = Counter()
        self.por_gravidade: Counter = Counter()
        self.nao_conferidas: Counter = Counter()
        self.msgs = dados.mensagens()
        self.tabs = dados.tabelas()
        self.fontes = dados.fontes()

    # ------------------------------------------------------------ registro

    def acusar(self, linha, registro, campo, regra, mensagem, valor="",
               gravidade=None):
        gravidade = gravidade or GRAVIDADE.get(regra, ERRO)
        self.total += 1
        self.por_regra[regra] += 1
        self.por_gravidade[gravidade] += 1
        if self.limite is None or len(self.saida) < self.limite:
            self.saida.append(Inconsistencia(
                linha=linha, registro=registro, campo=campo, regra=regra,
                mensagem=mensagem, gravidade=gravidade, valor=valor))

    def msg(self, chave: str, padrao: str = "") -> str:
        return self.msgs.get(chave) or padrao

    # ------------------------------------------------------------ execução

    def executar(self) -> Resultado:
        texto = self.texto
        for bom in BOMS:
            if texto.startswith(bom):
                self.acusar(1, "", "", "REGRA_FORMATO_LINHA",
                            "O arquivo começa com BOM (marca de UTF-8), que "
                            "não faz parte do leiaute: a primeira linha deixa "
                            "de começar com '|' e o PVA não acha o 0000.")
                texto = texto[len(bom):]
                break

        self.linhas_invalidas = self._linhas_fora_do_leiaute(texto)
        doc = ler_texto(texto)
        self.doc = doc
        if not doc.registros or doc.registros[0].reg != "0000":
            zeros = doc.por_reg("0000")
            if not zeros:
                raise ArquivoNaoValidavel(
                    "O arquivo não tem registro 0000: não é uma EFD ICMS IPI.")
        r0000 = doc.por_reg("0000")[0]
        tipo = detectar_leiaute(r0000.campos)
        if tipo == CONTRIBUICOES:
            raise LeiauteInesperado(
                "O arquivo enviado é um EFD Contribuições (PIS/COFINS). Este "
                "validador confere a EFD ICMS IPI (SPED Fiscal).")
        if tipo != FISCAL:
            raise ArquivoNaoValidavel(
                "O registro 0000 não tem as datas do período nas posições da "
                "EFD ICMS IPI (campos 04 e 05). Confira o cabeçalho do arquivo.")

        self.cod_ver = r0000.campo(2).strip()
        self.dt_ini = cp.data(r0000.campo(4).strip()) or ""
        self.dt_fin = cp.data(r0000.campo(5).strip()) or ""
        self.lt = _escolher_leiaute(self.cod_ver, self.dt_fin)
        d0000 = self.lt.registros["0000"]
        self.uf = r0000.campo(d0000.indice_campo["UF"].n).strip() \
            if "UF" in d0000.indice_campo else ""

        self.nos: list[_No] = []
        self._percorrer(doc)
        self._registros_obrigatorios()
        self._filhos_obrigatorios()
        self._existencia_por_modelo()
        self._cadastros()
        self._contas()
        self._totalizadores(doc)
        self._movimento_dos_blocos()

        # Na ordem do arquivo: o que é do arquivo inteiro (registro que
        # falta, 9900 ausente) primeiro, depois linha a linha. A detecção
        # acontece em duas passadas e sairia embaralhada.
        self.saida.sort(key=lambda i: (i.linha is not None, i.linha or 0))

        man = dados.manifesto()

        def c0(campo_id):
            c = d0000.indice_campo.get(campo_id)
            return r0000.campo(c.n).strip() if c else ""
        return Resultado(
            leiaute=self.lt.ato, leiaute_rotulo=self.lt.rotulo,
            cod_ver=self.cod_ver,
            empresa={"nome": c0("NOME"), "cnpj": c0("CNPJ"), "cpf": c0("CPF"),
                     "uf": self.uf, "ie": c0("IE"), "cod_fin": c0("COD_FIN"),
                     "dt_ini": self.dt_ini, "dt_fin": self.dt_fin},
            linhas=len(doc.registros), inconsistencias=self.saida,
            total=self.total, por_regra=self.por_regra,
            por_gravidade=self.por_gravidade,
            nao_conferidas=self.nao_conferidas,
            pva={"versao": man.get("pva", {}).get("versao", ""),
                 "extraido_em": man.get("extraido_em", "")})

    def _linhas_fora_do_leiaute(self, texto: str) -> int:
        """Linha em branco no meio do arquivo e linha que não é registro.

        O leitor fiel descarta as duas (com aviso, para a reescrita); aqui
        elas são inconsistência, com o número da linha. Em branco só conta se
        houver registro depois: o fim de linha da última linha é normal.
        """
        brancas = []
        invalidas = 0
        for numero, bruta in enumerate(texto.split("\n"), start=1):
            bruta = bruta.rstrip("\r")
            if not bruta.strip():
                brancas.append(numero)
                continue
            for b in brancas:
                self.acusar(b, "", "", "REGRA_FORMATO_LINHA",
                            "Linha em branco no meio do arquivo.")
            brancas = []
            partes = bruta.split("|")
            if not bruta.startswith("|") or len(partes) < 2 or not partes[1].strip():
                invalidas += 1
                self.acusar(numero, "", "", "REGRA_FORMATO_LINHA",
                            "A linha não é um registro do leiaute: toda linha "
                            "começa com '|' seguido do código do registro.",
                            valor=bruta[:60])
        return invalidas

    # ------------------------------------------------------------ percurso

    def _percorrer(self, doc) -> None:
        pilha: dict[int, _No] = {}
        ultimo_irmao: dict[int, tuple[int, str, int]] = {}
        por_arquivo: Counter = Counter()
        por_pai: Counter = Counter()
        chaves_vistas: dict[tuple, dict] = {}
        depois_do_9999 = False
        msg_hier = self.msg("MSG_HIERARQUIA_ARQUIVO",
                            "Organização hierárquica fora dos padrões.")

        for posicao, r in enumerate(doc.registros):
            if not r.crua.endswith("|"):
                self.acusar(r.linha, r.reg, "", "REGRA_FORMATO_LINHA",
                            "A linha não termina com '|': o último campo fica "
                            "sem delimitador e a contagem de campos não fecha.")
            d = self.lt.registros.get(r.reg)
            if d is None:
                self.acusar(r.linha, r.reg, "", "REGRA_REGISTRO_INEXISTENTE",
                            f"{self.msg('MSG_REGISTRO_INEXISTENTE')} O registro "
                            f"{r.reg} não existe no {self.lt.rotulo}.",
                            valor=r.reg)
                continue
            if depois_do_9999:
                self.acusar(r.linha, r.reg, "", "REGRA_HIERARQUIA",
                            f"{msg_hier} Há registro depois do 9999, que "
                            "encerra o arquivo.")

            hierarquia_ok = True
            if d.nivel == 0:
                pai = None
                if r.reg == "0000" and posicao != 0:
                    hierarquia_ok = False
                    self.acusar(r.linha, r.reg, "", "REGRA_HIERARQUIA",
                                f"{msg_hier} O 0000 tem de ser a primeira "
                                "linha do arquivo.")
            else:
                pai = pilha.get(d.nivel - 1)
                if pai is None or pai.d.reg != d.pai:
                    hierarquia_ok = False
                    aberto = (f"o registro aberto no nível {d.nivel - 1} é o "
                              f"{pai.d.reg} da linha {pai.r.linha}"
                              if pai else f"não há {d.pai} aberto antes dele")
                    self.acusar(r.linha, r.reg, "", "REGRA_HIERARQUIA",
                                f"{msg_hier} O {r.reg} ({d.rotulo}) fica "
                                f"dentro de um {d.pai}, e {aberto}.")

            if hierarquia_ok:
                escopo = id(pai) if pai else 0
                anterior = ultimo_irmao.get(escopo)
                if anterior and d.ordem < anterior[0]:
                    dentro = (f" dentro do mesmo {pai.d.reg} (linha "
                              f"{pai.r.linha})" if pai and pai.d.nivel > 0 else "")
                    self.acusar(r.linha, r.reg, "", "REGRA_ORDEM_REGISTROS",
                                f"{msg_hier} O {r.reg} veio depois do "
                                f"{anterior[1]} (linha {anterior[2]}){dentro}; "
                                f"no leiaute o {r.reg} vem antes.")
                else:
                    ultimo_irmao[escopo] = (d.ordem, r.reg, r.linha)

            no = _No(r, d, pai)
            no.uf = (r.campo(d.indice_campo["UF"].n).strip()
                     if r.reg in REGISTROS_COM_UF_PROPRIA and "UF" in d.indice_campo
                     else (pai.uf if pai else self.uf))
            no.sit = (r.campo(d.indice_campo["COD_SIT"].n).strip()
                      if "COD_SIT" in d.indice_campo
                      else (pai.sit if pai else None))
            if pai is not None:
                pai.filhos.append(no)
            pilha[d.nivel] = no
            for mais_fundo in [n for n in pilha if n > d.nivel]:
                del pilha[mais_fundo]
            self.nos.append(no)
            if r.reg == "9999":
                depois_do_9999 = True

            # Ocorrência
            if d.ocorrencia == 0:
                por_arquivo[r.reg] += 1
                if por_arquivo[r.reg] > 1:
                    self.acusar(r.linha, r.reg, "", "REGRA_OCORRENCIA",
                                self.msg("MSG_OCORRENCIA_POR_ARQUIVO_INVALIDA"))
            elif d.ocorrencia == 1 and pai is not None:
                por_pai[(id(pai), r.reg)] += 1
                if por_pai[(id(pai), r.reg)] > 1:
                    self.acusar(r.linha, r.reg, "", "REGRA_OCORRENCIA",
                                f"{self.msg('MSG_OCORRENCIA_POR_PAI_INVALIDA')} "
                                f"Já há um {r.reg} no {pai.d.reg} da linha "
                                f"{pai.r.linha}.")

            campos_ok = self._campos(no)
            no.campos_ok = campos_ok
            if campos_ok:
                self._duplicidade(no, chaves_vistas)
                self._regras_do_registro(no)
            for mod in d.modificadores:
                if mod not in CAMPOS_DE_MODIFICADOR:
                    self.nao_conferidas[mod] += 1
            for ob in d.obrigatoriedade:
                self.nao_conferidas[ob] += 1

    # -------------------------------------------------------------- campos

    def _campos(self, no: _No) -> bool:
        r, d = no.r, no.d
        if len(r.campos) != len(d.campos):
            self.acusar(r.linha, r.reg, "", "REGRA_NUMERO_CAMPOS",
                        f"{self.msg('MSG_ERRO_QUANTIDADE_CAMPOS_INVALIDA')} O "
                        f"{r.reg} tem {len(r.campos)} campos e o "
                        f"{self.lt.rotulo} pede {len(d.campos)}.",
                        valor=str(len(r.campos)))
            return False

        isentos, so_estes = self._isencoes(no)
        for c in d.campos:
            v = r.campos[c.n - 1]
            if v == "":
                if (so_estes is None or c.id in so_estes) and c.id not in isentos \
                        and self._exigido(c, no):
                    self.acusar(r.linha, r.reg, c.id, "REGRA_CAMPO_OBRIGATORIO",
                                f"{self.msg('MSG_CAMPO_OBRIGATORIO')} "
                                f"({c.id} — {c.rotulo})")
                continue
            if v != v.strip():
                self.acusar(r.linha, r.reg, c.id, "REGRA_ESPACOS_INICIO_FINAL",
                            self.msg("MSG_ESPACOS_INICIO_FINAL"), valor=v)
                v = v.strip()
                if not v:
                    continue
            if not self._formato(no, c, v):
                continue
            if c.valores is not None and v not in c.valores:
                aceitos = sorted(c.valores)
                lista = ", ".join(aceitos[:12]) + (" ..." if len(aceitos) > 12 else "")
                self.acusar(r.linha, r.reg, c.id, "REGRA_VALORES_VALIDOS",
                            f"{self.msg('MSG_VALORES_VALIDOS')} Aceitos: {lista}.",
                            valor=v)
            if c.fonte:
                self._fonte(no, c, v)
            for regra in c.regras:
                verificar = REGRAS_DE_CAMPO.get(regra)
                if verificar is not None:
                    verificar(self, no, c, v)
                elif regra not in JA_COBERTAS:
                    self.nao_conferidas[regra] += 1
        return True

    def _isencoes(self, no: _No) -> tuple[set, set | None]:
        """Campos que deixam de ser exigidos neste registro, e por quê.

        Devolve (isentos, só_estes): `isentos` nunca são exigidos; quando
        `só_estes` não é None, nenhum campo fora dele é exigido.
        """
        isentos: set = set()
        for mod in no.d.modificadores:
            isentos |= CAMPOS_DE_MODIFICADOR.get(mod, set())
        so_estes = None
        if "COD_SIT" in no.d.indice_campo:
            if no.sit in SITUACOES_SEM_EFEITO:
                so_estes = CAMPOS_DE_IDENTIFICACAO
            elif no.sit in SITUACOES_PARCIAIS:
                so_estes = CAMPOS_DOCUMENTO_PARCIAL
        elif no.sit in SITUACOES_SEM_EFEITO:
            # Filho de documento cancelado nem deveria existir; exigir campo
            # dele seria acusar duas vezes a mesma coisa.
            so_estes = frozenset()
        # NFC-e (modelo 65) não identifica o destinatário: COD_PART vazio.
        if no.doc is no and no.modelo == "65":
            isentos.add("COD_PART")
        return isentos, so_estes

    def _exigido(self, c: dados.Campo, no: _No) -> bool:
        if c.obrig_importacao or c.obrigatorio == "1":
            return True
        # Obrigatório só na saída ou só na entrada: depende do IND_OPER do
        # documento. Sem documento que defina a operação, não se exige.
        operacao = no.operacao
        if operacao == "1" and c.obrig_saida:
            return True
        if operacao == "0" and c.obrig_entrada:
            return True
        return False

    def _formato(self, no: _No, c: dados.Campo, v: str) -> bool:
        r = no.r
        if c.tipo == "N":
            if cp.numero(v) is None:
                self.acusar(r.linha, r.reg, c.id, "REGRA_CAMPO_INVALIDO_V2",
                            f"{self.msg('MSG_ERRO_TIPO_CAMPO')} Campo numérico: "
                            "só dígitos e vírgula decimal, sem ponto de milhar "
                            "e sem sinal.", valor=v)
                return False
            casas = cp.casas_decimais(v)
            if c.decimais is None and casas:
                self.acusar(r.linha, r.reg, c.id, "REGRA_CAMPO_INVALIDO_V2",
                            f"{self.msg('MSG_ERRO_TIPO_CAMPO')} O campo é "
                            "inteiro e não admite casas decimais.", valor=v)
                return False
            if c.decimais is not None and casas > c.decimais:
                self.acusar(r.linha, r.reg, c.id, "REGRA_TAMANHO_CAMPO",
                            f"{self.msg('MSG_ERRO_TAMANHO_CAMPO')} No máximo "
                            f"{c.decimais} casas decimais.", valor=v)
            if c.tamanho:
                digitos = cp.digitos_significativos(v)
                if (c.exato and digitos != c.tamanho) or \
                        (not c.exato and digitos > c.tamanho):
                    self._tamanho(no, c, v, digitos)
            return True
        if c.tipo == "D":
            if cp.data(v) is None:
                regra = ("REGRA_DATA_VALIDA_A4E5V1"
                         if "REGRA_DATA_VALIDA_A4E5V1" in c.regras
                         else "REGRA_CAMPO_INVALIDO_V2")
                self.acusar(r.linha, r.reg, c.id, regra,
                            self.msg("MSG_DATA_VALIDA"), valor=v)
                return False
            return True
        if cp.tem_controle(v):
            self.acusar(r.linha, r.reg, c.id, "REGRA_CAMPO_INVALIDO_V2",
                        f"{self.msg('MSG_ERRO_TIPO_CAMPO')} Há caractere de "
                        "controle (tabulação ou quebra) no campo.", valor=v)
            return False
        if c.tamanho and ((c.exato and len(v) != c.tamanho)
                          or (not c.exato and len(v) > c.tamanho)):
            self._tamanho(no, c, v, len(v))
        return True

    def _tamanho(self, no, c, v, medido):
        regra = "exatamente" if c.exato else "no máximo"
        self.acusar(no.r.linha, no.r.reg, c.id, "REGRA_TAMANHO_CAMPO",
                    f"{self.msg('MSG_ERRO_TAMANHO_CAMPO')} O {c.id} tem "
                    f"{regra} {c.tamanho} posições; veio com {medido}.",
                    valor=v)

    def _fonte(self, no: _No, c: dados.Campo, v: str) -> None:
        fonte = self.fontes.get(c.fonte, {})
        if fonte.get("parametros", {}).get("registro"):
            # Cadastro do próprio arquivo (0150, 0200...): conferido no fim,
            # quando todos os cadastros já foram lidos.
            return
        if c.fonte_codigos is not None and v not in c.fonte_codigos:
            self.acusar(no.r.linha, no.r.reg, c.id, "REGRA_VALORES_VALIDOS",
                        f"{self.msg('MSG_VALORES_VALIDOS')} Neste registro: "
                        f"{', '.join(sorted(c.fonte_codigos))}.", valor=v)
            return
        tipos = TABELA_DA_FONTE.get(c.fonte)
        if tipos is None:
            self.nao_conferidas[f"FONTE:{c.fonte}"] += 1
            return
        apuracao = APURACAO_DA_FONTE.get(c.fonte)
        if apuracao and len(v) >= 3 and v[2] not in apuracao:
            self.acusar(no.r.linha, no.r.reg, c.id,
                        "REGRA_VALORES_VALIDOS_TABELA_EXTERNA",
                        f"O 3º caractere do código de ajuste indica a apuração "
                        f"e, no {no.r.reg}, tem de ser "
                        f"{' ou '.join(apuracao)}.", valor=v)
            return
        uf = no.uf or self.uf
        numerico = c.tipo == "N" and not c.exato
        respostas = [self.tabs.consultar(t, v, uf, self.dt_fin, numerico)
                     for t in tipos]
        if "vigente" in respostas or all(x == "sem_tabela" for x in respostas):
            return
        nome = "/".join(tipos)
        if "fora_de_vigencia" in respostas:
            texto = (f"O código {v} existe na tabela {nome}, mas não está "
                     f"vigente em {cp.br(self.dt_fin)} (DT_FIN do 0000).")
        else:
            onde = f" (UF {uf})" if any(
                self.tabs.tem_tabela(t, uf) and t not in ("CFOP", "MUNICIPIOS")
                for t in tipos) else ""
            texto = (f"{self.msg('MSG_VALORES_VALIDOS')} O código {v} não "
                     f"consta da tabela {nome}{onde} das Tabelas Externas.")
        self.acusar(no.r.linha, no.r.reg, c.id,
                    "REGRA_VALORES_VALIDOS_TABELA_EXTERNA", texto, valor=v)

    # -------------------------------------------------------- duplicidade

    def _duplicidade(self, no: _No, vistos: dict) -> None:
        d = no.d
        if not d.chave or d.sem_duplicidade_por_chave:
            return
        valores = []
        for campo_id in d.chave:
            c = d.indice_campo.get(campo_id)
            v = no.r.campo(c.n).strip() if c else ""
            if c and c.tipo == "N" and v:
                n = cp.numero(v)
                if n is not None:
                    v = format(n.normalize(), "f")
            valores.append(v)
        escopo = (id(no.pai) if no.pai else 0, d.reg)
        chave = tuple(valores)
        primeira = vistos.setdefault(escopo, {}).get(chave)
        if primeira is not None:
            descricao = ", ".join(f"{k}={v or 'vazio'}"
                                  for k, v in zip(d.chave, valores))
            self.acusar(no.r.linha, d.reg, "", "REGRA_REGISTRO_DUPLICADO_A7E2V1",
                        f"Duplicidade de ocorrência da chave ({descricao}); "
                        f"a primeira está na linha {primeira}.")
        else:
            vistos[escopo][chave] = no.r.linha

    # ------------------------------------------------- regras do registro

    def _regras_do_registro(self, no: _No) -> None:
        for regra in no.d.regras:
            if (regra.id in CADASTRO_DA_REGRA or regra.id in JA_COBERTAS
                    or regra.id in REGRAS_POSTERIORES):
                continue
            verificar = REGRAS_DE_REGISTRO.get(regra.id)
            if verificar is not None:
                verificar(self, no, regra)
            else:
                self.nao_conferidas[regra.id] += 1

    def _data_do_campo(self, no: _No, campo_id: str | None) -> str | None:
        if not campo_id:
            return None
        return cp.data(no.valor(campo_id).strip())

    # ------------------------------------------------ depois do percurso

    def _registros_obrigatorios(self) -> None:
        presentes = Counter(no.d.reg for no in self.nos)
        for d in self.lt.registros.values():
            if presentes[d.reg]:
                continue
            if d.obrig_importacao or (d.obrigatorio == 1 and d.nivel <= 1):
                self.acusar(None, d.reg, "", "REGRA_REGISTRO_OBRIGATORIO",
                            f"O registro {d.reg} ({d.rotulo}) é obrigatório e "
                            "não está no arquivo.")

    def _filhos_obrigatorios(self) -> None:
        exigidos: dict[str, list] = {}
        for d in self.lt.registros.values():
            if d.obrigatorio == 1 and d.nivel >= 2 and d.pai:
                exigidos.setdefault(d.pai, []).append(d)
        msg = self.msg("MSG_REGISTRO_OBRIGATORIO",
                       "Registro filho obrigatório não foi informado.")
        for no in self.nos:
            filhos = exigidos.get(no.d.reg)
            if not filhos:
                continue
            if no.d.nivel == 1 and no.valor("IND_MOV") != "0":
                continue
            if no.sit in SITUACOES_SEM_EFEITO:
                continue
            if no.d.modificadores and "COD_SIT" not in no.d.indice_campo:
                # Obrigatoriedade modificada por regra que não implementamos
                # (ex.: REGRA_VL_BRT_ZERO no C405): melhor calar.
                continue
            tem = {f.d.reg for f in no.filhos}
            modelo = no.modelo
            for d in filhos:
                if d.modelos and (not modelo or modelo not in d.modelos):
                    continue
                if d.reg not in tem:
                    self.acusar(no.r.linha, no.d.reg, "",
                                "REGRA_REGISTRO_OBRIGATORIO",
                                f"{msg} O {no.d.reg} da linha {no.r.linha} não "
                                f"tem {d.reg} ({d.rotulo}).")

    def _existencia_por_modelo(self) -> None:
        msg = self.msg("MSG_EXISTENCIA_POR_MODELO")
        for no in self.nos:
            d = no.d
            if not d.modelos or d.define_modelo or no.pai is None:
                continue
            modelo = no.pai.modelo
            if modelo and modelo not in d.modelos:
                self.acusar(no.r.linha, d.reg, "", "REGRA_EXISTENCIA_POR_MODELO",
                            f"{msg} O {d.reg} só existe para os modelos "
                            f"{', '.join(sorted(d.modelos))}; o documento é "
                            f"modelo {modelo}.", valor=modelo)

    def _cadastros(self) -> None:
        """Código usado nos registros tem de estar no cadastro do arquivo."""
        campo_do_cadastro: dict[str, str] = {}
        for fonte in self.fontes.values():
            p = fonte.get("parametros", {})
            if p.get("registro") and p.get("campoCodigo"):
                campo_do_cadastro.setdefault(p["registro"], p["campoCodigo"])
        cadastros: dict[str, set] = {}
        for no in self.nos:
            campo_codigo = campo_do_cadastro.get(no.d.reg)
            if campo_codigo:
                cadastros.setdefault(no.d.reg, set()).add(
                    no.valor(campo_codigo).strip())

        for no in self.nos:
            if len(no.r.campos) != len(no.d.campos):
                continue
            ja = set()
            for regra in no.d.regras:
                alvo = CADASTRO_DA_REGRA.get(regra.id)
                if not alvo:
                    continue
                registro, chave_msg = alvo
                for c in no.d.campos:
                    p = self.fontes.get(c.fonte or "", {}).get("parametros", {})
                    if p.get("registro") != registro:
                        continue
                    v = no.r.campo(c.n).strip()
                    if v and v not in cadastros.get(registro, ()):
                        ja.add(c.id)
                        self.acusar(no.r.linha, no.d.reg, c.id, regra.id,
                                    f"{self.msg(chave_msg)} O código {v} não "
                                    f"está em nenhum {registro}.", valor=v)
            # Fonte de cadastro que se valida sozinha (sem o sufixo _SV).
            for c in no.d.campos:
                if c.id in ja or not c.fonte:
                    continue
                p = self.fontes.get(c.fonte, {}).get("parametros", {})
                registro = p.get("registro")
                if not registro or p.get("validar") == "false" or "condicao" in p:
                    continue
                v = no.r.campo(c.n).strip()
                if v and v not in cadastros.get(registro, ()):
                    self.acusar(no.r.linha, no.d.reg, c.id, "REGRA_VALORES_VALIDOS",
                                f"{self.msg(p.get('codMsg', ''), 'Código inexistente.')} "
                                f"O código {v} não está em nenhum {registro}.",
                                valor=v)

    def _contas(self) -> None:
        """Regras de conta (`contas.py`), com o arquivo inteiro já em árvore.

        Registro com campo inválido fica de fora: somar um valor que nem
        número é acusaria a mesma coisa duas vezes.
        """
        for no in self.nos:
            if not no.campos_ok:
                continue
            for regra in no.d.regras:
                verificar = REGRAS_POSTERIORES.get(regra.id)
                if verificar is not None:
                    verificar(self, no, regra)

    def _totalizadores(self, doc) -> None:
        """x990, 9900, 9990 e 9999 contra o que o arquivo de fato tem."""
        registros = doc.registros
        inicio: dict[str, int] = {}
        for i, r in enumerate(registros):
            if r.reg == "0000":
                inicio.setdefault("0", i)
            elif len(r.reg) == 4 and r.reg.endswith("001"):
                inicio.setdefault(r.reg[0], i)
        tem_9999 = any(r.reg == "9999" for r in registros)

        def numero_de(r) -> int | None:
            v = r.campo(2).strip()
            return int(v) if v.isdigit() else None

        for i, r in enumerate(registros):
            if len(r.reg) != 4 or not r.reg.endswith("990"):
                continue
            bloco = r.reg[0]
            if bloco not in inicio or inicio[bloco] > i:
                continue
            esperado = i - inicio[bloco] + 1
            if bloco == "9" and tem_9999:
                # O 9990 conta o 9999, que o Guia manda somar ao bloco 9.
                esperado += 1
            informado = numero_de(r)
            if informado is not None and informado != esperado:
                self.acusar(r.linha, r.reg, f"QTD_LIN_{bloco}",
                            f"REGRA_QTD_LIN_BLOCO{bloco}",
                            f"{self.msg('MSG_QTD_LIN_BLOCO' + bloco)} Informado "
                            f"{informado}; o bloco tem {esperado} linhas.",
                            valor=str(informado))

        total = len(registros) + self.linhas_invalidas
        for r in registros:
            if r.reg == "9999":
                informado = numero_de(r)
                if informado is not None and informado != total:
                    self.acusar(r.linha, "9999", "QTD_LIN", "REGRA_QTD_LIN_ARQUIVO",
                                f"A quantidade total de linhas do arquivo "
                                f"informada ({informado}) não confere: o arquivo "
                                f"tem {total}.", valor=str(informado))

        if not any(r.reg == "9001" for r in registros):
            return
        contagem = Counter(r.reg for r in registros)
        declarados: dict[str, int] = {}
        for r in registros:
            if r.reg != "9900":
                continue
            tipo, qtd = r.campo(2).strip(), r.campo(3).strip()
            if tipo in declarados:
                continue      # duplicidade da chave REG_BLC, já acusada
            declarados[tipo] = r.linha
            real = contagem.get(tipo, 0)
            if not qtd.isdigit():
                continue
            if real == 0:
                self.acusar(r.linha, "9900", "REG_BLC", "REGRA_QTD_REG_BLC_OBRIGATORIO",
                            f"O 9900 declara {qtd} linha(s) do registro {tipo}, "
                            "que não aparece no arquivo.", valor=tipo)
            elif int(qtd) != real:
                self.acusar(r.linha, "9900", "QTD_REG_BLC",
                            "REGRA_QTD_REG_BLC_OBRIGATORIO",
                            f"O 9900 do registro {tipo} informa {qtd} linha(s); "
                            f"o arquivo tem {real}.", valor=qtd)
        for tipo, n in sorted(contagem.items()):
            if tipo not in declarados:
                self.acusar(None, "9900", "REG_BLC", "REGRA_QTD_REG_BLC_OBRIGATORIO",
                            f"Falta o 9900 do registro {tipo} ({n} linha(s) no "
                            "arquivo).", valor=tipo)

    def _movimento_dos_blocos(self) -> None:
        conteudo = Counter(no.d.bloco for no in self.nos if no.d.nivel >= 2)
        for no in self.nos:
            if no.d.nivel != 1 or not no.d.reg.endswith("001"):
                continue
            ind = no.valor("IND_MOV").strip()
            bloco = no.d.bloco
            if ind == "0" and not conteudo[bloco]:
                self.acusar(no.r.linha, no.d.reg, "IND_MOV",
                            "REGRA_BLOCO_COM_SEM_MOVIMENTO",
                            self.msg("MSG_BLOCO_COM_MOVIMENTO"), valor=ind)
            elif ind == "1" and conteudo[bloco]:
                self.acusar(no.r.linha, no.d.reg, "IND_MOV",
                            "REGRA_BLOCO_COM_SEM_MOVIMENTO",
                            self.msg("MSG_BLOCO_SEM_MOVIMENTO"), valor=ind)


# ------------------------------------------------- regras genéricas: campo

def _maior_zero(v_: _Validacao, no, c, v):
    n = cp.numero(v)
    if n is not None and n <= 0:
        v_.acusar(no.r.linha, no.r.reg, c.id, "REGRA_MAIOR_ZERO",
                  v_.msg("MSG_MAIOR_ZERO"), valor=v)


def _aliq_menor_1000(v_, no, c, v):
    n = cp.numero(v)
    if n is not None and n > 999:
        v_.acusar(no.r.linha, no.r.reg, c.id, "REGRA_ALIQ_MENOR_1000",
                  v_.msg("MSG_ALIQ_MENOR_1000"), valor=v)


def _data_minima_doc(v_, no, c, v):
    d = cp.data(v)
    if d and d < "20000101":
        v_.acusar(no.r.linha, no.r.reg, c.id, "REGRA_DATA_MINIMA_DOC_FISCAL",
                  v_.msg("MSG_DATA_MINIMA_DOC_FISCAL"), valor=v)


def _ate_dt_fin_0000(v_, no, c, v, regra="REGRA_DATA_MENOR_IGUAL_DT_FIN_0000"):
    d = cp.data(v)
    if d and v_.dt_fin and d > v_.dt_fin:
        v_.acusar(no.r.linha, no.r.reg, c.id, regra,
                  f"{v_.msg('MSG_DATA_MENOR_IGUAL_DT_FIN_0000')} "
                  f"({cp.br(v_.dt_fin)})", valor=v)


def _digito_cnpj(v_, no, c, v):
    if not cp.cnpj_valido(v):
        v_.acusar(no.r.linha, no.r.reg, c.id, "REGRA_DIGITO_CNPJ",
                  v_.msg("MSG_VALIDA_CNPJ", "CNPJ inválido."), valor=v)


def _digito_cpf(v_, no, c, v):
    if not cp.cpf_valido(v):
        v_.acusar(no.r.linha, no.r.reg, c.id, "REGRA_DIGITO_CPF",
                  v_.msg("MSG_VALIDA_CPF", "CPF inválido."), valor=v)


def _cpf_ou_cnpj(v_, no, c, v):
    ok = (cp.cpf_valido(v) if len(v) == 11
          else cp.cnpj_valido(v) if len(v) == 14 else False)
    if not ok:
        v_.acusar(no.r.linha, no.r.reg, c.id, "REGRA_VALIDA_CPF_CNPJ_CAMPO_V2",
                  v_.msg("MSG_VALIDA_CNPJ_CPF", "CNPJ ou CPF inválido."), valor=v)


def _chave(v_, no, c, v):
    if not cp.chave_valida(v):
        v_.acusar(no.r.linha, no.r.reg, c.id, "REGRA_VALIDA_CHV_NFE_CTE",
                  f"{v_.msg('MSG_VALIDA_CHV_GENERICA', 'Chave inválida.')} A "
                  "chave de acesso tem 44 dígitos e o último é o dígito "
                  "verificador (módulo 11).", valor=v)


def _vl_doc_grande(v_, no, c, v):
    n = cp.numero(v)
    if n is not None and n > 200_000_000:
        v_.acusar(no.r.linha, no.r.reg, c.id, "REGRA_VL_DOC_GRANDE",
                  v_.msg("MSG_VL_DOC_GRANDE"), valor=v)


REGRAS_DE_CAMPO = {
    "REGRA_MAIOR_ZERO": _maior_zero,
    "REGRA_ALIQ_MENOR_1000": _aliq_menor_1000,
    "REGRA_DATA_MINIMA_DOC_FISCAL": _data_minima_doc,
    "REGRA_DATA_MENOR_IGUAL_DT_FIN_0000": _ate_dt_fin_0000,
    "REGRA_DIGITO_CNPJ": _digito_cnpj,
    "REGRA_DIGITO_CPF": _digito_cpf,
    "REGRA_VALIDA_CPF_CNPJ_CAMPO_V2": _cpf_ou_cnpj,
    "REGRA_VALIDA_CHV_NFE_CTE": _chave,
    "REGRA_VL_DOC_GRANDE": _vl_doc_grande,
    **REGRAS_DE_DOCUMENTO_CAMPO,
}


# ---------------------------------------------- regras genéricas: registro

def _campo_da_regra(no, regra):
    """(Campo, valor) do campo que a regra aponta; None se vazio/ausente."""
    c = no.d.indice_campo.get(regra.campo or "")
    if c is None:
        return None
    v = no.r.campo(c.n).strip()
    return (c, v) if v else None


def _r_chave(v_, no, regra):
    alvo = _campo_da_regra(no, regra)
    if alvo:
        _chave(v_, no, *alvo)


def _r_ate_dt_fin_0000(v_, no, regra):
    alvo = _campo_da_regra(no, regra)
    if alvo:
        _ate_dt_fin_0000(v_, no, *alvo, regra=regra.id)


def _r_dentro_do_periodo(v_, no, regra):
    alvo = _campo_da_regra(no, regra)
    if not alvo or not v_.dt_ini or not v_.dt_fin:
        return
    c, v = alvo
    d = cp.data(v)
    if d and not (v_.dt_ini <= d <= v_.dt_fin):
        v_.acusar(no.r.linha, no.r.reg, c.id, regra.id,
                  f"{v_.msg('MSG_DATA_ENTRE_DT_INI_DT_FIN_0000')} "
                  f"({cp.br(v_.dt_ini)} a {cp.br(v_.dt_fin)})", valor=v)


def _r_ini_ate_fin(v_, no, regra):
    """DT_INI do registro não pode passar da DT_FIN do MESMO registro."""
    alvo = _campo_da_regra(no, regra)
    outro = regra.outros[0] if regra.outros else "DT_FIN"
    if not alvo:
        return
    c, v = alvo
    d, fim = cp.data(v), cp.data(no.valor(outro).strip())
    if d and fim and d > fim:
        v_.acusar(no.r.linha, no.r.reg, c.id, regra.id,
                  v_.msg("MSG_DATA_MENOR_IGUAL_DT_FIN"), valor=v)


def _r_antes_do_fim(v_, no, regra):
    """Data ESTRITAMENTE anterior à DT_FIN do 0000 (0205: fim do uso do
    código anterior do item — se terminou, terminou antes do período fechar).

    Achado no gabarito: um 0205 com 01/01/3000 nos dois campos.
    """
    alvo = _campo_da_regra(no, regra)
    if not alvo or not v_.dt_fin:
        return
    c, v = alvo
    d = cp.data(v)
    if d and d >= v_.dt_fin:
        v_.acusar(no.r.linha, no.r.reg, c.id, regra.id,
                  f"{v_.msg('MSG_DATA_MENOR_DT_FIN_0000')} "
                  f"({cp.br(v_.dt_fin)})", valor=v)


def _r_reducao_de_base(v_, no, regra):
    """CST terminado em 20 (redução de base) ou 70 (redução com ST) pede
    VL_RED_BC maior que zero. As variantes `_COM_COD_SIT_` ficam de fora no
    documento complementar (06/07), que pode só completar o imposto."""
    if "_COM_COD_SIT_" in regra.id and no.sit in ("06", "07"):
        return
    cst = no.valor("CST_ICMS").strip()
    if cst[-2:] not in ("20", "70"):
        return
    texto = no.valor("VL_RED_BC").strip()
    valor = cp.numero(texto) if texto else None
    if valor is None and texto:
        return                      # formato já acusado
    if not valor:
        v_.acusar(no.r.linha, no.r.reg, "VL_RED_BC", regra.id,
                  f"{v_.msg('MSG_CST_ICMS_RED_BC')} CST {cst}.",
                  valor=texto)


def _r_data_minima(v_, no, regra):
    alvo = _campo_da_regra(no, regra)
    if alvo and (cp.data(alvo[1]) or "99999999") < "20090101":
        v_.acusar(no.r.linha, no.r.reg, alvo[0].id, regra.id,
                  v_.msg("MSG_DATA_MINIMA"), valor=alvo[1])


def _r_mes_escrituracao(v_, no, regra):
    if v_.dt_ini and v_.dt_fin and v_.dt_ini[:6] != v_.dt_fin[:6]:
        v_.acusar(no.r.linha, no.r.reg, regra.campo or "DT_FIN", regra.id,
                  v_.msg("MSG_MES_ESCRITURACAO"), valor=no.valor("DT_FIN"))


def _r_versao_leiaute(v_, no, regra):
    vig = dados.versoes_leiaute().get(v_.cod_ver)
    if not v_.dt_fin:
        return
    certo = next((cod for cod, x in sorted(dados.versoes_leiaute().items())
                  if x.get("inicio", "") <= v_.dt_fin <= (x.get("fim") or "99999999")),
                 None)
    if vig is None or not (vig.get("inicio", "") <= v_.dt_fin
                           <= (vig.get("fim") or "99999999")):
        sugestao = f" Para {cp.br(v_.dt_fin)} o leiaute é o {certo}." if certo else ""
        v_.acusar(no.r.linha, "0000", "COD_VER", regra.id,
                  f"{v_.msg('MSG_VERSAO_LEIAUTE')}{sugestao}", valor=v_.cod_ver)


REGRAS_DE_REGISTRO = {
    "REGRA_VALIDA_CHV_NFE_CTE": _r_chave,
    "REGRA_DATA_MENOR_IGUAL_DT_FIN_0000_DT_DOC": _r_ate_dt_fin_0000,
    "REGRA_DATA_ENTRE_DT_INI_DT_FIN_0000_DT_INI": _r_dentro_do_periodo,
    "REGRA_DATA_ENTRE_DT_INI_DT_FIN_0000_DT_FIN": _r_dentro_do_periodo,
    "REGRA_DATA_MENOR_IGUAL_DT_FIN_DT_INI_DT_FIN": _r_ini_ate_fin,
    "REGRA_DATA_MENOR_IGUAL_DT_FIN_DT_INI_DT_FIM": _r_ini_ate_fin,
    "REGRA_DATA_MENOR_DT_FIN_0000": _r_antes_do_fim,
    "REGRA_CST_ICMS_RED_BC": _r_reducao_de_base,
    "REGRA_CST_ICMS_RED_BC_COM_COD_SIT_C190": _r_reducao_de_base,
    "REGRA_CST_ICMS_RED_BC_COM_COD_SIT_C590": _r_reducao_de_base,
    "REGRA_CST_ICMS_RED_BC_COM_COD_SIT_D190": _r_reducao_de_base,
    "REGRA_CST_ICMS_RED_BC_COM_COD_SIT_D590": _r_reducao_de_base,
    "REGRA_DATA_MENOR_IGUAL_DT_FIN": _r_ini_ate_fin,
    "REGRA_DATA_MINIMA": _r_data_minima,
    "REGRA_MES_ESCRITURACAO": _r_mes_escrituracao,
    "REGRA_VERSAO_LEIAUTE": _r_versao_leiaute,
}

# Regras implementadas, para o relatório de cobertura.
IMPLEMENTADAS = frozenset(REGRAS_DE_CAMPO) | frozenset(REGRAS_DE_REGISTRO) \
    | frozenset(CADASTRO_DA_REGRA) | JA_COBERTAS | frozenset(REGRAS_POSTERIORES)


# -------------------------------------------------------------- entradas

def validar_texto(texto: str, limite: int | None = None) -> Resultado:
    """Valida o conteúdo do arquivo já decodificado (latin-1).

    `limite` corta a LISTA de inconsistências (a contagem segue inteira):
    arquivo com a hierarquia quebrada pode gerar uma por linha.
    """
    return _Validacao(texto, limite).executar()


def validar_bytes(bruto: bytes, limite: int | None = None) -> Resultado:
    """Valida o arquivo como veio do disco ou do upload.

    Decodifica em latin-1, como o PVA. Se o arquivo for UTF-8 com acento, a
    validação segue — mas sai a advertência, porque o PVA vai ler "Ã§" no
    lugar de "ç" e o campo fica com mais caracteres do que aparenta.
    """
    texto = bruto.decode("latin-1")
    resultado = validar_texto(texto, limite)
    corpo = bruto[3:] if bruto.startswith(b"\xef\xbb\xbf") else bruto
    if any(b >= 0x80 for b in corpo):
        try:
            corpo.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            item = Inconsistencia(
                linha=None, registro="", campo="", regra="REGRA_CODIFICACAO",
                mensagem="O arquivo está em UTF-8; o leiaute exige ISO-8859-1 "
                         "(Latin-1). O PVA lê 'Ã§' no lugar de 'ç', e campos "
                         "com acento ficam maiores do que parecem.",
                gravidade=ADVERTENCIA)
            resultado.inconsistencias.insert(0, item)
            resultado.total += 1
            resultado.por_regra[item.regra] += 1
            resultado.por_gravidade[ADVERTENCIA] += 1
    return resultado


def validar_arquivo(caminho: str, limite: int | None = None) -> Resultado:
    with open(caminho, "rb") as f:
        return validar_bytes(f.read(), limite)
