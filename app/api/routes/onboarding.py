from fastapi import APIRouter, Depends

from app.api.deps import get_current_user_id
from app.db import mongo

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


@router.get("/status")
async def onboarding_status(user_id: str = Depends(get_current_user_id)) -> dict:
    """Cheap polling endpoint: Mongo reads only, never touches GitHub/Jira/Neo4j.

    GitHub and Jira are reported independently — the dashboard must not
    hard-block on both being connected.
    """
    github_token = await mongo.oauth_tokens().find_one({"user_id": user_id, "provider": "github"})
    jira_token = await mongo.oauth_tokens().find_one({"user_id": user_id, "provider": "jira"})

    latest_repo = None
    async for doc in mongo.repos().find({"user_id": user_id}).sort("connected_at", -1).limit(1):
        latest_repo = doc

    latest_project = None
    async for doc in mongo.jira_projects().find({"user_id": user_id}).sort("connected_at", -1).limit(1):
        latest_project = doc

    return {
        "github": {
            "connected": bool(github_token) and github_token.get("status") == "active",
            "needs_reauth": bool(github_token) and github_token.get("status") == "needs_reauth",
            "repo_connected": latest_repo is not None,
            "parse_status": latest_repo["parse_status"] if latest_repo else None,
            "webhook_status": latest_repo["webhook_status"] if latest_repo else None,
        },
        "jira": {
            "connected": bool(jira_token) and jira_token.get("status") == "active",
            "needs_reauth": bool(jira_token) and jira_token.get("status") == "needs_reauth",
            "project_connected": latest_project is not None,
            "sync_status": latest_project["sync_status"] if latest_project else None,
        },
    }
