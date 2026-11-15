from fastapi import Depends

from app.api.deps import workspace_member
from app.core.responses import api_router
from app.sprints import service
from app.sprints.schemas import CompleteSprint, CreateSprint, Sprint, StartSprint, UpdateSprint

router = api_router(tags=["sprints"])


@router.get("/boards/{board_id}/sprints")
async def list_sprints(board_id: str, member: dict = Depends(workspace_member())) -> list[Sprint]:
    """The board's open sprints: the active one first, then the ones planned."""
    return await service.list_sprints(member["workspace_id"], board_id)


@router.post("/boards/{board_id}/sprints", status_code=201)
async def create_sprint(board_id: str, body: CreateSprint, member: dict = Depends(workspace_member())) -> Sprint:
    return await service.create(member, board_id, body)


@router.patch("/sprints/{sprint_id}")
async def update_sprint(sprint_id: str, body: UpdateSprint, member: dict = Depends(workspace_member())) -> Sprint:
    return await service.update(member, sprint_id, body)


@router.post("/sprints/{sprint_id}/start")
async def start_sprint(sprint_id: str, body: StartSprint, member: dict = Depends(workspace_member())) -> Sprint:
    return await service.start(member, sprint_id, body)


@router.post("/sprints/{sprint_id}/complete")
async def complete_sprint(sprint_id: str, body: CompleteSprint, member: dict = Depends(workspace_member())) -> Sprint:
    return await service.complete(member, sprint_id, body)
