"""A prévia: o que vai acontecer, ANTES de gerar o arquivo.

Sem isto, a pessoa marca dez correções, baixa o arquivo, entrega ao cliente — e
descobre no PVA que uma delas rejeitava o arquivo inteiro. A prévia mostra
antes: o que será aplicado, o que será recusado e por quê, e o que vai mudar
junto sem ela ter pedido.

**Como ela sabe: fazendo de verdade, numa cópia que é jogada fora.** A prévia
relê o texto original, aplica o patch nessa cópia e conta o que aconteceu — em
vez de reimplementar a previsão em paralelo. Duas implementações da mesma regra
divergem em algum momento, e a que mente é justamente a que a pessoa lê antes
de decidir. Aqui a prévia não pode mentir: ela é a execução.

O documento que o usuário está vendo na tela NÃO é tocado — `prever` recebe o
texto, não o `Documento` aberto.

Três coisas que ela responde e a lista de checkboxes sozinha não responde:

1. **O que o PVA recusaria** — com o motivo em português. A campeã é a trava do
   C170 campo 11: as regras do Synax são por item, e trocar o CFOP de um item
   só quando os outros começam com outro dígito rejeita o arquivo inteiro.
2. **O que muda junto** — C190 que nasce ou some, CST que acompanha a glosa,
   valor de IPI que entra no custo da mercadoria. A pessoa aprovou uma troca de
   CFOP; é justo ela ver as outras seis linhas antes.
3. **O que não muda nada** — correção cujo campo já tem aquele valor. Dizer
   "5 correções aplicadas" quando duas eram no-op é a mesma mentira do
   "Sucesso! 0 regras aplicadas" em caixa verde.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .cascata import NOMES_C170_CONTRIBUICOES, aplicar, aplicar_contribuicoes
from .documento import ler_texto
from .patch import (Alteracao, CorrecaoDocumento, CorrecaoFrete,
                    CorrecaoItem, Patch, decimal_para_texto,
                    texto_para_decimal)
from .relatorio import Relatorio, montar

ZERO = Decimal("0")

ACEITA = "aceita"
RECUSADA = "recusada"
SEM_EFEITO = "sem_efeito"


@dataclass
class ItemDaPrevia:
    """Uma correção marcada na tela e o que vai acontecer com ela."""

    documento: str
    alvo: str                     # "item 3" ou "frete CFOP 2352"
    campos: list
    situacao: str                 # aceita | recusada | sem_efeito
    motivo: str = ""              # por que foi recusada
    mudancas: list = field(default_factory=list)

    @property
    def recusada(self) -> bool:
        return self.situacao == RECUSADA


@dataclass
class EfeitosPrevistos:
    """O que a cascata vai mexer sem ninguém ter pedido."""

    c190_criados: int = 0
    c190_removidos: int = 0
    cst_ajustados: int = 0
    valor_migrado: Decimal = ZERO
    linhas: int = 0               # diferença no total de linhas do arquivo
    contadores: int = 0           # quantos registros de contagem mudam
    apuracao: int = 0             # campos do E110 reescritos
    # Itens que perderam base e alíquota de PIS/COFINS junto com o crédito.
    # Sem este contador a glosa do EFD Contribuições não apareceria em lugar
    # nenhum da prévia: são alterações de cascata, e `_casa` as exclui de
    # propósito — o que a pessoa marcou foi zerar o valor, não os outros.
    pis_cofins_completados: int = 0

    @property
    def houve(self) -> bool:
        return bool(self.c190_criados or self.c190_removidos or self.cst_ajustados
                    or self.valor_migrado or self.linhas or self.contadores
                    or self.apuracao or self.pis_cofins_completados)


@dataclass
class Previa:
    itens: list[ItemDaPrevia] = field(default_factory=list)
    efeitos: EfeitosPrevistos = field(default_factory=EfeitosPrevistos)
    avisos: list[str] = field(default_factory=list)
    relatorio: Relatorio | None = None

    @property
    def aceitas(self) -> list:
        return [i for i in self.itens if i.situacao == ACEITA]

    @property
    def recusadas(self) -> list:
        return [i for i in self.itens if i.situacao == RECUSADA]

    @property
    def sem_efeito(self) -> list:
        return [i for i in self.itens if i.situacao == SEM_EFEITO]

    @property
    def pronto_para_gerar(self) -> bool:
        """Sem nenhuma correção aplicável, gerar devolveria uma cópia igual.

        A tela precisa dizer isso em vez de oferecer o download — é a mesma
        regra do resto do sistema: operação em lote não termina em verde com
        zero aplicado.
        """
        return bool(self.aceitas)


def _alvo(correcao) -> str:
    if isinstance(correcao, CorrecaoFrete):
        return f"frete CFOP {correcao.cfop_atual}"
    if isinstance(correcao, CorrecaoDocumento):
        return "valor total do documento"
    return f"item {correcao.num_item}"


def _casa(alteracao: Alteracao, correcao) -> bool:
    """A alteração nasceu desta correção?

    Casa por documento + alvo + campo, e só entre as alterações que NÃO são de
    cascata: o CST que a glosa ajusta é efeito, não pedido, e não pode ser
    contado como se a pessoa o tivesse marcado.
    """
    if alteracao.origem in ("cascata", "bloco 9"):
        return False
    if alteracao.documento != correcao.documento:
        return False
    if alteracao.campo not in correcao.campos:
        return False
    if isinstance(correcao, CorrecaoFrete):
        return alteracao.registro == "D190"
    if isinstance(correcao, CorrecaoDocumento):
        return alteracao.registro in ("C100", "D100")
    return alteracao.registro == "C170" and alteracao.num_item == correcao.num_item


def _efeitos(alteracoes: list, linhas: int) -> EfeitosPrevistos:
    efeitos = EfeitosPrevistos(linhas=linhas)
    for a in alteracoes:
        if a.origem == "apuração":
            efeitos.apuracao += 1
        elif a.origem == "bloco 9":
            efeitos.contadores += 1
        elif a.origem == "cascata":
            if a.registro == "C190" and a.campo == "registro":
                if a.de:
                    efeitos.c190_removidos += 1
                else:
                    efeitos.c190_criados += 1
            elif a.campo in ("CST_ICMS", "CST_IPI", "CST_PIS", "CST_COFINS"):
                # Os de PIS/COFINS vêm ANTES do teste seguinte de propósito:
                # "CST_PIS" também está em NOMES_C170_CONTRIBUICOES, e cair
                # ali somaria uma troca de CST ao contador de base e
                # alíquota — dois efeitos diferentes numa linha só.
                efeitos.cst_ajustados += 1
            elif a.campo in NOMES_C170_CONTRIBUICOES.values():
                efeitos.pis_cofins_completados += 1
            elif a.campo == "VL_ITEM":
                efeitos.valor_migrado += (texto_para_decimal(a.para)
                                          - texto_para_decimal(a.de))
    return efeitos


def prever(texto_original: str, patch: Patch, origem: str = "",
           contribuicoes: bool = False) -> Previa:
    """Roda o patch numa cópia descartável e conta o que aconteceria.

    `contribuicoes` escolhe a cascata: a do EFD ICMS/IPI (C190, C100, Bloco E)
    ou a do EFD Contribuições (PIS/COFINS no item e no documento). Não é um
    detalhe de formatação — rodar a errada CRIA registros que o outro leiaute
    não tem.
    """
    copia = ler_texto(texto_original, caminho=origem)
    resultado = (aplicar_contribuicoes if contribuicoes else aplicar)(copia, patch)
    # O que a LEITURA viu de errado (linha que não é registro, item sem
    # documento, BOM) vai junto com os avisos da cascata: quem decide na
    # prévia precisa saber que o arquivo já chegou torto.
    resultado.avisos = list(copia.avisos) + list(resultado.avisos)
    relatorio = montar(resultado, copia, texto_original, origem=origem)

    # Quais documentos foram recusados inteiros, e por qual motivo.
    motivos = {r.documento: r.motivo for r in resultado.recusas}

    previa = Previa(avisos=list(resultado.avisos), relatorio=relatorio)
    for correcao in (list(patch.correcoes) + list(patch.fretes)
                     + list(patch.documentos)):
        motivo = motivos.get(correcao.documento, "")
        mudancas = [a for a in resultado.alteracoes if _casa(a, correcao)]
        if motivo:
            situacao = RECUSADA
        elif mudancas:
            situacao = ACEITA
        else:
            situacao = SEM_EFEITO
            motivo = "o campo já está com esse valor no arquivo"
        previa.itens.append(ItemDaPrevia(
            documento=correcao.documento, alvo=_alvo(correcao),
            campos=list(correcao.campos), situacao=situacao, motivo=motivo,
            mudancas=mudancas))

    previa.efeitos = _efeitos(resultado.alteracoes,
                              relatorio.linhas_depois - relatorio.linhas_antes)
    return previa


def texto(previa: Previa) -> str:
    """A prévia em texto — o que a tela mostra antes do botão de gerar."""
    linhas = []
    add = linhas.append

    add("=" * 78)
    add("PRÉVIA DA CORREÇÃO — nada foi gravado ainda")
    add("=" * 78)
    add(f"Marcadas: {len(previa.itens)}   "
        f"aplicáveis: {len(previa.aceitas)}   "
        f"recusadas: {len(previa.recusadas)}   "
        f"sem efeito: {len(previa.sem_efeito)}")

    if previa.recusadas:
        add("")
        add("NÃO SERÃO APLICADAS")
        for item in previa.recusadas:
            add(f"  x {item.alvo} ({item.documento[:20]}...)")
            add(f"    {item.motivo}")
        add("")
        add("  Correção recusada vale para o DOCUMENTO inteiro: aplicar só")
        add("  parte dele deixaria a nota inconsistente no PVA.")

    if previa.sem_efeito:
        add("")
        add("SEM EFEITO (o arquivo já está assim)")
        for item in previa.sem_efeito:
            add(f"  - {item.alvo}: {', '.join(item.campos)}")

    if previa.aceitas:
        add("")
        add("SERÃO APLICADAS")
        for item in previa.aceitas:
            # Marcador ASCII de propósito: este texto também vai para o log e
            # para o console do Windows, que é cp1252 — um "✓" derruba a
            # impressão inteira com UnicodeEncodeError, do mesmo jeito que já
            # aconteceu no tests/run_all.py.
            add(f"  + {item.alvo}")
            for m in item.mudancas:
                add(f"      {m.campo:<14} {m.de or '(vazio)':>12}  ->  "
                    f"{m.para or '(vazio)':<12}")

    e = previa.efeitos
    if e.houve:
        add("")
        add("MUDA JUNTO, POR OBRIGAÇÃO DO LEIAUTE")
        if e.c190_criados:
            add(f"  · {e.c190_criados} registro(s) C190 criado(s) — os itens "
                "passaram a existir em outra combinação de CST/CFOP/alíquota")
        if e.c190_removidos:
            add(f"  · {e.c190_removidos} registro(s) C190 removido(s) — não "
                "restou item naquela combinação")
        if e.cst_ajustados:
            add(f"  · {e.cst_ajustados} CST ajustado(s) para acompanhar o "
                "imposto do item — o arquivo não pode dizer 'tributada' num "
                "item sem imposto, nem o contrário")
        if e.valor_migrado:
            add(f"  · R$ {decimal_para_texto(e.valor_migrado)} de imposto "
                "glosado entra(m) no valor da mercadoria (custo)")
        if e.linhas:
            add(f"  · o arquivo fica com {abs(e.linhas)} linha(s) "
                f"{'a mais' if e.linhas > 0 else 'a menos'}")
        if e.contadores:
            add(f"  · {e.contadores} contador(es) de bloco recalculado(s)")
        if e.apuracao:
            add(f"  · {e.apuracao} campo(s) do E110 reescrito(s) — a APURAÇÃO do "
                "ICMS muda junto, porque a caixa da apuração está marcada")
        if e.pis_cofins_completados:
            add(f"  · {e.pis_cofins_completados} campo(s) de base e alíquota de "
                "PIS/COFINS zerados junto com o crédito glosado — o Guia manda "
                "não informar os três quando não há direito à apropriação")

    if previa.avisos:
        add("")
        add("ATENÇÃO")
        for aviso in previa.avisos:
            add(f"  ! {aviso}")

    add("")
    if previa.pronto_para_gerar:
        add(f"Pronto para gerar: {len(previa.aceitas)} correção(ões) "
            "serão gravadas no arquivo corrigido.")
    else:
        add("NADA a gerar: nenhuma das correções marcadas pode ser aplicada. "
            "O arquivo sairia idêntico ao original.")
    add("")
    return "\n".join(linhas)
