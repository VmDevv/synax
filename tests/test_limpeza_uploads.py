# -*- coding: utf-8 -*-
"""Limpeza de temp_uploads: apaga o abandonado, nunca o que está em uso.

Pedido do Sandro em 01/09/2026, depois de medirmos **1,04 GB em 233 pastas**
(66.368 arquivos), com 85 delas paradas havia mais de uma semana.

`execute_audit` apaga o SPED e os XMLs no `finally`, então o caminho feliz se
limpa sozinho. Três caminhos NÃO passam por lá: a auditoria que falhou antes do
motor, a que ficou esperando a confirmação da IA e o servidor reiniciou, e a que
nunca chegou a rodar. O que sobra é documento fiscal de cliente parado em disco.

As travas que este teste protege, em ordem de importância:

1. **Só apaga por IDADE, com prazo folgado.** Uma auditoria em andamento tem
   arquivos tocados há minutos; a que espera confirmação expira em 1 hora
   (`LIMITE_ESPERA_IA_S`). Sete dias está muito acima dos dois.
2. **Só apaga pasta com nome de task_id (UUID).** É o que impede um TEMP_DIR
   mal configurado virar `rmtree` em diretório do sistema — mesmo apontado para
   o lugar errado, não há o que casar com o padrão.
3. **A idade olha o conteúdo, não só a pasta.** No Windows o mtime da pasta não
   muda quando um arquivo lá dentro é reescrito, e uma auditoria em andamento
   pareceria antiga.
4. **Simula por padrão.** Apagar documento de cliente não pode ser o efeito de
   quem rodou o comando só para olhar.
"""
import io
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath('.'))
sys.path.insert(0, os.path.abspath('backend'))

from app.core.limpeza_uploads import (  # noqa: E402
    DIAS_PADRAO, limpar_uploads_antigos, limpar_no_startup)

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


UUID_A = "aaaaaaaa-1111-2222-3333-444444444444"
UUID_B = "bbbbbbbb-1111-2222-3333-444444444444"
UUID_C = "cccccccc-1111-2222-3333-444444444444"


def montar(base, nome, dias_atras, com_arquivos=True):
    """Cria uma pasta de upload e a envelhece."""
    pasta = os.path.join(base, nome)
    os.makedirs(os.path.join(pasta, "xmls"), exist_ok=True)
    if com_arquivos:
        io.open(os.path.join(pasta, "sped.txt"), "w").write("|0000|" + "x" * 500)
        io.open(os.path.join(pasta, "xmls", "nota.xml"), "w").write("<nfe/>" * 100)

    quando = time.time() - (dias_atras * 86400)
    for raiz, dirs, arquivos in os.walk(pasta, topdown=False):
        for nome_arq in arquivos:
            os.utime(os.path.join(raiz, nome_arq), (quando, quando))
        for nome_dir in dirs:
            os.utime(os.path.join(raiz, nome_dir), (quando, quando))
    os.utime(pasta, (quando, quando))
    return pasta


# ============================================================================
print("\n=== 1. Apaga o antigo, mantém o recente ===")

BASE = tempfile.mkdtemp(prefix="synax_limpeza_")
montar(BASE, UUID_A, dias_atras=30)   # abandonado
montar(BASE, UUID_B, dias_atras=0)    # auditoria de agora

r = limpar_uploads_antigos(BASE, dias=7)
check("a pasta antiga foi apagada", not os.path.exists(os.path.join(BASE, UUID_A)))
check("a pasta de hoje continua lá", os.path.isdir(os.path.join(BASE, UUID_B)))
check("o relatório conta 1 apagada", r["apagadas"] == 1, r)
check("e 1 mantida", r["mantidas"] == 1, r)
check("e soma os bytes liberados", r["bytes"] > 0, r)
check("sem erro", r["erros"] == 0, r)

# Uma pasta no limite: 6 dias com prazo de 7 fica.
montar(BASE, UUID_C, dias_atras=6)
limpar_uploads_antigos(BASE, dias=7)
check("6 dias com prazo de 7 NÃO é apagada", os.path.isdir(os.path.join(BASE, UUID_C)))
limpar_uploads_antigos(BASE, dias=5)
check("a mesma pasta some quando o prazo cai para 5",
      not os.path.exists(os.path.join(BASE, UUID_C)))


# ============================================================================
print("\n=== 2. A idade olha o CONTEÚDO, não só a pasta ===")

