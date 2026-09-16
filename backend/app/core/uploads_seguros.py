"""Recepção segura de arquivos enviados pelo cliente.

`UploadFile.filename` vem do cabeçalho Content-Disposition: é dado do atacante,
não metadado confiável. Sem reduzir a basename, um nome como `../../app/main.py`
(ou um caminho absoluto no Windows, que faz `os.path.join` descartar a base)
escapava de temp_uploads/<task_id> e sobrescrevia arquivo do servidor.

Os tetos de tamanho existem pelo mesmo motivo que em services/nfse: ler o
arquivo inteiro para a RAM é um DoS de uma requisição só, e `extractall` não
protege contra bomba de descompressão.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile

import aiofiles
from fastapi import HTTPException, UploadFile

BLOCO = 1024 * 1024
LIMITE_SPED_BYTES = 300 * 1024 * 1024
LIMITE_ZIP_BYTES = 500 * 1024 * 1024
LIMITE_DESCOMPRIMIDO_BYTES = 2 * 1024 * 1024 * 1024
LIMITE_XMLS = 20_000

# Teto de tempo do extrator externo de RAR. Arquivo corrompido faz o processo
# ficar pendurado, e sem isso o worker do upload ia junto.
TIMEOUT_EXTRATOR_S = 300

# Assinaturas dos formatos aceitos. A detecção é pelo CONTEÚDO e não pela
# extensão: o WinRAR grava .rar e .zip, e um pacote renomeado à mão é rotina no
# escritório. Confiar no sufixo faria um .zip chamado "xmls.rar" ser recusado
# sem motivo — e o contrário, pior ainda.
ASSINATURAS_ZIP = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
ASSINATURAS_RAR = (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")


def nome_seguro(nome: str | None, padrao: str) -> str:
    """Reduz o nome enviado pelo cliente a um basename inofensivo."""
    bruto = os.path.basename((nome or "").replace("\\", "/").strip())
    bruto = unicodedata.normalize("NFKD", bruto).encode("ascii", "ignore").decode()
    limpo = re.sub(r"[^A-Za-z0-9._-]", "_", bruto).strip("._")
    return limpo[:120] or padrao


async def gravar_com_limite(upload: UploadFile, destino: str, limite: int, rotulo: str) -> int:
    """Grava em blocos e aborta ao estourar o limite.

    `await upload.read()` sem argumento carregava o arquivo inteiro na memória:
    dois SPEDs grandes simultâneos derrubavam o worker antes de auditar nada.
    """
    total = 0
    try:
        async with aiofiles.open(destino, "wb") as saida:
            while bloco := await upload.read(BLOCO):
                total += len(bloco)
                if total > limite:
                    raise HTTPException(
                        status_code=413,
                        detail=f"{rotulo} excede o limite de {limite // (1024 * 1024)} MB.",
                    )
                await saida.write(bloco)
    except HTTPException:
        # Não deixar o arquivo pela metade ocupando disco.
        try:
            os.remove(destino)
        except OSError:
            pass
        raise
    return total


def extrair_xmls_do_zip(zip_path: str, destino: str) -> int:
    """Extrai só os .xml, com teto de tamanho descomprimido.

    `extractall` não protege contra bomba de descompressão: um ZIP de 50 KB pode
    virar gigabytes em disco. O tamanho real vem declarado no índice, então é
    conferido ANTES de descomprimir — mesma tática já usada em routes/nfse.py.
    """
    destino_abs = os.path.abspath(destino)
    orcamento = LIMITE_DESCOMPRIMIDO_BYTES
    extraidos = 0

    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".xml"):
                continue
            if info.file_size > orcamento:
                raise HTTPException(
                    status_code=413,
                    detail="O ZIP de XMLs excede o limite de conteúdo descomprimido.",
                )

            alvo = os.path.join(destino_abs, nome_seguro(info.filename, f"{extraidos}.xml"))
            # Cinto e suspensório: o basename já resolve, mas se um dia o nome
            # mudar de forma, o caminho final ainda tem de cair dentro do destino.
            if os.path.commonpath([destino_abs, os.path.abspath(alvo)]) != destino_abs:
                continue

            with z.open(info) as origem, open(alvo, "wb") as saida:
                shutil.copyfileobj(origem, saida, BLOCO)

            orcamento -= info.file_size
            extraidos += 1
            if extraidos >= LIMITE_XMLS:
                break

    return extraidos


def formato_do_pacote(caminho: str) -> str:
    """'zip', 'rar' ou 'desconhecido', pela assinatura do arquivo."""
    try:
        with open(caminho, "rb") as fh:
            cabecalho = fh.read(8)
    except OSError:
        return "desconhecido"

    if cabecalho.startswith(ASSINATURAS_ZIP):
        return "zip"
    if cabecalho.startswith(ASSINATURAS_RAR):
        return "rar"
    return "desconhecido"


def _e_bsdtar(executavel: str) -> bool:
    """GNU tar também se chama `tar` e NÃO lê RAR — só o bsdtar/libarchive lê.

    Sem esta conferência, num servidor Linux o `tar` do PATH seria escolhido e
    todo RAR falharia com um erro obscuro em vez da mensagem que ensina o
    caminho.
    """
    try:
        saida = subprocess.run([executavel, "--version"], capture_output=True,
                               text=True, timeout=10).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return "bsdtar" in saida or "libarchive" in saida


def extrator_de_rar() -> list[str] | None:
    """Comando capaz de extrair RAR, ou None se não houver nenhum na máquina.

    Ordem de preferência:
      1. bsdtar — vem de fábrica no Windows 10+ (System32\\tar.exe) e lê RAR4 e
         RAR5 por libarchive, sem instalar nada. É o caminho que funciona na
         máquina do escritório hoje.
      2. UnRAR.exe do WinRAR, quando o WinRAR está instalado.
      3. `unrar`/`bsdtar` do PATH, que é o caso de um servidor Linux
         (`apt install libarchive-tools` ou `unrar`).

    RAR é formato proprietário: não existe leitor puro em Python na stdlib, daí
    o extrator externo.
    """
    candidatos: list[str] = []

    if sys.platform == "win32":
        # Caminho absoluto primeiro: no Git Bash o PATH resolve `tar` para o GNU
        # tar, que não serve.
        candidatos.append(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                       "System32", "tar.exe"))
    candidatos += [c for c in (shutil.which("bsdtar"), shutil.which("tar")) if c]

    for exe in candidatos:
        if os.path.exists(exe) and _e_bsdtar(exe):
            return [exe, "-xf"]

    for unrar in (
        os.path.join(os.environ.get("ProgramFiles", ""), "WinRAR", "UnRAR.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "WinRAR", "UnRAR.exe"),
        shutil.which("unrar") or "",
    ):
        if unrar and os.path.exists(unrar):
            return [unrar, "x", "-y", "-idq"]

    return None


def extrair_xmls_do_rar(rar_path: str, destino: str) -> int:
    """Extrai os .xml de um RAR, com as mesmas travas do caminho do ZIP.

    Diferença inevitável em relação ao ZIP: `zipfile` deixa conferir o tamanho
    declarado ANTES de descomprimir, item a item. Um extrator externo, não — ele
    escreve tudo de uma vez. Por isso a extração vai para uma pasta temporária
    isolada e descartável, e o conteúdo só é aceito na conferência que vem
    depois. O pacote já chegou aqui limitado por `gravar_com_limite`, e a pasta
    temporária é apagada mesmo quando algo falha no meio.
    """
    comando = extrator_de_rar()
    if comando is None:
        raise HTTPException(
            status_code=400,
            detail=("Este servidor não tem um extrator de RAR instalado. "
                    "Abra o pacote no WinRAR e salve como ZIP, ou peça para "
                    "instalarem o bsdtar (libarchive) no servidor."),
        )

    destino_abs = os.path.abspath(destino)
    temporario = tempfile.mkdtemp(prefix="rar_", dir=os.path.dirname(destino_abs))
    extraidos = 0
    orcamento = LIMITE_DESCOMPRIMIDO_BYTES

    try:
        if comando[0].lower().endswith("unrar.exe") or comando[0].endswith("unrar"):
            argumentos = comando + [rar_path, temporario + os.sep]
        else:
            argumentos = comando + [rar_path, "-C", temporario, "--include=*.xml"]

        try:
            processo = subprocess.run(argumentos, capture_output=True, text=True,
                                      timeout=TIMEOUT_EXTRATOR_S)
        except subprocess.TimeoutExpired:
            raise HTTPException(
                status_code=400,
                detail="A extração do RAR passou do tempo limite. O arquivo pode estar corrompido.",
            )
        except OSError as err:
            raise HTTPException(status_code=400,
                                detail=f"Não foi possível abrir o RAR: {err}")

        # O extrator pode sair com erro e ainda assim ter gravado arquivos bons
        # (um item corrompido no meio do pacote). Só reclama se não veio nada.
        for raiz, _, arquivos in os.walk(temporario):
            for nome in arquivos:
                if not nome.lower().endswith(".xml"):
                    continue
                origem = os.path.join(raiz, nome)
                tamanho = os.path.getsize(origem)
                if tamanho > orcamento:
                    raise HTTPException(
                        status_code=413,
                        detail="O RAR de XMLs excede o limite de conteúdo descomprimido.",
                    )

                alvo = os.path.join(destino_abs, nome_seguro(nome, f"{extraidos}.xml"))
                if os.path.commonpath([destino_abs, os.path.abspath(alvo)]) != destino_abs:
                    continue

                shutil.move(origem, alvo)
                orcamento -= tamanho
                extraidos += 1
                if extraidos >= LIMITE_XMLS:
                    return extraidos

        if extraidos == 0:
            erro = (processo.stderr or processo.stdout or "").strip()[:200]
            raise HTTPException(
                status_code=400,
                detail=("Não foi encontrado nenhum XML dentro do RAR."
                        + (f" O extrator respondeu: {erro}" if erro else "")),
            )
        return extraidos
    finally:
        shutil.rmtree(temporario, ignore_errors=True)


def extrair_xmls_do_pacote(caminho: str, destino: str) -> int:
    """Extrai os XMLs de um ZIP ou de um RAR, escolhido pela assinatura."""
    formato = formato_do_pacote(caminho)
    if formato == "zip":
        return extrair_xmls_do_zip(caminho, destino)
    if formato == "rar":
        return extrair_xmls_do_rar(caminho, destino)
    raise HTTPException(
        status_code=400,
        detail="O arquivo enviado não é um ZIP nem um RAR válido.",
    )
