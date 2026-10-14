"""Gerar SPED a partir dos XMLs — a tela.

Sobe o ZIP do mês, sai o arquivo no leiaute da EFD para importar no Domínio.
É a ferramenta para o cliente que NÃO escritura: sem ela, alguém digita nota
por nota.

Três decisões desta rota:

- **O arquivo não é devolvido direto no POST.** Primeiro vem a tela de
  resultado, com o que foi lido, o que ficou de fora do pacote e tudo que o
  gerador precisou presumir; o download é o passo seguinte. Entregar o arquivo
  antes de a pessoa ler o que foi presumido é o mesmo erro de gerar SPED
  corrigido sem prévia.
- **O CNPJ vem do cadastro da empresa, nunca do ZIP.** É ele que separa entrada
  de saída e denuncia XML de outro cliente no meio do pacote. Deixar o gerador
  "descobrir" a empresa pelo arquivo mais frequente escrituraria o pacote
  errado sem ninguém perceber.
- **O CFOP de entrada só sai de regra APROVADA do gerador, e sem ela não há
  arquivo.** Pedido do Sandro em 09/10/2026. Antes de gerar, a tela lista as
  combinações do pacote — item de NF-e (fornecedor × CFOP × cProd) e frete de
  CT-e (CFOP × UF do transportador) — e o botão de gerar só destrava com todas
  aprovadas. Quem grava segue `rule_status_for`, como em todo o sistema: o
  operador manda para a Curadoria, o admin aprova. As regras moram em
  `gerador_cfop_regras` e `gerador_cte_cfop_regras`, SEPARADAS das da
  auditoria: esta rota não lê nem grava `cfop_classifications` (até 09/10
  gravava, e a regra feita para o arquivo de um cliente mudava o que a
  auditoria acusava). O CFOP que chega no POST de gerar é IGNORADO — o arquivo
  usa só o que está aprovado no banco; senão a trava seria só visual.
"""
import json
import logging
import os
import shutil
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.orm import Session

from ...core.authz import (assert_internal, get_profile,
                           resolve_tenant_for_write, rule_status_for)
from ...core.templating import build_templates
from ...core.uploads_seguros import (LIMITE_ZIP_BYTES, extrair_xmls_do_pacote,
                                     gravar_com_limite)
from ...domain.constants.cfops_entrada import CFOPS_ENTRADA, rotulo
from ...domain.models.auth_models import Tenant
from ...domain.models.rules import GeradorCfopRegra, GeradorCteCfopRegra
from ...infrastructure.database.session import get_db
from ...services.sped_builder import (Cadastro, cfop_de_entrada_valido,
                                       chave_frete, chave_item, deduzir_cfop,
                                       empresa_industrial, gerar_de_pasta,
                                       levantar_combinacoes_da_pasta, rotulo_mes)
from ...services.sped_rewriter.documento import ENCODING
from .uploads import get_current_user_email

logger = logging.getLogger(__name__)
router = APIRouter()
templates = build_templates()

TEMP_DIR = os.path.join(os.getcwd(), "temp_uploads")
NOME_ARQUIVO = "sped_gerado.txt"
# Guardado junto dos XMLs enquanto o pacote espera o mapeamento e a aprovação
# (e, depois de gerado, quando tem mais de um mês): a empresa e as escolhas do
# envio, para gerar sem reenviar o ZIP.
META_PACOTE = "pacote.json"
# As combinações do pacote, levantadas uma vez só: a tela de mapeamento é
# redesenhada a cada gravação e a cada "Atualizar", e reler centenas de XMLs
# para isso seria o gargalo da tela.
COMBINACOES = "combinacoes.json"

APROVADO = "aprovado"
# A ordem da tela: o que falta decidir primeiro, o que já está aprovado por último.
ORDEM_STATUS = {"a_definir": 0, "pendente": 1, "aprovada": 2}

# Aquisição de serviço de transporte — as opções da seção de fretes. Ficam aqui,
# e não em `CFOPS_ENTRADA`, porque aquela tabela é a da tela da auditoria.
CFOPS_FRETE = {
    "1351": "Aquisição de transporte para serviço da mesma natureza",
    "1352": "Aquisição de transporte por estabelecimento industrial",
    "1353": "Aquisição de transporte por estabelecimento comercial",
    "1354": "Aquisição de transporte por prestador de serviço de comunicação",
    "1355": "Aquisição de transporte por geradora ou distribuidora de energia",
    "1356": "Aquisição de transporte por estabelecimento de produtor rural",
    "1360": "Aquisição de transporte por contribuinte substituto do transporte",
    "1932": "Aquisição de transporte iniciado em UF diversa da do prestador",
    "2351": "Aquisição de transporte para serviço da mesma natureza (interestadual)",
    "2352": "Aquisição de transporte por estabelecimento industrial (interestadual)",
    "2353": "Aquisição de transporte por estabelecimento comercial (interestadual)",
    "2354": "Aquisição de transporte por prestador de comunicação (interestadual)",
    "2355": "Aquisição de transporte por geradora ou distribuidora de energia (interestadual)",
    "2356": "Aquisição de transporte por produtor rural (interestadual)",
    # Não há 2360: a tabela oficial só tem o 1360, e a opção que existia aqui
    # seria recusada na gravação — `cfop_de_entrada_valido` confere a tabela.
    "2932": "Aquisição de transporte iniciado em UF diversa da do prestador (interestadual)",
}


