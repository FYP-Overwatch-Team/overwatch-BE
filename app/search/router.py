from fastapi import Depends, Query

from app.api.deps import workspace_member
from app.core.responses import api_router
from app.search import service
from app.search.schemas import SearchResult

router = api_router(prefix="/search", tags=["search"])


@router.get("")
async def search(
    q: str = Query(min_length=2, max_length=200),
    # per kind: up to this many board items, as many pages, and as many decisions
    limit: int = Query(6, ge=1, le=20),
    member: dict = Depends(workspace_member()),
) -> list[SearchResult]:
    """Board items by key, title or description, docs pages by title or text, and decisions, for the ⌘K menu."""
    return await service.search(member["workspace_id"], q.strip(), limit)
