from fastapi import Depends

from app.api.deps import workspace_member
from app.comments import service
from app.comments.schemas import Comment, CommentBody
from app.core.responses import api_router

router = api_router(prefix="/work-items/{item_id}/comments", tags=["comments"])


@router.get("")
async def list_comments(item_id: str, member: dict = Depends(workspace_member())) -> list[Comment]:
    return await service.list_comments(member, item_id)


@router.post("", status_code=201)
async def add_comment(item_id: str, body: CommentBody, member: dict = Depends(workspace_member())) -> Comment:
    return await service.add(member, item_id, body.body)


@router.patch("/{comment_id}")
async def update_comment(item_id: str, comment_id: str, body: CommentBody, member: dict = Depends(workspace_member())) -> Comment:
    return await service.update(member, item_id, comment_id, body.body)


@router.delete("/{comment_id}")
async def delete_comment(item_id: str, comment_id: str, member: dict = Depends(workspace_member())) -> None:
    await service.delete(member, item_id, comment_id)