class _PacoteIndisponivel(Exception):
    """O pacote saiu do servidor, ou a empresa dele perdeu o CNPJ. A mensagem
    é a que vai para a tela."""


@dataclass
class _Pacote:
    id: str
    pasta: str
    meta: dict
    empresa: Tenant

    @property
    def xml_dir(self) -> str:
        return os.path.join(self.pasta, "xmls")

    def cadastro(self) -> Cadastro:
        return _cadastro(self.empresa, self.meta.get("perfil", "A"),
                         self.meta.get("ind_ativ", "1"))


def _so_digitos(texto) -> str:
    return "".join(c for c in str(texto or "") if c.isdigit())


def _empresas(db_session: Session, profile) -> list:
    if profile.role == "admin":
        return db_session.query(Tenant).order_by(Tenant.name).all()
    return sorted((t for t in (profile.allowed_tenants or []) if t.ativo),
                  key=lambda t: t.name)


def _cadastro(empresa, perfil: str, ind_ativ: str) -> Cadastro:
    return Cadastro(cnpj=empresa.cnpj, nome=empresa.name, uf=empresa.uf or "",
                    perfil=perfil, ind_ativ=ind_ativ)


@router.get("/gerar-sped", response_class=HTMLResponse)
def tela(request: Request, db_session: Session = Depends(get_db),
         profile=Depends(get_profile)):
    assert_internal(profile)
    return templates.TemplateResponse(request, "pages/gerar_sped.html", {
        "request": request, "tenants": _empresas(db_session, profile),
        "user_profile": profile,
    })


def _competencia_do_form(texto: str) -> str:
    """ "2026-09" (o <input type="month">) ou "09/2026" -> "2026-09"; vazio fica
    vazio; o que não for mês devolve o próprio texto, e o gerador recusa com o
    motivo."""
    bruto = (texto or "").strip()
    if len(bruto) == 7 and bruto[2] == "/":
        return f"{bruto[3:]}-{bruto[:2]}"
    return bruto


def _resposta(request: Request, resultado, empresa, processo: str | None,
              pacote: str | None):
    """A tela de resultado. `pacote` só vem quando o ZIP tem mais de um mês:
    é por ele que a tela gera de novo, com as notas anteriores marcadas, sem
    reenviar o arquivo."""
    contexto = {"request": request, "resultado": resultado, "empresa": empresa,
                "pacote": pacote, "rotulo_mes": rotulo_mes}
    if resultado.documento is None:
        contexto["erro"] = (resultado.avisos[0] if resultado.avisos
                            else "Nada a gerar com este pacote.")
    else:
        contexto.update(processo=processo, linhas=len(resultado.documento.registros))
    return templates.TemplateResponse(request, "partials/gerar_sped_resultado.html",
                                      contexto)


def _gravar_txt(resultado, tenant_id: str) -> str:
    """Grava o TXT gerado numa pasta própria e devolve o identificador do
    download. Pasta própria por geração: gerar agosto e depois setembro do
    mesmo pacote não pode fazer o link de um baixar o outro."""
    processo = str(uuid.uuid4())
    pasta = os.path.join(TEMP_DIR, processo)
    os.makedirs(pasta, exist_ok=True)
    with open(os.path.join(pasta, NOME_ARQUIVO), "w", encoding=ENCODING, newline="") as f:
        f.write(resultado.documento.serializar())
    logger.info("SPED gerado dos XMLs tenant=%s processo=%s competencia=%s notas=%d "
                "itens=%d fretes=%d", tenant_id, processo, resultado.competencia,
                resultado.notas, resultado.itens, resultado.fretes)
    return processo


def _erro_gerar(request: Request, msg: str):
    return templates.TemplateResponse(
        request, "partials/gerar_sped_resultado.html",
        {"request": request, "erro": msg})


def _cfop_opcoes() -> list:
    """(codigo, rotulo) dos CFOPs de entrada — o datalist da seção de itens.
    Um só, compartilhado por todas as linhas."""
    return [(c, rotulo(c)) for c in sorted(CFOPS_ENTRADA)]


