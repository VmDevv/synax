"""Lucro Presumido — a EFD-Contribuições gerada do Domínio, dentro do Synax.

A primeira página do programa SPED PIS/COFINS (`Desktop\\sped-pis-cofins`) trazida para cá, a pedido do Sandro em
22/10/2026. O motor continua LÁ: esta rota chama o programa pela linha de comando e lê o resultado em JSON (ver
`services/efd_contribuicoes/programa.py`). Correção fiscal se faz uma vez, no programa, e vale aqui e na tela dele.

Quatro decisões desta rota:

- **Quem vê qual empresa é a regra da casa.** A lista vem do Domínio (matrizes ativas) e é cruzada com os tenants
  do Synax pelo CNPJ. O admin vê todas — a que não está cadastrada aqui aparece marcada —; o operador vê só as de
  `user_tenants`, e TODA ação (gerar, baixar, salvar, lote) confere de novo no servidor: o código da empresa vem do
  formulário.
- **O arquivo não sai no POST.** Primeiro vem o resultado — situação, validação, conferência com o Domínio —, e o
  download é o passo seguinte, como no Gerar SPED. Arquivo que não passou na validação só baixa "para análise" e
  NUNCA vai para a pasta dos prontos: o PVA o recusaria.
- **A pasta de "Salvar" não vem do formulário.** É a configurada no programa (página Configurações de lá). Aceitar
  caminho da tela seria deixar qualquer usuário gravar em qualquer pasta da rede com o usuário do servidor.
- **O histórico dos lotes é o do programa**, o mesmo que a tela de lá mostra. A planilha e o .zip de um lote têm
  TODAS as empresas dele, então baixá-los é só do admin; o operador vê, de cada lote, só as linhas das empresas dele.
"""
import json
import logging
import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from ...core import competencia as comp
from ...core.authz import assert_internal, get_profile
from ...core.templating import build_templates
from ...infrastructure.database.session import get_db
from ...services.efd_contribuicoes import FalhaDoPrograma, ProgramaIndisponivel, empresas, lote, programa
from ...services.efd_contribuicoes.empresas import Empresa, EmpresaRecusada
from .correcao_sped import _cabecalhos_download
from .uploads import get_current_user_email

logger = logging.getLogger(__name__)
router = APIRouter()
templates = build_templates()

TEMP_DIR = os.path.join(os.getcwd(), "temp_uploads")
ARQUIVO = "efd.txt"
META = "meta.json"
PASTA_COM_ERRO = "_com_erro"

SITUACOES = {
    # situação do programa -> (rótulo, tom). Os rótulos são os da tela do programa (app/ui.py, SITUACAO_EMPRESA).
    "confere": ("Confere com o Domínio", "ok"),
    "diferente": ("Diferente do Domínio", "aviso"),
    "erro na validação": ("Erro na validação: não transmita", "erro"),
    "revisar cadastro": ("Revisar o cadastro: não transmita", "erro"),
    "sem apuração no Domínio": ("Sem apuração no Domínio para comparar", "neutro"),
    "sem movimento": ("Sem movimento", "neutro"),
    "fora do lote": ("Fora do lote", "neutro"),
    "erro": ("Com erro", "erro"),
}
STATUS_LOTE = {"ok": ("Concluído", "ok"), "com_avisos": ("Com avisos", "aviso"), "erro": ("Com erro", "erro"),
               "rodando": ("Rodando", "info"), "interrompida": ("Interrompido", "aviso")}


# Quem vê qual empresa é regra comum às páginas do programa SPED PIS/COFINS (services/efd_contribuicoes/empresas.py).
_empresas = empresas.do_usuario


class _Recusa(Exception):
    """O pedido não pode seguir; a mensagem vai para a tela (200, nunca 4xx: o htmx não troca o alvo em 4xx)."""


def _empresa_permitida(db_session: Session, profile, codi_emp) -> Empresa:
    try:
        return empresas.permitida(db_session, profile, codi_emp)
    except EmpresaRecusada as exc:
        raise _Recusa(str(exc))


def _competencia(texto: str) -> str:
    ok = comp.valida((texto or "").strip())
    if not ok:
        raise _Recusa("Informe a competência (mês e ano).")
    return ok


def _aviso(request: Request, mensagem: str, tom: str = "erro"):
    return templates.TemplateResponse(request, "partials/lucro_presumido/aviso.html",
                                      {"request": request, "mensagem": mensagem, "tom": tom})


