from fastapi import Depends, Query

from app.api.deps import workspace_member
from app.core.responses import api_router
from app.usage import service
from app.usage.schemas import UsageSummary

router = api_router(prefix="/usage", tags=["usage"])


@router.get("")
async def get_usage(days: int = Query(30, ge=1, le=90), member: dict = Depends(workspace_member())) -> UsageSummary:
    """The current workspace's AI usage over the last `days`."""
    return await service.summary(member["workspace_id"], days)
