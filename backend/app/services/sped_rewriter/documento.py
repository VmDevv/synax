"""Leitura FIEL do SPED — a que serve para reescrever o arquivo.

Por que não reaproveitar `audit_engine/sped_parser.py`: ele foi feito para
auditar e faz o certo para isso — extrai ~15 dos 38 campos do C170, converte
tudo com `clean_float()` e joga a linha crua fora. Reescrever a partir dele
corromperia o arquivo em silêncio: `1.234,50` viraria `1234.5` (quantas casas
na volta?), e os campos que ele não lê — `IND_MOV`, `COD_NAT`, `ALIQ_ST`,
`QUANT_BC_PIS`, `IND_APUR` — sairiam vazios num documento que os tinha.

São dois leitores com propósitos diferentes lendo o mesmo arquivo. Unificar os
dois estraga os dois.

A regra que governa este módulo inteiro:

    **Campo não tocado é devolvido byte a byte como veio. Só o campo alterado
    é reserializado.**

Por isso cada `Registro` guarda a linha crua e só a remonta quando alguém
chamou `set()` — e por isso `campos` é `list[str]`, texto, nunca número: no
PVA `||` e `|0,00|` são coisas diferentes, e ida-e-volta por `float` não
devolve a mesma string.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Encoding do SPED. Ler como utf-8 estoura em qualquer razão social com
# acento; ler com errors="ignore" apaga o caractere e muda o tamanho do campo.
# latin-1 mapeia byte a byte, e é isso que garante que o arquivo volta igual.
ENCODING = "latin-1"

# Marca de UTF-8 no começo do arquivo. Não faz parte do leiaute e o PVA
# recusa a primeira linha com ela. Lida como latin-1 vira "ï»¿" colado no
# |0000|; lida como UTF-8, "\ufeff". As duas formas são reconhecidas.
BOMS = ("\u00ef\u00bb\u00bf", "\ufeff")

# Nível hierárquico de cada registro, conforme o Guia Prático 3.2.2. O pai de
# um registro é o último visto com nível imediatamente menor.
#
# Registro fora deste mapa continua sendo lido, preservado e CONTADO no Bloco 9
# — ele só não participa da árvore. Mapear o que não se usa seria convidar erro
# de hierarquia em registro que este módulo nem toca.
NIVEL = {
    "0000": 0,
    "0001": 1, "0460": 2, "0990": 1,
    "C001": 1, "C990": 1,
    "C100": 2, "C170": 3, "C190": 3, "C195": 3, "C197": 4,
    "D001": 1, "D990": 1,
    "D100": 2, "D190": 3, "D195": 3, "D197": 4,
    "E001": 1, "E990": 1,
    "E100": 2, "E110": 3, "E111": 4, "E115": 4, "E116": 4,
    "E500": 2, "E510": 3, "E520": 3, "E530": 3,
    "1001": 1, "1010": 2, "1990": 1,
    "1900": 2, "1910": 3, "1920": 4,
    "9001": 1, "9900": 2, "9990": 1,
    "9999": 0,
}


@dataclass
class Registro:
    """Uma linha do SPED, fiel ao original até alguém mexer nela."""

    reg: str
    campos: list[str]                 # campos[0] == reg; índice = nº do Guia - 1
    linha: int                        # posição no arquivo de origem (0 = criado aqui)
    crua: str = ""                    # a linha exata como veio, sem o fim de linha
    fim: str = "\r\n"
    pai: "Registro | None" = None
    filhos: list["Registro"] = field(default_factory=list)
    alterado: bool = False
    criado: bool = False

    # ------------------------------------------------------------------ campos

    def campo(self, n: int) -> str:
        """Campo pelo número do Guia (1-based). Fora do alcance devolve ''."""
        return self.campos[n - 1] if 0 < n <= len(self.campos) else ""

    def set(self, n: int, valor: str) -> bool:
        """Grava o campo n. Devolve True se o conteúdo mudou de fato.

        Só marca `alterado` quando o texto muda: reescrever o mesmo valor não
        pode fazer a linha ser reserializada, senão uma correção em um item
        reformataria o documento inteiro.
        """
        while len(self.campos) < n:
            self.campos.append("")
        if self.campos[n - 1] == valor:
            return False
        self.campos[n - 1] = valor
        self.alterado = True
        return True

    # ------------------------------------------------------------------ saída

    def serializar(self) -> str:
        """A linha para gravar: a crua se ninguém mexeu, remontada se mexeu."""
        if not self.alterado and not self.criado:
            return self.crua
        return "|" + "|".join(self.campos) + "|"

    def chave_documento(self) -> str:
        """Identificação de um C100/D100 — a mesma que o patch usa como alvo.

        A numeração dos campos NÃO é a mesma nos dois: no C100 a chave da NF-e
        é o campo 9 e o número é o 8; no D100 a chave do CT-e é o campo 10 e o
        número é o 9. Tratar os dois igual devolvia o NÚMERO do CT-e como se
        fosse a chave, e a alteração do frete não casava com a correção que a
        gerou — o relatório abria dois documentos para o mesmo CT-e.
        """
        if self.reg == "D100":
            return (self.campo(10)
                    or f"{self.campo(5)}/{self.campo(7) or '-'}/{self.campo(9)}")
        return (self.campo(9)
                or f"{self.campo(5)}/{self.campo(7) or '-'}/{self.campo(8)}")


def nova_linha(reg: str, campos: dict[int, str], total: int,
              fim: str = "\r\n") -> "Registro":
    """Monta um `Registro` do zero, com o número exato de campos do leiaute.

    `campos` é {número do campo no Guia: valor} — só os preenchidos; o resto
    nasce vazio (`""`), que é o que o SPED espera de campo não usado (`||`, não
    `|0,00|`). Compartilhado por quem PRECISA criar linha nova no meio de um
    arquivo existente: `sped_builder` (monta o arquivo inteiro) e qualquer
    ajuste que insira um registro que o arquivo original não tinha.
    """
    valores = [""] * total
    valores[0] = reg
    for numero, valor in campos.items():
        if numero <= total:
            valores[numero - 1] = "" if valor is None else str(valor)
    return Registro(reg=reg, campos=valores, linha=0, crua="", fim=fim,
                    criado=True)


@dataclass
class Documento:
    """O arquivo inteiro: a lista ordenada de linhas é a fonte da verdade.

    `registros` é o que será gravado, na ordem. A árvore (`pai`/`filhos`) é um
    índice por cima dela — inserir ou remover um C190 exige mexer nos dois, e é
    por isso que `inserir_apos` e `remover` existem em vez de o chamador
    manipular as listas na mão.
    """

    registros: list[Registro]
    caminho: str = ""
    fim_padrao: str = "\r\n"
    # O que a leitura viu de errado e NÃO conseguiu manter: linha que não é
    # registro, registro sem pai, BOM. A prévia e o relatório mostram.
    avisos: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ busca

    def por_reg(self, reg: str) -> list[Registro]:
        return [r for r in self.registros if r.reg == reg]

    def filhos_reg(self, pai: Registro, reg: str) -> list[Registro]:
        return [f for f in pai.filhos if f.reg == reg]

    def indice_de_documentos(self) -> dict[str, Registro]:
        """{chave da NF-e ou "modelo/série/número": C100}.

        As duas formas ficam no mesmo índice porque nem todo documento tem
        chave — nota modelo 01 em papel não tem, e a correção precisa achá-la
        do mesmo jeito.
        """
        indice: dict[str, Registro] = {}
        for c100 in self.por_reg("C100"):
            chave = c100.campo(9)
            if chave:
                indice[chave] = c100
            indice[f"{c100.campo(5)}/{c100.campo(7) or '-'}/{c100.campo(8)}"] = c100
        return indice

    def indice_de_fretes(self) -> dict[str, Registro]:
        """{chave do CT-e ou "modelo/série/número": D100}.

        Índice separado do de mercadorias porque a numeração dos campos é
        outra: no D100 a chave é o campo 10 e o número do documento é o 9
        (no C100 são o 9 e o 8). Reaproveitar o mesmo código aqui indexaria o
        CT-e pela série errada.
        """
        indice: dict[str, Registro] = {}
        for d100 in self.por_reg("D100"):
            chave = d100.campo(10)
            if chave:
                indice[chave] = d100
            indice[f"{d100.campo(5)}/{d100.campo(7) or '-'}/{d100.campo(9)}"] = d100
        return indice

    # ------------------------------------------------------------- estrutura

    def inserir_apos(self, referencia: Registro, novo: Registro) -> None:
        """Insere `novo` logo depois de `referencia`, na lista e na árvore."""
        pos = self.registros.index(referencia)
        self.registros.insert(pos + 1, novo)
        if novo.pai is not None and novo not in novo.pai.filhos:
            novo.pai.filhos.append(novo)

    def inserir_antes(self, referencia: Registro, novo: Registro) -> None:
        """Insere `novo` logo ANTES de `referencia`, na lista e na árvore.

        Existe porque registro de abertura de grupo se declara por posição: o
        0460 tem de entrar antes do 0990 e o 1900 antes do 1990, e calcular
        "a linha anterior à referência" no chamador quebra quando a referência
        é a primeira do arquivo.
        """
        pos = self.registros.index(referencia)
        self.registros.insert(pos, novo)
        if novo.pai is not None and novo not in novo.pai.filhos:
            novo.pai.filhos.append(novo)

    def remover(self, alvo: Registro) -> None:
        """Tira o registro do arquivo e da árvore."""
        if alvo in self.registros:
            self.registros.remove(alvo)
        if alvo.pai is not None and alvo in alvo.pai.filhos:
            alvo.pai.filhos.remove(alvo)

    # ---------------------------------------------------------------- escrita

    def serializar(self) -> str:
        return "".join(r.serializar() + (r.fim or self.fim_padrao)
                       for r in self.registros)

    def gravar(self, destino: str) -> str:
        """Grava em arquivo NOVO. O original nunca é sobrescrito."""
        with open(destino, "w", encoding=ENCODING, newline="") as f:
            f.write(self.serializar())
        return destino


def nome_sugerido(doc: "Documento", com_competencia: bool = False) -> str:
    """"Sped_Corrigido_<NOME DA EMPRESA>.txt", tirado do registro 0000.

    O nome empresarial é o campo 6 do 0000 (100 caracteres). Ele vem do
    arquivo, e não do cadastro do tenant, de propósito: o que a pessoa baixa
    tem de se identificar pelo que está DENTRO dele — auditar o arquivo de uma
    empresa e baixar com o nome de outra é o tipo de troca que ninguém percebe
    até ser tarde.

    `com_competencia` acrescenta a competência antes da extensão. Fica
    desligado por padrão porque o padrão pedido é sem ela — mas duas
    competências da mesma empresa geram o MESMO nome, e a segunda sobrescreve
    a primeira na pasta de downloads.
    """
    zeros = doc.por_reg("0000")
    nome = (zeros[0].campo(6) if zeros else "").strip() or "Empresa"

    # Windows recusa \ / : * ? " < > | em nome de arquivo, e ignora ponto e
    # espaço no fim — o que transformaria "EMPRESA LTDA." em "EMPRESA LTDA".
    limpo = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", nome)
    limpo = re.sub(r"\s+", " ", limpo).strip(" .")
    limpo = limpo[:80].strip(" .") or "Empresa"

    if com_competencia and zeros:
        bruto = zeros[0].campo(4)
        if len(bruto) == 8:
            return f"Sped_Corrigido_{limpo}_{bruto[4:8]}-{bruto[2:4]}.txt"
    return f"Sped_Corrigido_{limpo}.txt"


def _quebrar(crua: str) -> list[str]:
    """Campos entre pipes, tolerando linha sem o pipe final.

    Um `split("|")[1:-1]` seco perde o último campo quando a linha não termina
    em pipe — arquivo gerado por sistema de terceiro faz isso, e o campo comido
    seria o VL_IPI de alguém.
    """
    corpo = crua
    if corpo.startswith("|"):
        corpo = corpo[1:]
    if corpo.endswith("|"):
        corpo = corpo[:-1]
    return corpo.split("|")


def ler(caminho: str) -> Documento:
    """Lê o arquivo preservando linha crua, fim de linha e hierarquia."""
    with open(caminho, "r", encoding=ENCODING, newline="") as f:
        conteudo = f.read()
    return ler_texto(conteudo, caminho=caminho)


def ler_texto(conteudo: str, caminho: str = "") -> Documento:
    """Mesma leitura, a partir de texto já em memória (usado nos testes)."""
    registros: list[Registro] = []
    pilha: dict[int, Registro] = {}
    fins: dict[str, int] = {}
    avisos: list[str] = []

    for bom in BOMS:
        if conteudo.startswith(bom):
            conteudo = conteudo[len(bom):]
            avisos.append(
                "O arquivo começava com BOM (marca de UTF-8), que não faz parte "
                "do leiaute e foi removida: com ela a primeira linha não começa "
                "com pipe e o PVA não acha o registro 0000.")
            break

    numero = 0
    inicio = 0
    total = len(conteudo)
    while inicio < total:
        quebra = conteudo.find("\n", inicio)
        if quebra == -1:
            bruta, fim = conteudo[inicio:], ""
            inicio = total
        else:
            bruta, fim = conteudo[inicio:quebra], "\n"
            if bruta.endswith("\r"):
                bruta, fim = bruta[:-1], "\r\n"
            inicio = quebra + 1

        numero += 1
        if not bruta.strip():
            continue
        fins[fim] = fins.get(fim, 0) + 1

        campos = _quebrar(bruta)
        if not bruta.startswith("|") or not campos or not campos[0].strip():
            # Não é registro. Mantê-la faria o 9900 contar um "tipo" que não
            # existe e o PVA rejeitar a linha; sai do arquivo, com aviso.
            avisos.append(f"linha {numero} ignorada: não é um registro do "
                          f"leiaute ({bruta[:40]!r})")
            continue
        r = Registro(reg=campos[0], campos=campos, linha=numero, crua=bruta,
                     fim=fim)

        nivel = NIVEL.get(r.reg)
        if nivel is not None:
            pai = pilha.get(nivel - 1)
            if pai is not None:
                r.pai = pai
                pai.filhos.append(r)
            elif nivel >= 2:
                # C170 antes de qualquer C100, D190 sem D100: a linha fica no
                # arquivo, mas fora da árvore — a cascata não a soma e o PVA
                # acusa hierarquia. Quem corrige precisa saber.
                avisos.append(f"linha {numero}: registro {r.reg} sem o "
                              "registro pai esperado antes dele; fica fora "
                              "da hierarquia e a cascata não o enxerga")
            pilha[nivel] = r
            # Some com os níveis mais fundos: sem isso, um C170 depois de um
            # C100 novo poderia se pendurar num filho do documento anterior.
            for mais_fundo in [n for n in pilha if n > nivel]:
                pilha.pop(mais_fundo, None)

        registros.append(r)

    padrao = max(fins, key=fins.get) if fins else "\r\n"
    return Documento(registros=registros, caminho=caminho,
                     fim_padrao=padrao or "\r\n", avisos=avisos)
