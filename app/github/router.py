from fastapi import BackgroundTasks, Depends

from app.api.deps import get_current_user_id
from app.core.responses import api_router
from app.github import repository, service
from app.github.schemas import AvailableRepo, ConnectedRepo, ConnectRepoRequest, Installations
from app.workers import jobs

router = api_router(prefix="/github", tags=["github"])


@router.get("/installations")
async def list_installations(user_id: str = Depends(get_current_user_id)) -> Installations:
    return await service.list_installations(user_id)


@router.get("/repos")
async def list_repos(user_id: str = Depends(get_current_user_id)) -> list[AvailableRepo]:
    return await service.list_repos(user_id)


@router.post("/repos/connect", status_code=201)
async def connect_repo(
    body: ConnectRepoRequest, background_tasks: BackgroundTasks, user_id: str = Depends(get_current_user_id),
) -> ConnectedRepo:
    repo = await service.connect(user_id, body.repo_full_name)
    background_tasks.add_task(jobs.run_index, user_id, repo["repo_full_name"], full=True)
    return repo


@router.get("/repos/connected")
async def connected_repos(user_id: str = Depends(get_current_user_id)) -> list[ConnectedRepo]:
    return await repository.list_for_user(user_id)


@router.post("/repos/{owner}/{name}/index", status_code=202)
async def reindex_repo(
    owner: str, name: str, background_tasks: BackgroundTasks, user_id: str = Depends(get_current_user_id),
) -> ConnectedRepo:
    repo = await service.request_index(user_id, f"{owner}/{name}")
    background_tasks.add_task(jobs.run_index, user_id, repo["repo_full_name"], full=True)
    return repo


@router.delete("/repos/{owner}/{name}")
async def disconnect_repo(owner: str, name: str, user_id: str = Depends(get_current_user_id)) -> None:
    await service.disconnect(user_id, f"{owner}/{name}")
