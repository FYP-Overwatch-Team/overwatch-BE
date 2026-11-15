from fastapi import Depends, Query

from app.api.deps import workspace_member
from app.core.responses import WithMeta, api_router
from app.documents import service
from app.documents.schemas import (
    ArchiveDocument,
    ArchivedDocument,
    CreateDocument,
    Document,
    DocumentSummary,
    ResolveConflict,
    UpdateDocument,
)

# not /docs: that's the API reference
router = api_router(prefix="/documents", tags=["documents"])


@router.get("")
async def list_children(
    # the tree's root when omitted; the tree loads one level at a time
    parent_id: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    member: dict = Depends(workspace_member()),
) -> WithMeta[list[DocumentSummary]]:
    nodes, total = await service.children(member["workspace_id"], parent_id, offset, limit)
    return WithMeta(nodes, {"total": total, "offset": offset, "limit": limit})


@router.get("/archived")
async def list_archived(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    member: dict = Depends(workspace_member()),
) -> WithMeta[list[ArchivedDocument]]:
    """What was archived, newest first: each archive once, with how much went with it."""
    nodes, total = await service.archived(member["workspace_id"], offset, limit)
    return WithMeta(nodes, {"total": total, "offset": offset, "limit": limit})


@router.post("", status_code=201)
async def create_document(body: CreateDocument, member: dict = Depends(workspace_member())) -> Document:
    return await service.create(member, body)


@router.get("/{node_id}")
async def get_document(node_id: str, member: dict = Depends(workspace_member())) -> Document:
    return await service.get(member["workspace_id"], node_id)


@router.patch("/{node_id}")
async def update_document(node_id: str, body: UpdateDocument, member: dict = Depends(workspace_member())) -> Document:
    return await service.update(member, node_id, body)


@router.post("/{node_id}/archive")
async def archive_document(node_id: str, body: ArchiveDocument, member: dict = Depends(workspace_member())) -> None:
    await service.archive(member, node_id, body)


@router.post("/{node_id}/restore")
async def restore_document(node_id: str, member: dict = Depends(workspace_member())) -> Document:
    return await service.restore(member, node_id)


@router.post("/{node_id}/resolve-conflict")
async def resolve_conflict(node_id: str, body: ResolveConflict, member: dict = Depends(workspace_member())) -> Document:
    return await service.resolve_conflict(member, node_id, body)


@router.delete("/{node_id}")
async def delete_document(node_id: str, member: dict = Depends(workspace_member())) -> None:
    """Delete an archive for good; only what's in the Archive can be deleted."""
    await service.delete_archived(member, node_id)
