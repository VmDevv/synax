"""O relatório de modificações: o que mudou, por que, e quanto isso vale.

Quem recebe o SPED corrigido precisa poder conferir cada linha alterada sem
abrir o arquivo — e precisa enxergar a diferença entre o que ELE aceitou e o
que veio junto por consequência. Por isso `Alteracao.origem` separa "correção
aceita" de "cascata" e "bloco 9": a pessoa aprovou UMA troca de CFOP e o motor
mexeu em nove linhas; as outras oito ela não pediu e tem o direito de auditar.

Três camadas, da mesma estrutura de dados:

1. **Integridade** — origem, destino, hash dos dois arquivos, competência,
   linhas antes e depois. É o que prova que o arquivo entregue é o que este
   relatório descreve.
2. **Por documento** — antes vs depois, item a item, com a cascata à parte.
3. **Impacto na apuração** — quanto mudou de crédito de ICMS e de IPI.

A terceira camada é sempre CALCULADA dos documentos. Gravá-la no arquivo é
outra coisa, e continua sendo decisão à parte: desde 10/09/2026 o E110 pode ser
reescrito, mas só quando a caixa da apuração está marcada (ver
`cascata.atualizar_e110`). Quando isso acontece, as linhas do Bloco E saem em
seção PRÓPRIA do relatório — é a única parte do arquivo que muda imposto a
recolher, e quem revisa precisa achá-la sem procurar. O E520 (IPI) e os ajustes
(E111/E530) seguem intocados: eles dependem de informação que não está no
documento.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from .cascata import INICIAIS_DE_ENTRADA, Resultado, creditos_de_icms
from .documento import Documento, ler_texto
from .patch import Alteracao, decimal_para_texto, texto_para_decimal

ZERO = Decimal("0")


@dataclass
class ImpactoApuracao:
    icms_creditos_antes: Decimal = ZERO
    icms_creditos_depois: Decimal = ZERO
    ipi_creditos_antes: Decimal = ZERO
    ipi_creditos_depois: Decimal = ZERO
    icms_declarado_e110: Decimal | None = None
    ipi_declarado_e520: Decimal | None = None

    @property
    def icms_diferenca(self) -> Decimal:
        return self.icms_creditos_depois - self.icms_creditos_antes

    @property
    def ipi_diferenca(self) -> Decimal:
        return self.ipi_creditos_depois - self.ipi_creditos_antes


@dataclass
class DocumentoAlterado:
    chave: str
    numero: str
    serie: str
    modelo: str
    emitente: str
    data: str
    valor: str
    aceitas: list[Alteracao] = field(default_factory=list)
    cascata: list[Alteracao] = field(default_factory=list)


@dataclass
class Relatorio:
    origem: str = ""
    destino: str = ""
    hash_origem: str = ""
    hash_destino: str = ""
    competencia: str = ""
    linhas_antes: int = 0
    linhas_depois: int = 0
    documentos: list[DocumentoAlterado] = field(default_factory=list)
    estruturais: list[Alteracao] = field(default_factory=list)
    # O Bloco E fica separado do resto de propósito: é a única parte do arquivo
    # que muda IMPOSTO A RECOLHER, e quem revisa o relatório precisa achá-la
    # sem procurar no meio das linhas de documento.
    apuracao: list[Alteracao] = field(default_factory=list)
    recusas: list = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    impacto: ImpactoApuracao = field(default_factory=ImpactoApuracao)

    @property
    def total_alteracoes(self) -> int:
        return (sum(len(d.aceitas) + len(d.cascata) for d in self.documentos)
                + len(self.estruturais) + len(self.apuracao))


# ------------------------------------------------------------------ cálculo

def _hash(texto: str) -> str:
    return hashlib.sha256(texto.encode("latin-1", errors="replace")).hexdigest()


def _creditos(doc: Documento) -> tuple[Decimal, Decimal]:
    """(crédito de ICMS, crédito de IPI) somados dos analíticos de ENTRADA.

    A conta é a que o Guia manda fazer para o E110 campo 06 e o E520 campo 04,
    restrita aos registros que este motor conhece (C190 e D190). Serve para
    mostrar o impacto — não para gravar a apuração.
    """
    # O ICMS sai de `cascata.creditos_de_icms`: é o mesmo número que vai para o
    # campo 06 do E110 quando a apuração é atualizada.
    ipi = ZERO
    for analitico in doc.por_reg("C190"):           # D190 não tem VL_IPI
        cfop = analitico.campo(3).strip()
        if cfop and cfop[0] in INICIAIS_DE_ENTRADA:
            ipi += texto_para_decimal(analitico.campo(11))
    return creditos_de_icms(doc), ipi


def _competencia(doc: Documento) -> str:
    """Competência do 0000 (DT_INI), no formato que a tela usa."""
    zeros = doc.por_reg("0000")
    if not zeros:
        return ""
    bruto = zeros[0].campo(4)
    if len(bruto) != 8:
        return ""
    return f"{bruto[2:4]}/{bruto[4:8]}"


def _participantes(doc: Documento) -> dict:
    """{COD_PART: nome} do registro 0150.

    O C100/D100 guarda só o código do participante. Um relatório que mostra
    "Emitente 30066" não serve para conferir nada — o nome está no 0150 e sai
    de graça.
    """
    return {p.campo(2): p.campo(3) for p in doc.por_reg("0150")}


def _rotulo_do_documento(registro, nomes: dict) -> DocumentoAlterado:
    """Cabeçalho do documento no relatório, para C100 ou D100.

    Os campos mudam de número entre os dois: no D100 o número do documento é o
    campo 9 (não o 8), a chave é o 10 (não o 9), a data é o 11 e o valor é o 15.
    """
    if registro.reg == "D100":
        chave, numero, data, valor = (registro.campo(10), registro.campo(9),
                                      registro.campo(11), registro.campo(15))
    else:
        chave, numero, data, valor = (registro.campo(9), registro.campo(8),
                                      registro.campo(10), registro.campo(12))
    codigo = registro.campo(4)
    return DocumentoAlterado(
        chave=chave, numero=numero, serie=registro.campo(7),
        modelo=registro.campo(5), emitente=nomes.get(codigo, codigo),
        data=data, valor=valor)


def montar(resultado: Resultado, doc: Documento, texto_original: str,
           origem: str = "", destino: str = "") -> Relatorio:
    """Monta o relatório comparando o documento corrigido com o texto original.

    O original é relido em vez de guardado em memória durante a correção: a
    comparação passa a ser contra o ARQUIVO, não contra um estado intermediário
    que ninguém consegue conferir depois.
    """
    antes = ler_texto(texto_original)
    texto_corrigido = doc.serializar()

    rel = Relatorio(
        origem=origem or doc.caminho, destino=destino,
        hash_origem=_hash(texto_original), hash_destino=_hash(texto_corrigido),
        competencia=_competencia(doc),
        linhas_antes=len(antes.registros), linhas_depois=len(doc.registros),
        recusas=list(resultado.recusas), avisos=list(resultado.avisos))

    icms_antes, ipi_antes = _creditos(antes)
    icms_depois, ipi_depois = _creditos(doc)
    e110 = doc.por_reg("E110")
    e520 = doc.por_reg("E520")
    rel.impacto = ImpactoApuracao(
        icms_creditos_antes=icms_antes, icms_creditos_depois=icms_depois,
        ipi_creditos_antes=ipi_antes, ipi_creditos_depois=ipi_depois,
        icms_declarado_e110=texto_para_decimal(e110[0].campo(6)) if e110 else None,
        ipi_declarado_e520=texto_para_decimal(e520[0].campo(4)) if e520 else None)

    # Agrupa por documento, na ordem em que aparecem no arquivo.
    nomes = _participantes(doc)
    indice = {}
    for registro in doc.por_reg("C100") + doc.por_reg("D100"):
        indice[registro.chave_documento()] = registro

    por_chave: dict[str, DocumentoAlterado] = {}
    for alteracao in resultado.alteracoes:
        if alteracao.origem == "apuração" or alteracao.documento == "(apuração)":
            rel.apuracao.append(alteracao)
            continue
        if alteracao.documento == "(arquivo)":
            rel.estruturais.append(alteracao)
            continue
        alvo = por_chave.get(alteracao.documento)
        if alvo is None:
            registro = indice.get(alteracao.documento)
            alvo = (_rotulo_do_documento(registro, nomes) if registro is not None
                    else DocumentoAlterado(chave=alteracao.documento, numero="",
                                           serie="", modelo="", emitente="",
                                           data="", valor=""))
            por_chave[alteracao.documento] = alvo
        if alteracao.origem in ("cascata", "bloco 9"):
            alvo.cascata.append(alteracao)
        else:
            alvo.aceitas.append(alteracao)

    rel.documentos = list(por_chave.values())
    return rel


# ------------------------------------------------------------------- saídas

def _moeda(valor: Decimal) -> str:
    texto = decimal_para_texto(Decimal(valor), original="0,00")
    inteiro, _, centavos = texto.partition(",")
    negativo = inteiro.startswith("-")
    inteiro = inteiro.lstrip("-")
    grupos = []
    while len(inteiro) > 3:
        grupos.insert(0, inteiro[-3:])
        inteiro = inteiro[:-3]
    grupos.insert(0, inteiro)
    return ("-" if negativo else "") + ".".join(grupos) + "," + centavos


def _data_br(bruto: str) -> str:
    return f"{bruto[:2]}/{bruto[2:4]}/{bruto[4:]}" if len(bruto) == 8 else bruto


def texto(rel: Relatorio) -> str:
    """O relatório em texto — o que vai para a tela e para o log."""
    linhas = []
    add = linhas.append

    add("=" * 78)
    add("RELATÓRIO DE CORREÇÃO DO SPED FISCAL")
    add("=" * 78)
    add(f"Competência : {rel.competencia}")
    add(f"Origem      : {rel.origem}")
    add(f"              sha256 {rel.hash_origem[:32]}...")
    add(f"Destino     : {rel.destino or '(não gravado)'}")
    add(f"              sha256 {rel.hash_destino[:32]}...")
    add(f"Linhas      : {rel.linhas_antes} -> {rel.linhas_depois} "
        f"({rel.linhas_depois - rel.linhas_antes:+d})")
    add(f"Alterações  : {rel.total_alteracoes} em {len(rel.documentos)} documento(s)")

    for doc in rel.documentos:
        add("")
        add("-" * 78)
        rotulo = f"Documento {doc.modelo}/{doc.serie or '-'}/{doc.numero}"
        add(f"{rotulo}    Emitente {doc.emitente}    {_data_br(doc.data)}"
            f"    R$ {doc.valor}")
        if doc.chave:
            add(f"Chave: {doc.chave}")

        if doc.aceitas:
            add("")
            add("  ACEITO PELO USUÁRIO")
            for a in doc.aceitas:
                alvo = f"item {a.num_item}" if a.num_item else a.registro
                add(f"    {alvo:<10} {a.campo:<16} {a.de or '(vazio)':>14}"
                    f"  ->  {a.para or '(vazio)':<14}")
                if a.motivo:
                    add(f"    {'':<10} {a.motivo}")
                if a.aprovada_por:
                    add(f"    {'':<10} aprovado por {a.aprovada_por}"
                        f"{' · ' + a.origem if a.origem else ''}")

        if doc.cascata:
            add("")
            add("  RECALCULADO EM CONSEQUÊNCIA (não pedido, mas obrigatório)")
            for a in doc.cascata:
                if a.campo == "registro":
                    verbo = "criado " if not a.de else "removido"
                    add(f"    {a.registro} {verbo}  {a.de or a.para}")
                else:
                    add(f"    {a.registro:<10} {a.campo:<16} "
                        f"{a.de or '(vazio)':>14}  ->  {a.para or '(vazio)':<14}")

    if rel.estruturais:
        add("")
        add("-" * 78)
        add("CONTADORES DO ARQUIVO")
        for a in rel.estruturais:
            add(f"    {a.registro:<10} {a.campo:<22} {a.de:>8}  ->  {a.para:<8}")

    if rel.apuracao:
        add("")
        add("-" * 78)
        add("APURAÇÃO DO ICMS (E110) — ISTO MUDA O IMPOSTO A RECOLHER")
        for a in rel.apuracao:
            add(f"    {a.registro:<10} {a.campo:<26} "
                f"{a.de or '0,00':>14}  ->  {a.para or '0,00':<14}")
            add(f"               {a.motivo}")

    i = rel.impacto
    add("")
    add("=" * 78)
    add("IMPACTO NA APURAÇÃO (calculado dos documentos — "
        + ("GRAVADO no E110, acima)" if rel.apuracao
           else "NÃO gravado no Bloco E)"))
    add("=" * 78)
    add(f"{'':<34}{'Antes':>14}{'Depois':>14}{'Diferença':>14}")
    add(f"{'Créditos de ICMS (C190 + D190)':<34}"
        f"{_moeda(i.icms_creditos_antes):>14}{_moeda(i.icms_creditos_depois):>14}"
        f"{_moeda(i.icms_diferenca):>14}")
    add(f"{'Créditos de IPI (C190)':<34}"
        f"{_moeda(i.ipi_creditos_antes):>14}{_moeda(i.ipi_creditos_depois):>14}"
        f"{_moeda(i.ipi_diferenca):>14}")

    if i.icms_declarado_e110 is not None:
        add("")
        if rel.apuracao:
            add(f"  O E110 foi atualizado nesta correção e agora declara "
                f"{_moeda(i.icms_declarado_e110)} de crédito de ICMS. Ajustes "
                "(E111) e saldo credor anterior não foram tocados.")
        else:
            add(f"  O E110 do arquivo declara {_moeda(i.icms_declarado_e110)} "
                "de crédito de ICMS e NÃO foi alterado.")
        if abs(i.icms_declarado_e110 - i.icms_creditos_depois) >= Decimal("0.01"):
            add(f"  Depois da correção os documentos somam "
                f"{_moeda(i.icms_creditos_depois)} — retificar a apuração é "
                "decisão de quem assina.")

    if rel.avisos:
        add("")
        add("AVISOS")
        for aviso in rel.avisos:
            add(f"  ! {aviso}")

    if rel.recusas:
        add("")
        add("RECUSADO — NADA FOI APLICADO NESTES DOCUMENTOS")
        for recusa in rel.recusas:
            add(f"  x {recusa.documento}: {recusa.motivo}")

    add("")
    return "\n".join(linhas)


def para_json(rel: Relatorio) -> str:
    """O relatório inteiro, para arquivar e comparar entre execuções."""
    def converter(obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if hasattr(obj, "__dict__"):
            return obj.__dict__
        return str(obj)

    dados = asdict(rel)
    dados["impacto"]["icms_diferenca"] = str(rel.impacto.icms_diferenca)
    dados["impacto"]["ipi_diferenca"] = str(rel.impacto.ipi_diferenca)
    dados["total_alteracoes"] = rel.total_alteracoes
    return json.dumps(dados, ensure_ascii=False, indent=2, default=converter)


def linhas_para_planilha(rel: Relatorio) -> list[dict]:
    """Uma linha por alteração, pronta para o exportador de Excel do projeto.

    Os títulos das colunas seguem `planilha_estilo.formato_da_coluna`, que
    decide sozinho o formato de moeda e de texto pelo nome da coluna.
    """
    linhas = []
    for doc in rel.documentos:
        for a in doc.aceitas + doc.cascata:
            linhas.append({
                "Documento": f"{doc.modelo}/{doc.serie or '-'}/{doc.numero}",
                "Chave": doc.chave,
                "Emitente": doc.emitente,
                "Data": _data_br(doc.data),
                "Registro": a.registro,
                "Item": a.num_item,
                "Campo": a.campo,
                "De": a.de,
                "Para": a.para,
                "Motivo": a.motivo,
                "Origem": a.origem,
                "Aprovado por": a.aprovada_por,
            })
    for a in rel.estruturais:
        linhas.append({
            "Documento": "(arquivo)", "Chave": "", "Emitente": "", "Data": "",
            "Registro": a.registro, "Item": "", "Campo": a.campo,
            "De": a.de, "Para": a.para, "Motivo": a.motivo,
            "Origem": a.origem, "Aprovado por": "",
        })
    for a in rel.apuracao:
        linhas.append({
            "Documento": "(apuração)", "Chave": "", "Emitente": "", "Data": "",
            "Registro": a.registro, "Item": "", "Campo": a.campo,
            "De": a.de, "Para": a.para, "Motivo": a.motivo,
            "Origem": a.origem, "Aprovado por": a.aprovada_por,
        })
    return linhas
