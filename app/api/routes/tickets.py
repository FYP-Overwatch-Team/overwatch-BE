from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_user_id
from app.services import ticket_service

router = APIRouter(prefix="/tickets", tags=["tickets"])


@router.get("")
async def list_tickets(
    project_key: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user_id: str = Depends(get_current_user_id),
) -> dict:
    return await ticket_service.list_tickets(
        user_id, project_key=project_key, status=status, assignee=assignee,
        page=page, page_size=page_size,
    )
