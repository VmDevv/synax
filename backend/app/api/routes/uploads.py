import html
import os
import re
import shutil
import uuid
import zipfile
import logging
import aiofiles
from fastapi import APIRouter, UploadFile, File, BackgroundTasks, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session
import json
import pandas as pd
from io import BytesIO
from typing import List, Optional

from ...infrastructure.database.session import get_db
from ...infrastructure.repositories.rules_repository import RulesRepository
from ...domain.models.audit import AuditTask
from ...domain.models.rules import CfopClassification, CteCfopClassification, IpiCreditRule, CfopCreditRule, PisCofinsCreditRule
from ...services.audit_engine.cross_checker import (
    execute_audit, levantar_nao_conferidos, chave_item_match)
from ...services.audit_engine.ia_matcher import casar_nao_conferidos, ia_disponivel
from ...services.audit_engine import email_catalog
from ...services.audit_engine.resumo_financeiro import resumo_financeiro

logger = logging.getLogger(__name__)

router = APIRouter()
from ...core.templating import build_templates
templates = build_templates()

TEMP_DIR = os.path.join(os.getcwd(), "temp_uploads")

# Prazo para confirmar os pares sugeridos pela IA. Generoso de propósito — a
# pessoa precisa ler a tela e conferir item a item —, mas existe porque enquanto
# a decisão não vem os arquivos fiscais seguem em disco.
LIMITE_ESPERA_IA_S = 3600

# CNPJ entra na chave da regra só com dígito: o mesmo fornecedor aparece ora
# formatado, ora não, e a chave é comparada literalmente.
RE_SO_DIGITOS = re.compile(r"\D")
os.makedirs(TEMP_DIR, exist_ok=True)

from ...api.dependencies.auth import get_current_user
from ...core.authz import (
    get_profile, get_task_or_403, resolve_tenant_for_write,
    assert_internal, rule_status_for, INTERNAL_ROLES,
)
from ...core.rate_limit import limitar
from ...core.uploads_seguros import (
    LIMITE_SPED_BYTES, LIMITE_ZIP_BYTES,
    extrair_xmls_do_pacote, gravar_com_limite, nome_seguro,
)
from ...domain.models.auth_models import UserProfile

# Autoria das regras (created_by): e-mail real do usuário autenticado no Supabase.
def get_current_user_email(current_user = Depends(get_current_user)):
    if current_user and getattr(current_user, "email", None):
        return current_user.email
    return "sistema"

@router.post("/api/upload", status_code=202)
async def upload_files(
    request: Request,
    background_tasks: BackgroundTasks,
    sped_file: UploadFile = File(...),
    xmls_zip: UploadFile = File(None),
    use_sieg: bool = Form(False),
    sped_only_mode: bool = Form(False),
    ignore_saidas: bool = Form(False),
    # Combustíveis: soma o ICMS monofásico do XML ao crédito de ICMS.
    considerar_icms_mono: bool = Form(False),
    tenant_id: str = Form(...),
    current_user = Depends(get_current_user),
    db_session: Session = Depends(get_db),
    _rl = Depends(limitar("upload", maximo=12, janela_seg=600))
):
    """
    Endpoint isolado para receber o upload, salvar efemeramente no disco e
    injetar a função síncrona na ThreadPool do FastAPI (BackgroundTasks).
    """
    task_id = str(uuid.uuid4())
    
    # === VALIDAÇÃO ZERO TRUST (RBAC) — MANDATÓRIA ===
    if not current_user:
        raise HTTPException(status_code=401, detail="Autenticação necessária.")
    user_profile = db_session.query(UserProfile).filter(UserProfile.id == current_user.id).first()
    if not user_profile:
        raise HTTPException(status_code=403, detail="Perfil de usuário não encontrado.")
    if user_profile.role == 'operador':
        # Operador escolhe entre as empresas vinculadas a ele (tabela user_tenants)
        allowed = [str(t.id) for t in user_profile.allowed_tenants]
        if tenant_id not in allowed:
            raise HTTPException(status_code=403, detail="Acesso negado para esta empresa.")
    elif user_profile.role != 'admin':
        tenant_id = str(user_profile.tenant_id)


    # A importação automática pelo SIEG ainda não existe (o serviço devolvia
    # notas fictícias). Recusar na entrada em vez de deixar a auditoria rodar
    # sobre dados falsos e entregar o resultado como legítimo.
    if use_sieg and not sped_only_mode:
        raise HTTPException(
            status_code=400,
            detail=("A importação automática pelo SIEG ainda não está disponível. "
                    "Envie o ZIP de XMLs ou habilite o Modo Somente SPED."),
        )

    # 1. Criar pastas efêmeras e receber os arquivos ANTES de registrar a task:
    # upload recusado não pode deixar uma AuditTask órfã presa em 'pendente'.
    task_dir = os.path.join(TEMP_DIR, task_id)
    xml_dir = os.path.join(task_dir, "xmls")
    os.makedirs(xml_dir, exist_ok=True)

    try:
        # 2. Salvar SPED. O nome vindo do cliente não entra no caminho de forma
        # alguma: os arquivos são efêmeros e ninguém os abre pelo nome, então
        # nomes internos fixos resolvem o traversal E a colisão (um SPED chamado
        # "xmls.zip" sobrescreveria o ZIP). O nome original fica na AuditTask,
        # que é onde a trilha de auditoria precisa dele.
        sped_path = os.path.join(task_dir, "sped.txt")
        await gravar_com_limite(sped_file, sped_path, LIMITE_SPED_BYTES, "O arquivo SPED")

        # 3. Salvar e extrair o ZIP de XMLs
        if xmls_zip:
            # Nome interno neutro: o pacote pode ser ZIP ou RAR, e quem decide
            # qual é a assinatura do conteúdo, não a extensão que veio do cliente.
            zip_path = os.path.join(task_dir, "xmls.pacote")
            await gravar_com_limite(xmls_zip, zip_path, LIMITE_ZIP_BYTES, "O pacote de XMLs")
            try:
                extrair_xmls_do_pacote(zip_path, xml_dir)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="O arquivo enviado não é um ZIP válido.")
            finally:
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
        elif not sped_only_mode:
            # Se não vier ZIP e não for Modo Somente SPED
            raise HTTPException(status_code=400, detail="É necessário enviar o ZIP de XMLs, marcar a opção SIEG, ou habilitar o Modo Somente SPED.")
    except Exception:
        # Upload recusado não pode deixar pasta órfã crescendo em temp_uploads/.
        shutil.rmtree(task_dir, ignore_errors=True)
        raise

    # 4. Registrar Task no banco como 'pendente'
    # user_id/file_name são a trilha de auditoria: sem eles não há como saber
    # quem disparou o processamento de um arquivo fiscal.
    repo = RulesRepository(db_session, tenant_id)
    new_task = AuditTask(
        id=task_id,
        status="pendente",
        user_id=str(current_user.id),
        file_name=nome_seguro(sped_file.filename, "sped.txt")
    )
    repo.audit_tasks.add(new_task)
    
    # 5. Acionar processamento em Background (Liberando o Event Loop)
    from ...infrastructure.database.session import SessionLocal

    def process_and_audit():
        """Usa sessão dedicada para não compartilhar com a request HTTP."""
        bg_session = SessionLocal()
        try:
            # ETAPA DE IA, antes da auditoria: os itens que os sinais
            # determinísticos NÃO conseguiram parear são oferecidos ao modelo.
            # Havendo sugestão, a auditoria PARA aqui e espera uma pessoa
            # confirmar na tela — nada de IA entra num relatório fiscal sem
            # alguém ter olhado. Sem chave, sem sugestão ou com falha na API, o
            # fluxo segue direto como sempre foi.
            sugestoes = []
            if not sped_only_mode and ia_disponivel():
                try:
                    # Pares já aprovados antes não voltam para a tela: o motor os
                    # aplica sozinho, e perguntar de novo pelo mesmo item todo mês
                    # é exatamente o que a tabela de regras existe para evitar.
                    conhecidos = {
                        chave_item_match(r.company_cnpj, r.supplier_cnpj,
                                         r.sped_cod, r.xml_cod): r
                        for r in RulesRepository(bg_session, tenant_id).item_match_rules.get_all()
                        if getattr(r, "status", "aprovado") == "aprovado"
                    }
                    notas = levantar_nao_conferidos(sped_path, xml_dir, ignore_saidas,
                                                    pares_conhecidos=conhecidos)
                    sugestoes = casar_nao_conferidos(notas).get("sugestoes", [])
                except Exception:
                    # Modelo fora do ar não pode impedir a apuração do mês.
                    logger.exception("Etapa de IA falhou; auditoria segue sem ela. task_id=%s",
                                     task_id)

            if sugestoes:
                tarefa = bg_session.query(AuditTask).filter(AuditTask.id == task_id).first()
                if tarefa:
                    tarefa.status = "aguardando_ia"
                    # As opções viajam junto: a segunda fase roda a auditoria de
                    # verdade e precisa das mesmas escolhas da tela inicial.
                    tarefa.result_data = json.dumps({
                        "sugestoes_ia": sugestoes,
                        "opcoes": {
                            "sped_only_mode": sped_only_mode,
                            "ignore_saidas": ignore_saidas,
                            "considerar_icms_mono": considerar_icms_mono,
                        },
                    })
                    bg_session.commit()
                return

            # O ramo do SIEG saiu daqui: o serviço devolvia notas fictícias e a
            # auditoria era entregue como real. A rota agora recusa use_sieg na
            # entrada, então não há caminho para dados falsos chegarem ao motor.
            execute_audit(task_id, tenant_id, sped_path, xml_dir, bg_session,
                          sped_only_mode=sped_only_mode, ignore_saidas=ignore_saidas,
                          considerar_icms_mono=considerar_icms_mono)
        except Exception:
            # O traceback vai para o log do servidor, NUNCA para o banco: error_message
            # é renderizado na tela do cliente e exporia caminhos internos e trechos
            # de código. O task_id correlaciona o que o usuário vê com o log.
            logger.exception("Falha ao processar auditoria task_id=%s", task_id)
            try:
                bg_session.rollback()
            except Exception:
                pass
            task = bg_session.query(AuditTask).filter(AuditTask.id == task_id).first()
            if task:
                task.status = "erro"
                task.error_message = (
                    "Não foi possível concluir a auditoria. Verifique se o SPED e os XMLs "
                    "estão corretos e tente novamente. Se persistir, informe o código abaixo ao suporte."
                )
                try:
                    bg_session.commit()
                except Exception:
                    bg_session.rollback()
        finally:
            try:
                bg_session.close()
            except Exception:
                pass

    background_tasks.add_task(process_and_audit)
    
    # 6. Responder com fragmento de Polling do HTMX
    return templates.TemplateResponse(request, "partials/curation/polling.html", {
        "request": request,
        "task_id": task_id
    })


