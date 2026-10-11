from typing import Any

from pydantic import BaseModel

from app.workspaces.schemas import Role


class OnboardingRepo(BaseModel):
    repo_full_name: str
    graph_stats: dict[str, Any] | None = None
    parse_status: str
    parse_error: str | None = None
    access_status: str = "active"


class GithubStatus(BaseModel):
    authorized: bool
    needs_reauth: bool
    repos: list[OnboardingRepo]


class JiraProjectStatus(BaseModel):
    key: str
    sync_status: str
    sync_error: str | None = None


class JiraStatus(BaseModel):
    connected: bool
    needs_reauth: bool
    project: JiraProjectStatus | None


class OnboardingStatus(BaseModel):
    role: Role  # only admins connect repos and Jira
    github: GithubStatus
    jira: JiraStatus
    ready: bool  # at least one reachable repo is indexed; Jira is optional
