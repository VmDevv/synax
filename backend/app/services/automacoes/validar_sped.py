"""Rotina automática "Validar e assinar SPED": a pasta onde os SPED chegam, todo arquivo pelo PVA.

Pedido do Sandro em 23/10/2026: "o sistema vai buscar os SPED do Domínio e vai validar e assinar todos". A rotina
lê a pasta configurada (a que o Domínio grava), manda cada SPED ao PVA pelo MESMO caminho da aba Validar SPED
(`validar_sped_fila.processar_agora`, um por vez, com a trava do motor) e grava os assinados na pasta de destino.

Regras:

- **Nada some.** Todo `.txt` da pasta sai no relatório da rodada com o que aconteceu: assinado, aprovado,
  reprovado (com o número de erros), não assinado (com o motivo), EFD-Contribuições (aguarda o PVA dela, que não está
  instalado), empresa fora da rotina, já assinado antes, ou que não é SPED.
- **A empresa é a do 0000**, pelo CNPJ do cadastro. Arquivo de empresa fora do escopo da rotina (ou que o Synax não
  conhece) não é validado nem assinado: a rotina não age em nome de quem não está nela.
- **Não assina duas vezes.** Arquivo que já tem assinatura (depois do 9999) ou que já tem cópia na pasta de
  destino é pulado, com o motivo. O original nunca é alterado: o assinado é uma CÓPIA, com o mesmo nome, no destino.
- **Transmitir não existe.** O envio à Receita não tem volta e não está ligado no Synax.
- **Histórico igual ao da aba**: cada arquivo validado vira uma linha de `sped_validacoes` (origem "rotina"), e o
  painel do mês mostra.
"""
import logging
import os
import re
import shutil
import uuid
from html import escape

from sqlalchemy import null

from ...domain.models.audit import SpedValidacao
from ...domain.models.auth_models import Tenant
from .. import configuracoes as cfg
from .. import pva_motor
from .. import validar_sped_fila as fila
from ..audit_engine import sped_parser
from ..sped_entregas import hash_do_corpo_arquivo

logger = logging.getLogger(__name__)

PASTA_ASSINADOS = "Assinados"
# Uma rodada não varre o disco inteiro: pasta errada (C:\, a rede toda) é engano, e a fila passaria de um dia.
MAX_ARQUIVOS = 500

# situação de cada arquivo -> (rótulo, tom)
SITUACOES = {
    "assinado": ("Assinado", "ok"),
    "aprovado": ("Aprovado, sem assinar", "ok"),
    "nao_assinado": ("Aprovado, não assinado", "aviso"),
    "reprovado": ("Reprovado pelo PVA", "erro"),
    "falhou": ("Sem resposta do PVA", "erro"),
    "contribuicoes": ("EFD-Contribuições: aguarda o PVA dela", "aviso"),
    "fora": ("Empresa fora da rotina", "aviso"),
    "ja_assinado": ("Já assinado antes", "info"),
    "nao_sped": ("Não é SPED", "info"),
}


def _identificador(valor) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(valor or "")).upper()


def destino_da(config: dict) -> str:
    pasta = (config.get("pasta") or "").strip()
    return (config.get("destino") or "").strip() or os.path.join(pasta, PASTA_ASSINADOS)


def listar(config: dict) -> list[str]:
    """Os `.txt` da pasta (e das subpastas, se marcado), fora a pasta de destino. Em ordem de nome."""
    pasta = (config.get("pasta") or "").strip()
    destino = os.path.normcase(os.path.abspath(destino_da(config)))
    achados = []
    for raiz, subpastas, nomes in os.walk(pasta):
        if os.path.normcase(os.path.abspath(raiz)).startswith(destino):
            subpastas[:] = []
            continue
        subpastas[:] = sorted(s for s in subpastas
                              if os.path.normcase(os.path.abspath(os.path.join(raiz, s))) != destino)
        achados += [os.path.join(raiz, n) for n in sorted(nomes) if n.lower().endswith(".txt")]
        if not config.get("subpastas"):
            break
    return achados


def _ja_assinado(caminho: str) -> bool:
    """Tem algo depois da linha do 9999 — a assinatura do PVA ou do ReceitanetBX."""
    with open(caminho, "rb") as f:
        bruto = f.read()
    marca = bruto.rfind(b"|9999|")
    if marca < 0:
        return False
    fim = bruto.find(b"\n", marca)
    return fim >= 0 and bool(bruto[fim + 1:].strip())


def empresas_da_rotina(db, automacao) -> dict:
    """CNPJ (só dígitos) -> Tenant, das empresas ativas no escopo da rotina."""
    consulta = db.query(Tenant).filter(Tenant.ativo.is_(True))
    if automacao.escopo == "selecionadas":
        ids = [i for i in (automacao.empresas or "").split(",") if i]
        consulta = consulta.filter(Tenant.id.in_(ids)) if ids else consulta.filter(False)
    return {_identificador(t.cnpj): t for t in consulta.all() if _identificador(t.cnpj)}


