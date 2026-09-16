"""Correção do SPED: da divergência apontada ao arquivo retificado.

O caminho é sempre o mesmo, e a ordem não é enfeite:

    divergência da auditoria -> proposta conferida no arquivo -> PRÉVIA
        -> arquivo corrigido + relatório

A prévia é obrigatória: o botão de gerar só existe dentro da resposta dela. Sem
isso, a pessoa marcaria dez correções, baixaria o arquivo e descobriria no PVA
que uma delas rejeitava o arquivo inteiro — e o cliente descobriria junto. A
prévia não é uma simulação paralela: ela aplica o patch numa cópia descartável
e conta o que aconteceu (ver `sped_rewriter/previa.py`), então não pode mentir
sobre o que o botão seguinte vai fazer.

Duas coisas que esta rota NÃO faz, de propósito:

- **Não sobrescreve o SPED original.** O arquivo em `temp_uploads` continua
  intocado; o corrigido nasce em memória e vai direto para o download.
- **Não mexe no Bloco E.** O impacto na apuração é calculado e mostrado; gravar
  E110/E520 muda imposto a recolher e é um segundo aceite, à parte — a apuração
  real tem ajustes (E111/E530) e saldo anterior que não saem do documento.

A ferramenta é interna (`assert_internal`): quem gera arquivo de retificação é
a equipe do escritório, não o cliente que recebe o relatório.
"""
import hashlib
import io
import json
import logging
import os
import time
import unicodedata
import uuid
from datetime import datetime
from urllib.parse import quote

import pandas as pd
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from ...api.dependencies.auth import get_current_user
from ...core.authz import assert_internal, get_profile, get_task_or_403
from ...core.fuso import hora_local
from ...core.templating import build_templates
from ...domain.models.audit import SpedCorrectionRun
from ...infrastructure.database.session import get_db
from ...services.planilha_estilo import (AMBAR, AZUL,
                                         estilizar_planilha, montar_capa)
from ...services.sped_rewriter import (aplicar, ler_texto,
                                       linhas_para_planilha, montar_patch,
                                       montar, nome_sugerido, para_json,
                                       prever, propor)
from ...services.sped_rewriter.documento import ENCODING
from ...services.sped_rewriter.patch import OPCOES_DE_CORRECAO
from ...services.sped_rewriter.resumo_ia import resumir

logger = logging.getLogger(__name__)
router = APIRouter()
templates = build_templates()

TEMP_DIR = os.path.join(os.getcwd(), "temp_uploads")

# O SPED fica em disco depois da auditoria justamente para esta tela (ver o
# `finally` de execute_audit), mas não para sempre: `limpar_uploads_antigos`
# varre o que passou de 7 dias. Depois disso não há o que corrigir, e a tela
# precisa dizer isso — não sumir com o botão sem explicação.
SEM_ARQUIVO = ("O arquivo SPED desta auditoria não está mais no servidor "
               "(ele é descartado após 7 dias). Rode a auditoria de novo com o "
               "mesmo arquivo para poder gerar a versão corrigida.")

# Resumos já escritos, por (auditoria + correções marcadas).
#
# Existe para o texto que a pessoa leu na tela ser EXATAMENTE o que vai para a
# planilha e para a trilha — pedir de novo ao modelo daria outro parágrafo, e
# "o resumo mudou sozinho entre a tela e o arquivo" é o tipo de coisa que faz
# perder a confiança na ferramenta inteira. De quebra, o download não paga a
# espera de uma chamada de API.
#
# Em memória de propósito: perder o cache num restart só faz o próximo pedido
# gerar de novo.
_RESUMOS: dict = {}
VALIDADE_RESUMO_S = 3600


def _chave_resumo(task_id: str, aceitas) -> str:
    marcadas = chr(10).join(sorted(aceitas or []))
    return f"{task_id}|{hashlib.sha256(marcadas.encode()).hexdigest()}"


def _guardar_resumo(task_id: str, aceitas, texto: str, da_ia: bool,
                    motivo: str = "") -> None:
    agora = time.time()
    for chave, guardado in list(_RESUMOS.items()):
        if agora - guardado[0] > VALIDADE_RESUMO_S:
            _RESUMOS.pop(chave, None)
    _RESUMOS[_chave_resumo(task_id, aceitas)] = (agora, texto, da_ia, motivo)


