"""Janela de novidades da tela principal (ver `services/novidades.py`).

A tela principal pede a janela por htmx ao carregar; sem novidade pendente a
resposta é vazia e nada aparece. Qualquer papel logado usa — o filtro do que
cada um vê está no serviço.
"""
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ...core.authz import get_profile
from ...core.templating import build_templates
from ...infrastructure.database.session import get_db
from ...services import novidades

logger = logging.getLogger(__name__)
router = APIRouter()
templates = build_templates()


@router.get("/api/novidades", response_class=HTMLResponse)
def janela(request: Request, profile=Depends(get_profile)):
    itens = novidades.pendentes(profile.role or "", profile.novidades_vistas_ate)
    if not itens:
        return HTMLResponse("")
    return templates.TemplateResponse(
        request, "partials/novidades.html", {"request": request, "itens": itens})


@router.post("/api/novidades/vistas", response_class=HTMLResponse)
def marcar_vistas(db_session: Session = Depends(get_db),
                  profile=Depends(get_profile)):
    """Marca como visto até a novidade mais recente do papel.

    Falha ao gravar não derruba nada: a janela só volta a aparecer na próxima
    entrada, que é o pior caso aceitável para um aviso.
    """
    ultima = novidades.mais_recente(profile.role or "")
    if ultima and (profile.novidades_vistas_ate or "") < ultima:
        try:
            profile.novidades_vistas_ate = ultima
            db_session.commit()
        except Exception:                                 # noqa: BLE001
            db_session.rollback()
            logger.warning("Não foi possível marcar as novidades como vistas",
                           exc_info=True)
    return HTMLResponse("")
