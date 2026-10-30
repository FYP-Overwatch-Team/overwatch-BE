from fastapi import Depends, Path, Query

from app.api.deps import workspace_member
from app.core.responses import WithMeta, api_router
from app.decision_comments import service
from app.decision_comments.schemas import (
    EMOJI,
    DecisionComment,
    EditDecisionComment,
    NewDecisionComment,
)

router = api_router(prefix="/decisions/{decision_id}/comments", tags=["decision comments"])


@router.get("")
async def list_comments(
    decision_id: str,
    #: the oldest message already shown: the page is the ones before it
    before: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    member: dict = Depends(workspace_member()),
) -> WithMeta[list[DecisionComment]]:
    """A page of the discussion, oldest first; `meta.has_more` says whether older messages remain."""
    comments, has_more = await service.list_page(member, decision_id, before, limit)
    return WithMeta(comments, {"has_more": has_more, "limit": limit})


@router.post("", status_code=201)
async def add_comment(decision_id: str, body: NewDecisionComment, member: dict = Depends(workspace_member())) -> DecisionComment:
    return await service.add(member, decision_id, body.client_id, body.body, body.attachment_ids)


@router.patch("/{comment_id}")
async def edit_comment(
    decision_id: str, comment_id: str, body: EditDecisionComment, member: dict = Depends(workspace_member()),
) -> DecisionComment:
    return await service.edit(member, decision_id, comment_id, body.body)


@router.delete("/{comment_id}")
async def delete_comment(decision_id: str, comment_id: str, member: dict = Depends(workspace_member())) -> None:
    await service.delete(member, decision_id, comment_id)


@router.put("/{comment_id}/reactions/{emoji}")
async def react(
    decision_id: str, comment_id: str, emoji: str = Path(pattern=EMOJI), member: dict = Depends(workspace_member()),
) -> DecisionComment:
    """Add the caller's reaction; doing it again changes nothing."""
    return await service.react(member, decision_id, comment_id, emoji)


@router.delete("/{comment_id}/reactions/{emoji}")
async def unreact(
    decision_id: str, comment_id: str, emoji: str = Path(pattern=EMOJI), member: dict = Depends(workspace_member()),
) -> DecisionComment:
    """Take the caller's reaction off; not there, nothing changes."""
    return await service.unreact(member, decision_id, comment_id, emoji)