def _resumo_guardado(task_id: str, aceitas):
    """(texto, veio da IA, motivo) do que já foi escrito, ou None."""
    guardado = _RESUMOS.get(_chave_resumo(task_id, aceitas))
    if not guardado:
        return None
    quando, texto, da_ia, motivo = guardado
    if time.time() - quando > VALIDADE_RESUMO_S:
        return None
    return texto, da_ia, motivo


def usuario_email(current_user=Depends(get_current_user)) -> str:
    """E-mail real de quem está gerando o arquivo — vai para `aprovada_por`.

    Sai do usuário autenticado no Supabase, e não do `UserProfile`, que não tem
    coluna de e-mail: é o mesmo critério do `created_by` das regras.
    """
    return getattr(current_user, "email", None) or "sistema"


def _cabecalhos_download(nome: str) -> dict:
    """Cabeçalhos que fazem o navegador SALVAR o arquivo, nunca exibi-lo.

    Três coisas, e nenhuma é redundante:

    - `attachment` manda salvar. Sem ele um .txt de 90 KB abre como página, e o
      SPED do cliente aparece na tela em vez de ir para a pasta de downloads.
    - `nosniff` impede o navegador de adivinhar o tipo pelo conteúdo e decidir
      renderizar assim mesmo.
    - O nome vai DUAS vezes: em ASCII puro no `filename` e em UTF-8 no
      `filename*` (RFC 5987). O nome sai do registro 0000 e razão social com
      acento é regra, não exceção — cabeçalho HTTP é latin-1, e navegador que
      ignora o `filename*` ainda recebe um nome legível.
    """
    ascii_nome = (unicodedata.normalize("NFKD", nome)
                  .encode("ascii", "ignore").decode("ascii").strip()) or "arquivo"
    return {
        "Content-Disposition": (f'attachment; filename="{ascii_nome}"; '
                                f"filename*=UTF-8''{quote(nome)}"),
        "X-Content-Type-Options": "nosniff",
    }


def _texto_do_sped(task_id: str) -> str:
    """O SPED original desta auditoria, ou "" se já saiu do disco."""
    caminho = os.path.join(TEMP_DIR, task_id, "sped.txt")
    if not os.path.exists(caminho):
        return ""
    with open(caminho, "r", encoding=ENCODING, newline="") as f:
        return f.read()


def _propostas(db_session: Session, profile, task_id: str):
    """(task, texto do SPED, propostas). 403/404 vêm de `get_task_or_403`."""
    assert_internal(profile)
    task = get_task_or_403(db_session, profile, task_id)
    try:
        dados = json.loads(task.result_data or "{}")
    except (ValueError, TypeError):
        dados = {}

    texto = _texto_do_sped(task_id)
    if not texto:
        return task, "", []
    return task, texto, propor(dados, texto)


def _geracoes_anteriores(db_session: Session, task_id: str) -> list:
    """As últimas vezes que este SPED foi corrigido e baixado.

    Coluna por coluna, e nunca `detalhe`: aquela coluna guarda o relatório
    inteiro em JSON, e um `query(SpedCorrectionRun)` inocente aqui traria todos
    eles para desenhar um cabeçalho de cinco linhas — a mesma armadilha do
    `result_data` no painel do Radar.
    """
    try:
        return (db_session.query(
                    SpedCorrectionRun.created_at, SpedCorrectionRun.user_email,
                    SpedCorrectionRun.file_name, SpedCorrectionRun.alteracoes,
                    SpedCorrectionRun.icms_diferenca)
                .filter(SpedCorrectionRun.task_id == task_id)
                .order_by(SpedCorrectionRun.created_at.desc())
                .limit(5).all())
    except Exception:
        # Banco sem a tabela ainda (deploy antes do create_all) não pode impedir
        # a tela de correção de abrir.
        logger.warning("Não foi possível ler as gerações anteriores", exc_info=True)
        return []


def _contexto(task, texto: str, propostas: list, geracoes=()) -> dict:
    """O que a tela precisa, já separado entre o que dá e o que não dá."""
    aplicaveis = [p for p in propostas if p.aplicavel]
    impedidas = [p for p in propostas if not p.aplicavel]

    # Agrupadas pelas cinco opções de correção, na ordem em que foram
    # definidas: é a ordem em que a pessoa pensa (primeiro o CFOP, que é a
    # classificação, depois os créditos que dependem dele).
    grupos = []
    for opcao in OPCOES_DE_CORRECAO:
        do_grupo = [p for p in aplicaveis if p.opcao == opcao.codigo]
        if do_grupo:
            grupos.append({"opcao": opcao, "propostas": do_grupo})

    return {
        "task": task,
        "task_id": task.id,
        "grupos": grupos,
        "impedidas": impedidas,
        "total_aplicaveis": len(aplicaveis),
        "sem_arquivo": "" if texto else SEM_ARQUIVO,
        "geracoes": list(geracoes),
    }


