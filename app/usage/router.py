from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, Query

from app.api.deps import workspace_member
from app.core.responses import api_router
from app.usage import service
from app.usage.schemas import UsageLimits, UsageSummary

router = api_router(prefix="/usage", tags=["usage"])


def _zone(name: str) -> ZoneInfo:
    """The IANA time zone asked for (the browser's), or UTC when it isn't one."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


@router.get("")
async def get_usage(
    days: int = Query(30, ge=1, le=90),
    tz: str = Query("UTC", max_length=64, description="IANA time zone the days are counted in, e.g. Asia/Karachi"),
    member: dict = Depends(workspace_member()),
) -> UsageSummary:
    """The current workspace's AI usage over the last `days`, today included."""
    return await service.summary(member["workspace_id"], days, _zone(tz))


@router.get("/limits")
async def get_limits(member: dict = Depends(workspace_member())) -> UsageLimits:
    """How much of each AI limit the workspace, and the member this hour, has used."""
    return await service.limits(member["workspace_id"], member["user_id"])