@router.get("/api/status/{task_id}", response_class=HTMLResponse)
def get_task_status(
    request: Request,
    task_id: str,
    db_session: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    profile = Depends(get_profile)
):
    # Escopo por tenant: sem isso qualquer usuário logado leria a auditoria de outro cliente.
    task = get_task_or_403(db_session, profile, task_id)

    if task.status in ["pendente", "processando"]:
        from datetime import datetime, timezone
        if task.created_at:
            now = datetime.now(timezone.utc) if task.created_at.tzinfo else datetime.utcnow()
            if (now - task.created_at).total_seconds() > 600:  # 10 minutos de timeout SRE
                task.status = "timeout"
                task.error_message = "A auditoria excedeu o tempo limite de processamento (10 minutos) e foi cancelada automaticamente para proteger o servidor."
                try:
                    db_session.commit()
                except Exception:
                    db_session.rollback()
                    
        if task.status in ["pendente", "processando"]:
            return templates.TemplateResponse(request, "partials/curation/polling.html", {
                "request": request,
                "task_id": task_id
            })
        
    # A auditoria parou para uma pessoa decidir sobre os pares que a IA sugeriu.
    # O polling do HTMX cai aqui e troca a barra de progresso por esta tela.
    if task.status == "aguardando_ia":
        from datetime import datetime, timezone
        # Enquanto espera a decisão, o SPED e os XMLs continuam em disco: quem
        # os apaga é o `finally` do execute_audit, e ele ainda não rodou. Sem
        # este prazo, uma aba fechada sem decidir deixaria os arquivos fiscais
        # ali para sempre — caminho que não existia antes desta etapa, porque
        # todo upload terminava em execute_audit.
        if task.created_at:
            agora = (datetime.now(timezone.utc) if task.created_at.tzinfo
                     else datetime.utcnow())
            if (agora - task.created_at).total_seconds() > LIMITE_ESPERA_IA_S:
                task.status = "timeout"
                task.error_message = (
                    "A confirmação dos itens relacionados por IA não foi feita dentro de "
                    "1 hora, e os arquivos enviados foram descartados. Envie o SPED e os "
                    "XMLs novamente para rodar a auditoria."
                )
                shutil.rmtree(os.path.join(TEMP_DIR, task_id), ignore_errors=True)
                try:
                    db_session.commit()
                except Exception:
                    db_session.rollback()

        if task.status == "aguardando_ia":
            dados = json.loads(task.result_data or "{}")
            return templates.TemplateResponse(request, "partials/curation/confirmar_ia.html", {
                "request": request,
                "task_id": task_id,
                "sugestoes": dados.get("sugestoes_ia", []),
            })

    if task.status in ["erro", "falha", "timeout"]:
        error_title = "Tempo Limite Excedido (Timeout)" if task.status == "timeout" else "Erro na Auditoria"
        # error_message e task_id vêm do banco e entram num f-string, fora do
        # autoescape do Jinja. Escapar aqui é a defesa em profundidade: se algum
        # dia um texto de exceção com dado do XML do fornecedor voltar a ser
        # gravado, ele não vira <script> na tela do operador.
        error_msg = html.escape(task.error_message or "Ocorreu uma falha no processamento em segundo plano.")
        task_id = html.escape(task_id)
        return HTMLResponse(f"""
        <div class="p-6 bg-red-50 border border-red-200 rounded-2xl shadow-sm text-left my-4">
            <div class="flex items-center gap-3 mb-2">
                <i class="fa-solid fa-triangle-exclamation text-red-600 text-xl"></i>
                <h3 class="text-lg font-bold text-red-800">{error_title}</h3>
            </div>
            <p class="text-sm text-red-700 mb-4 whitespace-pre-wrap">{error_msg}</p>
            <div class="flex items-center justify-between">
                <span class="text-xs text-red-500 font-mono">Task ID: {task_id}</span>
                <button onclick="window.location.reload()" class="px-4 py-2 bg-red-600 hover:bg-red-700 text-white text-xs font-semibold rounded-lg shadow-sm transition">
                    <i class="fa-solid fa-rotate-right mr-1.5"></i>Tentar Novamente
                </button>
            </div>
        </div>
        """)
        
    elif task.status == "concluido":
        from ...domain.models.auth_models import Tenant
        tenant = db_session.query(Tenant).filter(Tenant.id == task.tenant_id).first()
        result = json.loads(task.result_data or "{}")
        is_admin = profile.role == "admin"
        return templates.TemplateResponse(request, "partials/curation/curation_results.html", {
            "request": request,
            "task_id": task_id,
            "task": task,
            "tenant_id": str(task.tenant_id),
            "tenant": tenant,
            "result": result,
            "is_admin": is_admin,
            # Ferramentas internas do escritório (ex.: gerador de e-mail de
            # apontamentos) ficam ocultas para o cliente final.
            "is_internal": profile.role in INTERNAL_ROLES,
            # Opções do seletor de CFOP de entrada, montadas uma única vez: as
            # linhas reaproveitam isso, evitando uma requisição HTMX por linha.
            "cfop_opcoes": _opcoes_cfop_entrada(db_session, task),
            # Catálogo de apontamentos do gerador de e-mail, já com a contagem
            # desta auditoria. A tela é montada a partir daqui — não há mais
            # lista de checkboxes escrita à mão no HTML.
            "email_categorias": email_catalog.resumo_para_tela(result),
            # Quanto dinheiro está em jogo, e de que lado. Calculado na
            # hora a partir do relatório: vale para as auditorias antigas
            # também, sem backfill nem coluna nova.
            "financeiro": resumo_financeiro(result),
            "email_grupos": email_catalog.GRUPOS,
            "current_user": current_user
        })

def _gravar_regras_de_par(db_session: Session, tenant_id: str, profile,
                          sugestoes: list[dict]) -> int:
    """Grava os pares aprovados como ItemMatchRule. Devolve quantos entraram.

    Falha aqui NÃO impede a auditoria: a regra é uma economia para o mês que
    vem, e perder isso é muito menos grave do que travar a apuração de hoje.

    Sugestão sem CNPJ ou sem um dos dois códigos é pulada — a chave tem cinco
    campos e todos são exatos. Gravar com campo vazio criaria regra que casa com
    qualquer produto daquele fornecedor, que é justamente o que este projeto
    proíbe em toda tabela de regra.
    """
    from ...domain.models.rules import ItemMatchRule

    status = rule_status_for(profile)
    repo = RulesRepository(db_session, tenant_id)
    gravadas = 0

    # UMA consulta para saber o que já existe, em vez de um `session.get()` por
    # sugestão. Cada ida ao Supabase custa ~140ms de rede contra ~0.1ms de
    # execução (mesma conta do `_counts` da Curadoria): com 60 sugestões o laço
    # antigo gastava ~8s antes de gravar a primeira linha, e a tela ficava muda
    # esse tempo todo.
    ja_existem = {
        (r.company_cnpj, r.supplier_cnpj, r.sped_cod, r.xml_cod)
        for r in db_session.query(
            ItemMatchRule.company_cnpj, ItemMatchRule.supplier_cnpj,
            ItemMatchRule.sped_cod, ItemMatchRule.xml_cod,
        ).filter(ItemMatchRule.tenant_id == tenant_id).all()
    }
    # Sugestão repetida dentro do MESMO envio também não pode gravar duas vezes.
    vistos = set()
    novas = []

    for s in sugestoes:
        company = RE_SO_DIGITOS.sub("", str(s.get("company_cnpj") or ""))
        supplier = RE_SO_DIGITOS.sub("", str(s.get("supplier_cnpj") or ""))
        sped_cod = str(s.get("sped_cod") or "").strip().upper()
        xml_cod = str(s.get("xml_cod") or "").strip().upper()
        if not (company and supplier and sped_cod and xml_cod):
            continue

        chave = (company, supplier, sped_cod, xml_cod)
        if chave in ja_existem or chave in vistos:
            continue
        vistos.add(chave)

        novas.append(ItemMatchRule(
            company_cnpj=company, supplier_cnpj=supplier,
            sped_cod=sped_cod, xml_cod=xml_cod, tenant_id=tenant_id,
            sped_descr=str(s.get("sped_descr") or "")[:255],
            xml_descr=str(s.get("xml_descr") or "")[:255],
            origem=str(s.get("origem") or "ia")[:20],
            confianca=str(s.get("confianca") or "")[:10],
            justificativa=str(s.get("justificativa") or "")[:400],
            status=status,
            created_by=str(getattr(profile, "id", "") or ""),
        ))

    if not novas:
        return 0

    # `add_all` grava tudo num commit só. O `add()` unitário do BaseRepository
    # faz commit E refresh a CADA objeto — três idas ao Supabase por regra, o
    # que com 60 aprovações eram ~180 round-trips (~25s) de tela parada.
    # `add_all` também injeta o tenant_id em cada objeto, que é a trava que
    # impede uma regra nascer no escopo de outra empresa.
    try:
        repo.item_match_rules.add_all(novas)
        gravadas = len(novas)
    except Exception:
        logger.exception("Falha ao gravar regras de par SPED×XML (tenant=%s)", tenant_id)
        try:
            db_session.rollback()
        except Exception:
            pass
        return 0
    return gravadas


