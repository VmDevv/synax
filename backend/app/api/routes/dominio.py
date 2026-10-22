"""Domínio — o que o Synax lê do banco do Domínio, num lugar só.

A segunda página do programa SPED PIS/COFINS (`Desktop\\sped-pis-cofins`) trazida para o Synax, a pedido do Sandro em
22/10/2026, com o mesmo desenho da primeira (Lucro Presumido): quem fala com o Domínio é o PROGRAMA, chamado pela
linha de comando (`services/efd_contribuicoes/programa.py`), e tudo é só leitura.

Quatro decisões desta rota:

- **Empresa e notas seguem a regra de empresas comum** (`services/efd_contribuicoes/empresas.py`): o operador só vê
  as dele, e o código da empresa, que vem do formulário, é conferido de novo em toda ação.
- **Consulta livre (SQL) e explorador de tabelas só com `pode_sql_dominio`** (`core/authz.py`): o admin sempre; o
  operador com a marca que o admin dá na edição do usuário. A consulta lê QUALQUER tabela, de qualquer empresa — por
  isso não vem com o papel. Quem impede escrita é a trava do programa (`odbc._somente_leitura`, a mesma da tela de
  lá); quem decide quem pode usar é daqui. Cada consulta vai para o log com quem a fez.
- **O SQL vai para o programa num arquivo**, nunca na linha de comando: texto livre ali tem limite de tamanho e
  regra de aspas própria do Windows.
- **As planilhas são geradas pelo programa** em `temp_uploads/<uuid>` (limpas em 7 dias, como as outras) e baixadas
  com o acesso conferido de novo: a das notas, pela empresa; a da consulta, só por quem a fez e ainda tem a permissão.
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
from ...core.authz import assert_internal, get_profile, pode_sql_dominio
from ...core.templating import build_templates
from ...infrastructure.database.session import get_db
from ...services.efd_contribuicoes import FalhaDoPrograma, ProgramaIndisponivel, empresas, programa
from ...services.efd_contribuicoes.empresas import EmpresaRecusada
from .correcao_sped import _cabecalhos_download
from .uploads import get_current_user_email

logger = logging.getLogger(__name__)
router = APIRouter()
templates = build_templates()

TEMP_DIR = os.path.join(os.getcwd(), "temp_uploads")
META = "meta.json"
PLANILHA = "planilha.xlsx"
# Uma consulta de verdade cabe com folga; acima disso é colagem errada, e o programa nem precisa ser chamado.
LIMITE_SQL = 20_000
# As colunas de dinheiro das notas: formatadas em reais na tela (a planilha leva o número).
DINHEIRO = {"vl_item", "vl_desc", "vl_icms", "vl_icms_st", "vl_ipi", "base_pis", "pis", "base_cofins", "cofins"}
# O nome legível das colunas das notas (o técnico continua na planilha). A consulta livre não passa por aqui: lá a
# coluna tem o nome que a pessoa escreveu.
ROTULOS = {"tipo": "Tipo", "cfop": "CFOP", "notas": "Notas", "vl_item": "Valor", "vl_desc": "Desconto",
           "vl_icms": "ICMS", "vl_icms_st": "ICMS ST", "vl_ipi": "IPI", "base_pis": "Base PIS", "aliq_pis": "Alíq. PIS",
           "pis": "PIS", "base_cofins": "Base COFINS", "aliq_cofins": "Alíq. COFINS", "cofins": "COFINS",
           "origem": "Origem", "estab": "Estab.", "documento": "Documento", "data": "Data",
           "participante": "Participante", "cst_pis": "CST PIS", "cst_cofins": "CST COFINS",
           "nat_bc_cred": "Nat. crédito", "cod_cta": "Conta", "descricao": "Descrição"}
# "origem" é sempre DOMINIO aqui: na tela é ruído (na planilha fica).
SEMPRE_OCULTAS = ("origem",)


def _colunas_sem_informacao(t: dict) -> list[str]:
    """As colunas da tabela em que nenhuma linha mostrada tem valor — somem da tela, não da planilha."""
    linhas = t.get("linhas") or []
    vazias = [c for i, c in enumerate(t.get("colunas") or [])
              if linhas and all(linha[i] in (None, "") for linha in linhas)]
    return [c for c in t.get("colunas") or [] if c in SEMPRE_OCULTAS or c in vazias]

COMO_FUNCIONA = (
    ("Empresas", "As matrizes ativas do Domínio (geempre). \"Presumida\" é quem tem PIS cumulativo no cadastro de "
                 "impostos — é só indicação: o cadastro de alguns clientes está errado."),
    ("Apuração gravada", "O PIS/COFINS que o Domínio apurou no mês (efsdoimp). No Lucro Presumido é o gabarito da "
                         "EFD gerada; no Lucro Real é só referência."),
    ("Notas do mês", "Saídas e entradas com o PIS/COFINS lançado. Servem para conferir e para o Recálculo da "
                     "Conferência do programa."),
    ("Geração da EFD", "Notas, serviços, acumuladores, contas e parâmetros da empresa: é o que a página Lucro "
                       "Presumido e a automação mensal leem para montar o arquivo."),
)


class _Recusa(Exception):
    """O pedido não pode seguir; a mensagem vai para a tela (200, nunca 4xx: o htmx não troca o alvo em 4xx)."""


def _aviso(request: Request, mensagem: str, tom: str = "erro"):
    return templates.TemplateResponse(request, "partials/lucro_presumido/aviso.html",
                                      {"request": request, "mensagem": mensagem, "tom": tom})


def _competencia(texto: str) -> str:
    ok = comp.valida((texto or "").strip())
    if not ok:
        raise _Recusa("Informe a competência (mês e ano).")
    return ok


def _empresa(db_session: Session, profile, codi_emp):
    try:
        return empresas.permitida(db_session, profile, codi_emp)
    except EmpresaRecusada as exc:
        raise _Recusa(str(exc))


def _nova_pasta(meta: dict) -> Path:
    pasta = Path(TEMP_DIR) / str(uuid.uuid4())
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / META).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return pasta


# ------------------------------------------------------------------------------------------------ a página
@router.get("/dominio", response_class=HTMLResponse)
def tela(request: Request, profile=Depends(get_profile)):
    assert_internal(profile)
    return templates.TemplateResponse(request, "pages/dominio.html", {
        "request": request, "user_profile": profile, "competencia": comp.fechada(),
        "pode_sql": pode_sql_dominio(profile), "admin": profile.role == "admin", "como_funciona": COMO_FUNCIONA,
    })


@router.get("/api/dominio/status", response_class=HTMLResponse)
def status(request: Request, profile=Depends(get_profile)):
    assert_internal(profile)
    situacao = {"ok": False, "erro": "", "ativas": 0, "presumidas": 0, "conexao": ""}
    try:
        lista = programa.empresas()
        situacao.update(ok=True, ativas=len(lista), presumidas=sum(1 for e in lista if e.get("presumido")))
        situacao["conexao"] = programa.info().get("conexao") or ""
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        situacao["erro"] = str(exc)
    return templates.TemplateResponse(request, "partials/dominio/status.html", {
        "request": request, "s": situacao, "admin": profile.role == "admin"})


@router.post("/api/dominio/testar", response_class=HTMLResponse)
def testar(request: Request, profile=Depends(get_profile)):
    assert_internal(profile)
    # Testar é também "esquecer o que estava guardado": a lista de empresas em cache é de antes do teste.
    programa.esquecer_empresas()
    try:
        return _aviso(request, programa.testar(), "ok")
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        return _aviso(request, f"Falhou: {exc}")


@router.get("/api/dominio/seletor", response_class=HTMLResponse)
def seletor(request: Request, db_session: Session = Depends(get_db), profile=Depends(get_profile)):
    assert_internal(profile)
    try:
        lista = empresas.do_usuario(db_session, profile)
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        return _aviso(request, f"Não consegui ler as empresas do Domínio: {exc}")
    return templates.TemplateResponse(request, "partials/dominio/seletor.html", {
        "request": request, "empresas": lista, "admin": profile.role == "admin"})


# ------------------------------------------------------------------------------------------------ empresa e notas
@router.get("/api/dominio/empresa", response_class=HTMLResponse)
def empresa(request: Request, codi_emp: str = "", competencia: str = "",
            db_session: Session = Depends(get_db), profile=Depends(get_profile)):
    assert_internal(profile)
    if not codi_emp.strip():
        return templates.TemplateResponse(request, "partials/dominio/empresa.html", {"request": request, "cad": None,
                                                                                     "vazio": True})
    try:
        e = _empresa(db_session, profile, codi_emp)
        mes = _competencia(competencia)
        cad = programa.cadastro(e.codi_emp, mes)
    except _Recusa as exc:
        return _aviso(request, str(exc), "aviso")
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        return _aviso(request, f"Não deu para ler o cadastro: {exc}")
    return templates.TemplateResponse(request, "partials/dominio/empresa.html", {
        "request": request, "cad": cad, "empresa": e, "competencia": mes, "rotulo_competencia": comp.rotulo(mes),
        "vazio": False})


@router.post("/api/dominio/notas", response_class=HTMLResponse)
def notas(request: Request, codi_emp: str = Form(""), competencia: str = Form(""),
          db_session: Session = Depends(get_db), profile=Depends(get_profile),
          user_email: str = Depends(get_current_user_email)):
    assert_internal(profile)
    try:
        e = _empresa(db_session, profile, codi_emp)
        mes = _competencia(competencia)
    except _Recusa as exc:
        return _aviso(request, str(exc), "aviso")
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        return _aviso(request, f"Não consegui ler as empresas do Domínio: {exc}")
    pasta = _nova_pasta({"tipo": "notas", "codi_emp": e.codi_emp,
                         "nome": f"notas_dominio_{e.codi_emp}_{mes.replace('-', '')}.xlsx"})
    try:
        r = programa.notas(e.codi_emp, mes, pasta / PLANILHA)
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        shutil.rmtree(pasta, ignore_errors=True)
        return _aviso(request, f"Não deu para ler as notas: {exc}")
    logger.info("Domínio: notas da empresa %s em %s lidas por %s (%s linhas)", e.codi_emp, mes, user_email,
                r["notas"]["total"])
    return templates.TemplateResponse(request, "partials/dominio/notas.html", {
        "request": request, "r": r, "empresa": e, "rotulo_competencia": comp.rotulo(mes), "dinheiro": DINHEIRO,
        "rotulos": ROTULOS, "ocultas_notas": _colunas_sem_informacao(r["notas"]),
        "processo": pasta.name if r.get("excel") else None})


@router.get("/api/dominio/lista", response_class=HTMLResponse)
def lista(request: Request, busca: str = "", so_presumidas: str = "", db_session: Session = Depends(get_db),
          profile=Depends(get_profile)):
    assert_internal(profile)
    try:
        todas = empresas.do_usuario(db_session, profile)
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        return _aviso(request, f"Não consegui ler as empresas do Domínio: {exc}")
    visiveis = [e for e in todas if e.presumido] if so_presumidas else todas
    termo = busca.strip().upper()
    if termo:
        # Poucos dígitos são código de empresa, não pedaço de CNPJ: "30" casaria com todo CNPJ que tem "30" no meio.
        digitos = empresas.so_digitos(termo)
        visiveis = [e for e in visiveis if termo in e.nome.upper() or str(e.codi_emp) == termo
                    or (len(digitos) >= 5 and digitos in e.cnpj)]
    return templates.TemplateResponse(request, "partials/dominio/lista.html", {
        "request": request, "empresas": visiveis, "total": len(todas), "admin": profile.role == "admin"})


# ------------------------------------------------------------------------------------------------ consulta livre
def _exigir_sql(profile) -> None:
    if not pode_sql_dominio(profile):
        raise _Recusa("A consulta livre (SQL) do Domínio é do admin, ou de operador com a permissão marcada na "
                      "edição do usuário.")


@router.get("/api/dominio/sql", response_class=HTMLResponse)
def sql_tela(request: Request, profile=Depends(get_profile)):
    assert_internal(profile)
    try:
        _exigir_sql(profile)
        prontas = programa.consultas_prontas()
    except _Recusa as exc:
        return _aviso(request, str(exc), "aviso")
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        return _aviso(request, f"Não consegui ler as consultas prontas: {exc}")
    return templates.TemplateResponse(request, "partials/dominio/sql.html", {
        "request": request, "prontas": prontas, "limite": LIMITE_SQL})


@router.post("/api/dominio/sql/executar", response_class=HTMLResponse)
def sql_executar(request: Request, sql: str = Form(""), codi_emp: str = Form(""), competencia: str = Form(""),
                 db_session: Session = Depends(get_db), profile=Depends(get_profile),
                 user_email: str = Depends(get_current_user_email)):
    assert_internal(profile)
    try:
        _exigir_sql(profile)
        if not sql.strip():
            raise _Recusa("Escreva a consulta.")
        if len(sql) > LIMITE_SQL:
            raise _Recusa(f"Consulta grande demais ({len(sql)} caracteres; o limite é {LIMITE_SQL}).")
        mes = _competencia(competencia)
        codi = _empresa(db_session, profile, codi_emp).codi_emp if codi_emp.strip() else None
    except _Recusa as exc:
        return _aviso(request, str(exc), "aviso")
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        return _aviso(request, f"Não consegui ler as empresas do Domínio: {exc}")
    pasta = _nova_pasta({"tipo": "consulta", "usuario": str(profile.id), "nome": "consulta_dominio.xlsx"})
    arquivo = pasta / "consulta.sql"
    arquivo.write_text(sql, encoding="utf-8")
    # A trilha de quem consultou o quê: a consulta pode ler qualquer empresa do Domínio.
    logger.info("Domínio: consulta livre por %s (empresa=%s, competencia=%s): %s", user_email, codi, mes,
                " ".join(sql.split())[:300])
    try:
        r = programa.consulta(arquivo, codi, mes, pasta / PLANILHA)
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        shutil.rmtree(pasta, ignore_errors=True)
        return _aviso(request, f"Erro na consulta: {exc}")
    return templates.TemplateResponse(request, "partials/dominio/sql_resultado.html", {
        "request": request, "t": r["resultado"], "processo": pasta.name if r.get("excel") else None})


@router.get("/api/dominio/sql/tabelas", response_class=HTMLResponse)
def sql_tabelas(request: Request, esquema: str = "bethadba", filtro: str = "", profile=Depends(get_profile)):
    assert_internal(profile)
    try:
        _exigir_sql(profile)
        t = programa.tabelas(esquema.strip() or "bethadba", filtro.strip())
    except _Recusa as exc:
        return _aviso(request, str(exc), "aviso")
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        return _aviso(request, str(exc))
    return templates.TemplateResponse(request, "partials/dominio/tabelas.html", {
        "request": request, "t": t, "esquema": esquema.strip() or "bethadba"})


@router.get("/api/dominio/sql/colunas", response_class=HTMLResponse)
def sql_colunas(request: Request, tabela: str = "", esquema: str = "bethadba", profile=Depends(get_profile)):
    assert_internal(profile)
    try:
        _exigir_sql(profile)
        if not tabela.strip():
            raise _Recusa("Escolha a tabela.")
        t = programa.colunas(tabela.strip(), esquema.strip() or "bethadba")
    except _Recusa as exc:
        return _aviso(request, str(exc), "aviso")
    except (ProgramaIndisponivel, FalhaDoPrograma) as exc:
        return _aviso(request, str(exc))
    return templates.TemplateResponse(request, "partials/dominio/tabela.html", {
        "request": request, "t": t, "titulo": f"Colunas de {tabela.strip()}", "dinheiro": set()})


# ------------------------------------------------------------------------------------------------ planilhas
@router.get("/api/dominio/arquivo/{processo}")
def baixar(processo: str, db_session: Session = Depends(get_db), profile=Depends(get_profile)):
    assert_internal(profile)
    # O identificador entra num caminho: conferir que é UUID é o que impede `..\..\` de virar leitura do servidor.
    try:
        uuid.UUID(processo)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    pasta = Path(TEMP_DIR) / processo
    try:
        meta = json.loads((pasta / META).read_text(encoding="utf-8"))
        conteudo = (pasta / PLANILHA).read_bytes()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="A planilha não está mais no servidor. Busque de novo.")
    if meta.get("tipo") == "notas":
        try:
            _empresa(db_session, profile, meta.get("codi_emp"))
        except (_Recusa, ProgramaIndisponivel, FalhaDoPrograma):
            raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    elif meta.get("tipo") == "consulta":
        if meta.get("usuario") != str(profile.id) or not pode_sql_dominio(profile):
            raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    else:
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")
    nome = meta.get("nome") or "dominio.xlsx"
    return StreamingResponse(iter([conteudo]), media_type="application/octet-stream",
                             headers={**_cabecalhos_download(nome), "Content-Length": str(len(conteudo))})
