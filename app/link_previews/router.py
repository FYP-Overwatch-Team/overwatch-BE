from fastapi import Depends, Query

from app.api.deps import workspace_member
from app.core.responses import api_router
from app.link_previews import service
from app.link_previews.schemas import LinkPreview

router = api_router(prefix="/link-previews", tags=["link previews"])


@router.get("")
async def get_preview(url: str = Query(max_length=service.MAX_URL), _member: dict = Depends(workspace_member())) -> LinkPreview:
    """A link's title, description, icon and picture, for showing it as a card in a page."""
    return await service.preview(url)