@router.post("/api/audit/{task_id}/aplicar-ia", response_class=HTMLResponse)
def aplicar_pares_ia(
    request: Request,
    task_id: str,
    background_tasks: BackgroundTasks,
    acao: str = Form(""),
    aprovados: list[str] = Form([]),
    db_session: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    profile = Depends(get_profile),
):
    """Segunda fase: roda a auditoria com os pares que a pessoa APROVOU.

    `aprovados` traz o índice de cada sugestão marcada na tela. O que não vier
    marcado é descartado e o item volta para "Itens Não Conferidos" — que é o
    comportamento de sempre, e por isso descartar é o caminho seguro.

    `acao="ignorar"` descarta tudo de uma vez, sem olhar a seleção.

    Cada par aprovado vira uma `ItemMatchRule`: na próxima auditoria daquele
    fornecedor o motor casa direto, sem tela e sem IA. O status segue o
    maker-checker do resto do sistema (`rule_status_for`) — admin grava
    `aprovado`, operador grava `pendente_revisao` para a Curadoria revisar.

    `Form("")` e não `Form(...)`: neste FastAPI um campo obrigatório vazio vira
    422 antes de entrar na função, e como o htmx não troca o alvo em 4xx o
    usuário clicaria e nada aconteceria na tela.
    """
    task = get_task_or_403(db_session, profile, task_id)
    if task.status != "aguardando_ia":
        # Botão clicado duas vezes, ou aba velha aberta. Devolve o que já existe
        # em vez de rodar a auditoria de novo por cima.
        return HTMLResponse(
            '<div hx-get="/api/status/' + html.escape(task_id) + '" hx-trigger="load" '
            'hx-swap="outerHTML"></div>')

    dados = json.loads(task.result_data or "{}")
    opcoes = dados.get("opcoes", {})
    sugestoes = dados.get("sugestoes_ia", [])
    ignorou = str(acao).strip().lower() == "ignorar"

    # Índices marcados na tela. Valor forjado ou fora da lista é descartado sem
    # erro — o formulário vem do cliente e não é fonte confiável de índice.
    selecionados = set()
    if not ignorou:
        for bruto in aprovados:
            try:
                i = int(str(bruto).strip())
            except (TypeError, ValueError):
                continue
            if 0 <= i < len(sugestoes):
                selecionados.add(i)

    # Agrupa por chave da nota — é o formato que execute_audit consome.
    pares_ia = {}
    for i in sorted(selecionados):
        sugestao = sugestoes[i]
        pares_ia.setdefault(sugestao.get("chave", ""), []).append(sugestao)

    tenant_id = str(task.tenant_id)
    task_dir = os.path.join(TEMP_DIR, task_id)
    sped_path = os.path.join(task_dir, "sped.txt")
    xml_dir = os.path.join(task_dir, "xmls")

    # O status muda ANTES do trabalho pesado, e commita na hora. O guard lá em
    # cima só serve se a janela entre ele e esta linha for curta: gravando as
    # regras primeiro, a janela durava os segundos da escrita no Supabase, e
    # cada clique repetido nesse intervalo passava pelo guard e gravava tudo de
    # novo. Foi o que aconteceu na prática — a tela não respondia, o usuário
    # clicava várias vezes, e todos os cliques trabalhavam em paralelo.
    task.status = "processando"
    task.result_data = None
    db_session.commit()

    if selecionados:
        _gravar_regras_de_par(db_session, tenant_id, profile,
                              [sugestoes[i] for i in sorted(selecionados)])

    from ...infrastructure.database.session import SessionLocal

    def rodar():
        bg_session = SessionLocal()
        try:
            execute_audit(task_id, tenant_id, sped_path, xml_dir, bg_session,
                          sped_only_mode=bool(opcoes.get("sped_only_mode")),
                          ignore_saidas=bool(opcoes.get("ignore_saidas", True)),
                          considerar_icms_mono=bool(opcoes.get("considerar_icms_mono")),
                          pares_ia=pares_ia)
        except Exception:
            logger.exception("Falha ao processar auditoria task_id=%s", task_id)
            try:
                bg_session.rollback()
            except Exception:
                pass
            tarefa = bg_session.query(AuditTask).filter(AuditTask.id == task_id).first()
            if tarefa:
                tarefa.status = "erro"
                tarefa.error_message = (
                    "Não foi possível concluir a auditoria. Verifique se o SPED e os XMLs "
                    "estão corretos e tente novamente. Se persistir, informe o código abaixo ao suporte."
                )
                try:
                    bg_session.commit()
                except Exception:
                    bg_session.rollback()
        finally:
            try:
                bg_session.close()
            except Exception:
                pass

    background_tasks.add_task(rodar)
    return templates.TemplateResponse(request, "partials/curation/polling.html", {
        "request": request,
        "task_id": task_id,
    })


