# -*- coding: utf-8 -*-
"""Pacote de XMLs em RAR, e arrastar-e-soltar que não falha calado.

Dois relatos do Sandro (28/08/2026):

1. "se eu arrasto ele não vai, não processa igual da falta de xml"

   O handler do drop fazia `updateDropzoneUI(zoneId, files[0].name)` sem
   conferir se veio alguma coisa. Ao arrastar uma PASTA, o navegador não entrega
   o conteúdo em `dataTransfer.files` — a lista chega VAZIA. `files[0].name`
   estourava um TypeError, o handler morria calado, o input continuava sem
   arquivo e o form era enviado assim mesmo. A auditoria então acusava falta de
   XML e ninguém ligava uma coisa à outra.

2. "aceitar o arquivo compactado do Winrar, que não aceita só o zip padrão"

   O upload só abria ZIP. Agora o formato é decidido pela ASSINATURA do arquivo,
   não pela extensão: o WinRAR grava .rar e .zip, e renomear pacote à mão é
   rotina — confiar no sufixo recusaria um ZIP chamado "xmls.rar" sem motivo.

RAR é formato proprietário e não tem leitor na stdlib, então a extração usa um
programa externo. O preferido é o bsdtar, que vem de fábrica no Windows 10+
(System32\\tar.exe) e lê RAR4 e RAR5 por libarchive — não é preciso instalar
nada na máquina do escritório. Sem nenhum extrator, a resposta é uma mensagem
que ensina o caminho, não um erro obscuro.
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('backend'))

from fastapi import HTTPException  # noqa: E402

from app.core import uploads_seguros as us  # noqa: E402

OK = FALHAS = 0
_falhou = []


def check(nome, condicao, detalhe=""):
    global OK, FALHAS
    if condicao:
        OK += 1
        print(f"  OK   | {nome}")
    else:
        FALHAS += 1
        _falhou.append(nome)
        print(f" FALHA | {nome}  [{detalhe}]")


BASE = tempfile.mkdtemp(prefix="synax_pacotes_")


def pasta(nome):
    p = os.path.join(BASE, nome)
    os.makedirs(p, exist_ok=True)
    return p


ORIGEM = pasta("origem")
for _i in range(3):
    io.open(os.path.join(ORIGEM, f"nota{_i}.xml"), "w", encoding="utf-8").write(
        f"<nfe>{_i}</nfe>")
io.open(os.path.join(ORIGEM, "leiame.txt"), "w", encoding="utf-8").write("nao e xml")


def caminho_rar_exe():
    """Rar.exe do WinRAR, só para MONTAR o .rar do teste (a extração é do bsdtar)."""
    for p in (os.path.join(os.environ.get("ProgramFiles", ""), "WinRAR", "Rar.exe"),
              os.path.join(os.environ.get("ProgramFiles(x86)", ""), "WinRAR", "Rar.exe"),
              shutil.which("rar") or ""):
        if p and os.path.exists(p):
            return p
    return None


# ============================================================================
print("\n=== 1. O formato sai da assinatura, não da extensão ===")

ZIP = shutil.make_archive(os.path.join(BASE, "pacote"), "zip", ORIGEM)
check("ZIP é reconhecido", us.formato_do_pacote(ZIP) == "zip", us.formato_do_pacote(ZIP))

# ZIP renomeado para .rar — acontece quando alguém "converte" trocando o sufixo.
ZIP_DISFARCADO = os.path.join(BASE, "xmls.rar")
shutil.copy(ZIP, ZIP_DISFARCADO)
check("ZIP com extensão .rar continua sendo lido como ZIP",
      us.formato_do_pacote(ZIP_DISFARCADO) == "zip", us.formato_do_pacote(ZIP_DISFARCADO))

QUALQUER = os.path.join(BASE, "sped.txt")
io.open(QUALQUER, "w", encoding="utf-8").write("|0000|017|0|" * 50)
check("arquivo que não é pacote é 'desconhecido'",
      us.formato_do_pacote(QUALQUER) == "desconhecido", us.formato_do_pacote(QUALQUER))
check("arquivo inexistente não explode",
      us.formato_do_pacote(os.path.join(BASE, "nao_existe.zip")) == "desconhecido")


# ============================================================================
print("\n=== 2. ZIP continua funcionando pelo ponto de entrada novo ===")

d = pasta("saida_zip")
check("extrai os 3 XMLs do ZIP", us.extrair_xmls_do_pacote(ZIP, d) == 3,
      sorted(os.listdir(d)))
check("o .txt de dentro do pacote é ignorado",
      all(f.lower().endswith(".xml") for f in os.listdir(d)), sorted(os.listdir(d)))

d = pasta("saida_disfarcado")
check("ZIP com nome .rar é extraído sem reclamar",
      us.extrair_xmls_do_pacote(ZIP_DISFARCADO, d) == 3, sorted(os.listdir(d)))

try:
    us.extrair_xmls_do_pacote(QUALQUER, pasta("saida_nada"))
    check("arquivo que não é pacote é recusado", False, "não levantou")
except HTTPException as e:
    check("arquivo que não é pacote é recusado", e.status_code == 400, e.status_code)
    check("a recusa cita os dois formatos",
          "ZIP" in e.detail and "RAR" in e.detail, e.detail)


# ============================================================================
print("\n=== 3. RAR de verdade ===")

EXTRATOR = us.extrator_de_rar()
RAR_EXE = caminho_rar_exe()
print(f"       extrator disponível: {EXTRATOR}")

if RAR_EXE and EXTRATOR:
    RAR = os.path.join(BASE, "pacote.rar")
    subprocess.run([RAR_EXE, "a", "-ep", RAR]
                   + [os.path.join(ORIGEM, f) for f in os.listdir(ORIGEM)],
                   capture_output=True, timeout=120)

    check("RAR é reconhecido pela assinatura",
          us.formato_do_pacote(RAR) == "rar", us.formato_do_pacote(RAR))

    d = pasta("saida_rar")
    check("extrai os 3 XMLs do RAR", us.extrair_xmls_do_pacote(RAR, d) == 3,
          sorted(os.listdir(d)))
    check("o .txt de dentro do RAR é ignorado",
          all(f.lower().endswith(".xml") for f in os.listdir(d)), sorted(os.listdir(d)))
    check("o conteúdo chega íntegro",
          io.open(os.path.join(d, "nota0.xml"), encoding="utf-8").read() == "<nfe>0</nfe>")
    check("a pasta temporária da extração não sobrou",
          not [x for x in os.listdir(d) if x.startswith("rar_")], sorted(os.listdir(d)))
else:
    print("       (sem WinRAR nesta máquina — os checks de RAR real não rodam)")

# Sem extrator na máquina, a resposta ensina o caminho em vez de estourar.
_original = us.extrator_de_rar
us.extrator_de_rar = lambda: None
try:
    fake_rar = os.path.join(BASE, "falso.rar")
    with open(fake_rar, "wb") as fh:
        fh.write(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 64)
    try:
        us.extrair_xmls_do_pacote(fake_rar, pasta("saida_sem_extrator"))
        check("sem extrator, recusa explicada", False, "não levantou")
    except HTTPException as e:
        check("sem extrator, recusa explicada", e.status_code == 400, e.status_code)
        check("a mensagem diz o que fazer (salvar como ZIP)",
              "ZIP" in e.detail, e.detail)
finally:
    us.extrator_de_rar = _original


# ============================================================================
print("\n=== 4. As travas de segurança valem para os dois formatos ===")

FONTE = io.open(os.path.join("backend", "app", "core", "uploads_seguros.py"),
                encoding="utf-8").read()
bloco_rar = FONTE[FONTE.find("def extrair_xmls_do_rar"):]
bloco_rar = bloco_rar[:bloco_rar.find("def extrair_xmls_do_pacote")]

check("o RAR passa cada nome por nome_seguro (traversal)",
      "nome_seguro(nome" in bloco_rar)
check("o RAR confere que o alvo cai dentro do destino",
      "commonpath" in bloco_rar)
check("o RAR respeita o teto de conteúdo descomprimido",
      "LIMITE_DESCOMPRIMIDO_BYTES" in bloco_rar and "413" in bloco_rar)
check("o RAR respeita o teto de quantidade de XMLs",
      "LIMITE_XMLS" in bloco_rar)
check("o extrator externo tem timeout",
      "TIMEOUT_EXTRATOR_S" in bloco_rar and "TimeoutExpired" in bloco_rar)
check("a pasta temporária é apagada mesmo em falha",
      "finally:" in bloco_rar and "rmtree" in bloco_rar)
check("GNU tar não é aceito como extrator de RAR",
      "_e_bsdtar" in FONTE and "libarchive" in FONTE)

ROTA = io.open(os.path.join("backend", "app", "api", "routes", "uploads.py"),
               encoding="utf-8").read()
check("a rota usa o ponto de entrada que aceita os dois",
      "extrair_xmls_do_pacote(zip_path, xml_dir)" in ROTA)
check("a rota não chama mais só o do ZIP",
      "extrair_xmls_do_zip(" not in ROTA)


# ============================================================================
print("\n=== 5. O arrastar-e-soltar da tela ===")

TPL = io.open(os.path.join("frontend", "templates", "pages", "dashboard.html"),
              encoding="utf-8").read()
drop = TPL[TPL.find("dropzone.addEventListener('drop'"):]
drop = drop[:drop.find("function extensaoAceita")]

check("o drop não acessa mais files[0] às cegas",
      "updateDropzoneUI(zoneId, files[0].name)" not in TPL,
      "a chamada antiga, que estourava com a lista vazia, voltou")
# O onchange do próprio input é outro caminho e sempre foi seguro (`?.`) — a
# trava aqui é para não passar a ser.
check("o onchange do input segue com acesso opcional",
      TPL.count("this.files[0]?.name") == 2, TPL.count("this.files[0]?.name"))
check("lista vazia (pasta arrastada) é tratada",
      "arquivos.length === 0" in drop, drop[:400])
check("a pasta arrastada gera aviso visível",
      "Arraste o ARQUIVO" in drop)
check("o formato solto é conferido contra o accept do input",
      "extensaoAceita" in drop and "accept" in drop)
check("monta o FileList por DataTransfer",
      "new DataTransfer()" in drop and "balde.items.add" in drop)
check("dispara change para quem escuta o input",
      "new Event('change'" in drop)
check("zona desligada (Somente SPED/SIEG) ignora o drop",
      "fileInput.disabled" in drop)
check("existe a função que mostra a recusa na tela",
      "function avisarDropzone" in TPL)

check("o input de XMLs aceita .rar",
      'accept=".zip,.rar"' in TPL, "accept não inclui .rar")
check("o rótulo da tela avisa que aceita os dois",
      "(.zip ou .rar)" in TPL)


shutil.rmtree(BASE, ignore_errors=True)

# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

sys.exit(1 if FALHAS else 0)
