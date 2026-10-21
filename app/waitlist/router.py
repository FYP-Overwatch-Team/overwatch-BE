from fastapi import BackgroundTasks, Depends

from app.api.deps import client_ip, same_origin
from app.core.responses import api_router
from app.waitlist import service
from app.waitlist.schemas import JoinWaitlist, WaitlistJoined

router = api_router(tags=["waitlist"])


@router.post("/waitlist", status_code=202, dependencies=[Depends(same_origin)])
async def join_waitlist(
    body: JoinWaitlist, background: BackgroundTasks, ip: str = Depends(client_ip),
) -> WaitlistJoined:
    """Join the waitlist; someone already on it is told so, and gets no second email."""
    added = await service.join(background, ip, body.name, body.email, body.heard_from)
    return WaitlistJoined(already_joined=not added)