BASE2 = tempfile.mkdtemp(prefix="synax_limpeza2_")
pasta = montar(BASE2, UUID_A, dias_atras=30)
# Pasta com data antiga, mas com arquivo mexido agora: é auditoria em andamento.
io.open(os.path.join(pasta, "sped.txt"), "w").write("gravando agora")
os.utime(pasta, (time.time() - 30 * 86400,) * 2)

r = limpar_uploads_antigos(BASE2, dias=7)
check("pasta antiga com arquivo recente NÃO é apagada",
      os.path.isdir(pasta), r)
check("ela conta como mantida", r["mantidas"] == 1, r)


# ============================================================================
print("\n=== 3. Só toca em pasta com nome de task_id ===")

BASE3 = tempfile.mkdtemp(prefix="synax_limpeza3_")
montar(BASE3, UUID_A, dias_atras=99)
for intruso in ("backend", "config", "..", "logs", "meus-documentos"):
    alvo = os.path.join(BASE3, intruso.replace("..", "pai"))
    os.makedirs(alvo, exist_ok=True)
    io.open(os.path.join(alvo, "importante.txt"), "w").write("nao apague")
    antigo = time.time() - 99 * 86400
    os.utime(os.path.join(alvo, "importante.txt"), (antigo, antigo))
    os.utime(alvo, (antigo, antigo))

r = limpar_uploads_antigos(BASE3, dias=7)
check("a pasta de upload antiga foi apagada",
      not os.path.exists(os.path.join(BASE3, UUID_A)))
for intruso in ("backend", "config", "pai", "logs", "meus-documentos"):
    check(f"a pasta {intruso!r} (sem nome de task_id) foi poupada",
          os.path.isdir(os.path.join(BASE3, intruso)))
check("e ela nem entra na contagem", r["apagadas"] == 1, r)

# Arquivo solto no diretório também não é tocado.
io.open(os.path.join(BASE3, "leiame.txt"), "w").write("x")
limpar_uploads_antigos(BASE3, dias=1)
check("arquivo solto no diretório é ignorado",
      os.path.isfile(os.path.join(BASE3, "leiame.txt")))


# ============================================================================
print("\n=== 4. Simulação não apaga nada ===")

BASE4 = tempfile.mkdtemp(prefix="synax_limpeza4_")
montar(BASE4, UUID_A, dias_atras=30)
r = limpar_uploads_antigos(BASE4, dias=7, simular=True)
check("com simular=True a pasta continua lá", os.path.isdir(os.path.join(BASE4, UUID_A)))
check("mas o relatório diz o que seria apagado", r["apagadas"] == 1, r)
check("e quanto seria liberado", r["bytes"] > 0, r)
check("com o detalhe por pasta", r["detalhe"] and r["detalhe"][0]["pasta"] == UUID_A,
      r["detalhe"])

SCRIPT = io.open(os.path.join("scripts", "limpar_uploads.py"), encoding="utf-8").read()
check("o script simula por padrão",
      'action="store_true"' in SCRIPT and "simular=not args.aplicar" in SCRIPT)
check("e exige --aplicar para valer", '"--aplicar"' in SCRIPT)


# ============================================================================
print("\n=== 5. Falha na limpeza não derruba o servidor ===")

check("pasta inexistente não levanta",
      limpar_uploads_antigos(os.path.join(BASE4, "nao-existe"), dias=7)["apagadas"] == 0)
limpar_no_startup(os.path.join(BASE4, "tambem-nao-existe"))
check("limpar_no_startup engole qualquer falha", True)

FONTE = io.open(os.path.join("backend", "app", "core", "limpeza_uploads.py"),
                encoding="utf-8").read()
check("o startup é protegido por try/except", "except Exception:" in FONTE
      and "startup acima de tudo" in FONTE)

MAIN = io.open(os.path.join("backend", "app", "main.py"), encoding="utf-8").read()
check("o main chama a limpeza no startup", "limpar_no_startup(" in MAIN)
check("apontando para temp_uploads", 'os.path.join(os.getcwd(), "temp_uploads")' in MAIN)
check("o prazo é configurável por ambiente", 'TEMP_UPLOADS_DIAS' in FONTE)
check("o padrão é 7 dias", DIAS_PADRAO == 7 or os.environ.get("TEMP_UPLOADS_DIAS"),
      DIAS_PADRAO)


import shutil  # noqa: E402
for pasta in (BASE, BASE2, BASE3, BASE4):
    shutil.rmtree(pasta, ignore_errors=True)

# ============================================================================
print("\n" + "=" * 62)
print(f"RESULTADO: {OK}/{OK + FALHAS} passaram")
if _falhou:
    print("Falharam: " + ", ".join(_falhou))
print("=" * 62)

sys.exit(1 if FALHAS else 0)