@router.get("/api/audit/{task_id}/correcao", response_class=HTMLResponse)
def listar_correcoes(request: Request, task_id: str,
                     db_session: Session = Depends(get_db),
                     profile=Depends(get_profile)):
    """A lista de correções que esta auditoria autoriza oferecer."""
    task, texto, propostas = _propostas(db_session, profile, task_id)
    geracoes = _geracoes_anteriores(db_session, task_id)
    return templates.TemplateResponse(
        request, "partials/curation/_correcao_sped.html",
        {"request": request, **_contexto(task, texto, propostas, geracoes)})


@router.post("/api/audit/{task_id}/correcao/previa", response_class=HTMLResponse)
def previa_da_correcao(request: Request, task_id: str,
                       aceitas: list[str] = Form(default=[]),
                       atualizar_e110: bool = Form(default=False),
                       db_session: Session = Depends(get_db),
                       profile=Depends(get_profile),
                       email: str = Depends(usuario_email)):
    """O que vai acontecer, antes de gerar. Nada é gravado aqui."""
    task, texto, propostas = _propostas(db_session, profile, task_id)
    if not texto:
        return templates.TemplateResponse(
            request, "partials/curation/_correcao_previa.html",
            {"request": request, "task_id": task_id, "erro": SEM_ARQUIVO},
            status_code=200)

    escolhidas = [p for p in propostas if p.id in set(aceitas)]
    if not escolhidas:
        # Resposta 200 com mensagem visível, e não 4xx: em 4xx o htmx não troca
        # o alvo e o usuário clica sem nada acontecer na tela.
        return templates.TemplateResponse(
            request, "partials/curation/_correcao_previa.html",
            {"request": request, "task_id": task_id,
             "erro": "Nenhuma correção marcada. Marque ao menos uma para ver a prévia."},
            status_code=200)

    patch = montar_patch(propostas, aceitas, aprovada_por=email,
                         atualizar_apuracao=atualizar_e110)
    previa = prever(texto, patch, origem=task.file_name or "SPED")
    return templates.TemplateResponse(
        request, "partials/curation/_correcao_previa.html",
        {"request": request, "task_id": task_id, "previa": previa,
         "escolhidas": escolhidas, "atualizar_e110": atualizar_e110,
         "ids": [p.id for p in escolhidas if p.aplicavel]})


@router.post("/api/audit/{task_id}/correcao/resumo", response_class=HTMLResponse)
def resumo_executivo(request: Request, task_id: str,
                     aceitas: list[str] = Form(default=[]),
                     atualizar_e110: bool = Form(default=False),
                     db_session: Session = Depends(get_db),
                     profile=Depends(get_profile),
                     email: str = Depends(usuario_email)):
    """O parágrafo que explica a correção para quem não acompanhou a tela.

    Fica atrás de um botão, e não dentro da prévia, por dois motivos: a prévia é
    clicada a cada ajuste de seleção, e cada geração é uma chamada paga a um
    modelo; e o resumo só faz sentido quando a escolha já está fechada.
    """
    task, texto, propostas = _propostas(db_session, profile, task_id)
    if not texto:
        return templates.TemplateResponse(
            request, "partials/curation/_correcao_resumo.html",
            {"request": request, "erro": SEM_ARQUIVO})

    patch = montar_patch(propostas, aceitas, aprovada_por=email,
                         atualizar_apuracao=atualizar_e110)
    if patch.vazio:
        return templates.TemplateResponse(
            request, "partials/curation/_correcao_resumo.html",
            {"request": request,
             "erro": "Marque ao menos uma correção aplicável para gerar o resumo."})

    doc = ler_texto(texto, caminho=task.file_name or "SPED")
    resultado = aplicar(doc, patch)
    relatorio = montar(resultado, doc, texto, origem=task.file_name or "SPED",
                       destino=nome_sugerido(doc))

    resumo, da_ia, motivo = resumir(relatorio)
    _guardar_resumo(task_id, aceitas, resumo, da_ia, motivo)
    return templates.TemplateResponse(
        request, "partials/curation/_correcao_resumo.html",
        {"request": request, "resumo": resumo, "da_ia": da_ia, "motivo": motivo})


