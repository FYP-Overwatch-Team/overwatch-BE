"""Ports: what the connector framework needs from each provider.

A connector is an adapter for one provider (Jira today; ClickUp, Slack and Google later).
It never holds a token: every call goes through an `AuthBackend`, so the same adapter
works whether we sign requests ourselves or a platform like Composio proxies them.

`conn` is the workspace's `connections` document for that provider.
"""

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.constants import StatusCategory


@dataclass(frozen=True)
class Page:
    items: list[dict]
    next_token: str | None


class AuthBackend(Protocol):
    async def request(self, conn: dict, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send an authenticated request to the provider on behalf of the connection."""


class WorkItemSource(Protocol):
    """A provider whose tickets are mirrored into `work_items` and can be changed from the board."""

    async def resolve_account(self, access_token: str) -> dict:
        """The external account just authorised: {id, name, url}."""

    async def list_targets(self, auth: AuthBackend, conn: dict) -> list[dict]:
        """Projects (or lists, boards) the account can sync: [{key, name, id}]."""

    async def list_assignees(self, auth: AuthBackend, conn: dict, target_key: str) -> list[dict]:
        """People a work item in the target can be assigned to: [{id, name, avatar_url}]."""

    async def fetch_page(
        self, auth: AuthBackend, conn: dict, target_key: str, *, since_minutes: int | None, page_token: str | None,
    ) -> Page:
        """One page of raw items, only those changed in the last `since_minutes` when given."""

    def normalise(self, conn: dict, target_key: str, raw: dict) -> dict:
        """The `work_items` fields for one raw item."""

    async def fetch(self, auth: AuthBackend, conn: dict, external_id: str) -> dict:
        """One raw item, re-read after a write."""

    async def create(self, auth: AuthBackend, conn: dict, target_key: str, fields: dict) -> str:
        """Create an item from {title, description, assignee_id}; returns its external id."""

    async def update(self, auth: AuthBackend, conn: dict, external_id: str, fields: dict) -> None:
        """Change any of {title, description, assignee_id}."""

    async def move(self, auth: AuthBackend, conn: dict, external_id: str, category: StatusCategory) -> None:
        """Put the item in a status of the given category."""

    async def delete(self, auth: AuthBackend, conn: dict, external_id: str) -> None: ...
