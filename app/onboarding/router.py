from fastapi import Depends

from app.api.deps import workspace_member
from app.connectors import repository as connectors
from app.connectors.service import auth_status
from app.core.constants import AccessStatus, ConnectionStatus, JobStatus, Provider
from app.core.responses import api_router
from app.db import oauth_tokens
from app.github import repository as repos
from app.onboarding.schemas import GithubStatus, JiraProjectStatus, JiraStatus, OnboardingStatus

router = api_router(prefix="/onboarding", tags=["onboarding"])


@router.get("/status")
async def onboarding_status(member: dict = Depends(workspace_member())) -> OnboardingStatus:
    """Cheap to poll: Mongo reads only, never GitHub, Jira or Neo4j.

    GitHub sign-in is the user's; repos and Jira are the workspace's.
    """
    workspace_id = member["workspace_id"]
    github_token = await oauth_tokens.get(member["user_id"], Provider.GITHUB)
    jira_status = await auth_status(await connectors.get_connection(workspace_id, Provider.JIRA))
    connected = await repos.list_for_workspace(workspace_id)
    project = await connectors.latest_target(workspace_id, Provider.JIRA)

    return OnboardingStatus(
        role=member["role"],
        github=GithubStatus(
            authorized=_status(github_token) == ConnectionStatus.ACTIVE,
            needs_reauth=_status(github_token) == ConnectionStatus.NEEDS_REAUTH,
            repos=connected,
        ),
        jira=JiraStatus(
            connected=jira_status == ConnectionStatus.ACTIVE,
            needs_reauth=jira_status == ConnectionStatus.NEEDS_REAUTH,
            project=project and JiraProjectStatus(
                key=project["key"], sync_status=project["sync_status"], sync_error=project.get("sync_error"),
            ),
        ),
        ready=any(
            repo["parse_status"] == JobStatus.DONE and repo.get("access_status", AccessStatus.ACTIVE) == AccessStatus.ACTIVE
            for repo in connected
        ),
    )


def _status(token: dict | None) -> str | None:
    return token.get("status") if token else None
