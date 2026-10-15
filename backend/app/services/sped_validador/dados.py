"""Os dados do PVA, como o motor os consome.

Os JSON em `dados/` são gerados por `scripts/extrair_dados_pva.py` a partir do
PVA instalado — este módulo só os lê, uma vez por processo. O que é
INTERPRETAÇÃO do Synax (qual tabela atende cada fonte de valores, qual
caractere do código de ajuste diz a apuração) mora aqui e no motor, não no
JSON: o JSON é cópia fiel do PVA e é regravado inteiro a cada extração.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache

PASTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados")
FORMATO = 1

# Pacotes de tabela que valem para qualquer UF.
PACOTES_NACIONAIS = ("GLOBAL", "ST", "IPI", "CARDOC", "CLASCONS")


class DadosAusentes(RuntimeError):
    """Os JSON do PVA não estão na pasta, ou são de outro formato."""


def _ler(nome: str) -> dict:
    caminho = os.path.join(PASTA, nome)
    if not os.path.exists(caminho):
        raise DadosAusentes(
            f"Falta {nome} em {PASTA}. Rode scripts/extrair_dados_pva.py numa "
            "máquina com o PVA da EFD ICMS IPI instalado.")
    with open(caminho, encoding="utf-8") as f:
        dados = json.load(f)
    if dados.get("formato") != FORMATO:
        # Ler formato diferente faria campo renomeado passar como vazio — e
        # campo vazio, no validador, é "sem regra", não erro.
        raise DadosAusentes(
            f"{nome} é do formato {dados.get('formato')}, o motor lê o "
            f"{FORMATO}. Rode scripts/extrair_dados_pva.py de novo.")
    return dados


# ------------------------------------------------------------------ leiaute

@dataclass(frozen=True)
class Campo:
    n: int
    id: str
    tipo: str                       # "C", "N" ou "D"
    tamanho: int | None
    exato: bool
    decimais: int | None
    rotulo: str
    obrigatorio: str | None         # "1", "0" ou None (depende da operação)
    obrig_saida: bool | None
    obrig_entrada: bool | None
    obrig_importacao: bool
    valores: frozenset | None
    fonte: str | None
    fonte_codigos: frozenset | None
    regras: tuple                   # ids das regras do campo


@dataclass(frozen=True)
class RegraAplicada:
    id: str
    tipo: str
    campo: str | None = None
    outros: tuple = ()


@dataclass
class Registro:
    reg: str
    bloco: str
    nivel: int
    pai: str | None
    ocorrencia: int                 # 0 arquivo, 1 por pai, 2 várias
    obrigatorio: int                # 1 sim, 0 não, 2 condicional
    rotulo: str
    chave: tuple
    campos: tuple
    ordem: int                      # posição na pré-ordem do descritor
    modelos: frozenset | None = None
    define_modelo: bool = False
    define_operacao: bool = False
    obrig_importacao: bool = False
    sem_duplicidade_por_chave: bool = False
    obrigatoriedade: tuple = ()
    modificadores: tuple = ()
    regras: tuple = ()
    indice_campo: dict = field(default_factory=dict)   # id -> Campo


@dataclass
class Leiaute:
    ato: str
    estrutura: str
    versao: str
    id: str
    registros: dict                 # reg -> Registro
    regras_molde: tuple

    @property
    def rotulo(self) -> str:
        return f"leiaute {self.ato} (estrutura {self.estrutura} v{self.versao})"


def _campo(c: dict, posicao: int) -> Campo:
    """`posicao` é a ordem do campo no descritor, e é ela que vale.

    O atributo de número do próprio descritor do PVA erra em cinco registros
    do leiaute 020 e anteriores: o 0220 traz FAT_CONV e COD_BARRA os dois
    como campo 3, o C890 dá o número 6 a três campos, o K300 e o 1710 pulam
    números, e o D700 dos leiautes 017/018 tem dois campos 27. A ORDEM dos
    campos é a do Guia em todos; o número, não. Lido pelo número, o motor
    conferia o fator de conversão como se fosse o código de barras e nunca
    lia o 4º campo do 0220.
    """
    return Campo(
        n=posicao, id=c["id"], tipo=c["tipo"], tamanho=c.get("tamanho"),
        exato=bool(c.get("exato")), decimais=c.get("decimais"),
        rotulo=c.get("rotulo", ""), obrigatorio=c.get("obrigatorio"),
        obrig_saida=c.get("obrig_saida"), obrig_entrada=c.get("obrig_entrada"),
        obrig_importacao=bool(c.get("obrig_importacao")),
        valores=frozenset(c["valores"]) if "valores" in c else None,
        fonte=c.get("fonte"),
        fonte_codigos=(frozenset(c["fonte_codigos"])
                       if "fonte_codigos" in c else None),
        regras=tuple(r["id"] for r in c.get("regras", ())),
    )


def _regras(lista) -> tuple:
    return tuple(RegraAplicada(id=r["id"], tipo=r.get("tipo", ""),
                               campo=r.get("campo"),
                               outros=tuple(r.get("outros", ())))
                 for r in lista or ())


@lru_cache(maxsize=None)
def leiaute(ato: str) -> Leiaute | None:
    """O leiaute do COD_VER `ato` ("020"), ou None se não houver descritor."""
    if not os.path.exists(os.path.join(PASTA, f"leiaute_{ato}.json")):
        return None
    bruto = _ler(f"leiaute_{ato}.json")
    registros = {}
    for ordem, r in enumerate(bruto["registros"]):
        campos = tuple(_campo(c, i) for i, c in enumerate(r["campos"], start=1))
        registros[r["reg"]] = Registro(
            reg=r["reg"], bloco=r["bloco"], nivel=r["nivel"], pai=r["pai"],
            ocorrencia=r["ocorrencia"], obrigatorio=r["obrigatorio"],
            rotulo=r.get("rotulo", ""), chave=tuple(r.get("chave", ())),
            campos=campos, ordem=ordem,
            modelos=frozenset(r["modelos"]) if r.get("modelos") else None,
            define_modelo=bool(r.get("define_modelo")),
            define_operacao=bool(r.get("define_operacao")),
            obrig_importacao=bool(r.get("obrig_importacao")),
            sem_duplicidade_por_chave=bool(r.get("sem_duplicidade_por_chave")),
            obrigatoriedade=tuple(r.get("obrigatoriedade", ())),
            modificadores=tuple(r.get("modificadores", ())),
            regras=_regras(r.get("regras")),
            indice_campo={c.id: c for c in campos},
        )
    return Leiaute(ato=bruto["ato"], estrutura=bruto["estrutura"],
                   versao=bruto["versao"], id=bruto["id"],
                   registros=registros,
                   regras_molde=_regras(bruto.get("regras_molde")))


@lru_cache(maxsize=None)
def manifesto() -> dict:
    return _ler("manifesto.json")


@lru_cache(maxsize=None)
def mensagens() -> dict:
    return _ler("mensagens.json")["mensagens"]


@lru_cache(maxsize=None)
def fontes() -> dict:
    return _ler("fontes.json")["fontes"]


def versoes_leiaute() -> dict:
    """{COD_VER: {"inicio": AAAAMMDD, "fim": AAAAMMDD|""}} da tabela do PVA."""
    return manifesto().get("versoes_leiaute", {})


def atos_disponiveis() -> list[str]:
    return sorted(l["ato"] for l in manifesto().get("leiautes", []))


# ------------------------------------------------------------------ tabelas

class Tabelas:
    """Tabelas Externas: "este código existe, e vale nesta data, nesta UF?".

    A resposta junta três fontes, porque é assim que o PVA as organiza: a
    tabela da UF do contribuinte, a GENERICA (que vale para qualquer UF e traz
    "UF" no lugar da sigla — UF009999 é ES009999 para quem é do ES) e os
    pacotes nacionais (CFOP, municípios, CST, modelos...).
    """

    def __init__(self, tipos: dict):
        # {tipo: {pacote: {codigo: [(ini, fim), ...]}}}
        self._idx: dict[str, dict[str, dict[str, list]]] = {}
        self._numericos: dict[int, dict] = {}
        for tipo, pacotes in tipos.items():
            por_pacote = {}
            for pacote, tabela in pacotes.items():
                codigos: dict[str, list] = {}
                for cod, ini, fim in tabela["codigos"]:
                    codigos.setdefault(cod, []).append((ini, fim))
                por_pacote[pacote] = codigos
            self._idx[tipo] = por_pacote

    def _candidatos(self, tipo: str, uf: str) -> list[tuple[dict, str]]:
        """[(códigos, prefixo a trocar)] que valem para a UF."""
        pacotes = self._idx.get(tipo, {})
        saida = []
        if uf and uf in pacotes:
            saida.append((pacotes[uf], ""))
        if "GENERICA" in pacotes:
            saida.append((pacotes["GENERICA"], "UF"))
        for nacional in PACOTES_NACIONAIS:
            if nacional in pacotes:
                saida.append((pacotes[nacional], ""))
        return saida

    def _por_valor(self, codigos: dict) -> dict:
        chave = id(codigos)
        if chave not in self._numericos:
            normal: dict[str, list] = {}
            for cod, vigencias in codigos.items():
                normal.setdefault(cod.lstrip("0") or "0", []).extend(vigencias)
            self._numericos[chave] = normal
        return self._numericos[chave]

    def tem_tabela(self, tipo: str, uf: str) -> bool:
        return any(codigos for codigos, _ in self._candidatos(tipo, uf))

    def consultar(self, tipo: str, codigo: str, uf: str, data: str,
                  numerico: bool = False) -> str:
        """"vigente", "fora_de_vigencia", "inexistente" ou "sem_tabela".

        `data` é AAAAMMDD — a DT_FIN do 0000, que é a data com que o PVA
        confere a vigência (`dataValidaCodigos=Reg_0000_DT_FIN` no
        configurador). Sem tabela para a UF a resposta é "sem_tabela", e o
        motor NÃO acusa nada: tabela que não existe não é prova de código
        inválido.

        `numerico` compara pelo VALOR, ignorando zero à esquerda: o campo
        COD_PAIS do 0150 é numérico de até 5 posições, o Domínio grava o
        Brasil como "01058" e a tabela de países traz "1058". Campo de tamanho
        exato (CFOP, CST, município) não passa por aqui — lá "060" e "60" são
        coisas diferentes.
        """
        candidatos = self._candidatos(tipo, uf)
        if not any(c for c, _ in candidatos):
            return "sem_tabela"
        achou = False
        for codigos, coringa in candidatos:
            # Na GENERICA convivem dois jeitos: o código com "UF" no lugar da
            # sigla (ajustes, UF009999) e o código nacional sem prefixo
            # nenhum (receita GNRE, 100102). Os dois são tentados.
            # O "UF" literal no arquivo não é código de nenhum estado.
            chaves = [] if coringa and codigo.startswith(coringa) else [codigo]
            if coringa and uf and codigo.startswith(uf):
                chaves.append(coringa + codigo[len(uf):])
            if numerico:
                codigos = self._por_valor(codigos)
                chaves = [c.lstrip("0") or "0" for c in chaves]
            for chave in chaves:
                for ini, fim in codigos.get(chave, ()):
                    achou = True
                    if (not ini or ini <= data) and (not fim or data <= fim):
                        return "vigente"
        return "fora_de_vigencia" if achou else "inexistente"


@lru_cache(maxsize=None)
def tabelas() -> Tabelas:
    return Tabelas(_ler("tabelas.json")["tipos"])