def _competencia_aaaa_mm(mm_aaaa: str) -> str | None:
    """"04/2026" -> "2026-04". O relatório mostra; a coluna ordena.

    O formato da coluna é o mesmo de `audit_tasks.competencia` de propósito: é
    o que ordena cronologicamente e agrupa direto no banco (ver
    core/competencia.py). Guardar "04/2026" faria abril de 2026 vir antes de
    janeiro de 2025 em qualquer ORDER BY.
    """
    partes = (mm_aaaa or "").split("/")
    if len(partes) == 2 and len(partes[0]) == 2 and len(partes[1]) == 4:
        return f"{partes[1]}-{partes[0]}"
    return None


def _registrar_geracao(db_session: Session, task, relatorio, resultado,
                       aceitas: list, email: str) -> None:
    """Grava a trilha: quem gerou, quando, sobre qual arquivo e o que mudou.

    Este é o único ponto do sistema em que o Synax PRODUZ um documento fiscal em
    vez de conferir um — o arquivo sai daqui e o cliente o transmite. Falhar a
    gravação não pode impedir o download (a pessoa ficaria sem o arquivo por um
    problema de banco), mas some do log em nível de erro: trilha que falha em
    silêncio é pior do que trilha nenhuma, porque parece existir.
    """
    guardado = _resumo_guardado(task.id, aceitas)
    try:
        db_session.add(SpedCorrectionRun(
            id=str(uuid.uuid4()),
            tenant_id=str(task.tenant_id),
            task_id=task.id,
            user_email=email,
            file_name=relatorio.destino,
            competencia=_competencia_aaaa_mm(relatorio.competencia),
            hash_origem=relatorio.hash_origem,
            hash_destino=relatorio.hash_destino,
            correcoes_aceitas=len(aceitas or []),
            alteracoes=relatorio.total_alteracoes,
            recusas=len(resultado.recusas),
            icms_diferenca=relatorio.impacto.icms_diferenca,
            ipi_diferenca=relatorio.impacto.ipi_diferenca,
            apuracao_atualizada=bool(relatorio.apuracao),
            detalhe=para_json(relatorio),
            resumo_ia=guardado[0] if guardado else None,
        ))
        db_session.commit()
    except Exception:
        db_session.rollback()
        logger.exception("Falha ao registrar a geração do SPED corrigido "
                         "task_id=%s por=%s", task.id, email)


def _aplicar(db_session: Session, profile, task_id: str, aceitas: list,
             email: str, atualizar_e110: bool = False):
    """Roda a correção de verdade. Devolve (task, doc, relatorio, resultado)."""
    task, texto, propostas = _propostas(db_session, profile, task_id)
    if not texto:
        raise HTTPException(status_code=409, detail=SEM_ARQUIVO)

    patch = montar_patch(propostas, aceitas, aprovada_por=email,
                         atualizar_apuracao=atualizar_e110)
    if patch.vazio:
        # Sem nada aplicável o arquivo sairia idêntico ao original — baixar uma
        # cópia do próprio SPED como se fosse a versão corrigida é pior do que
        # o erro na tela.
        raise HTTPException(
            status_code=409,
            detail=("Nenhuma das correções marcadas pode ser aplicada. "
                    "Volte à prévia para ver o motivo de cada uma."))

    doc = ler_texto(texto, caminho=task.file_name or "SPED")
    resultado = aplicar(doc, patch)
    relatorio = montar(resultado, doc, texto, origem=task.file_name or "SPED",
                       destino=nome_sugerido(doc))
    logger.info("SPED corrigido gerado task_id=%s por=%s alteracoes=%d recusas=%d "
                "apuracao=%s", task_id, email, len(resultado.alteracoes),
                len(resultado.recusas), "sim" if relatorio.apuracao else "nao")
    return task, doc, relatorio, resultado


