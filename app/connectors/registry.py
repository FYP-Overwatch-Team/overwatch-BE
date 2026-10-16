"""Every connector Overwatch supports; adding a provider is an adapter plus an entry here."""

from dataclasses import dataclass, field

from app.connectors import atlassian, confluence, jira
from app.connectors.auth_backends import NativeOAuth
from app.connectors.oauth import OAuth2Flow, OAuthConfig
from app.connectors.ports import AuthBackend, DocSource, WorkItemSource
from app.core.constants import AuthBackendName, ConnectorCategory, Provider
from app.core.exceptions import NotFoundError

# one flow (and token) per OAuth app, so products sharing an app share their credential
_flows: dict[str, OAuth2Flow] = {}


@dataclass(frozen=True)
class Connector:
    provider: str
    name: str
    category: ConnectorCategory
    oauth: OAuthConfig
    adapter: WorkItemSource | DocSource
    # what this product needs on top of the app's own scopes
    scopes: tuple[str, ...]
    # the scope that lets changes be written back; older grants may lack it
    write_scope: str | None
    # the scope that lets the tracker's boards and sprints be read and changed; None if it has no sprints
    sprint_scope: str | None = None
    flow: OAuth2Flow = field(init=False)
    backends: dict[str, AuthBackend] = field(init=False)

    def __post_init__(self) -> None:
        flow = _flows.setdefault(self.oauth.provider, OAuth2Flow(self.oauth))
        object.__setattr__(self, "flow", flow)
        object.__setattr__(self, "backends", {AuthBackendName.NATIVE_OAUTH: NativeOAuth(flow)})

    @property
    def credential(self) -> str:
        """The `oauth_tokens` provider key: the app's, shared by every product on it."""
        return self.oauth.provider


CONNECTORS = {
    Provider.JIRA: Connector(
        provider=Provider.JIRA, name="Jira", category=ConnectorCategory.WORK_ITEMS, oauth=atlassian.OAUTH,
        adapter=jira.JiraAdapter(), scopes=jira.SCOPES, write_scope=jira.WRITE_SCOPE, sprint_scope=jira.AGILE_SCOPE,
    ),
    Provider.CONFLUENCE: Connector(
        provider=Provider.CONFLUENCE, name="Confluence", category=ConnectorCategory.DOCUMENTS, oauth=atlassian.OAUTH,
        adapter=confluence.ConfluenceAdapter(), scopes=confluence.SCOPES, write_scope=confluence.WRITE_SCOPE,
    ),
}


def get(provider: str) -> Connector:
    if provider not in CONNECTORS:
        raise NotFoundError(f"unknown connector {provider!r}", error_code="unknown_connector")
    return CONNECTORS[provider]


def siblings(provider: str) -> list[str]:
    """The other providers sharing this one's credential."""
    credential = get(provider).credential
    return [p for p, c in CONNECTORS.items() if c.credential == credential and p != provider]


def has_work_items(provider: str) -> bool:
    """Whether a provider's targets are trackers (boards), not docs spaces."""
    connector = CONNECTORS.get(provider)
    return connector is not None and connector.category == ConnectorCategory.WORK_ITEMS


def work_item_source(provider: str) -> WorkItemSource:
    connector = get(provider)
    if connector.category != ConnectorCategory.WORK_ITEMS:
        raise NotFoundError(f"{provider} has no work items", error_code="unknown_connector")
    return connector.adapter


def doc_source(provider: str) -> DocSource:
    connector = get(provider)
    if connector.category != ConnectorCategory.DOCUMENTS:
        raise NotFoundError(f"{provider} has no documents", error_code="unknown_connector")
    return connector.adapter


def auth_for(conn: dict) -> AuthBackend:
    return get(conn["provider"]).backends[conn["auth_backend"]]