def _cfop_opcoes_frete() -> list:
    return [(c, f"{c} — {d}") for c, d in sorted(CFOPS_FRETE.items())]


# ------------------------------------------------------------------ o pacote

def _gravar_meta(pasta: str, meta: dict) -> None:
    # Regravar também renova a idade da pasta: a limpeza de temp_uploads apaga
    # o que está parado há 7 dias, e pacote esperando aprovação está em uso.
    with open(os.path.join(pasta, META_PACOTE), "w", encoding="utf-8") as f:
        json.dump(meta, f)


def _gravar_combinacoes(pasta: str, combinacoes: dict) -> None:
    with open(os.path.join(pasta, COMBINACOES), "w", encoding="utf-8") as f:
        json.dump({"nfe": combinacoes.get("nfe", []),
                   "cte": combinacoes.get("cte", []),
                   "ignorados": [list(i) for i in combinacoes.get("ignorados", [])]},
                  f)


def _abrir_pacote(db_session: Session, profile, pacote_id: str) -> _Pacote:
    """O pacote guardado, com a permissão conferida de novo.

    O identificador não é credencial: a empresa vem do META, gravado no envio,
    e quem não pode escrever nela não mexe no pacote — nem para ler o
    mapeamento. O formato é conferido antes de virar caminho, para `..\\..\\`
    nunca virar leitura de arquivo do servidor.
    """
    try:
        uuid.UUID(pacote_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="Pacote não encontrado.")
    pasta = os.path.join(TEMP_DIR, pacote_id)
    try:
        with open(os.path.join(pasta, META_PACOTE), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = None
    if not meta or not os.path.isdir(os.path.join(pasta, "xmls")):
        raise _PacoteIndisponivel("Este pacote não está mais no servidor (fica "
                                  "7 dias). Envie o ZIP de novo.")

    tenant_id = resolve_tenant_for_write(profile, meta.get("tenant_id", ""))
    empresa = db_session.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not empresa or not empresa.cnpj:
        raise _PacoteIndisponivel("A empresa deste pacote não tem mais CNPJ no cadastro.")
    return _Pacote(id=pacote_id, pasta=pasta, meta=meta, empresa=empresa)


def _combinacoes(pacote: _Pacote) -> dict:
    try:
        with open(os.path.join(pacote.pasta, COMBINACOES), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        pass
    # Pacote guardado antes de existir o arquivo (a tela de 08/10/2026 só
    # gravava o META): levanta de novo, uma vez.
    combinacoes = levantar_combinacoes_da_pasta(pacote.xml_dir, pacote.cadastro())
    _gravar_combinacoes(pacote.pasta, combinacoes)
    return combinacoes


# ------------------------------------------------------------------ as regras

def _regras(db_session: Session, empresa) -> tuple[dict, dict]:
    """As regras do GERADOR desta empresa — aprovadas e pendentes —, pela mesma
    chave que o gerador consulta. Nunca as da auditoria."""
    tenant_id, cnpj = str(empresa.id), _so_digitos(empresa.cnpj)
    itens = (db_session.query(GeradorCfopRegra)
             .filter(GeradorCfopRegra.tenant_id == tenant_id,
                     GeradorCfopRegra.company_cnpj == cnpj).all())
    fretes = (db_session.query(GeradorCteCfopRegra)
              .filter(GeradorCteCfopRegra.tenant_id == tenant_id,
                      GeradorCteCfopRegra.company_cnpj == cnpj).all())
    return ({chave_item(r.supplier_cnpj, r.supplier_cfop, r.product_code): r
             for r in itens},
            {chave_frete(r.supplier_cfop, r.emitter_uf): r for r in fretes})


def _aprovadas(regras: dict) -> dict:
    """{chave: CFOP} só do que um admin aprovou — o único insumo do arquivo.
    Regra `pendente_revisao` é sugestão que ninguém revisou."""
    return {k: r.entry_cfop for k, r in regras.items()
            if r.status == APROVADO and cfop_de_entrada_valido(r.entry_cfop)}


def _situacao(regra, sugerido: str) -> tuple[str, str, str]:
    """(status, CFOP mostrado, quem gravou) de uma combinação."""
    if regra is None:
        return "a_definir", sugerido, ""
    if regra.status == APROVADO and cfop_de_entrada_valido(regra.entry_cfop):
        return "aprovada", regra.entry_cfop, regra.created_by or ""
    if regra.status == APROVADO:
        # Aprovada com CFOP que não serve para entrada (editada fora daqui):
        # não vale para o arquivo e volta para quem mapeia, com o valor à vista.
        return "a_definir", regra.entry_cfop or sugerido, regra.created_by or ""
    return "pendente", regra.entry_cfop, regra.created_by or ""


def _linhas(combinacoes: dict, regras_itens: dict, regras_fretes: dict,
            industrial: bool = False) -> tuple:
    """As linhas da tela. A sugestão é refeita aqui a partir do CFOP de origem,
    e não lida do `combinacoes.json`: o pacote guardado antes de 14/10/2026
    trazia "1405" para o 5405, e a tela continuaria oferecendo um CFOP que a
    gravação recusa."""
    nfe, cte = [], []
    for c in combinacoes.get("nfe", []):
        k = chave_item(c["fornecedor_cnpj"], c["cfop_origem"], c["produto_cod"])
        sugerido, como = deduzir_cfop(c["cfop_origem"])
        status, cfop, autor = _situacao(regras_itens.get(k), sugerido)
        nfe.append({**c, "chave": "|".join(k), "status": status,
                    "cfop_entrada": cfop, "autor": autor,
                    "cfop_sugerido": sugerido, "sugestao_como": como})
    for c in combinacoes.get("cte", []):
        k = chave_frete(c["cfop_origem"], c["uf"])
        sugerido, como = deduzir_cfop(c["cfop_origem"], industrial)
        status, cfop, autor = _situacao(regras_fretes.get(k), sugerido)
        cte.append({**c, "chave": "|".join(k), "status": status,
                    "cfop_entrada": cfop, "autor": autor,
                    "cfop_sugerido": sugerido, "sugestao_como": como})
    nfe.sort(key=lambda l: (ORDEM_STATUS[l["status"]], l["fornecedor_nome"],
                            l["cfop_origem"], l["produto_cod"]))
    cte.sort(key=lambda l: (ORDEM_STATUS[l["status"]], l["cfop_origem"], l["uf"]))
    return nfe, cte


def _resumo(linhas: list) -> dict:
    resumo = {"total": len(linhas), "aprovada": 0, "pendente": 0, "a_definir": 0}
    for linha in linhas:
        resumo[linha["status"]] += 1
    return resumo


def _tela_mapa(request: Request, profile, pacote: _Pacote, combinacoes: dict,
               regras_itens: dict, regras_fretes: dict, mensagem: str = "",
               tipo: str = "ok"):
    nfe, cte = _linhas(combinacoes, regras_itens, regras_fretes,
                       empresa_industrial(pacote.cadastro()))
    resumo_nfe, resumo_cte = _resumo(nfe), _resumo(cte)
    faltam = (resumo_nfe["total"] - resumo_nfe["aprovada"]
              + resumo_cte["total"] - resumo_cte["aprovada"])
    return templates.TemplateResponse(request, "partials/gerar_sped_mapa_cfop.html", {
        "request": request, "empresa": pacote.empresa, "pacote_id": pacote.id,
        "competencia": pacote.meta.get("competencia", ""), "rotulo_mes": rotulo_mes,
        "linhas_nfe": nfe, "linhas_cte": cte,
        "resumo_nfe": resumo_nfe, "resumo_cte": resumo_cte,
        "itens": sum(l.get("qtd_itens", 0) for l in nfe),
        "ctes": sum(l.get("qtd_ctes", 0) for l in cte),
        "faltam": faltam, "pode_gerar": faltam == 0,
        "pode_aprovar": profile.role == "admin",
        "cfop_opcoes": _cfop_opcoes(), "cfop_opcoes_frete": _cfop_opcoes_frete(),
        "ignorados": combinacoes.get("ignorados", []),
        "mensagem": mensagem, "mensagem_tipo": tipo,
    })


def _falha_regras(request: Request, db_session: Session):
    # Sem as regras não há como saber o que está aprovado: falha fechada e à
    # vista, nunca "tudo a definir" em silêncio nem arquivo com presunção.
    db_session.rollback()
    logger.exception("Não foi possível ler as regras de CFOP do gerador")
    return _erro_gerar(request, "Não foi possível ler as regras de CFOP do gerador "
                       "agora. Tente de novo em instantes.")


def _responder_mapa(request: Request, db_session: Session, profile,
                    pacote: _Pacote, mensagem: str = "", tipo: str = "ok"):
    try:
        regras_itens, regras_fretes = _regras(db_session, pacote.empresa)
    except Exception:                                     # noqa: BLE001
        return _falha_regras(request, db_session)
    return _tela_mapa(request, profile, pacote, _combinacoes(pacote),
                      regras_itens, regras_fretes, mensagem, tipo)


# ------------------------------------------------------------------ gerar

def _gerar_e_responder(request: Request, db_session: Session, profile,
                       pacote: _Pacote, regras_itens: dict, regras_fretes: dict,
                       competencia: str | None = None, trazer=(),
                       data_entrada: str = ""):
    """Gera com as regras APROVADAS e o modo estrito do gerador, trata o
    pacote de vários meses (mantém a pasta; com um mês só, limpa) e devolve a
    tela de resultado."""
    alvo = _competencia_do_form(pacote.meta.get("competencia", "")
                                if competencia is None else competencia)
    try:
        resultado = gerar_de_pasta(pacote.xml_dir, pacote.cadastro(), regras_itens,
                                   alvo, trazer=trazer, data_entrada=data_entrada,
                                   regras_cte=regras_fretes, exigir_regra=True)
    except Exception:                                     # noqa: BLE001
        logger.exception("Falha ao gerar SPED do pacote %s tenant=%s",
                         pacote.id, pacote.empresa.id)
        return _erro_gerar(request, "Não foi possível gerar o arquivo. Confira "
                           "se o ZIP tem XMLs de NF-e e tente de novo.")

    if resultado.sem_regra:
        # A regra mudou entre a tela e o clique (descartada na Curadoria, por
        # exemplo): o pacote fica, e a pessoa volta ao mapeamento.
        return _responder_mapa(request, db_session, profile, pacote,
                               resultado.avisos[-1], "erro")

    varios_meses = len(resultado.meses) > 1
    if not varios_meses:
        # Com um mês só, os XMLs vão embora como sempre — são o volume, e
        # documento de cliente não fica parado sem motivo. Com vários, FICAM
        # (os mesmos 7 dias do TXT) para gerar o outro mês com um clique.
        shutil.rmtree(pacote.pasta, ignore_errors=True)

    processo = (_gravar_txt(resultado, str(pacote.empresa.id))
                if resultado.documento is not None else None)
    return _resposta(request, resultado, pacote.empresa, processo,
                     pacote.id if varios_meses else None)


@router.post("/api/gerar-sped", response_class=HTMLResponse)
@router.post("/api/gerar-sped/analisar", response_class=HTMLResponse)
async def analisar(request: Request,
                   xmls_zip: UploadFile = File(None),
                   tenant_id: str = Form(...),
                   perfil: str = Form("A"),
                   ind_ativ: str = Form("1"),
                   competencia: str = Form(""),
                   usar_xmls_email: bool = Form(False),
                   db_session: Session = Depends(get_db),
                   profile=Depends(get_profile),
                   user_email: str = Depends(get_current_user_email)):
    """Passo 1: guarda o pacote e devolve a tela de mapeamento, ANTES de gerar.

    `/api/gerar-sped` — a rota antiga, que gerava direto — cai aqui também:
    gerar sem passar pelo mapeamento era a porta que escriturava CFOP que
    ninguém aprovou.
    """
    assert_internal(profile)
    tenant_id = resolve_tenant_for_write(profile, tenant_id)
    empresa = db_session.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not empresa or not empresa.cnpj:
        return _erro_gerar(request, "A empresa selecionada não tem CNPJ no "
                           "cadastro, e é ele que separa as notas de entrada "
                           "dela das demais do pacote.")

    # Input de arquivo vazio chega como UploadFile sem nome — não é pacote.
    if xmls_zip is not None and not (xmls_zip.filename or "").strip():
        xmls_zip = None
    if xmls_zip is None and not usar_xmls_email:
        return _erro_gerar(request, "Envie o ZIP de XMLs do mês ou marque para "
                           "usar os XMLs recebidos por e-mail.")

    pacote_id = str(uuid.uuid4())
    pasta = os.path.join(TEMP_DIR, pacote_id)
    os.makedirs(os.path.join(pasta, "xmls"), exist_ok=True)
    meta = {"tenant_id": str(tenant_id), "perfil": perfil, "ind_ativ": ind_ativ,
            "competencia": _competencia_do_form(competencia),
            "enviado_por": user_email}
    pacote = _Pacote(id=pacote_id, pasta=pasta, meta=meta, empresa=empresa)
    arquivo = os.path.join(pasta, "xmls.pacote")
    try:
        if xmls_zip is not None:
            await gravar_com_limite(xmls_zip, arquivo, LIMITE_ZIP_BYTES,
                                    "O pacote de XMLs")
            try:
                extrair_xmls_do_pacote(arquivo, pacote.xml_dir)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400,
                                    detail="O arquivo enviado não é um ZIP válido.")
            finally:
                try:
                    os.remove(arquivo)
                except OSError:
                    pass
        if usar_xmls_email:
            # TODOS os guardados desta empresa: o gerador já separa os meses,
            # escolhe o mais recente e oferece os anteriores para a 1ª
            # quinzena — filtrar aqui duplicaria essa regra.
            from ...services.email_recebimento import recebidos as xmls_recebidos
            copiados = xmls_recebidos.copiar_todos(db_session, tenant_id, pacote.xml_dir)
            if not copiados and xmls_zip is None:
                shutil.rmtree(pasta, ignore_errors=True)
                return _erro_gerar(request, "Não há XMLs recebidos por e-mail "
                                   "guardados para esta empresa. Envie o ZIP de "
                                   "XMLs do mês.")
        combinacoes = levantar_combinacoes_da_pasta(pacote.xml_dir, pacote.cadastro())
    except HTTPException:
        shutil.rmtree(pasta, ignore_errors=True)
        raise
    except Exception:                                     # noqa: BLE001
        shutil.rmtree(pasta, ignore_errors=True)
        logger.exception("Falha ao ler os XMLs para o mapeamento tenant=%s", tenant_id)
        return _erro_gerar(request, "Não foi possível ler os XMLs. Confira se o "
                           "ZIP tem XMLs de NF-e ou CT-e e tente de novo.")

    if not combinacoes["nfe"] and not combinacoes["cte"]:
        # Nenhum item nem frete de entrada desta empresa: não há o que mapear,
        # e o gerador diz por quê (saídas, outra empresa, XML ilegível) na
        # tela de resultado de sempre.
        return _gerar_e_responder(request, db_session, profile, pacote, {}, {})

    _gravar_meta(pasta, meta)
    _gravar_combinacoes(pasta, combinacoes)
    return _responder_mapa(request, db_session, profile, pacote)


@router.get("/api/gerar-sped/{pacote_id}/mapa", response_class=HTMLResponse)
def mapa(pacote_id: str, request: Request,
         db_session: Session = Depends(get_db), profile=Depends(get_profile)):
    """A tela de mapeamento de novo — o "Atualizar" depois que o admin aprova
    na Curadoria, sem reenviar o ZIP."""
    assert_internal(profile)
    try:
        pacote = _abrir_pacote(db_session, profile, pacote_id)
    except _PacoteIndisponivel as e:
        return _erro_gerar(request, str(e))
    return _responder_mapa(request, db_session, profile, pacote)


def _atualizar(regra, cfop: str, status: str, admin: bool, autor: str) -> str:
    """Aplica o CFOP da tela a uma regra que já existe. Devolve o que houve."""
    if (regra.status == APROVADO and cfop_de_entrada_valido(regra.entry_cfop)
            and not admin):
        # Só o admin muda regra aprovada. Gravar por cima rebaixaria para
        # pendente — e travaria a geração — uma regra que ninguém revisou de
        # novo. A tela nem manda essas linhas; isto é para o POST montado.
        return "igual" if regra.entry_cfop == cfop else "bloqueada"
    if regra.entry_cfop == cfop and regra.status == status:
        return "igual"
    regra.entry_cfop = cfop
    regra.status = status
    regra.created_by = autor
    return "alterada"


def _plural(n: int, um: str, varios: str) -> str:
    return f"{n} {um if n == 1 else varios}"


def _mensagem_gravacao(conta: Counter, invalidos: list, admin: bool) -> tuple[str, str]:
    """O que a gravação fez, em português — e nunca "sucesso" com zero."""
    gravadas = conta["nova"] + conta["alterada"]
    partes = []
    if gravadas:
        um = gravadas == 1
        if admin:
            fim = " e aprovada." if um else " e aprovadas."
        else:
            fim = ((" como pendente — vai" if um else " como pendentes — vão")
                   + " para o admin aprovar na Curadoria (abas Gerador · NF-e "
                     "e Gerador · CT-e).")
        partes.append(_plural(gravadas, "regra gravada", "regras gravadas") + fim)
    if conta["bloqueada"]:
        partes.append(_plural(conta["bloqueada"], "regra aprovada não foi alterada",
                              "regras aprovadas não foram alteradas")
                      + ": só o admin muda regra aprovada.")
    if invalidos:
        partes.append(_plural(len(invalidos), "CFOP não serve", "CFOPs não servem")
                      + " para entrada e não " + ("foi" if len(invalidos) == 1 else "foram")
                      + " gravado" + ("" if len(invalidos) == 1 else "s")
                      + f" ({', '.join(sorted(set(invalidos))[:5])}): o CFOP de "
                        "entrada tem 4 dígitos e começa com 1, 2 ou 3.")
    if conta["vazia"]:
        partes.append(_plural(conta["vazia"], "linha sem CFOP ficou", "linhas sem CFOP ficaram")
                      + " de fora.")
    if conta["desconhecida"]:
        partes.append(_plural(conta["desconhecida"], "linha não é", "linhas não são")
                      + " deste pacote e " + ("foi ignorada." if conta["desconhecida"] == 1
                                              else "foram ignoradas."))
    if not gravadas:
        motivo = " ".join(partes) or "os CFOPs da tela já eram os gravados."
        return "Nada foi gravado: " + motivo, "aviso"
    return " ".join(partes), ("ok" if len(partes) == 1 else "aviso")


@router.post("/api/gerar-sped/{pacote_id}/regras", response_class=HTMLResponse)
def gravar_regras(pacote_id: str, request: Request,
                  chave_nfe: list[str] = Form([]),
                  cfop_nfe: list[str] = Form([]),
                  chave_cte: list[str] = Form([]),
                  cfop_cte: list[str] = Form([]),
                  db_session: Session = Depends(get_db),
                  profile=Depends(get_profile),
                  user_email: str = Depends(get_current_user_email)):
    """Grava o CFOP de cada combinação como regra DO GERADOR desta empresa.

    Maker-checker de sempre: o admin grava aprovado, os demais pendente. Só
    entram combinações deste pacote — a chave vem da tela e é conferida contra
    o que os XMLs têm, senão o POST viraria um cadastro de regra para qualquer
    fornecedor.
    """
    assert_internal(profile)
    try:
        pacote = _abrir_pacote(db_session, profile, pacote_id)
    except _PacoteIndisponivel as e:
        return _erro_gerar(request, str(e))
    try:
        regras_itens, regras_fretes = _regras(db_session, pacote.empresa)
    except Exception:                                     # noqa: BLE001
        return _falha_regras(request, db_session)
    combinacoes = _combinacoes(pacote)

    if len(chave_nfe) != len(cfop_nfe) or len(chave_cte) != len(cfop_cte):
        return _tela_mapa(request, profile, pacote, combinacoes, regras_itens,
                          regras_fretes, "O formulário chegou incompleto (chave "
                          "sem CFOP). Nada foi gravado — clique em Atualizar e "
                          "tente de novo.", "erro")

    admin = profile.role == "admin"
    status = rule_status_for(profile)
    tenant_id, cnpj = str(pacote.empresa.id), _so_digitos(pacote.empresa.cnpj)
    itens_do_pacote = {c["chave"]: c for c in combinacoes.get("nfe", [])}
    fretes_do_pacote = {c["chave"]: c for c in combinacoes.get("cte", [])}
    conta, invalidos = Counter(), []

    def _conferir(combo, cfop: str) -> str | None:
        if combo is None:
            conta["desconhecida"] += 1
            return None
        cfop = (cfop or "").strip()
        if not cfop:
            conta["vazia"] += 1
            return None
        if not cfop_de_entrada_valido(cfop):
            invalidos.append(cfop)
            return None
        return cfop

    for chave, cfop in zip(chave_nfe, cfop_nfe):
        combo = itens_do_pacote.get(chave)
        cfop = _conferir(combo, cfop)
        if cfop is None:
            continue
        k = chave_item(combo["fornecedor_cnpj"], combo["cfop_origem"], combo["produto_cod"])
        regra = regras_itens.get(k)
        if regra is None:
            regras_itens[k] = GeradorCfopRegra(
                tenant_id=tenant_id, company_cnpj=cnpj, supplier_cnpj=k[0],
                supplier_cfop=k[1], product_code=k[2],
                supplier_name=combo.get("fornecedor_nome") or None,
                product_description=combo.get("produto_desc") or None,
                entry_cfop=cfop, status=status, created_by=user_email)
            db_session.add(regras_itens[k])
            conta["nova"] += 1
        else:
            conta[_atualizar(regra, cfop, status, admin, user_email)] += 1

    for chave, cfop in zip(chave_cte, cfop_cte):
        combo = fretes_do_pacote.get(chave)
        cfop = _conferir(combo, cfop)
        if cfop is None:
            continue
        k = chave_frete(combo["cfop_origem"], combo["uf"])
        regra = regras_fretes.get(k)
        if regra is None:
            regras_fretes[k] = GeradorCteCfopRegra(
                tenant_id=tenant_id, company_cnpj=cnpj, supplier_cfop=k[0],
                emitter_uf=k[1],
                carrier_names=", ".join(combo.get("transportadoras", [])[:3]) or None,
                entry_cfop=cfop, status=status, created_by=user_email)
            db_session.add(regras_fretes[k])
            conta["nova"] += 1
        else:
            conta[_atualizar(regra, cfop, status, admin, user_email)] += 1

    if conta["nova"] or conta["alterada"]:
        try:
            db_session.commit()
        except Exception:                                 # noqa: BLE001
            db_session.rollback()
            logger.exception("Falha ao gravar regras do gerador tenant=%s pacote=%s",
                             tenant_id, pacote.id)
            return _responder_mapa(request, db_session, profile, pacote,
                                   "Não foi possível gravar as regras (outra pessoa "
                                   "pode ter gravado as mesmas ao mesmo tempo). "
                                   "Clique em Atualizar e tente de novo.", "erro")
        logger.info("Regras do gerador gravadas tenant=%s pacote=%s novas=%d "
                    "alteradas=%d status=%s por=%s", tenant_id, pacote.id,
                    conta["nova"], conta["alterada"], status, user_email)

    _gravar_meta(pacote.pasta, pacote.meta)
    mensagem, tipo = _mensagem_gravacao(conta, invalidos, admin)
    return _responder_mapa(request, db_session, profile, pacote, mensagem, tipo)


@router.post("/api/gerar-sped/confirmar", response_class=HTMLResponse)
def confirmar(request: Request,
              pacote_id: str = Form(...),
              db_session: Session = Depends(get_db),
              profile=Depends(get_profile)):
    """Passo 2: gera — só com tudo aprovado, e só com o que está aprovado.

    O CFOP que vier no formulário (a tela de 08/10/2026 mandava um por linha)
    é ignorado de propósito: aceitá-lo deixaria a trava do botão só no visual.
    """
    assert_internal(profile)
    try:
        pacote = _abrir_pacote(db_session, profile, pacote_id)
    except _PacoteIndisponivel as e:
        return _erro_gerar(request, str(e))
    try:
        regras_itens, regras_fretes = _regras(db_session, pacote.empresa)
    except Exception:                                     # noqa: BLE001
        return _falha_regras(request, db_session)

    combinacoes = _combinacoes(pacote)
    nfe, cte = _linhas(combinacoes, regras_itens, regras_fretes,
                       empresa_industrial(pacote.cadastro()))
    faltam = sum(1 for linha in nfe + cte if linha["status"] != "aprovada")
    if faltam:
        return _tela_mapa(request, profile, pacote, combinacoes, regras_itens,
                          regras_fretes,
                          f"Ainda {'falta' if faltam == 1 else 'faltam'} "
                          f"{_plural(faltam, 'combinação', 'combinações')} com CFOP "
                          "aprovado. O SPED só é gerado com tudo aprovado.", "erro")

    return _gerar_e_responder(request, db_session, profile, pacote,
                              _aprovadas(regras_itens), _aprovadas(regras_fretes))


@router.post("/api/gerar-sped/{pacote_id}/regerar", response_class=HTMLResponse)
def regerar(pacote_id: str, request: Request,
            competencia: str = Form(""),
            trazer: list[str] = Form([]),
            data_entrada: str = Form(""),
            db_session: Session = Depends(get_db),
            profile=Depends(get_profile)):
    """Gera de novo a partir do MESMO pacote, sem reenviar o ZIP — com as
    notas de meses anteriores que a pessoa marcou (`trazer`) entrando na 1ª
    quinzena (`data_entrada`).

    A empresa, o perfil e a atividade são os do envio original (guardados junto
    dos XMLs), a permissão é conferida de novo, e o CFOP é o das regras
    aprovadas, como no primeiro arquivo: a nota trazida de mês anterior também
    precisa de regra aprovada para cada item.
    """
    assert_internal(profile)
    try:
        pacote = _abrir_pacote(db_session, profile, pacote_id)
    except _PacoteIndisponivel as e:
        return _erro_gerar(request, str(e))
    try:
        regras_itens, regras_fretes = _regras(db_session, pacote.empresa)
    except Exception:                                     # noqa: BLE001
        return _falha_regras(request, db_session)
    return _gerar_e_responder(request, db_session, profile, pacote,
                              _aprovadas(regras_itens), _aprovadas(regras_fretes),
                              competencia=competencia, trazer=trazer,
                              data_entrada=data_entrada)


@router.get("/api/gerar-sped/{processo}/arquivo")
def baixar(processo: str, profile=Depends(get_profile)):
    assert_internal(profile)
    # O identificador é um UUID gerado aqui e não vem de nome de arquivo, mas
    # ele entra num caminho: conferir o formato é o que impede `..\..\` virar
    # leitura de arquivo do servidor.
    try:
        uuid.UUID(processo)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Arquivo não encontrado.")

    caminho = os.path.join(TEMP_DIR, processo, NOME_ARQUIVO)
    if not os.path.exists(caminho):
        raise HTTPException(
            status_code=404,
            detail="O arquivo gerado não está mais no servidor. Gere de novo.")

    with open(caminho, "rb") as f:
        conteudo = f.read()

    from .correcao_sped import _cabecalhos_download
    return StreamingResponse(
        iter([conteudo]), media_type="application/octet-stream",
        headers={**_cabecalhos_download("Sped_Gerado.txt"),
                 "Content-Length": str(len(conteudo))})