def rodar(db, automacao, execucao, config: dict) -> dict:
    """Uma rodada. Devolve o detalhe (também gravado em `execucao.detalhe` por quem chama)."""
    pasta = (config.get("pasta") or "").strip()
    destino = destino_da(config)
    assinar = bool(config.get("assinar"))
    detalhe = {"pasta": pasta, "destino": destino, "assinar": assinar, "arquivos": [], "aviso": ""}

    if not pasta or not os.path.isdir(pasta):
        detalhe["aviso"] = f"A pasta {pasta or '(em branco)'} não está acessível. Nada foi validado."
        return detalhe
    try:
        pva_motor.saude()
    except pva_motor.MotorIndisponivel as exc:
        detalhe["aviso"] = exc.mensagem
        return detalhe
    if assinar:
        situacao = cfg.situacao_certificado(db)
        if situacao is None or situacao["vencido"]:
            detalhe["aviso"] = ("Assinar está marcado, mas " + ("não há certificado cadastrado" if situacao is None
                                else "o certificado cadastrado venceu") + " em Configurações: os arquivos são só "
                                "validados nesta rodada.")
            assinar = False
            detalhe["assinar"] = False

    arquivos = listar(config)
    if len(arquivos) > MAX_ARQUIVOS:
        detalhe["aviso"] = (f"A pasta tem {len(arquivos)} arquivos .txt; esta rodada pega só os {MAX_ARQUIVOS} "
                            "primeiros. Confira se a pasta configurada é a certa.")
        arquivos = arquivos[:MAX_ARQUIVOS]
    empresas = empresas_da_rotina(db, automacao)
    execucao.empresas_total = len(arquivos)
    db.commit()

    for caminho in arquivos:
        item = _um(db, automacao, caminho, pasta, destino, assinar, empresas)
        detalhe["arquivos"].append(item)
        if item["situacao"] in ("assinado", "aprovado"):
            execucao.empresas_ok += 1
        elif item["situacao"] in ("reprovado", "falhou", "nao_assinado"):
            execucao.empresas_falha += 1
        else:
            execucao.empresas_adiadas += 1           # pulado, com motivo
        db.commit()                                  # progresso visível no histórico enquanto roda
    return detalhe


def _um(db, automacao, caminho: str, pasta: str, destino: str, assinar: bool, empresas: dict) -> dict:
    nome = os.path.relpath(caminho, pasta)
    item = {"arquivo": nome, "empresa": "", "cnpj": "", "competencia": "", "situacao": "", "erros": None,
            "advertencias": None, "motivo": "", "processo": None}
    try:
        cab = fila.ler_0000(caminho)
        if cab is None:
            return {**item, "situacao": "nao_sped", "motivo": "não começa com o registro 0000"}
        if sped_parser.detectar_leiaute(caminho) == sped_parser.CONTRIBUICOES:
            return {**item, "empresa": cab["nome"], "situacao": "contribuicoes",
                    "motivo": "o PVA da EFD-Contribuições não está instalado no servidor"}
        cnpj = _identificador(cab["cnpj"] or cab["cpf"])
        item.update(empresa=cab["nome"], cnpj=cnpj, competencia=fila.competencia_do_0000(cab["dt_ini"]) or "")
        empresa = empresas.get(cnpj)
        if empresa is None:
            return {**item, "situacao": "fora", "motivo": "empresa não cadastrada no Synax ou fora desta rotina"}
        item["empresa"] = empresa.name
        alvo = os.path.join(destino, nome)
        if assinar and os.path.exists(alvo):
            return {**item, "situacao": "ja_assinado", "motivo": "já há uma cópia assinada na pasta de destino"}
        if assinar and _ja_assinado(caminho):
            return {**item, "situacao": "ja_assinado", "motivo": "o arquivo já vem assinado"}
        return _validar(db, automacao, caminho, nome, alvo, cab, empresa, assinar, item)
    except OSError as exc:
        logger.warning("Rotina Validar SPED: não li %s: %s", caminho, exc)
        return {**item, "situacao": "falhou", "motivo": f"não foi possível ler o arquivo ({exc.strerror or exc})"}


