from fastapi import Depends

from app.api.deps import get_current_user_id
from app.core.responses import api_router
from app.db import oauth_tokens
from app.github import repository as repos
from app.jira import repository as jira_projects
from app.onboarding.schemas import GithubStatus, JiraProjectStatus, JiraStatus, OnboardingStatus

router = api_router(prefix="/onboarding", tags=["onboarding"])


@router.get("/status")
async def onboarding_status(user_id: str = Depends(get_current_user_id)) -> OnboardingStatus:
    """Cheap to poll: Mongo reads only, never GitHub, Jira or Neo4j."""
    github_token = await oauth_tokens.get(user_id, "github")
    jira_token = await oauth_tokens.get(user_id, "jira")
    connected = await repos.list_for_user(user_id)
    project = await jira_projects.latest_active(user_id)

    return OnboardingStatus(
        github=GithubStatus(
            authorized=_status(github_token) == "active",
            needs_reauth=_status(github_token) == "needs_reauth",
            repos=connected,
        ),
        jira=JiraStatus(
            connected=_status(jira_token) == "active",
            needs_reauth=_status(jira_token) == "needs_reauth",
            project=project and JiraProjectStatus(
                key=project["project_key"], sync_status=project["sync_status"], sync_error=project.get("sync_error"),
            ),
        ),
        ready=any(
            repo["parse_status"] == "done" and repo.get("access_status", "active") == "active"
            for repo in connected
        ),
    )


def _status(token: dict | None) -> str | None:
    return token.get("status") if token else None
