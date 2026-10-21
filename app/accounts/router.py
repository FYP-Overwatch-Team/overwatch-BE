from fastapi import BackgroundTasks, Depends

from app.accounts import service
from app.accounts.schemas import ConfirmMerge, MergePreview
from app.api.deps import client_ip, get_current_user, same_origin
from app.auth.schemas import Me
from app.core.responses import api_router

router = api_router(tags=["accounts"])


@router.get("/me/merge")
async def pending_merge(user: dict = Depends(get_current_user)) -> MergePreview:
    """The merge the signed-in account has proven and not yet confirmed."""
    return await service.pending_preview(user)


@router.post("/me/merge", dependencies=[Depends(same_origin)])
async def confirm_merge(
    body: ConfirmMerge, background: BackgroundTasks, ip: str = Depends(client_ip), user: dict = Depends(get_current_user),
) -> Me:
    """Merge the proven account into this one; the other account is signed out everywhere and removed."""
    return Me.of(await service.confirm(background, user, body.keep_profile, ip))


@router.delete("/me/merge", dependencies=[Depends(same_origin)])
async def cancel_merge(user: dict = Depends(get_current_user)) -> None:
    await service.cancel(user)
