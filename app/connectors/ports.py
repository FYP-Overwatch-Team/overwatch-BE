"""Ports: what the connector framework needs from each provider.

A connector is an adapter for one provider (Jira and Confluence today; ClickUp, Slack and Google later).
It never holds a token: every call goes through an `AuthBackend`, so the same adapter
works whether we sign requests ourselves or a platform like Composio proxies them.

`conn` is the workspace's `connections` document for that provider.
"""

from dataclasses import dataclass
from datetime import datetime
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

    async def resolve_account(self, access_token: str, known_site_id: str | None = None) -> dict:
        """The external account just authorised: {id, name, url}; the known site when one is already in use."""

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
        """Create an item from {title, description, assignee_id, issue_type (a type name)}; returns its external id."""

    async def update(self, auth: AuthBackend, conn: dict, external_id: str, fields: dict, board: dict | None = None) -> None:
        """Change any of {title, description, assignee_id, priority, labels, due_date, story_points, issue_type_id}."""

    async def allowed_types(self, auth: AuthBackend, conn: dict, external_id: str) -> list[dict]:
        """The tracker's types this item can be changed to in place: [{id, name}]."""

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

    async def move(self, auth: AuthBackend, conn: dict, external_id: str, column: dict, status_id: str | None = None) -> None:
        """Put the item in one of the column's statuses (`status_id` when given), or any of its category when it lists none.

        Raises ConflictError `transition_not_allowed` when the tracker's workflow doesn't allow it.
        """

    async def allowed_statuses(self, auth: AuthBackend, conn: dict, external_id: str) -> list[dict] | None:
        """Where the item can move now: [{status_id, status, status_category, transition_id, transition_name}]; None when the tracker has no rules."""

    async def workflow(self, auth: AuthBackend, conn: dict, target_key: str) -> dict | None:
        """Every move the target's workflows allow, for hints: {statuses, workflows, by_issue_type}; None when unknown."""

    async def delete(self, auth: AuthBackend, conn: dict, external_id: str) -> None: ...


class RemoteConflict(Exception):
    """The page changed at the source since we last read it, so a write would overwrite someone's edit."""


class DocSource(Protocol):
    """A provider whose pages are mirrored into `documents` and can be edited from Overwatch.

    Content crosses this port as editor JSON (plus Markdown); converting to the provider's format
    is the adapter's job, so every source looks the same to the sync and the push.
    """

    async def resolve_account(self, access_token: str, known_site_id: str | None = None) -> dict:
        """The external account just authorised: {id, name, url}."""

    async def list_targets(self, auth: AuthBackend, conn: dict) -> list[dict]:
        """Spaces (or workspaces, drives) the account can sync: [{key, name, id}]."""

    async def target_root(self, auth: AuthBackend, conn: dict, target_key: str) -> dict:
        """The target itself, mirrored as a top-level folder: {external_id, title, external_url}."""

    async def list_nodes(
        self, auth: AuthBackend, conn: dict, target_key: str, *, changed_since: datetime | None, page_token: str | None,
    ) -> Page:
        """One page of the target's folders and pages, metadata only, newest change first when `changed_since` is given.

        Each item: {external_id, kind, parent_external_id, title, position, external_version, status, external_url, updated_at}.
        """

    async def fetch_bodies(self, auth: AuthBackend, conn: dict, external_ids: list[str], images: dict[str, str]) -> dict[str, dict]:
        """Pages' content by external id: {title, content (editor JSON), content_md, external_version}.

        `images` maps the source's media ids to our copies' attachment ids, so pictures show from our storage.
        """

    async def list_attachments(self, auth: AuthBackend, conn: dict, external_id: str) -> list[dict]:
        """A page's image files: [{external_id, media_id, filename, content_type, size, external_version}]."""

    async def download_attachment(self, auth: AuthBackend, conn: dict, page_external_id: str, attachment_external_id: str) -> bytes: ...

    async def update_page(
        self, auth: AuthBackend, conn: dict, external_id: str, *, title: str, content: dict, base_version: int, message: str,
    ) -> int:
        """Replace a page's title and content; the new version. Raises `RemoteConflict` if it moved past `base_version`."""

    async def create_page(self, auth: AuthBackend, conn: dict, target_key: str, parent_external_id: str | None, title: str, content: dict) -> dict:
        """A new page under the parent (the target's top level when None), as `list_nodes` describes one."""

    async def move_page(self, auth: AuthBackend, conn: dict, external_id: str, parent_external_id: str) -> None:
        """Make the page the last child of another page."""

    async def archive_pages(self, auth: AuthBackend, conn: dict, external_ids: list[str]) -> None:
        """Archive pages at the source (each on its own: what's under them stays where it is there)."""

    async def delete_page(self, auth: AuthBackend, conn: dict, external_id: str) -> None:
        """Delete the page for good; one already gone counts as deleted."""
