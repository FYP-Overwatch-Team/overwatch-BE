from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel

from app.api.deps import get_current_user_id
from app.db import mongo
from app.integrations.github_client import get_github_client
from app.services import repo_service
from app.workers import jobs

router = APIRouter(prefix="/github", tags=["github"])


class ConnectRepoRequest(BaseModel):
    repo_full_name: str


@router.get("/repos")
async def list_repos(user_id: str = Depends(get_current_user_id)) -> dict:
    token = await repo_service.get_github_token(user_id)
    repos = await get_github_client().list_repos(token)
    return {
        "repos": [
            {
                "full_name": r["full_name"],
                "private": r.get("private", False),
                "default_branch": r.get("default_branch"),
                "admin": r.get("permissions", {}).get("admin", False),
                "updated_at": r.get("updated_at"),
            }
            for r in repos
        ]
    }


@router.post("/repos/connect", status_code=201)
async def connect_repo(
    body: ConnectRepoRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user_id),
) -> dict:
    doc = await repo_service.connect_repo(user_id, body.repo_full_name)
    if doc["parse_status"] == "pending":
        jobs.enqueue_initial_parse(background_tasks, user_id, body.repo_full_name)
    return {
        "repo_full_name": doc["repo_full_name"],
        "default_branch": doc["default_branch"],
        "webhook_status": doc["webhook_status"],
        "parse_status": doc["parse_status"],
    }


@router.get("/repos/connected")
async def connected_repos(user_id: str = Depends(get_current_user_id)) -> dict:
    docs = [d async for d in mongo.repos().find({"user_id": user_id})]
    return {
        "repos": [
            {
                "repo_full_name": d["repo_full_name"],
                "default_branch": d["default_branch"],
                "webhook_status": d["webhook_status"],
                "parse_status": d["parse_status"],
                "parse_error": d.get("parse_error"),
            }
            for d in docs
        ]
    }
