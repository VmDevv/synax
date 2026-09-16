from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ...infrastructure.database.session import get_db
from ...core.authz import get_profile, get_task_or_403

router = APIRouter()
from ...core.templating import build_templates
templates = build_templates()

@router.get("/api/audit/status/{task_id}", response_class=HTMLResponse)
def get_audit_status(
    request: Request,
    task_id: str,
    db_session = Depends(get_db),
    profile = Depends(get_profile)
):
    # Escopo por tenant: o ramo "concluido" devolve o relatório completo da auditoria.
    task = get_task_or_403(db_session, profile, task_id)

    if task.status in ["pendente", "processando"]:
        # Continua o polling retornando a barra de progresso
        return templates.TemplateResponse(request, "components/progress_bar.html", 
            {"request": request, "task_id": task_id}
        )
        
    elif task.status == "erro":
        return HTMLResponse(f"<div class='bg-red-50 text-red-600 p-4 rounded-lg'>Erro na auditoria: {task.error_message}</div>")
        
    elif task.status == "concluido":
        # Processamento finalizado! Retornamos o fragmento HTMX contendo o Relatório Interativo.
        import json
        data = json.loads(task.result_data)
        return templates.TemplateResponse(request, "components/audit_report.html", 
            {"request": request, "data": data}
        )
