from fastapi import Depends, Query

from app.api.deps import workspace_member
from app.core.responses import WithMeta, api_router
from app.services import ticket_service

router = api_router(prefix="/tickets", tags=["tickets"])


@router.get("")
async def list_tickets(
    project_key: str | None = None,
    status: str | None = None,
    assignee: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    member: dict = Depends(workspace_member()),
) -> WithMeta[list[dict]]:
    result = await ticket_service.list_tickets(
        member["workspace_id"], project_key=project_key, status=status, assignee=assignee,
        page=page, page_size=page_size,
    )
    return WithMeta(result["tickets"], {"total": result["total"], "page": result["page"], "page_size": result["page_size"]})
