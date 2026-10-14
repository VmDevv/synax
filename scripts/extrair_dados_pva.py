# -*- coding: utf-8 -*-
"""Extrai do PVA instalado os DADOS que o validador de EFD ICMS IPI usa.

O PVA da EFD ICMS IPI guarda o leiaute inteiro — registros, campos, tamanhos,
valores válidos e quais regras valem em cada campo — como XML dentro do
`fiscalpva-dominio.jar`, e as Tabelas Externas como texto simples. É isso que
este script copia, convertido para JSON compacto, para
`backend/app/services/sped_validador/dados/`. O servidor do Synax lê só esses
JSON: não depende de o PVA estar instalado na máquina dele.

O que NÃO sai daqui, de propósito:

- **Código do PVA.** Nenhuma classe é lida, descompilada ou copiada; os JARs
  não são redistribuídos. O descritor diz QUE a regra `REGRA_MAIOR_ZERO` vale
  no campo NUM_DOC; o que ela faz o motor implementa a partir do Guia Prático,
  e o PVA serve de gabarito.
- **A pasta `mysql` da instalação.** É onde o PVA guarda as escriturações que
  alguém importou — dado de cliente, não leiaute.
- **O arquivo `metadados` das tabelas** (cifrado e desnecessário) e os `.obj`
  (o mesmo descritor em Java serializado).

Uso (da raiz do projeto):

    venv/Scripts/python.exe scripts/extrair_dados_pva.py
    venv/Scripts/python.exe scripts/extrair_dados_pva.py --pva "D:\\outro\\Fiscal"

Rode de novo depois de "Atualizar Tabelas" no PVA ou de instalar versão nova:
os arquivos são regravados por inteiro e o `manifesto.json` registra a versão
do PVA, a de cada tabela e o hash das fontes — para saber, olhando o
repositório, contra qual PVA o validador foi gerado.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import zipfile

import defusedxml.ElementTree as ET

PVA_PADRAO = r"C:\Arquivos de Programas RFB\Programas SPED\Fiscal"
RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DESTINO_PADRAO = os.path.join(RAIZ, "backend", "app", "services",
                              "sped_validador", "dados")

JAR_DOMINIO = os.path.join("lib", "br.gov.serpro.sped.fiscalpva",
                           "fiscalpva-dominio.jar")
JAR_PRINCIPAL = "fiscalpva.jar"
PASTA_TABELAS = os.path.join("recursos", "TabelasExternas")
CONFIGURADOR = "descritor/comum/configurador/configurador.xml"
MENSAGENS = "descritor/comum/mensagens/validador.prop"
RE_DESCRITOR = re.compile(
    r"^descritor/escrituracao/ato(\d{3})/estrutura(\d{3})/v(\d+)/descritor\.xml$")

# Formato dos JSON gerados. O motor recusa dado de formato diferente em vez
# de ler um campo que mudou de nome como se fosse vazio.
FORMATO = 1


# --------------------------------------------------------------------- util

def _sha256(caminho: str) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def _outros(texto: str | None) -> dict[str, str]:
    """`[modelos=01,55;definemodelo=true]` -> {"modelos": "01,55", ...}."""
    if not texto:
        return {}
    corpo = texto.strip().lstrip("[").rstrip("]")
    saida: dict[str, str] = {}
    for parte in corpo.split(";"):
        if "=" in parte:
            chave, valor = parte.split("=", 1)
            saida[chave.strip()] = valor.strip()
    return saida


def _verdade(valor: str | None) -> bool | None:
    if valor is None:
        return None
    return valor.strip().lower() == "true"


def _tamanho(texto: str | None) -> tuple[int | None, bool]:
    """`[4]` é tamanho EXATO; `60` é MÁXIMO; ausente é sem limite.

    Confirmado no próprio descritor: REG é sempre `[4]`, CNPJ `[14]`, data
    `[8]`, CFOP `[4]` — campos de tamanho fixo no Guia —, e NOME `100`,
    COD_PART `60`, SER `3` — campos "até N".
    """
    if not texto:
        return None, False
    t = texto.strip()
    if t.startswith("[") and t.endswith("]"):
        return int(t[1:-1]), True
    return int(t), False


def _valores(texto: str) -> list[str]:
    """`0=Entrada;1=Saída;` -> ["0", "1"]. Item vazio (`;;`, `;` final) some."""
    codigos = []
    for item in texto.split(";"):
        if not item.strip():
            continue
        codigos.append(item.split("=", 1)[0].strip())
    return codigos


def _regras(elemento) -> list[dict]:
    saida = []
    for v in elemento.findall("validador"):
        regra = {"id": v.get("id"), "tipo": v.get("tipo")}
        campo = v.get("campoMsg") or v.get("camposMsg")
        if campo:
            regra["campo"] = campo
        # O PVA grafou o atributo errado em um registro
        # ("outrosCaremposEnvolvidosValidacao"); os dois são o mesmo dado.
        outros = (v.get("outrosCamposEnvolvidosValidacao")
                  or v.get("outrosCaremposEnvolvidosValidacao"))
        if outros:
            regra["outros"] = [o for o in outros.split(";") if o]
        saida.append(regra)
    return saida


# ----------------------------------------------------------------- leiaute

def _campo(c) -> dict:
    tamanho, exato = _tamanho(c.get("tamanho"))
    outros = _outros(c.get("outros"))
    decimais = c.get("casasdecimais")
    campo = {
        # Há descritor antigo com `n="31'"`; o número é o que vale.
        "n": int(re.sub(r"\D", "", c.get("n"))),
        "id": c.get("id"),
        # Tipo vazio aparece em dois campos (COD_MOD_DOC_REF do C500/D700); o
        # Guia os define como alfanuméricos.
        "tipo": c.get("tipo") or "C",
        "tamanho": tamanho,
        "exato": exato,
        "decimais": int(decimais) if decimais else None,
        "rotulo": c.get("rotulo") or "",
        # "1" obrigatório, "0" facultativo, None quando a obrigatoriedade
        # depende da operação (ver obrig_saida/obrig_entrada) ou de regra
        # específica.
        "obrigatorio": c.get("obrigatorio") or None,
    }
    if "obrigatorioSaida" in outros:
        campo["obrig_saida"] = _verdade(outros["obrigatorioSaida"])
    if "obrigatorioEntrada" in outros:
        campo["obrig_entrada"] = _verdade(outros["obrigatorioEntrada"])
    if _verdade(outros.get("obrigatorioimportacao")):
        campo["obrig_importacao"] = True

    vv = c.find("valores-validos")
    if vv is not None:
        if vv.get("valores") is not None:
            campo["valores"] = _valores(vv.get("valores"))
        if vv.get("datasource"):
            campo["fonte"] = vv.get("datasource")
        restricao = _outros(vv.get("outros")).get("codigos")
        if restricao:
            campo["fonte_codigos"] = [x for x in restricao.split(",") if x]

    regras = _regras(c)
    if regras:
        campo["regras"] = regras
    return campo


def extrair_leiaute(xml: bytes, origem: str) -> dict:
    raiz = ET.fromstring(xml)
    molde = raiz.find("registro-molde")
    blocos = []
    registros = []

    def percorrer(elemento, bloco_id: str, pai: str | None, nivel_pai: int):
        """Pré-ordem: devolve os registros na ordem do descritor.

        O pai e o nível saem do ANINHAMENTO do XML (o C101 está dentro do
        C100), não do atributo `nivel`: em dois registros o atributo contradiz
        a árvore — C880 declara nível 3 aninhado no C870, que também é 3, e o
        E531 declara 4 dentro do E530 (4). O Guia põe os dois um nível abaixo
        do pai, que é o que a árvore diz. O atributo fica em `nivel_descritor`.
        """
        for r in elemento.findall("registro"):
            declarado = int(r.get("nivel"))
            if pai is None:
                # Topo do bloco: 0000 e 9999 (nível 0) e as aberturas e
                # encerramentos dos demais blocos (nível 1, pendem do 0000).
                nivel = declarado
                pai_r = None if declarado == 0 else "0000"
            else:
                nivel = nivel_pai + 1
                pai_r = pai
            registros.append(_registro(r, bloco_id, nivel, declarado, pai_r))
            percorrer(r, bloco_id, r.get("id"), nivel)

    for bloco in raiz.findall("bloco"):
        blocos.append({"id": bloco.get("id"), "rotulo": bloco.get("rotulo")})
        percorrer(bloco, bloco.get("id"), None, -1)

    return {
        "formato": FORMATO,
        "id": raiz.get("id"),
        "versao_descritor": raiz.get("versao"),
        "origem": origem,
        "regras_molde": _regras(molde) if molde is not None else [],
        "blocos": blocos,
        "registros": registros,
    }


def _registro(r, bloco_id: str, nivel: int, declarado: int,
              pai: str | None) -> dict:
    outros = _outros(r.get("outros"))
    registro = {
        "reg": r.get("id"),
        "bloco": bloco_id,
        "nivel": nivel,
        "pai": pai,
        # 0 = uma vez no arquivo; 1 = uma vez por registro pai; 2 = várias.
        "ocorrencia": int(r.get("ocorrencia")),
        # 1 = obrigatório; 0 = facultativo; 2 = condicional, decidido pelas
        # regras em "obrigatoriedade".
        "obrigatorio": int(r.get("obrigatorio")),
        "rotulo": r.get("rotulo") or "",
        "chave": [k for k in (r.get("chave") or "").split(",") if k],
        "campos": [_campo(c) for c in r.findall("campo")],
    }
    if declarado != nivel:
        registro["nivel_descritor"] = declarado
    if outros.get("modelos"):
        registro["modelos"] = [m for m in outros["modelos"].split(",") if m]
    for chave_xml, chave_json in (
            ("definemodelo", "define_modelo"),
            ("defineoperacao", "define_operacao"),
            ("obrigatorioimportacao", "obrig_importacao"),
            ("naousachavenaduplicidade", "sem_duplicidade_por_chave")):
        if _verdade(outros.get(chave_xml)):
            registro[chave_json] = True
    ob = [o.get("id") for o in r.findall("obrigatoriedade")]
    if ob:
        registro["obrigatoriedade"] = ob
    mod = [o.get("id") for o in r.findall("modificador-obrigatoriedade")]
    if mod:
        registro["modificadores"] = mod
    regras = _regras(r)
    if regras:
        registro["regras"] = regras
    return registro


def descritores_mais_recentes(jar: zipfile.ZipFile) -> dict[str, tuple]:
    """{ato: (estrutura, versão, entrada)} — a última estrutura e versão.

    Cada ato é um `COD_VER` (ato020 = leiaute 020, de 2026, conforme a
    tabela VERSOES_LEIAUTE). Dentro do ato, uma estrutura nova substitui a
    anterior e cada `vN` é uma revisão dela; o descritor não traz data de
    início, então vale o mais recente. Hipótese a confirmar no gabarito do PVA.
    """
    escolhido: dict[str, tuple] = {}
    for nome in jar.namelist():
        m = RE_DESCRITOR.match(nome)
        if not m:
            continue
        ato, estrutura, versao = m.group(1), m.group(2), int(m.group(3))
        atual = escolhido.get(ato)
        if atual is None or (estrutura, versao) > (atual[0], atual[1]):
            escolhido[ato] = (estrutura, versao, nome)
    return escolhido


# --------------------------------------------------------- configurador

def extrair_fontes(xml: bytes) -> dict:
    """As fontes de valores válidos (`data-source`): id -> classe e parâmetros.

    Só o NOME da classe é guardado, para o motor saber qual tabela a fonte
    consulta; os parâmetros dizem, por exemplo, que `COD_ITEM_0200_SV` olha o
    registro 0200 e tem `validar=false` — a existência do código é conferida
    pela regra REGRA_EXISTE_COD_ITEM_V2, não pela lista de valores.
    """
    raiz = ET.fromstring(xml)
    fontes = {}
    for ds in raiz.iter("data-source"):
        classe = (ds.get("classe") or "").rsplit(".", 1)[-1]
        parametros = {}
        bruto = (ds.get("parametros") or "").strip().lstrip("[").rstrip("]")
        for parte in bruto.split(";"):
            if "=" in parte:
                k, v = parte.split("=", 1)
                parametros[k.strip()] = v.strip()
        fontes[ds.get("id")] = {"classe": classe, "parametros": parametros}
    return fontes


def extrair_mensagens(bruto: bytes) -> dict[str, str]:
    """validador.prop -> {MSG_X: texto}. Latin-1, como todo arquivo do PVA.

    Algumas mensagens terminam em `##MSG_..._RESUMO` (a versão curta usada no
    relatório agrupado); fica só o texto.
    """
    texto = bruto.decode("latin-1")
    mensagens: dict[str, str] = {}
    for linha in texto.splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        mensagens[chave.strip()] = valor.split("##", 1)[0].strip()
    return mensagens


# ----------------------------------------------------------------- tabelas

def _data_aaaammdd(ddmmaaaa: str) -> str:
    d = ddmmaaaa.strip()
    if len(d) == 8 and d.isdigit():
        return d[4:8] + d[2:4] + d[0:2]
    return ""


def extrair_tabelas(pasta: str) -> tuple[dict, list[dict], list[str]]:
    """Tabelas Externas -> {tipo: {pacote: {...}}}.

    Nome do arquivo: `SPEDFISCAL_<PACOTE>$<TIPO>$<versão>$<idTabela>`. Pacote é
    a UF, GENERICA (vale para qualquer UF, com o prefixo "UF" no lugar da
    sigla), GLOBAL, ST, IPI, CARDOC ou CLASCONS. Linhas:
    `código|descrição|DDMMAAAA|DDMMAAAA`, com o fim vazio quando vigente. A
    descrição não é guardada — o validador pergunta só "existe e está vigente".
    Datas viram AAAAMMDD para comparar como texto.
    """
    tipos: dict[str, dict] = {}
    inventario: list[dict] = []
    problemas: list[str] = []
    for nome in sorted(os.listdir(pasta)):
        if not nome.startswith("SPEDFISCAL_") or nome.count("$") != 3:
            continue
        pacote, tipo, versao, id_tabela = nome[len("SPEDFISCAL_"):].split("$")
        with open(os.path.join(pasta, nome), "r", encoding="latin-1") as f:
            linhas = f.read().splitlines()
        cabecalho = linhas[0].strip() if linhas else ""
        codigos = []
        pendente = ""
        for numero, linha in enumerate(linhas[1:], start=2):
            if not linha.strip():
                continue
            # Descrição com quebra de linha: o CFOP 5909 vem em duas linhas
            # ("5909|Retorno ... comoda" e "|01122019|"). Junta até ter os
            # quatro campos, em vez de perder o código ou criar um falso.
            if pendente:
                linha = pendente + " " + linha
                pendente = ""
            partes = linha.split("|")
            if len(partes) < 4:
                pendente = linha
                continue
            codigos.append([partes[0].strip(), _data_aaaammdd(partes[-2]),
                            _data_aaaammdd(partes[-1])])
        if pendente:
            problemas.append(f"{nome}: última linha incompleta ({pendente[:60]!r})")
        tipos.setdefault(tipo, {})[pacote] = {
            "arquivo": nome, "versao": versao, "id": id_tabela,
            "codigos": codigos,
        }
        inventario.append({"arquivo": nome, "pacote": pacote, "tipo": tipo,
                           "versao": versao, "cabecalho": cabecalho,
                           "linhas": len(codigos)})
    return tipos, inventario, problemas


# --------------------------------------------------------------------- main

def _gravar(destino: str, nome: str, dados) -> int:
    caminho = os.path.join(destino, nome)
    with open(caminho, "w", encoding="utf-8", newline="\n") as f:
        json.dump(dados, f, ensure_ascii=False, separators=(",", ":"))
    return os.path.getsize(caminho)


def _versao_pva(pva: str) -> str:
    with zipfile.ZipFile(os.path.join(pva, JAR_PRINCIPAL)) as jar:
        manifesto = jar.read("META-INF/MANIFEST.MF").decode("latin-1")
    for linha in manifesto.splitlines():
        if linha.lower().startswith("versao_pva:"):
            return linha.split(":", 1)[1].strip()
    for linha in manifesto.splitlines():
        if linha.startswith("Implementation-Version:"):
            return linha.split(":", 1)[1].strip()
    return ""


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pva", default=PVA_PADRAO,
                    help="pasta de instalação do PVA da EFD ICMS IPI")
    ap.add_argument("--destino", default=DESTINO_PADRAO,
                    help="pasta onde gravar os JSON")
    ap.add_argument("--desde-ato", default="015",
                    help="primeiro leiaute (COD_VER) a extrair; os anteriores "
                         "a cinco anos não se retificam mais (padrão: 015)")
    args = ap.parse_args(argv)

    jar_dominio = os.path.join(args.pva, JAR_DOMINIO)
    pasta_tabelas = os.path.join(args.pva, PASTA_TABELAS)
    for obrigatorio in (jar_dominio, pasta_tabelas,
                        os.path.join(args.pva, JAR_PRINCIPAL)):
        if not os.path.exists(obrigatorio):
            print(f"ERRO: não achei {obrigatorio}. Confira o --pva.")
            return 2

    os.makedirs(args.destino, exist_ok=True)
    versao_pva = _versao_pva(args.pva)
    print(f"PVA {versao_pva} em {args.pva}")

    leiautes = []
    with zipfile.ZipFile(jar_dominio) as jar:
        fontes = extrair_fontes(jar.read(CONFIGURADOR))
        mensagens = extrair_mensagens(jar.read(MENSAGENS))
        for ato, (estrutura, versao, entrada) in sorted(
                descritores_mais_recentes(jar).items()):
            if ato < args.desde_ato:
                continue
            leiaute = extrair_leiaute(jar.read(entrada), entrada)
            leiaute.update({"ato": ato, "estrutura": estrutura,
                            "versao": str(versao)})
            nome = f"leiaute_{ato}.json"
            tamanho = _gravar(args.destino, nome, leiaute)
            n_campos = sum(len(r["campos"]) for r in leiaute["registros"])
            leiautes.append({"ato": ato, "estrutura": estrutura,
                             "versao": str(versao), "id": leiaute["id"],
                             "origem": entrada, "arquivo": nome,
                             "registros": len(leiaute["registros"]),
                             "campos": n_campos})
            print(f"  leiaute {ato} (estrutura {estrutura} v{versao}): "
                  f"{len(leiaute['registros'])} registros, {n_campos} "
                  f"campos, {tamanho // 1024} KB")

    _gravar(args.destino, "fontes.json", {"formato": FORMATO, "fontes": fontes})
    _gravar(args.destino, "mensagens.json",
            {"formato": FORMATO, "mensagens": mensagens})

    tipos, inventario, problemas = extrair_tabelas(pasta_tabelas)
    tamanho = _gravar(args.destino, "tabelas.json",
                      {"formato": FORMATO, "tipos": tipos})
    print(f"  {len(inventario)} tabelas externas, {tamanho // 1024} KB")
    for p in problemas:
        print(f"  aviso: {p}")

    # Leiaute que o PVA conhece pela tabela de versões mas para o qual não há
    # descritor (o do ano que vem costuma chegar antes do descritor).
    versoes = {}
    for cod, ini, fim in (tipos.get("VERSOES_LEIAUTE", {})
                          .get("GLOBAL", {}).get("codigos", [])):
        versoes[cod] = {"inicio": ini, "fim": fim}
    extraidos = {l["ato"] for l in leiautes}
    sem_descritor = sorted(c for c in versoes
                           if c >= args.desde_ato and c not in extraidos)

    manifesto = {
        "formato": FORMATO,
        "extraido_em": dt.datetime.now().isoformat(timespec="seconds"),
        "pva": {
            "versao": versao_pva,
            "caminho": args.pva,
            "dominio_jar_sha256": _sha256(jar_dominio),
        },
        "leiautes": leiautes,
        "leiautes_sem_descritor": sem_descritor,
        "versoes_leiaute": versoes,
        "tabelas": inventario,
        "tabelas_avisos": problemas,
    }
    _gravar(args.destino, "manifesto.json", manifesto)
    if sem_descritor:
        print(f"  leiaute(s) sem descritor neste PVA: {', '.join(sem_descritor)}")
    print(f"Gravado em {args.destino}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