# ------------------------------------------------------------------------------------------------ a página
@router.get("/lucro-presumido", response_class=HTMLResponse)
def tela(request: Request, codi_emp: str = "", competencia: str = "", profile=Depends(get_profile)):
    """`codi_emp` e `competencia` na URL chegam já escolhidos — é como a página Domínio manda para cá. Só pré-escolhem:
    a empresa ainda passa pela lista do usuário quando o formulário é montado, e de novo ao gerar."""
    assert_internal(profile)
    atual = lote.atual()
    return templates.TemplateResponse(request, "pages/lucro_presumido.html", {
        "request": request, "user_profile": profile, "competencia": comp.valida(competencia) or comp.fechada(),
        "selecionada": codi_emp if codi_emp.isdigit() else "",
        "admin": profile.role == "admin", "lote_ativo": atual is not None and atual.rodando,
    })


@router.get("/api/lucro-presumido/empresas", response_class=HTMLResponse)
def lista_de_empresas(request: Request, para: str = "uma", todas: int = 0, atualizar: int = 0, selecionada: str = "",
                      db_session: Session = Depends(get_db), profile=Depends(get_profile)):
    """O formulário de cada aba com a lista de empresas — carregado depois da página, porque ler o Domínio leva uns
    segundos e a página não precisa esperar por isso."""
    assert_internal(profile)
    try:
        lista = _empresas(db_session, profile, atualizar=bool(atualizar))
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        return _aviso(request, f"Não consegui ler as empresas do Domínio: {exc}")
    escolhida = next((e for e in lista if selecionada.isdigit() and e.codi_emp == int(selecionada)), None)
    # Pré-escolhida que não é presumida só aparece com a lista inteira: o filtro a esconderia.
    todas = bool(todas) or (escolhida is not None and not escolhida.presumido)
    visiveis = lista if todas else [e for e in lista if e.presumido]
    para = "lote" if para == "lote" else "uma"
    return templates.TemplateResponse(request, f"partials/lucro_presumido/{para}_form.html", {
        "request": request, "empresas": visiveis, "todas": todas, "total": len(lista),
        "admin": profile.role == "admin", "para": para, "selecionada": escolhida.codi_emp if escolhida else None,
    })


# ------------------------------------------------------------------------------------------------ uma empresa
def _pasta_do_processo(processo: str) -> str:
    # O identificador é um UUID gerado aqui, mas ele entra num caminho: conferir o formato é o que impede `..\..\`
    # de virar leitura de arquivo do servidor.
    try:
        uuid.UUID(processo)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    return os.path.join(TEMP_DIR, processo)


def _meta(db_session: Session, profile, processo: str) -> tuple[str, dict]:
    """A geração `processo`, conferindo que a empresa dela ainda é deste perfil."""
    pasta = _pasta_do_processo(processo)
    try:
        with open(os.path.join(pasta, META), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="O arquivo gerado não está mais no servidor. Gere de novo.")
    try:
        _empresa_permitida(db_session, profile, meta.get("codi_emp"))
    except _Recusa:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    return pasta, meta


