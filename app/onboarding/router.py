from fastapi import Depends

from app.api.deps import workspace_member
from app.core.responses import api_router
from app.db import oauth_tokens
from app.github import repository as repos
from app.jira import repository as jira_projects
from app.onboarding.schemas import GithubStatus, JiraProjectStatus, JiraStatus, OnboardingStatus

router = api_router(prefix="/onboarding", tags=["onboarding"])


@router.get("/status")
async def onboarding_status(member: dict = Depends(workspace_member())) -> OnboardingStatus:
    """Cheap to poll: Mongo reads only, never GitHub, Jira or Neo4j.

    GitHub sign-in is the user's; repos and Jira are the workspace's.
    """
    workspace_id = member["workspace_id"]
    github_token = await oauth_tokens.get(member["user_id"], "github")
    jira_token = await oauth_tokens.get(workspace_id, "jira")
    connected = await repos.list_for_workspace(workspace_id)
    project = await jira_projects.latest_active(workspace_id)

    return OnboardingStatus(
        role=member["role"],
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