def _validar(db, automacao, caminho, nome, alvo, cab, empresa, assinar, item) -> dict:
    processo = str(uuid.uuid4())
    os.makedirs(fila.pasta(processo), exist_ok=True)
    shutil.copyfile(caminho, os.path.join(fila.pasta(processo), fila.ARQUIVO_SPED))
    copia = os.path.join(fila.pasta(processo), fila.ARQUIVO_SPED)
    try:
        db.add(SpedValidacao(
            id=processo, tenant_id=str(empresa.id), user_id="", user_name=f"Rotina: {automacao.nome}"[:255],
            file_name=os.path.basename(caminho)[:255], competencia=fila.competencia_do_0000(cab["dt_ini"]),
            cnpj=_identificador(cab["cnpj"] or cab["cpf"])[:14] or None, cod_fin=(cab["cod_fin"][:1] or None),
            leiaute=(cab["cod_ver"][:3] or None), linhas=fila.contar_linhas(copia),
            erros=null(), advertencias=null(), nao_conferidas=null(),
            hash_corpo=hash_do_corpo_arquivo(copia), motor=fila.MOTOR, situacao=fila.NA_FILA,
            assinar=assinar or None, origem="rotina"))
        db.commit()
    except Exception:                                      # noqa: BLE001
        logger.exception("Rotina Validar SPED: não registrou %s", caminho)
        db.rollback()
        fila.descartar(processo)
        return {**item, "situacao": "falhou", "motivo": "não foi possível registrar no histórico; nada foi validado"}

    final = fila.processar_agora(fila.Pedido(processo=processo, tenant_id=str(empresa.id), assinar=assinar))
    item.update(processo=processo, erros=final.get("erros"), advertencias=final.get("advertencias"))
    if final.get("situacao") != fila.CONCLUIDA:
        return {**item, "situacao": "falhou", "motivo": final.get("falha") or "o PVA não respondeu"}
    if not final.get("aprovado"):
        return {**item, "situacao": "reprovado",
                "motivo": f"{final.get('erros') or 0} erro(s); abra a validação na aba Validar SPED"}
    if not assinar:
        return {**item, "situacao": "aprovado"}
    if not final.get("assinado"):
        return {**item, "situacao": "nao_assinado", "motivo": final.get("assinatura_motivo") or "o PVA não assinou"}
    try:
        os.makedirs(os.path.dirname(alvo), exist_ok=True)
        shutil.copyfile(os.path.join(fila.pasta(processo), fila.ARQUIVO_ASSINADO), alvo)
    except OSError as exc:
        logger.warning("Rotina Validar SPED: não gravou %s: %s", alvo, exc)
        return {**item, "situacao": "nao_assinado",
                "motivo": f"o PVA assinou, mas a pasta de destino recusou a gravação ({exc.strerror or exc})"}
    fila.registrar_destino(processo, alvo)
    return {**item, "situacao": "assinado", "motivo": alvo}


def desfecho(detalhe: dict) -> tuple[str, str]:
    """(status da execução, mensagem). Nunca "verde com zero"."""
    if detalhe.get("aviso") and not detalhe["arquivos"]:
        return "falhou", detalhe["aviso"][:500]
    arquivos = detalhe["arquivos"]
    if not arquivos:
        return "vazia", f"Nenhum arquivo .txt em {detalhe['pasta']}."
    conta: dict = {}
    for a in arquivos:
        conta[a["situacao"]] = conta.get(a["situacao"], 0) + 1
    partes = [f"{conta[s]} {SITUACOES[s][0]}" for s in SITUACOES if conta.get(s)]
    ruins = sum(conta.get(s, 0) for s in ("reprovado", "falhou", "nao_assinado"))
    avisos = sum(conta.get(s, 0) for s in ("contribuicoes", "fora"))
    status = "com_falhas" if (ruins or avisos or detalhe.get("aviso")) else "ok"
    if ruins == len(arquivos):
        status = "falhou"
    return status, (f"{len(arquivos)} arquivo(s): " + " · ".join(partes))[:500]


def montar_email(detalhe: dict, nome_automacao: str, mensagem: str) -> tuple[str, str, str]:
    """(assunto, html, texto) do informativo da rodada."""
    linhas_html, linhas_txt = [], []
    for a in detalhe.get("arquivos", []):
        rotulo = SITUACOES.get(a["situacao"], (a["situacao"], ""))[0]
        motivo = a.get("motivo") or ""
        linhas_html.append(
            f"<tr><td style='padding:4px 8px'>{escape(a.get('empresa') or '')}</td>"
            f"<td style='padding:4px 8px'>{escape(a['arquivo'])}</td>"
            f"<td style='padding:4px 8px'>{escape(a.get('competencia') or '')}</td>"
            f"<td style='padding:4px 8px'><b>{escape(rotulo)}</b></td>"
            f"<td style='padding:4px 8px;color:#555'>{escape(motivo)}</td></tr>")
        linhas_txt.append(f"- {a.get('empresa') or ''} | {a['arquivo']} | {rotulo}" + (f" | {motivo}" if motivo else ""))
    aviso = detalhe.get("aviso") or ""
    assunto = f"{nome_automacao}: {mensagem}"[:200]
    html = (f"<p><b>{escape(nome_automacao)}</b> — {escape(mensagem)}</p>"
            + (f"<p style='color:#92400e'>{escape(aviso)}</p>" if aviso else "")
            + f"<p>Pasta: {escape(detalhe.get('pasta') or '')}<br>Assinados em: {escape(detalhe.get('destino') or '')}</p>"
            + ("<table style='border-collapse:collapse;font-size:13px'><tr>"
               "<th align=left>Empresa</th><th align=left>Arquivo</th><th align=left>Competência</th>"
               "<th align=left>Situação</th><th align=left>Detalhe</th></tr>" + "".join(linhas_html) + "</table>"
               if linhas_html else "")
            + "<p style='color:#555;font-size:12px'>Transmitir não está ligado no Synax: a entrega continua pelo PVA "
              "ou pelo Receitanet.</p>")
    texto = "\n".join([f"{nome_automacao} — {mensagem}", aviso, f"Pasta: {detalhe.get('pasta')}",
                       f"Assinados em: {detalhe.get('destino')}", "", *linhas_txt])
    return assunto, html, texto