@router.post("/api/audit/{task_id}/correcao/gerar")
def gerar_sped_corrigido(task_id: str, aceitas: list[str] = Form(default=[]),
                         atualizar_e110: bool = Form(default=False),
                         db_session: Session = Depends(get_db),
                         profile=Depends(get_profile),
                         email: str = Depends(usuario_email)):
    """O arquivo corrigido, para download. O original continua intocado."""
    task, doc, rel, resultado = _aplicar(db_session, profile, task_id, aceitas,
                                        email, atualizar_e110)
    _registrar_geracao(db_session, task, rel, resultado, aceitas, email)

    conteudo = doc.serializar().encode(ENCODING, errors="replace")
    nome = nome_sugerido(doc)
    # `octet-stream`, e não `text/plain`: o tipo textual convida o navegador a
    # renderizar o arquivo, e o que apareceria na tela é o SPED inteiro do
    # cliente. O que este botão faz é baixar.
    return StreamingResponse(
        io.BytesIO(conteudo), media_type="application/octet-stream",
        headers={**_cabecalhos_download(nome),
                 "Content-Length": str(len(conteudo))})


@router.post("/api/audit/{task_id}/correcao/relatorio")
def baixar_relatorio(task_id: str, aceitas: list[str] = Form(default=[]),
                     atualizar_e110: bool = Form(default=False),
                     db_session: Session = Depends(get_db),
                     profile=Depends(get_profile),
                     email: str = Depends(usuario_email)):
    """O relatório de modificações em Excel — uma linha por alteração.

    É o documento que justifica a retificação para quem não acompanhou a tela:
    cabeçalho de integridade (hash dos dois arquivos), o que mudou em cada
    documento e o impacto na apuração.
    """
    task, _doc, rel, _res = _aplicar(db_session, profile, task_id, aceitas,
                                    email, atualizar_e110)

    linhas = linhas_para_planilha(rel)
    saida = io.BytesIO()
    with pd.ExcelWriter(saida, engine="openpyxl") as writer:
        pd.DataFrame(linhas or [{"Situação": "Nenhuma alteração aplicada"}]
                     ).to_excel(writer, sheet_name="Alterações", index=False)

        impacto = rel.impacto
        pd.DataFrame([
            {"Tributo": "ICMS — créditos (soma dos analíticos de entrada)",
             "Antes": float(impacto.icms_creditos_antes),
             "Depois": float(impacto.icms_creditos_depois),
             "Diferença": float(impacto.icms_diferenca)},
            {"Tributo": "IPI — créditos (soma dos analíticos de entrada)",
             "Antes": float(impacto.ipi_creditos_antes),
             "Depois": float(impacto.ipi_creditos_depois),
             "Diferença": float(impacto.ipi_diferenca)},
        ]).to_excel(writer, sheet_name="Impacto na Apuração", index=False)

        guardado = _resumo_guardado(task_id, aceitas)
        resumo, da_ia, _motivo = guardado if guardado else resumir(rel)
        origem_resumo = ("Resumo executivo (redigido por IA sobre os números "
                         "apurados pelo sistema)" if da_ia else
                         "Resumo executivo")
        pd.DataFrame([{origem_resumo: resumo}]).to_excel(
            writer, sheet_name="Resumo", index=False)

        if rel.apuracao:
            pd.DataFrame([{
                "Registro": a.registro, "Campo": a.campo, "De": a.de,
                "Para": a.para, "Motivo": a.motivo,
            } for a in rel.apuracao]).to_excel(
                writer, sheet_name="Apuração (E110)", index=False)

        wb = writer.book
        montar_capa(
            wb,
            titulo="Correções aplicadas ao SPED",
            subtitulo=rel.destino,
            linhas=[
                ("Arquivo de origem", rel.origem),
                ("Arquivo gerado", rel.destino),
                ("SHA-256 do original", rel.hash_origem),
                ("SHA-256 do corrigido", rel.hash_destino),
                ("Competência", rel.competencia or "(não informada no 0000)"),
                ("Linhas antes / depois", f"{rel.linhas_antes} / {rel.linhas_depois}"),
                ("Alterações", rel.total_alteracoes),
                ("Apuração (E110) reescrita",
                 "sim" if rel.apuracao else "não"),
                ("Gerado por", email),
                ("Gerado em", hora_local(datetime.utcnow()).strftime("%d/%m/%Y às %H:%M")),
            ],
        )
        estilizar_planilha(wb, cores={"Alterações": AZUL,
                                      "Apuração (E110)": AMBAR})

    saida.seek(0)
    nome = f"Correcoes_{(task.file_name or 'sped').rsplit('.', 1)[0]}.xlsx"
    return StreamingResponse(
        saida,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=_cabecalhos_download(nome))
