from fastapi import APIRouter, Depends

from app.api.deps import get_current_user_id
from app.core.exceptions import NotFoundError
from app.db import mongo
from app.services.graph_service import get_graph_repository

router = APIRouter(prefix="/graph", tags=["graph"])


@router.get("")
async def get_graph(repo_full_name: str, user_id: str = Depends(get_current_user_id)) -> dict:
    repo = await mongo.repos().find_one({"user_id": user_id, "repo_full_name": repo_full_name})
    if repo is None:
        raise NotFoundError("repository is not connected", error_code="repo_not_connected")

    graph = await get_graph_repository().fetch_graph(repo_full_name)
    return {
        "repo_full_name": repo_full_name,
        "parse_status": repo["parse_status"],
        **graph,
    }