@router.post("/api/lucro-presumido/gerar", response_class=HTMLResponse)
def gerar(request: Request, codi_emp: str = Form(""), competencia: str = Form(""),
          db_session: Session = Depends(get_db), profile=Depends(get_profile),
          user_email: str = Depends(get_current_user_email)):
    assert_internal(profile)
    try:
        empresa = _empresa_permitida(db_session, profile, codi_emp)
        mes = _competencia(competencia)
    except _Recusa as exc:
        return _aviso(request, str(exc), "aviso")
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        return _aviso(request, f"Não consegui ler as empresas do Domínio: {exc}")

    processo = str(uuid.uuid4())
    pasta = os.path.join(TEMP_DIR, processo)
    os.makedirs(pasta, exist_ok=True)
    try:
        r = programa.gerar(empresa.codi_emp, mes, Path(pasta) / ARQUIVO)
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        shutil.rmtree(pasta, ignore_errors=True)
        return _aviso(request, f"Não deu para gerar: {exc}")

    meta = {"codi_emp": empresa.codi_emp, "cnpj": empresa.cnpj, "competencia": mes, "empresa": r.get("empresa"),
            "nome_arquivo": r.get("nome_arquivo") or f"EFD_CONTRIB_{empresa.cnpj}_{mes.replace('-', '')}.txt",
            "erros_validacao": int(r.get("erros_validacao") or 0), "situacao": r.get("situacao"),
            "pasta_padrao": str(r.get("pasta_padrao") or ""), "gerado_por": user_email}
    with open(os.path.join(pasta, META), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    logger.info("EFD-Contribuições do Presumido gerada: empresa=%s competencia=%s situacao=%s erros=%s por=%s "
                "processo=%s", empresa.codi_emp, mes, meta["situacao"], meta["erros_validacao"], user_email, processo)

    texto = ""
    try:
        with open(os.path.join(pasta, ARQUIVO), encoding="latin-1") as f:
            texto = f.read()
    except OSError:
        pass
    pis_cofins = [l for l in r.get("resumo") or [] if l.get("item") == "Valor a recolher"]
    return templates.TemplateResponse(request, "partials/lucro_presumido/resultado.html", {
        "request": request, "r": r, "empresa": empresa, "processo": processo, "competencia": mes,
        "rotulo_competencia": comp.rotulo(mes), "situacao": SITUACOES.get(r.get("situacao"), (r.get("situacao"), "neutro")),
        "pis_cofins": pis_cofins, "registros": [l for l in texto.splitlines() if l and not l.startswith("|99")],
        "destino": _destino(meta),
    })


def _destino(meta: dict) -> str:
    if not meta.get("pasta_padrao"):
        return ""
    return str(Path(meta["pasta_padrao"]) / meta["competencia"] / meta["nome_arquivo"])


@router.get("/api/lucro-presumido/{processo}/arquivo")
def baixar(processo: str, db_session: Session = Depends(get_db), profile=Depends(get_profile)):
    assert_internal(profile)
    pasta, meta = _meta(db_session, profile, processo)
    try:
        with open(os.path.join(pasta, ARQUIVO), "rb") as f:
            conteudo = f.read()
    except OSError:
        raise HTTPException(status_code=404, detail="O arquivo gerado não está mais no servidor. Gere de novo.")
    nome = meta["nome_arquivo"]
    if meta.get("erros_validacao"):
        # O nome diz o que o arquivo é: baixado "para análise", ele não pode ser confundido com o pronto.
        nome = nome.replace(".txt", "_COM_ERRO_NAO_TRANSMITIR.txt")
    return StreamingResponse(iter([conteudo]), media_type="application/octet-stream",
                             headers={**_cabecalhos_download(nome), "Content-Length": str(len(conteudo))})


@router.post("/api/lucro-presumido/{processo}/salvar", response_class=HTMLResponse)
def salvar(request: Request, processo: str, db_session: Session = Depends(get_db), profile=Depends(get_profile),
           user_email: str = Depends(get_current_user_email)):
    assert_internal(profile)
    try:
        pasta, meta = _meta(db_session, profile, processo)
    except HTTPException as exc:
        return _aviso(request, str(exc.detail))
    if meta.get("erros_validacao"):
        return _aviso(request, "Este arquivo não passou na validação e o PVA vai recusá-lo: ele não vai para a pasta "
                               "dos arquivos prontos. Baixe para análise.")
    destino = _destino(meta)
    if not destino:
        return _aviso(request, "A pasta dos arquivos gerados não está configurada no programa SPED PIS/COFINS "
                               "(página Configurações de lá).")
    existia = os.path.exists(destino)
    try:
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        shutil.copyfile(os.path.join(pasta, ARQUIVO), destino)
    except OSError as exc:
        return _aviso(request, f"Não deu para salvar em {os.path.dirname(destino)}: {exc}")
    logger.info("EFD-Contribuições do Presumido salva: %s por=%s processo=%s", destino, user_email, processo)
    return _aviso(request, f"Salvo em {destino}" + (" — substituiu o que estava lá." if existia else "."), "ok")


# ------------------------------------------------------------------------------------------------ lote
def _codigos_permitidos(db_session: Session, profile) -> set[int]:
    return {e.codi_emp for e in _empresas(db_session, profile)}


def _pode_ver_lote(atual, db_session: Session, profile) -> bool:
    if atual is None:
        return False
    if profile.role == "admin":
        return True
    try:
        return set(atual.codigos) <= _codigos_permitidos(db_session, profile)
    except (ProgramaIndisponivel, FalhaDoPrograma):
        return False


def _andamento(request: Request, db_session: Session, profile, aviso: str = ""):
    atual = lote.atual()
    visivel = atual if _pode_ver_lote(atual, db_session, profile) else None
    execucao = (visivel.execucao if visivel and not visivel.rodando else None) or {}
    return templates.TemplateResponse(request, "partials/lucro_presumido/andamento.html", {
        "request": request, "lote": visivel, "aviso": aviso, "rotulo_competencia": comp.rotulo,
        "status": STATUS_LOTE.get(execucao.get("status"), (execucao.get("status"), "neutro")),
        "linhas": _linhas_visiveis(execucao, db_session, profile), "admin": profile.role == "admin",
        "situacoes": SITUACOES,
    })


@router.post("/api/lucro-presumido/lote", response_class=HTMLResponse)
def iniciar_lote(request: Request, competencia: str = Form(""), quais: str = Form("todas"), todas: str = Form(""),
                 codigos: list[str] = Form([]), db_session: Session = Depends(get_db),
                 profile=Depends(get_profile), user_email: str = Depends(get_current_user_email)):
    assert_internal(profile)
    try:
        mes = _competencia(competencia)
        empresas = _empresas(db_session, profile)
    except _Recusa as exc:
        return _andamento(request, db_session, profile, str(exc))
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        return _andamento(request, db_session, profile, f"Não consegui ler as empresas do Domínio: {exc}")
    if quais == "escolhidas":
        permitidos = {e.codi_emp for e in empresas}
        try:
            pedidos = {int(c) for c in codigos if str(c).strip()}
        except ValueError:
            pedidos = set()
        fora = pedidos - permitidos
        if fora:
            return _andamento(request, db_session, profile, "Há empresa fora da sua lista no pedido; nada foi gerado.")
        alvo = sorted(pedidos)
    else:
        alvo = [e.codi_emp for e in empresas if todas or e.presumido]
    if not alvo:
        return _andamento(request, db_session, profile, "Nenhuma empresa escolhida; nada foi gerado.")
    try:
        lote.iniciar(mes, alvo, user_email)
    except (lote.LoteOcupado, ProgramaIndisponivel, FalhaDoPrograma) as exc:
        return _andamento(request, db_session, profile, str(exc))
    return _andamento(request, db_session, profile)


@router.get("/api/lucro-presumido/lote/andamento", response_class=HTMLResponse)
def andamento(request: Request, db_session: Session = Depends(get_db), profile=Depends(get_profile)):
    assert_internal(profile)
    return _andamento(request, db_session, profile)


# ------------------------------------------------------------------------------------------------ histórico
def _linhas_visiveis(execucao: dict, db_session: Session, profile) -> list[dict]:
    linhas = list((execucao or {}).get("linhas") or [])
    if profile.role == "admin" or not linhas:
        return linhas
    try:
        permitidos = _codigos_permitidos(db_session, profile)
    except (ProgramaIndisponivel, FalhaDoPrograma):
        return []
    return [l for l in linhas if _codigo(l) in permitidos]


def _codigo(linha: dict) -> int | None:
    try:
        return int(linha.get("codigo"))
    except (TypeError, ValueError):
        return None


@router.get("/api/lucro-presumido/historico", response_class=HTMLResponse)
def historico(request: Request, db_session: Session = Depends(get_db), profile=Depends(get_profile)):
    assert_internal(profile)
    try:
        execucoes = programa.historico(20)
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        return _aviso(request, f"Não consegui ler o histórico do programa SPED PIS/COFINS: {exc}")
    admin = profile.role == "admin"
    itens = []
    for ex in execucoes:
        linhas = _linhas_visiveis(ex, db_session, profile)
        if not admin and not linhas:
            continue
        itens.append({"ex": ex, "linhas": linhas,
                      "status": STATUS_LOTE.get(ex.get("status"), (ex.get("status"), "neutro")),
                      "arquivos": [Path(str(a)).name for a in ex.get("arquivos") or []] if admin else []})
    return templates.TemplateResponse(request, "partials/lucro_presumido/historico.html", {
        "request": request, "itens": itens, "admin": admin, "situacoes": SITUACOES,
    })


@router.get("/api/lucro-presumido/execucao/{ident}/arquivo/{n}")
def baixar_do_lote(ident: str, n: int, profile=Depends(get_profile)):
    """A planilha ou o .zip de um lote. Só o admin: os dois têm TODAS as empresas do lote. E só caminho que esteja na
    lista de arquivos daquela execução — o índice vem da URL, o caminho nunca."""
    assert_internal(profile)
    if profile.role != "admin":
        raise HTTPException(status_code=403, detail="Só o admin baixa os arquivos do lote: eles têm todas as empresas.")
    try:
        ex = programa.execucao(ident)
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    arquivos = (ex or {}).get("arquivos") or []
    if not 0 <= n < len(arquivos):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    caminho = Path(str(arquivos[n]))
    if caminho.suffix.lower() not in (".xlsx", ".zip", ".txt") or not caminho.is_file():
        raise HTTPException(status_code=404, detail="O arquivo não está mais na pasta.")
    conteudo = caminho.read_bytes()
    return StreamingResponse(iter([conteudo]), media_type="application/octet-stream",
                             headers={**_cabecalhos_download(caminho.name), "Content-Length": str(len(conteudo))})
