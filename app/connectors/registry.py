"""Every connector Overwatch supports; adding a provider is an adapter plus an entry here."""

from dataclasses import dataclass, field

from app.connectors import jira
from app.connectors.auth_backends import NativeOAuth
from app.connectors.oauth import OAuth2Flow, OAuthConfig
from app.connectors.ports import AuthBackend, WorkItemSource
from app.core.constants import AuthBackendName, Provider
from app.core.exceptions import NotFoundError


@dataclass(frozen=True)
class Connector:
    provider: str
    name: str
    oauth: OAuthConfig
    adapter: WorkItemSource
    # the scope that lets board changes be written back; older grants may lack it
    write_scope: str | None
    # the scope that lets the tracker's boards and sprints be read and changed; None if it has no sprints
    sprint_scope: str | None = None
    flow: OAuth2Flow = field(init=False)
    backends: dict[str, AuthBackend] = field(init=False)

    def __post_init__(self) -> None:
        flow = OAuth2Flow(self.oauth)
        object.__setattr__(self, "flow", flow)
        object.__setattr__(self, "backends", {AuthBackendName.NATIVE_OAUTH: NativeOAuth(flow)})


CONNECTORS = {
    Provider.JIRA: Connector(
        provider=Provider.JIRA, name="Jira", oauth=jira.OAUTH, adapter=jira.JiraAdapter(), write_scope=jira.WRITE_SCOPE,
        sprint_scope=jira.AGILE_SCOPE,
    ),
}


def get(provider: str) -> Connector:
    if provider not in CONNECTORS:
        raise NotFoundError(f"unknown connector {provider!r}", error_code="unknown_connector")
    return CONNECTORS[provider]


def auth_for(conn: dict) -> AuthBackend:
    return get(conn["provider"]).backends[conn["auth_backend"]]
