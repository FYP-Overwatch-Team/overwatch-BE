"""Ports: what the connector framework needs from each provider.

A connector is an adapter for one provider (Jira today; ClickUp, Slack and Google later).
It never holds a token: every call goes through an `AuthBackend`, so the same adapter
works whether we sign requests ourselves or a platform like Composio proxies them.

`conn` is the workspace's `connections` document for that provider.
"""

from dataclasses import dataclass
from typing import Any, Protocol

import httpx


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
        board: dict | None = None,
    ) -> Page:
        """One page of raw items, only those changed in the last `since_minutes` when given."""

    def normalise(self, conn: dict, target_key: str, raw: dict, board: dict | None = None) -> dict:
        """The `work_items` fields for one raw item; `tracker_sprint` (sprint fields or None) once the board says how."""

    async def fetch(self, auth: AuthBackend, conn: dict, external_id: str, board: dict | None = None) -> dict:
        """One raw item, re-read after a write."""

    async def create(self, auth: AuthBackend, conn: dict, target_key: str, fields: dict) -> str:
        """Create an item from {title, description, assignee_id}; returns its external id."""

    async def update(self, auth: AuthBackend, conn: dict, external_id: str, fields: dict, board: dict | None = None) -> None:
        """Change any of {title, description, assignee_id, priority, labels, due_date, story_points}."""

    async def board_columns(self, auth: AuthBackend, conn: dict, target_key: str) -> list[dict]:
        """The tracker's columns for the target, in order: [{id, name, status_category, status_ids}]."""

    async def list_priorities(self, auth: AuthBackend, conn: dict) -> list[str]:
        """The priority names an item can have, most urgent first."""

    async def list_comments(self, auth: AuthBackend, conn: dict, external_id: str) -> list[dict]:
        """[{external_id, author: {id, name}, body (Markdown), created_at, updated_at}], oldest first."""

    async def add_comment(self, auth: AuthBackend, conn: dict, external_id: str, text: str) -> dict: ...

    async def update_comment(self, auth: AuthBackend, conn: dict, external_id: str, comment_id: str, text: str) -> dict: ...

    async def delete_comment(self, auth: AuthBackend, conn: dict, external_id: str, comment_id: str) -> None: ...

    async def board_details(self, auth: AuthBackend, conn: dict, target_key: str) -> dict:
        """{external_id, kind, fields: {sprint}} for the target's board; None where the grant can't tell."""

    async def list_sprints(self, auth: AuthBackend, conn: dict, board_external_id: str) -> list[dict]:
        """The board's open sprints: [{external_id, name, state, goal, start_at, end_at, completed_at}]."""

    async def set_sprint(self, auth: AuthBackend, conn: dict, external_ids: list[str], sprint_external_id: str | None) -> None:
        """Move items into a sprint, or to the backlog when it's None."""

    async def create_sprint(self, auth: AuthBackend, conn: dict, board_external_id: str, fields: dict) -> dict:
        """Create a sprint from {name, goal, start_at, end_at}; returns it as `list_sprints` would."""

    async def update_sprint(self, auth: AuthBackend, conn: dict, sprint_external_id: str, fields: dict) -> dict:
        """Change any of {name, goal, start_at, end_at, state}."""

    async def move(self, auth: AuthBackend, conn: dict, external_id: str, column: dict) -> None:
        """Put the item in one of the column's statuses, or any status of its category when it lists none."""

    async def delete(self, auth: AuthBackend, conn: dict, external_id: str) -> None: ...
