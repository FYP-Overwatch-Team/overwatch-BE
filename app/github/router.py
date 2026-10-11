from fastapi import BackgroundTasks, Depends

from app.api.deps import get_current_user_id, workspace_member
from app.core.responses import api_router
from app.github import repository, service
from app.github.schemas import AvailableRepo, ConnectedRepo, ConnectRepoRequest, Installations
from app.workers import jobs

router = api_router(prefix="/github", tags=["github"])


@router.get("/installations")
async def list_installations(user_id: str = Depends(get_current_user_id)) -> Installations:
    return await service.list_installations(user_id)


@router.get("/repos")
async def list_repos(member: dict = Depends(workspace_member())) -> list[AvailableRepo]:
    return await service.list_repos(member["user_id"], member["workspace_id"])


@router.post("/repos/connect", status_code=201)
async def connect_repo(
    body: ConnectRepoRequest, background_tasks: BackgroundTasks, member: dict = Depends(workspace_member("admin")),
) -> ConnectedRepo:
    repo = await service.connect(member["user_id"], member["workspace_id"], body.repo_full_name)
    background_tasks.add_task(jobs.run_index, repo["repo_full_name"], full=True)
    return repo


@router.get("/repos/connected")
async def connected_repos(member: dict = Depends(workspace_member())) -> list[ConnectedRepo]:
    return await repository.list_for_workspace(member["workspace_id"])


@router.post("/repos/{owner}/{name}/index", status_code=202)
async def reindex_repo(
    owner: str, name: str, background_tasks: BackgroundTasks, member: dict = Depends(workspace_member()),
) -> ConnectedRepo:
    repo = await service.request_index(member["workspace_id"], f"{owner}/{name}")
    background_tasks.add_task(jobs.run_index, repo["repo_full_name"], full=True)
    return repo


@router.delete("/repos/{owner}/{name}")
async def disconnect_repo(owner: str, name: str, member: dict = Depends(workspace_member("admin"))) -> None:
    await service.disconnect(member["workspace_id"], f"{owner}/{name}")
