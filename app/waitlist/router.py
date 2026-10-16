from fastapi import BackgroundTasks, Depends

from app.api.deps import client_ip, same_origin
from app.core.responses import api_router
from app.waitlist import service
from app.waitlist.schemas import JoinWaitlist

router = api_router(tags=["waitlist"])


@router.post("/waitlist", status_code=202, dependencies=[Depends(same_origin)])
async def join_waitlist(body: JoinWaitlist, background: BackgroundTasks, ip: str = Depends(client_ip)) -> None:
    """Join the waitlist; someone already on it gets the same answer and no second email."""
    await service.join(background, ip, body.name, body.email, body.heard_from)