@router.get("/api/export/{task_id}")
def export_audit_excel(
    task_id: str,
    db_session: Session = Depends(get_db),
    profile = Depends(get_profile)
):
    task = get_task_or_403(db_session, profile, task_id)
    if not task.result_data:
        return JSONResponse({"error": "Dados não disponíveis"}, status_code=404)

    data = json.loads(task.result_data)

    # Esta rota tinha um gerador PRÓPRIO, que procurava as chaves
    # "xmls_faltantes", "cfops_desconhecidos" e "divergencias_valor" — nomes que
    # o motor nunca produziu (são "missing_xmls", "unmapped_items" e
    # "value_discrepancies"). Resultado: baixava um arquivo com uma aba de
    # resumo e mais nada, parecendo auditoria sem apontamento. Nenhuma tela
    # apontava para cá; a URL fica de pé para não quebrar link antigo, mas
    # devolvendo o MESMO relatório de /api/export/excel/{task_id}.
    output = generate_excel_from_results(data)
    headers = {
        'Content-Disposition': f'attachment; filename="auditoria_{task_id}.xlsx"'
    }
    return StreamingResponse(output, headers=headers, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

def _opcoes_cfop_entrada(db_session: Session, task, selecionado: str = ""):
    """Monta os três grupos do seletor de CFOP de entrada.

    A ordem reflete o que o operador mais usa: primeiro o que o motor sugeriu
    NESTA auditoria, depois o histórico já aprovado DESTA empresa (com a
    frequência real de uso), e por último a tabela padrão.
    """
    from ...domain.constants.cfops_entrada import CFOPS_ENTRADA, rotulo

    # 1) Sugestões do motor para as pendências desta tela
    sugeridos_set: list[str] = []
    try:
        dados = json.loads(task.result_data or "{}")
    except (ValueError, TypeError):
        dados = {}
    for chave, campo in (("cfops_desconhecidos", "cfop_sugerido"),
                         ("unmapped_items", "current_sped_cfop")):
        for item in dados.get(chave, []) or []:
            valor = str(item.get(campo) or "").strip()
            if valor and valor not in sugeridos_set:
                sugeridos_set.append(valor)

    # 2) Histórico aprovado desta empresa, do mais usado para o menos usado
    recorrentes_set: list[str] = []
    try:
        linhas = (
            db_session.query(CfopClassification.entry_cfop, func.count().label("n"))
            .filter(CfopClassification.tenant_id == str(task.tenant_id),
                    CfopClassification.status == 'aprovado')
            .group_by(CfopClassification.entry_cfop)
            .order_by(func.count().desc())
            .limit(20).all()
        )
        recorrentes_set = [str(r[0]).strip() for r in linhas if r[0] and str(r[0]).strip()]
    except Exception:
        logger.warning("Não foi possível carregar o histórico de CFOP de entrada", exc_info=True)

    vistos = set(sugeridos_set)
    recorrentes_set = [c for c in recorrentes_set if c not in vistos]
    vistos |= set(recorrentes_set)
    padrao_set = [c for c in sorted(CFOPS_ENTRADA) if c not in vistos]

    par = lambda lista: [(c, rotulo(c)) for c in lista]
    return {
        "sugeridos": par(sugeridos_set),
        "recorrentes": par(recorrentes_set),
        "padrao": par(padrao_set),
        "selecionado": (selecionado or "").strip(),
    }


@router.get("/api/audit/{task_id}/entry-cfops", response_class=HTMLResponse)
def listar_cfops_entrada(
    request: Request,
    task_id: str,
    selecionado: str = "",
    db_session: Session = Depends(get_db),
    profile = Depends(get_profile),
):
    """Opções do seletor de CFOP de entrada, específicas desta auditoria/empresa."""
    task = get_task_or_403(db_session, profile, task_id)
    ctx = _opcoes_cfop_entrada(db_session, task, selecionado)
    return templates.TemplateResponse(
        request, "partials/curation/_cfop_entrada_options.html", {"request": request, **ctx}
    )


@router.post("/api/mapping/cfop", response_class=HTMLResponse)
def map_cfop_inline(
    request: Request,
    cfop: str = Form(...),
    mapped_cfop: str = Form(...),
    tenant_id: str = Form(...),
    cnpj_emitente: str = Form(""),
    company_cnpj: str = Form(""),
    product_code: str = Form(""),
    product_description: str = Form(""),
    current_user = Depends(get_current_user),
    user_email: str = Depends(get_current_user_email),
    db_session: Session = Depends(get_db)
):
    """ Grava a regra de CFOP no banco e retorna sucesso para a linha HTMX. """
    try:
        # === VALIDAÇÃO ZERO TRUST (RBAC) — MANDATÓRIA ===
        if not current_user:
            raise HTTPException(status_code=401, detail="Autenticação necessária.")
        user_profile = db_session.query(UserProfile).filter(UserProfile.id == current_user.id).first()
        if not user_profile:
            raise HTTPException(status_code=403, detail="Perfil não encontrado.")
        if user_profile.role == 'operador':
            # Operador só opera nas empresas vinculadas a ele (tabela user_tenants)
            allowed = [str(t.id) for t in user_profile.allowed_tenants]
            if tenant_id not in allowed:
                raise HTTPException(status_code=403, detail="Acesso negado para este cliente.")
        elif user_profile.role != 'admin':
            # Allow-list: qualquer papel não-admin fica travado no próprio tenant
            tenant_id = str(user_profile.tenant_id)
                
        # Status da regra com base na role
        rule_status = rule_status_for(user_profile)

        # A chave tem quatro campos e o motor consulta exato, sem curinga. Faltando
        # qualquer um, a regra nasceria morta: ocupa espaço, aparece na lista e nunca
        # casa com nota nenhuma. Melhor recusar visivelmente do que gravar mentira.
        cnpj_emitente = (cnpj_emitente or "").strip()
        company_cnpj = (company_cnpj or "").strip()
        product_code = (product_code or "").strip()
        if not (cnpj_emitente and company_cnpj and product_code) or "*" in (cnpj_emitente, company_cnpj, product_code):
            # `cfop-row` + data-cfop/data-cnpj: sem isso a paginação da aba
            # "Novos CFOPs" (applyTableState, no template) para de enxergar
            # esta linha — ela some da contagem de páginas mas continua no
            # DOM, então fica visível em TODAS as páginas para sempre, em vez
            # de só na página onde foi aprovada/recusada.
            return HTMLResponse(f"""
                <tr class='cfop-row bg-amber-50' data-cfop='{html.escape(cfop)}' data-cnpj='{html.escape(cnpj_emitente)}'><td colspan='3' class='text-amber-700 text-center py-3'>
                    <i class='fa-solid fa-triangle-exclamation mr-1'></i>
                    Regra não gravada: falta CNPJ da empresa, CNPJ do fornecedor ou código do produto.
                    A regra de CFOP exige os quatro campos — sem eles ela nunca casaria com uma nota.
                </td></tr>
            """)

        existing_rule = db_session.query(CfopClassification).filter_by(
            tenant_id=tenant_id,
            company_cnpj=company_cnpj,
            supplier_cnpj=cnpj_emitente,
            supplier_cfop=cfop,
            product_code=product_code
        ).first()

        # Descrição real do item vinda do XML. É o que permite ao admin saber o
        # QUE está aprovando, em vez de um texto gerado pelo próprio sistema.
        descricao = (product_description or "").strip() or None

        if existing_rule:
            existing_rule.entry_cfop = mapped_cfop
            if descricao:
                existing_rule.product_description = descricao
            existing_rule.created_by = user_email
            existing_rule.status = rule_status
        else:
            new_rule = CfopClassification(
                tenant_id=tenant_id,
                company_cnpj=company_cnpj,
                supplier_cnpj=cnpj_emitente,
                supplier_cfop=cfop,
                product_code=product_code,
                entry_cfop=mapped_cfop,
                product_description=descricao,
                status=rule_status,
                is_global=False,
                created_by=user_email
            )
            db_session.add(new_rule)
            
        db_session.commit()
        # Mesma razão do bloco de validação acima: a linha de sucesso precisa
        # continuar contando como `cfop-row` para a paginação da tela saber
        # em qual página ela mora — senão a mensagem "mapeado com sucesso"
        # de uma página vaza e fica visível em todas as outras.
        return HTMLResponse(f"""
            <tr class="cfop-row bg-emerald-50" data-cfop="{html.escape(cfop)}" data-cnpj="{html.escape(cnpj_emitente)}">
                <td colspan="3" class="text-emerald-700 font-bold text-center py-3">
                    <i class="fa-solid fa-check-circle mr-1"></i> CFOP {html.escape(cfop)} mapeado para {html.escape(mapped_cfop)} com sucesso! (por {html.escape(user_email)})
                </td>
            </tr>
        """)
    except Exception:
        logger.exception("Falha ao gravar mapeamento inline")
        db_session.rollback()
        return HTMLResponse(f"""
            <tr class='cfop-row bg-red-50' data-cfop='{html.escape(cfop)}' data-cnpj='{html.escape(cnpj_emitente or "")}'>
                <td colspan='3' class='text-red-600 text-center'>Não foi possível salvar a regra. Tente novamente.</td>
            </tr>
        """)

@router.post("/api/mapping/cte_cfop", response_class=HTMLResponse)
def map_cte_cfop_inline(
    request: Request,
    cfop: str = Form(...),
    emitter_uf: str = Form(...),
    mapped_cfop: str = Form(...),
    tenant_id: str = Form(...),
    company_cnpj: str = Form(...),
    current_user = Depends(get_current_user),
    user_email: str = Depends(get_current_user_email),
    db_session: Session = Depends(get_db)
):
    """ Grava a regra de CFOP para CT-e (Frete) no banco com isolamento por empresa. """
    try:
        if not current_user:
            raise HTTPException(status_code=401, detail="Autenticação necessária.")
        user_profile = db_session.query(UserProfile).filter(UserProfile.id == current_user.id).first()
        if not user_profile:
            raise HTTPException(status_code=403, detail="Perfil não encontrado.")
        if user_profile.role == 'operador':
            # Operador só opera nas empresas vinculadas a ele (tabela user_tenants)
            allowed = [str(t.id) for t in user_profile.allowed_tenants]
            if tenant_id not in allowed:
                raise HTTPException(status_code=403, detail="Acesso negado para este cliente.")
        elif user_profile.role != 'admin':
            # Allow-list: qualquer papel não-admin fica travado no próprio tenant
            tenant_id = str(user_profile.tenant_id)
                
        # Status da regra com base na role
        rule_status = rule_status_for(user_profile)

        existing_rule = db_session.query(CteCfopClassification).filter_by(
            tenant_id=tenant_id,
            company_cnpj=company_cnpj,
            supplier_cfop=cfop,
            emitter_uf=emitter_uf
        ).first()

        if existing_rule:
            existing_rule.entry_cfop = mapped_cfop
            existing_rule.created_by = user_email
            existing_rule.status = rule_status
        else:
            new_rule = CteCfopClassification(
                tenant_id=tenant_id,
                company_cnpj=company_cnpj,
                supplier_cfop=cfop,
                emitter_uf=emitter_uf,
                entry_cfop=mapped_cfop,
                status=rule_status,
                is_global=False,
                created_by=user_email
            )
            db_session.add(new_rule)
            
        db_session.commit()
        return HTMLResponse(f"""
            <tr class="bg-emerald-50">
                <td colspan="5" class="text-emerald-700 font-bold text-center py-3">
                    <i class="fa-solid fa-check-circle mr-1"></i> CT-e CFOP {cfop} ({emitter_uf}) mapeado para {mapped_cfop} com sucesso! (por {user_email})
                </td>
            </tr>
        """)
    except Exception:
        logger.exception("Falha ao gravar mapeamento inline")
        db_session.rollback()
        return HTMLResponse("<tr class='bg-red-50'><td colspan='5' class='text-red-600 text-center'>Não foi possível salvar a regra. Tente novamente.</td></tr>")

@router.post("/api/mapping/icms_rule", response_class=HTMLResponse)
def map_icms_inline(
    request: Request,
    cfop: str = Form(...),
    allow_credit: bool = Form(...),
    tenant_id: str = Form(...),
    current_user = Depends(get_current_user),
    user_email: str = Depends(get_current_user_email),
    db_session: Session = Depends(get_db)
):
    """ Grava a regra de crédito de ICMS no banco (Sim/Não). """
    try:
        if not current_user:
            raise HTTPException(status_code=401, detail="Autenticação necessária.")
        user_profile = db_session.query(UserProfile).filter(UserProfile.id == current_user.id).first()
        if not user_profile:
            raise HTTPException(status_code=403, detail="Perfil não encontrado.")
        if user_profile.role == 'operador':
            # Operador só opera nas empresas vinculadas a ele (tabela user_tenants)
            allowed = [str(t.id) for t in user_profile.allowed_tenants]
            if tenant_id not in allowed:
                raise HTTPException(status_code=403, detail="Acesso negado para este cliente.")
        elif user_profile.role != 'admin':
            # Allow-list: qualquer papel não-admin fica travado no próprio tenant
            tenant_id = str(user_profile.tenant_id)
                
        # Status da regra com base na role
        rule_status = rule_status_for(user_profile)
                
        existing_rule = db_session.query(CfopCreditRule).filter_by(
            tenant_id=tenant_id,
            cfop=cfop
        ).first()
        
        if existing_rule:
            existing_rule.allow_credit = allow_credit
            existing_rule.description = f"Mapeamento ICMS One-Click Upsert ({'Permite' if allow_credit else 'Glosado'})"
            existing_rule.created_by = user_email
            existing_rule.status = rule_status
        else:
            new_rule = CfopCreditRule(
                cfop=cfop,
                tenant_id=tenant_id,
                allow_credit=allow_credit,
                description=f"Mapeamento ICMS One-Click ({'Permite' if allow_credit else 'Glosado'})",
                status=rule_status,
                is_global=False,
                created_by=user_email
            )
            db_session.add(new_rule)
            
        db_session.commit()
        
        status_text = "Permite Crédito" if allow_credit else "Crédito Glosado"
        color = "emerald" if allow_credit else "red"
        
        return HTMLResponse(f"""
            <tr class="bg-{color}-50">
                <td colspan="4" class="text-{color}-700 font-bold text-center py-3">
                    <i class="fa-solid fa-check-circle mr-1"></i> ICMS para {cfop} salvo como: {status_text} (por {user_email})
                </td>
            </tr>
        """)
    except Exception:
        logger.exception("Falha ao gravar mapeamento inline")
        db_session.rollback()
        return HTMLResponse("<tr class='bg-red-50'><td colspan='4' class='text-red-600 text-center'>Não foi possível salvar a regra. Tente novamente.</td></tr>")

@router.post("/api/mapping/ipi_rule", response_class=HTMLResponse)
def map_ipi_inline(
    request: Request,
    cfop: str = Form(...),
    # Form(...) recusa string vazia com 422 antes de entrar na função, e o htmx
    # não troca o alvo em 4xx: o usuário clicaria e nada aconteceria na tela.
    # A validação do CST acontece dentro, devolvendo 200 com o motivo visível.
    cst_ipi: str = Form(""),
    allow_credit: bool = Form(...),
    tenant_id: str = Form(...),
    current_user = Depends(get_current_user),
    user_email: str = Depends(get_current_user_email),
    db_session: Session = Depends(get_db)
):
    """ Grava a regra de crédito de IPI no banco (Sim/Não). """
    try:
        # === VALIDAÇÃO ZERO TRUST (RBAC) — MANDATÓRIA ===
        if not current_user:
            raise HTTPException(status_code=401, detail="Autenticação necessária.")
        user_profile = db_session.query(UserProfile).filter(UserProfile.id == current_user.id).first()
        if not user_profile:
            raise HTTPException(status_code=403, detail="Perfil não encontrado.")
        if user_profile.role == 'operador':
            # Operador só opera nas empresas vinculadas a ele (tabela user_tenants)
            allowed = [str(t.id) for t in user_profile.allowed_tenants]
            if tenant_id not in allowed:
                raise HTTPException(status_code=403, detail="Acesso negado para este cliente.")
        elif user_profile.role != 'admin':
            # Allow-list: qualquer papel não-admin fica travado no próprio tenant
            tenant_id = str(user_profile.tenant_id)
                
        # Status da regra com base na role
        rule_status = rule_status_for(user_profile)

        # O motor consulta (CFOP, CST). A tela mandava "*" aqui e o CST virava
        # curinga: uma resposta "glosa" valia para todo CST daquele CFOP, em
        # toda nota. Sem CST real não há regra a gravar.
        cst_ipi = (cst_ipi or "").strip()
        if not cst_ipi or cst_ipi == "*":
            return HTMLResponse("""
                <tr class='bg-amber-50'><td colspan='4' class='text-amber-700 text-center py-3'>
                    <i class='fa-solid fa-triangle-exclamation mr-1'></i>
                    Regra não gravada: sem o CST de IPI. A regra vale para um CFOP e um CST
                    específicos — não existe regra de IPI que valha para qualquer CST.
                </td></tr>
            """)

        existing_rule = db_session.query(IpiCreditRule).filter_by(
            tenant_id=tenant_id,
            cfop=cfop,
            cst_expected=cst_ipi
        ).first()
        
        if existing_rule:
            existing_rule.allow_credit = allow_credit
            existing_rule.description = f"Mapeamento One-Click Upsert ({'Permite' if allow_credit else 'Glosado'})"
            existing_rule.created_by = user_email
            existing_rule.status = rule_status
        else:
            new_rule = IpiCreditRule(
                cfop=cfop,
                cst_expected=cst_ipi,
                tenant_id=tenant_id,
                allow_credit=allow_credit,
                description=f"Mapeamento One-Click ({'Permite' if allow_credit else 'Glosado'})",
                status=rule_status,
                is_global=False,
                created_by=user_email
            )
            db_session.add(new_rule)
            
        db_session.commit()
        
        status_text = "Permite Crédito" if allow_credit else "Crédito Glosado"
        color = "emerald" if allow_credit else "red"
        
        return HTMLResponse(f"""
            <tr class="bg-{color}-50">
                <td colspan="4" class="text-{color}-700 font-bold text-center py-3">
                    <i class="fa-solid fa-check-circle mr-1"></i> IPI para {cfop} (CST {cst_ipi}) salvo como: {status_text} (por {user_email})
                </td>
            </tr>
        """)
    except Exception:
        logger.exception("Falha ao gravar mapeamento inline")
        db_session.rollback()
        return HTMLResponse("<tr class='bg-red-50'><td colspan='4' class='text-red-600 text-center'>Não foi possível salvar a regra. Tente novamente.</td></tr>")

@router.post("/api/mapping/pis_cofins_rule", response_class=HTMLResponse)
def map_pis_cofins_inline(
    request: Request,
    cfop: str = Form(...),
    cst_expected: str = Form(...),
    allow_credit: bool = Form(...),
    tenant_id: str = Form(...),
    current_user = Depends(get_current_user),
    user_email: str = Depends(get_current_user_email),
    db_session: Session = Depends(get_db)
):
    """ Grava a regra de crédito de PIS/COFINS no banco (Sim/Não). """
    try:
        if not current_user:
            raise HTTPException(status_code=401, detail="Autenticação necessária.")
        user_profile = db_session.query(UserProfile).filter(UserProfile.id == current_user.id).first()
        if not user_profile:
            raise HTTPException(status_code=403, detail="Perfil não encontrado.")
        if user_profile.role == 'operador':
            # Operador só opera nas empresas vinculadas a ele (tabela user_tenants)
            allowed = [str(t.id) for t in user_profile.allowed_tenants]
            if tenant_id not in allowed:
                raise HTTPException(status_code=403, detail="Acesso negado para este cliente.")
        elif user_profile.role != 'admin':
            # Allow-list: qualquer papel não-admin fica travado no próprio tenant
            tenant_id = str(user_profile.tenant_id)
                
        # Status da regra com base na role
        rule_status = rule_status_for(user_profile)
                
        existing_rule = db_session.query(PisCofinsCreditRule).filter_by(
            tenant_id=tenant_id,
            cfop=cfop,
            cst_expected=cst_expected
        ).first()
        
        if existing_rule:
            existing_rule.allow_credit = allow_credit
            existing_rule.description = f"Mapeamento One-Click Upsert ({'Permite' if allow_credit else 'Glosado'})"
            existing_rule.created_by = user_email
            existing_rule.status = rule_status
        else:
            new_rule = PisCofinsCreditRule(
                cfop=cfop,
                cst_expected=cst_expected,
                tenant_id=tenant_id,
                allow_credit=allow_credit,
                description=f"Mapeamento One-Click ({'Permite' if allow_credit else 'Glosado'})",
                status=rule_status,
                is_global=False,
                created_by=user_email
            )
            db_session.add(new_rule)
            
        db_session.commit()
        
        status_text = "Permite Crédito" if allow_credit else "Crédito Glosado"
        color = "emerald" if allow_credit else "red"
        
        return HTMLResponse(f"""
            <tr class="bg-{color}-50">
                <td colspan="4" class="text-{color}-700 font-bold text-center py-3">
                    <i class="fa-solid fa-check-circle mr-1"></i> PIS/COFINS para {cfop} (CST {cst_expected}) salvo como: {status_text} (por {user_email})
                </td>
            </tr>
        """)
    except Exception:
        logger.exception("Falha ao gravar mapeamento inline")
        db_session.rollback()
        return HTMLResponse("<tr class='bg-red-50'><td colspan='4' class='text-red-600 text-center'>Não foi possível salvar a regra. Tente novamente.</td></tr>")

def _so_digitos(valor) -> str:
    """CNPJ sem pontuacao: o operador digita '12.345', o XML traz '12345678000199'."""
    return "".join(c for c in (valor or "") if c.isdigit())


def _cnpj_casa(cnpj_do_item, filtro: str) -> bool:
    """Espelha o filtro da tela (substring, sem pontuacao).

    O campo da tela e uma busca ("Buscar CNPJ...") e filtra a tabela com
    String.includes(). Igualdade exata aqui fazia o lote nao casar nada quando o
    operador digitava CNPJ parcial — e ainda respondia sucesso com 0 regras.
    """
    return _so_digitos(filtro) in _so_digitos(cnpj_do_item)


def _js(valor) -> str:
    """Literal JS seguro para interpolar em <script>.

    Esta resposta e montada por f-string, fora do Jinja, entao o autoescape do
    projeto nao cobre este ponto: sem isso, uma aspa no CNPJ digitado quebra a
    string e injeta script.
    """
    literal = json.dumps("" if valor is None else str(valor))
    return literal.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _aviso_lote(mensagem: str) -> HTMLResponse:
    """Recusa visivel do lote. Nunca verde quando nada foi gravado."""
    return HTMLResponse(
        "<div class='bg-amber-50 text-amber-800 p-3 rounded mb-4 font-bold border border-amber-200'>"
        "<i class='fa-solid fa-triangle-exclamation'></i> " + html.escape(mensagem) +
        "</div>"
    )


@router.post("/api/mapping/cfop/bulk", response_class=HTMLResponse)
def map_cfop_bulk(
    request: Request,
    task_id: str = Form(...),
    tenant_id: str = Form(...),
    cnpj_emitente: Optional[str] = Form(None),
    current_user = Depends(get_current_user),
    user_email: str = Depends(get_current_user_email),
    db_session: Session = Depends(get_db)
):
    """ Aprova todas as sugestões válidas da Task em lote. """
    try:
        # === VALIDAÇÃO ZERO TRUST (RBAC) — MANDATÓRIA ===
        if not current_user:
            raise HTTPException(status_code=401, detail="Autenticação necessária.")
        user_profile = db_session.query(UserProfile).filter(UserProfile.id == current_user.id).first()
        if not user_profile:
            raise HTTPException(status_code=403, detail="Perfil não encontrado.")
        if user_profile.role == 'operador':
            # Operador só opera nas empresas vinculadas a ele (tabela user_tenants)
            allowed = [str(t.id) for t in user_profile.allowed_tenants]
            if tenant_id not in allowed:
                raise HTTPException(status_code=403, detail="Acesso negado para este cliente.")
        elif user_profile.role != 'admin':
            # Allow-list: qualquer papel não-admin fica travado no próprio tenant
            tenant_id = str(user_profile.tenant_id)
                
        # Status da regra com base na role
        rule_status = rule_status_for(user_profile)
                
        task = db_session.query(AuditTask).filter(AuditTask.id == task_id).first()
        if not task or not task.result_data:
            return HTMLResponse("<div class='alert alert-error'>Dados não encontrados.</div>")
            
        data = json.loads(task.result_data)
        desconhecidos = data.get("cfops_desconhecidos", [])
        
        mapped_count = 0
        
        # OTIMIZAÇÃO N+1
        existing_rules_query = db_session.query(CfopClassification).filter_by(tenant_id=tenant_id).all()
        existing_rules_map = {
            (r.company_cnpj, r.supplier_cnpj, r.supplier_cfop, r.product_code): r 
            for r in existing_rules_query
        }
        new_rules_to_add = []

        filtro_cnpj = (cnpj_emitente or "").strip()

        for item in desconhecidos:
            if filtro_cnpj and not _cnpj_casa(item.get("cnpj_emitente"), filtro_cnpj):
                continue
            cfop = item.get("cfop")
            cfop_sugerido = item.get("cfop_sugerido")
            if cfop and cfop_sugerido:
                company_cnpj = item.get("company_cnpj") or data.get("company_cnpj", "")
                supplier_cnpj = item.get("cnpj_emitente") or item.get("supplier_cnpj", "")
                # Regra unitária: o produto faz parte da chave. Gravar "*" aqui
                # criaria uma regra genérica que o motor nunca encontra — a busca
                # exige casamento exato dos 4 campos.
                product_code = item.get("product_code") or ""
                if not product_code or product_code == "*":
                    continue
                    
                rule_key = (company_cnpj, supplier_cnpj, cfop, product_code)
                existing_rule = existing_rules_map.get(rule_key)
                descricao = (item.get("descricao") or "").strip() or None
                
                if existing_rule:
                    existing_rule.entry_cfop = cfop_sugerido
                    if descricao:
                        existing_rule.product_description = descricao
                    existing_rule.status = rule_status
                    existing_rule.created_by = user_email
                else:
                    new_rule = CfopClassification(
                        tenant_id=tenant_id,
                        company_cnpj=company_cnpj,
                        supplier_cnpj=supplier_cnpj,
                        supplier_cfop=cfop,
                        product_code=product_code,
                        entry_cfop=cfop_sugerido,
                        product_description=descricao,
                        status=rule_status,
                        is_global=False,
                        created_by=user_email
                    )
                    new_rules_to_add.append(new_rule)
                    existing_rules_map[rule_key] = new_rule
                    
                mapped_count += 1
                
        if new_rules_to_add:
            db_session.add_all(new_rules_to_add)
                
        db_session.commit()
        
        if mapped_count == 0:
            return _aviso_lote(
                "Nenhuma sugestao aplicavel encontrada"
                + (" para o CNPJ %s" % filtro_cnpj if filtro_cnpj else "")
                + ". Nada foi gravado."
            )

        return HTMLResponse(f"""
        <div class='alert alert-success mt-4'>Sucesso! {mapped_count} regras mapeadas automaticamente.</div>
        <script>
            setTimeout(() => {{
                const cnpjFiltro = {_js(_so_digitos(filtro_cnpj))};
                document.querySelectorAll('tr.cfop-row').forEach(row => {{
                    const rowCnpj = (row.getAttribute('data-cnpj') || '').replace(/\\D/g, '');
                    if (!cnpjFiltro || rowCnpj.includes(cnpjFiltro)) {{
                        const btn = row.querySelector('.btn-success');
                        if (btn && btn.innerText.includes('Aprovar')) {{
                            const sugeridoMatch = btn.innerText.match(/\\[(.*?)\\]/);
                            if (sugeridoMatch) {{
                                const entry = sugeridoMatch[1];
                                row.innerHTML = `<td colspan="4" class="text-emerald-700 font-bold text-center py-3 bg-emerald-50"><i class="fa-solid fa-check-circle mr-1"></i> Mapeado para ${{entry}}</td>`;
                            }}
                        }}
                    }}
                }});
            }}, 50);
        </script>
        """)
    except Exception:
        logger.exception("Falha ao gravar mapeamento em lote")
        db_session.rollback()
        return HTMLResponse("<div class='alert alert-error mt-4'>Não foi possível salvar a regra. Tente novamente.</div>")

@router.post("/api/mapping/cte_cfop/bulk", response_class=HTMLResponse)
def map_cte_cfop_bulk(
    request: Request,
    task_id: str = Form(...),
    tenant_id: str = Form(...),
    current_user = Depends(get_current_user),
    user_email: str = Depends(get_current_user_email),
    db_session: Session = Depends(get_db)
):
    """ Aprova todas as sugestões válidas de CT-e da Task em lote com isolamento por empresa. """
    try:
        if not current_user:
            raise HTTPException(status_code=401, detail="Autenticação necessária.")
        user_profile = db_session.query(UserProfile).filter(UserProfile.id == current_user.id).first()
        if not user_profile:
            raise HTTPException(status_code=403, detail="Perfil não encontrado.")
        if user_profile.role == 'operador':
            # Operador só opera nas empresas vinculadas a ele (tabela user_tenants)
            allowed = [str(t.id) for t in user_profile.allowed_tenants]
            if tenant_id not in allowed:
                raise HTTPException(status_code=403, detail="Acesso negado para este cliente.")
        elif user_profile.role != 'admin':
            # Allow-list: qualquer papel não-admin fica travado no próprio tenant
            tenant_id = str(user_profile.tenant_id)
                
        # Status da regra com base na role
        rule_status = rule_status_for(user_profile)
                
        task = db_session.query(AuditTask).filter(AuditTask.id == task_id).first()
        if not task or not task.result_data:
            return HTMLResponse("<div class='alert alert-error'>Dados não encontrados.</div>")
            
        data = json.loads(task.result_data)
        cte_items = data.get("unmapped_cte_items", [])
        
        mapped_count = 0
        
        # OTIMIZAÇÃO N+1
        existing_rules_query = db_session.query(CteCfopClassification).filter_by(tenant_id=tenant_id).all()
        existing_rules_map = {
            (r.company_cnpj, r.supplier_cfop, r.emitter_uf): r 
            for r in existing_rules_query
        }
        new_rules_to_add = []

        for item in cte_items:
            cfop = item.get("supplier_cfop")
            emitter_uf = item.get("emitter_uf")
            cfop_sugerido = item.get("current_sped_cfop")
            company_cnpj = item.get("company_cnpj") or data.get("company_cnpj", "*")
            
            if cfop and emitter_uf and cfop_sugerido:
                rule_key = (company_cnpj, cfop, emitter_uf)
                existing_rule = existing_rules_map.get(rule_key)
                
                if existing_rule:
                    existing_rule.entry_cfop = cfop_sugerido
                    existing_rule.status = rule_status
                    existing_rule.created_by = user_email
                else:
                    new_rule = CteCfopClassification(
                        tenant_id=tenant_id,
                        company_cnpj=company_cnpj,
                        supplier_cfop=cfop,
                        emitter_uf=emitter_uf,
                        entry_cfop=cfop_sugerido,
                        status=rule_status,
                        is_global=False,
                        created_by=user_email
                    )
                    new_rules_to_add.append(new_rule)
                    existing_rules_map[rule_key] = new_rule
                    
                mapped_count += 1
                
        if new_rules_to_add:
            db_session.add_all(new_rules_to_add)
                
        db_session.commit()
        return HTMLResponse(f"""
        <div class='alert alert-success mt-4'>Sucesso! {mapped_count} regras de frete (CT-e) salvas.</div>
        <script>
            setTimeout(() => {{
                document.querySelectorAll('tr[id*="-cte-"]').forEach(row => {{
                    const btn = row.querySelector('.btn-success');
                    if (btn && btn.innerText.includes('Aprovar')) {{
                        const sugeridoMatch = btn.innerText.match(/\\[(.*?)\\]/);
                        if (sugeridoMatch) {{
                            const entry = sugeridoMatch[1];
                            row.innerHTML = `<td colspan="5" class="text-emerald-700 font-bold text-center py-3 bg-emerald-50"><i class="fa-solid fa-check-circle mr-1"></i> Mapeado para ${{entry}}</td>`;
                        }}
                    }}
                }});
            }}, 50);
        </script>
        """)
    except Exception:
        logger.exception("Falha ao gravar mapeamento em lote")
        db_session.rollback()
        return HTMLResponse("<div class='alert alert-error mt-4'>Não foi possível salvar a regra. Tente novamente.</div>")

@router.post("/api/mapping/cfop/bulk_by_cfop", response_class=HTMLResponse)
def map_cfop_bulk_by_cfop(
    request: Request,
    task_id: str = Form(...),
    supplier_cfop: str = Form(""),
    entry_cfop: str = Form(""),
    tenant_id: str = Form(...),
    cnpj_emitente: Optional[str] = Form(None),
    current_user = Depends(get_current_user),
    user_email: str = Depends(get_current_user_email),
    db_session: Session = Depends(get_db)
):
    """ Aplica um CFOP de entrada para todos os produtos de um CFOP de origem. """
    try:
        if not current_user:
            raise HTTPException(status_code=401, detail="Autenticação necessária.")
        user_profile = db_session.query(UserProfile).filter(UserProfile.id == current_user.id).first()
        if not user_profile:
            raise HTTPException(status_code=403, detail="Perfil não encontrado.")
        if user_profile.role == 'operador':
            # Operador só opera nas empresas vinculadas a ele (tabela user_tenants)
            allowed = [str(t.id) for t in user_profile.allowed_tenants]
            if tenant_id not in allowed:
                raise HTTPException(status_code=403, detail="Acesso negado para este cliente.")
        elif user_profile.role != 'admin':
            # Allow-list: qualquer papel não-admin fica travado no próprio tenant
            tenant_id = str(user_profile.tenant_id)
                
        # Status da regra com base na role
        rule_status = rule_status_for(user_profile)

        # O CFOP de origem define o lote; o CNPJ apenas o estreita. "Todos os CFOPs
        # (Sem Filtro)" e reset do filtro visual da tabela, nunca um modo de gravacao:
        # um unico CFOP de entrada nao serve para 5102, 5405 e 6102 ao mesmo tempo.
        supplier_cfop = (supplier_cfop or "").strip()
        filtro_cnpj = (cnpj_emitente or "").strip()
        entry_cfop = (entry_cfop or "").strip()
        if not supplier_cfop:
            return _aviso_lote(
                "Selecione um CFOP de origem para aplicar o lote. Nada foi gravado."
            )
        if not entry_cfop:
            return _aviso_lote(
                "Selecione o CFOP de entrada para aplicar o lote. Nada foi gravado."
            )
                
        task = db_session.query(AuditTask).filter(AuditTask.id == task_id).first()
        if not task or not task.result_data:
            return HTMLResponse("<div class='alert alert-error'>Dados não encontrados.</div>")
            
        data = json.loads(task.result_data)
        
        # We need the full granularity from unmapped_items
        unmapped_items = data.get("unmapped_items", [])
        
        mapped_count = 0
        
        # OTIMIZAÇÃO: Busca todas as regras de uma vez para evitar N+1 queries no banco
        existing_rules_query = db_session.query(CfopClassification).filter_by(
            tenant_id=tenant_id, 
            supplier_cfop=supplier_cfop
        ).all()
        
        # A PK da tabela inclui o CFOP de origem. Sem ele na chave do mapa, duas
        # regras de CFOPs diferentes colidiriam na mesma entrada.
        existing_rules_map = {
            (r.company_cnpj, r.supplier_cnpj, r.supplier_cfop, r.product_code): r
            for r in existing_rules_query
        }
        
        new_rules_to_add = []

        ignorados = 0

        for item in unmapped_items:
            if item.get("supplier_cfop") != supplier_cfop:
                continue
            # Espelha o filtro da tela: o campo e uma busca ("Buscar CNPJ..."), entao
            # igualdade exata fazia o lote nao casar nada com CNPJ digitado parcial.
            if filtro_cnpj and not _cnpj_casa(item.get("supplier_cnpj"), filtro_cnpj):
                continue

            # A regra em lote nasce com a mesma chave completa da regra manual. O motor
            # busca (empresa, fornecedor, CFOP de origem, produto) exato e nao tem
            # fallback com curinga: gravar "*" aqui cria regra que nunca casa com nada.
            company_cnpj = (item.get("company_cnpj") or data.get("company_cnpj") or "").strip()
            supplier_cnpj = (item.get("supplier_cnpj") or "").strip()
            product_code = (item.get("product_code") or "").strip()
            if not (company_cnpj and supplier_cnpj and product_code):
                ignorados += 1
                continue
            if "*" in (company_cnpj, supplier_cnpj, product_code):
                ignorados += 1
                continue

            rule_key = (company_cnpj, supplier_cnpj, supplier_cfop, product_code)
            existing_rule = existing_rules_map.get(rule_key)
            descricao = (item.get("product_description") or "").strip()

            if existing_rule:
                existing_rule.entry_cfop = entry_cfop
                if descricao:
                    existing_rule.product_description = descricao
                existing_rule.status = rule_status
                existing_rule.created_by = user_email
            else:
                new_rule = CfopClassification(
                    tenant_id=tenant_id,
                    company_cnpj=company_cnpj,
                    supplier_cnpj=supplier_cnpj,
                    supplier_cfop=supplier_cfop,
                    product_code=product_code,
                    entry_cfop=entry_cfop,
                    product_description=descricao,
                    status=rule_status,
                    is_global=False,
                    created_by=user_email
                )
                new_rules_to_add.append(new_rule)
                existing_rules_map[rule_key] = new_rule

            mapped_count += 1

        if new_rules_to_add:
            db_session.add_all(new_rules_to_add)
            
        db_session.commit()
        
        if mapped_count == 0:
            alvo = "CFOP %s" % supplier_cfop
            if filtro_cnpj:
                alvo += " do CNPJ %s" % filtro_cnpj
            extra = ""
            if ignorados:
                extra = (" %d item(ns) ignorados por nao terem empresa, fornecedor e"
                         " produto completos — regra incompleta nunca casa na auditoria." % ignorados)
            return _aviso_lote("Nenhum item do %s nesta auditoria. Nada foi gravado.%s" % (alvo, extra))

        aviso_ignorados = ""
        if ignorados:
            aviso_ignorados = (
                "<div class='bg-amber-50 text-amber-800 p-2 rounded mb-4 text-sm border border-amber-200'>"
                "%d item(ns) ignorados por nao terem empresa, fornecedor e produto completos."
                "</div>" % ignorados
            )

        return HTMLResponse(f"""
        <div class='bg-emerald-50 text-emerald-700 p-3 rounded mb-4 font-bold border border-emerald-200'>
            <i class='fa-solid fa-check-circle'></i> Sucesso! {mapped_count} regras aplicadas para o CFOP {html.escape(supplier_cfop)} -> {html.escape(entry_cfop)}.
        </div>
        {aviso_ignorados}
        <script>
            setTimeout(() => {{
                const cfopFiltro = {_js(supplier_cfop)};
                const cnpjFiltro = {_js(_so_digitos(filtro_cnpj))};
                const destino = {_js(entry_cfop)};
                document.querySelectorAll('tr.cfop-row').forEach(row => {{
                    const rowCfop = row.getAttribute('data-cfop');
                    const rowCnpj = (row.getAttribute('data-cnpj') || '').replace(/\\D/g, '');
                    if (rowCfop === cfopFiltro && rowCnpj.includes(cnpjFiltro)) {{
                        const td = document.createElement('td');
                        td.colSpan = 4;
                        td.className = 'text-emerald-700 font-bold text-center py-3 bg-emerald-50';
                        td.textContent = 'Mapeado para ' + destino;
                        row.replaceChildren(td);
                    }}
                }});
            }}, 50);
        </script>
        """)
    except Exception:
        logger.exception("Falha ao gravar mapeamento em lote")
        db_session.rollback()
        return HTMLResponse("<div class='alert alert-error mt-4'>Não foi possível salvar a regra. Tente novamente.</div>")


from fastapi.responses import StreamingResponse
from ...services.audit_engine.excel_exporter import generate_excel_from_results

@router.get("/api/export/excel/{task_id}")
def export_excel(
    task_id: str,
    db_session: Session = Depends(get_db),
    profile = Depends(get_profile)
):
    """Gera o relatório Excel com base no result_data da task usando excel_exporter."""
    try:
        # Escopo por tenant (respeita allowed_tenants do operador)
        task = get_task_or_403(db_session, profile, task_id)
        if not task.result_data:
            raise HTTPException(status_code=404, detail="Dados da auditoria não encontrados.")

        data = json.loads(task.result_data)
        
        # Gera o Excel usando o módulo de serviço centralizado
        output = generate_excel_from_results(data)
        
        headers = {
            'Content-Disposition': f'attachment; filename="auditoria_sped_{task_id}.xlsx"'
        }
        return StreamingResponse(output, headers=headers, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar Excel: {str(e)}")


def _get_first_word(name_str: str) -> str:
    import re
    if not name_str:
        return "Fornecedor"
    clean = re.sub(r'^[01]\s*-\s*', '', str(name_str)).strip()
    parts = clean.split()
    return parts[0] if parts else "Fornecedor"


def _linhas_da_categoria(result_data: dict, categoria: dict) -> list:
    """Transforma os itens crus de uma categoria nas linhas do e-mail.

    Deduplica: o motor pode apontar o mesmo item por caminhos diferentes, e o
    cliente não deve receber a mesma frase repetida.
    """
    linhas, vistos = [], set()
    for item in email_catalog.itens_de(result_data, categoria):
        try:
            partes = categoria["formatador"](item)
        except Exception:
            # Um item malformado não pode derrubar o e-mail inteiro.
            logger.warning("Item ignorado na categoria %s", categoria["chave"], exc_info=True)
            continue

        chave_dedup = (partes["num"], partes["emit"], partes["descr"], partes["detalhe"])
        if chave_dedup in vistos:
            continue
        vistos.add(chave_dedup)

        if partes["emit"] and partes["emit"] != "-":
            partes["emit"] = _get_first_word(partes["emit"])
        linhas.append(partes)
    return linhas


def _texto_da_linha(partes: dict, secao: str) -> str:
    rotulo_doc = "Nº CT-e" if secao == "cte" else "Nº NF"
    pedacos = []
    if partes["num"] and partes["num"] != "-":
        pedacos.append(rotulo_doc + " " + partes["num"])
    if partes["emit"] and partes["emit"] != "-":
        pedacos.append(partes["emit"])
    if partes["descr"]:
        pedacos.append('Item "' + partes["descr"] + '"')
    pedacos.append(partes["detalhe"])
    return " - ".join(pedacos)


def _html_da_linha(partes: dict, secao: str, cor_detalhe: str) -> str:
    """Monta a linha em HTML.

    TUDO que vem do documento fiscal (descrição de produto, razão social) passa
    por html.escape: este HTML é montado em f-string, fora do autoescape do
    Jinja, e a descrição do item vem do XML de um fornecedor — é exatamente o
    vetor de XSS armazenado descrito em core/templating.py.
    """
    e = html.escape
    rotulo_doc = "Nº CT-e" if secao == "cte" else "Nº NF"
    pedacos = []
    if partes["num"] and partes["num"] != "-":
        pedacos.append("<b>" + rotulo_doc + " " + e(partes["num"]) + "</b>")
    if partes["emit"] and partes["emit"] != "-":
        pedacos.append(e(partes["emit"]))
    if partes["descr"]:
        pedacos.append('Item "' + e(partes["descr"]) + '"')
    pedacos.append('<span style="color: ' + cor_detalhe + ';">' + e(partes["detalhe"]) + "</span>")
    return '<li style="margin-bottom: 6px;">' + " - ".join(pedacos) + "</li>"


def _montar_secoes_email(result_data: dict, chaves_selecionadas) -> list:
    """Seções do e-mail, na ordem do catálogo, só com o que foi marcado e tem conteúdo."""
    selecionadas = set(chaves_selecionadas or [])
    secoes = []
    for categoria in email_catalog.CATEGORIAS:
        if categoria["chave"] not in selecionadas:
            continue
        linhas = _linhas_da_categoria(result_data, categoria)
        if not linhas:
            continue
        secoes.append({
            "categoria": categoria,
            "linhas": linhas,
            "titulo": "{} ({})".format(categoria["rotulo"].upper(), len(linhas)),
        })
    return secoes


def _build_audit_email_contents(
    task_id: str,
    db_session: Session,
    recipient_name: str,
    period: str,
    incluir_resumo: bool,
    chaves_selecionadas,
    custom_notes: str
):
    task = db_session.query(AuditTask).filter(AuditTask.id == task_id).first()
    tenant_name = "Cliente"
    tenant_cnpj = ""
    if task:
        tenant_name = task.tenant_id or "Cliente"
        try:
            from ...domain.models.auth_models import Tenant
            tenant_obj = db_session.query(Tenant).filter(Tenant.id == task.tenant_id).first()
            if tenant_obj:
                tenant_name = tenant_obj.name
                tenant_cnpj = tenant_obj.cnpj or ""
        except Exception:
            logger.warning("Não foi possível carregar a empresa da auditoria %s", task_id, exc_info=True)

    result_data = {}
    try:
        if task and task.result_data:
            result_data = json.loads(task.result_data)
    except (ValueError, TypeError):
        logger.warning("result_data ilegível na auditoria %s", task_id, exc_info=True)

    subject = "[{}] Synax - Auditoria Fiscal e Divergências Identificadas".format(tenant_name)
    secoes = _montar_secoes_email(result_data, chaves_selecionadas)

    resumo = result_data.get("resumo") or {}
    notas = resumo.get("notas_processadas", 0)
    inconsistencias = resumo.get("inconsistencias", 0)

    cnpj_txt = " (CNPJ: {})".format(tenant_cnpj) if tenant_cnpj else ""
    abertura = email_catalog.TEXTO_ABERTURA.format(
        empresa=tenant_name, cnpj_txt=cnpj_txt, periodo=period or "o período apurado")

    # ---------------- Texto puro ----------------
    partes_txt = ["Prezado(a) {},".format(recipient_name), "", abertura, ""]
    if incluir_resumo:
        partes_txt += [
            "--- RESUMO DA AUDITORIA ---",
            "Documentos verificados: {}".format(notas),
            "Inconsistências identificadas: {}".format(inconsistencias),
            "",
        ]
    for secao in secoes:
        cat = secao["categoria"]
        partes_txt.append("--- {} ---".format(secao["titulo"]))
        partes_txt.append(cat["intro"])
        partes_txt += ["  - " + _texto_da_linha(l, cat["secao"]) for l in secao["linhas"]]
        partes_txt.append("")

    if not secoes and not incluir_resumo:
        partes_txt += ["Nenhum apontamento foi selecionado para este comunicado.", ""]

    if custom_notes and custom_notes.strip():
        partes_txt += ["Observações adicionais:", custom_notes.strip(), ""]

    partes_txt += [email_catalog.TEXTO_FECHAMENTO, "", "Atenciosamente,", email_catalog.ASSINATURA]
    text_body = "\n".join(partes_txt)

    # ---------------- HTML ----------------
    e = html.escape
    blocos_html = []

    if incluir_resumo:
        blocos_html.append(
            '<div style="margin: 20px 0; padding: 14px; background-color: #f8fafc;'
            ' border: 1px solid #e2e8f0; border-radius: 6px;">'
            '<b style="color: #0f172a;">Resumo da auditoria</b><br>'
            "Documentos verificados: <b>" + e(str(notas)) + "</b><br>"
            "Inconsistências identificadas: <b>" + e(str(inconsistencias)) + "</b></div>"
        )

    for secao in secoes:
        cat = secao["categoria"]
        cor = email_catalog.cor_hex(cat)
        itens_html = "".join(_html_da_linha(l, cat["secao"], cor) for l in secao["linhas"])
        tag_info = ""
        if cat["informativo"]:
            tag_info = ('<span style="font-size: 11px; font-weight: normal; color: #64748b;'
                        ' border: 1px solid #cbd5e1; border-radius: 10px; padding: 1px 7px;'
                        ' margin-left: 8px;">informativo</span>')
        blocos_html.append(
            '<div style="margin: 22px 0;">'
            '<h4 style="color: ' + cor + '; margin: 0 0 6px 0; font-size: 14px;'
            ' text-transform: uppercase; border-bottom: 1px solid #e2e8f0; padding-bottom: 6px;">'
            + e(secao["titulo"]) + tag_info + "</h4>"
            '<p style="margin: 8px 0 10px 0; font-size: 13px; color: #475569;">'
            + e(cat["intro"]) + "</p>"
            '<ul style="margin: 0; padding-left: 20px; font-size: 14px; line-height: 1.6;'
            ' color: #334155;">' + itens_html + "</ul></div>"
        )

    if not secoes and not incluir_resumo:
        blocos_html.append(
            '<p style="color: #64748b;">Nenhum apontamento foi selecionado para este comunicado.</p>')

    if custom_notes and custom_notes.strip():
        blocos_html.append(
            '<div style="margin: 20px 0; padding: 12px; background-color: #fffbeb;'
            ' border-left: 4px solid #f59e0b; border-radius: 4px; font-size: 14px;">'
            "<b>Observações adicionais:</b><br>" + e(custom_notes.strip()) + "</div>"
        )

    html_body = (
        '<div style="font-family: Arial, sans-serif; font-size: 14px; color: #1e293b;'
        ' line-height: 1.6; max-width: 750px;">'
        "<p>Prezado(a) " + e(recipient_name) + ",</p>"
        "<p>" + e(abertura) + "</p>"
        + "".join(blocos_html) +
        '<p style="margin-top: 25px;">' + e(email_catalog.TEXTO_FECHAMENTO) + "</p>"
        '<p style="margin-top: 20px;">Atenciosamente,<br><b>'
        + e(email_catalog.ASSINATURA) + "</b></p></div>"
    )

    return subject, html_body, text_body.strip(), tenant_name, period


@router.post("/api/audit/{task_id}/email-generate", response_class=HTMLResponse)
def generate_audit_client_email(
    request: Request,
    task_id: str,
    recipient_name: str = Form("Responsável Fiscal / Contabilidade"),
    period: str = Form("Período Apurado"),
    # Uma lista em vez de um booleano por categoria: o catálogo já tem 19
    # apontamentos e cresce; um `include_*` para cada um obrigaria a mexer na
    # assinatura da rota toda vez que o motor ganhasse uma verificação nova.
    incluir: List[str] = Form(default=[]),
    incluir_resumo: bool = Form(False),
    custom_notes: str = Form(""),
    db_session: Session = Depends(get_db),
    profile = Depends(get_profile)
):
    # Ferramenta interna do escritório: o cliente final não gera este e-mail.
    # Esconder só o botão deixaria a rota acessível por chamada direta.
    assert_internal(profile)
    # Escopo por tenant: o corpo do e-mail contém os apontamentos completos da auditoria.
    get_task_or_403(db_session, profile, task_id)

    # Só chaves que existem no catálogo entram: um valor forjado no formulário
    # não deve virar seção vazia nem KeyError.
    chaves = [c for c in incluir if c in email_catalog.POR_CHAVE]

    subject, html_body, text_body, tenant_name, period = _build_audit_email_contents(
        task_id, db_session, recipient_name, period,
        incluir_resumo, chaves, custom_notes
    )
    return templates.TemplateResponse(request, "partials/admin/email_preview.html", {
        "request": request,
        "task_id": task_id,
        "subject": subject,
        "html_body": html_body,
        "text_body": text_body,
        "tenant_name": tenant_name,
        "period": period
    })

