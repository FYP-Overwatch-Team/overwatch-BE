"""Confluence Cloud connector: spaces mirrored into `documents`, and page edits written back.

Uses the v2 REST API (cursor pages of up to 250) and the page's ADF body, converted to and from
our editor's JSON in `adf.py`. Archive and move only exist in the v1 API.
"""

import json
from datetime import datetime

import httpx
import structlog

from app.connectors import atlassian
from app.connectors.adf import adf_to_editor, adf_to_markdown, editor_to_adf
from app.connectors.ports import AuthBackend, Page, RemoteConflict
from app.core.constants import DocumentKind
from app.core.exceptions import ExternalServiceError, ForbiddenError, NotFoundError

logger = structlog.get_logger("app.connectors.confluence")

API_BASE = "https://api.atlassian.com/ex/confluence"
WRITE_SCOPE = "write:page:confluence"
# granular scopes: the v2 API accepts only these, and archive/move (v1) take them too
SCOPES = (
    "read:space:confluence",
    "read:page:confluence",
    WRITE_SCOPE,
    "read:folder:confluence",
    "read:attachment:confluence",
    "read:content.metadata:confluence",
    "delete:page:confluence",
)
PAGE_SIZE = 250
# pages archived per request
ARCHIVE_BATCH = 100
# what Confluence calls a page that's no longer live
GONE_STATUSES = {"archived", "trashed", "deleted"}


def _api(conn: dict, path: str) -> str:
    return f"{API_BASE}/{conn['external_account']['id']}/wiki/api/v2{path}"


def _v1(conn: dict, path: str) -> str:
    return f"{API_BASE}/{conn['external_account']['id']}/wiki/rest/api{path}"


def _next(conn: dict, body: dict) -> str | None:
    """The next page's absolute URL: Confluence gives it relative to the site (`/wiki/api/v2/...`)."""
    path = (body.get("_links") or {}).get("next")
    return f"{API_BASE}/{conn['external_account']['id']}{path}" if path else None


def _web_url(conn: dict, webui: str | None) -> str | None:
    site = (conn.get("external_account") or {}).get("url")
    return f"{site}/wiki{webui}" if site and webui else None


def _time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _check(resp: httpx.Response, ok: set[int], error_code: str, message: str) -> httpx.Response:
    if resp.status_code in ok:
        return resp
    try:
        errors = resp.json().get("errors") or []
        detail = "; ".join(e.get("title") or e.get("detail") or "" for e in errors) or resp.json().get("message")
    except (ValueError, AttributeError, TypeError):
        detail = None
    logger.error(error_code, status=resp.status_code, detail=detail)
    message = f"{message}: {detail}" if detail else message
    if resp.status_code == 403:
        raise ForbiddenError(message, error_code="confluence_forbidden")
    if resp.status_code == 404:
        raise NotFoundError(message, error_code="confluence_page_not_found")
    raise ExternalServiceError(message, error_code=error_code)


def _node(conn: dict, raw: dict, kind: DocumentKind) -> dict:
    version = raw.get("version") or {}
    parent = raw.get("parentId")
    return {
        "external_id": str(raw["id"]),
        "kind": kind,
        "parent_external_id": str(parent) if parent and raw.get("parentType") in ("page", "folder") else None,
        "title": raw.get("title") or "Untitled",
        "position": raw.get("position"),
        "external_version": version.get("number"),
        "status": raw.get("status"),
        "external_url": _web_url(conn, (raw.get("_links") or {}).get("webui")),
        "updated_at": _time(version.get("createdAt")),
    }


def _body(raw: dict) -> dict | None:
    value = ((raw.get("body") or {}).get("atlas_doc_format") or {}).get("value")
    try:
        return json.loads(value) if value else None
    except ValueError:
        return None


class ConfluenceAdapter:
    async def resolve_account(self, access_token: str, known_site_id: str | None = None) -> dict:
        return await atlassian.pick_site(access_token, known_site_id, "confluence", "no_confluence_site")

    async def list_targets(self, auth: AuthBackend, conn: dict) -> list[dict]:
        spaces: list[dict] = []
        url: str | None = _api(conn, "/spaces")
        params: dict | None = {"limit": PAGE_SIZE, "status": "current", "sort": "name"}
        while url:
            body = _check(await auth.request(conn, "GET", url, params=params), {200}, "confluence_spaces_unavailable", "could not list Confluence spaces").json()
            spaces.extend({"key": str(s["id"]), "name": s.get("name"), "id": s.get("key")} for s in body.get("results", []))
            url, params = _next(conn, body), None
        return spaces

    async def target_root(self, auth: AuthBackend, conn: dict, target_key: str) -> dict:
        space = _check(
            await auth.request(conn, "GET", _api(conn, f"/spaces/{target_key}")), {200}, "confluence_space_unavailable", "could not read the Confluence space",
        ).json()
        return {
            "external_id": f"space:{space['id']}",
            "title": space.get("name") or space.get("key") or "Confluence space",
            "external_url": _web_url(conn, (space.get("_links") or {}).get("webui")),
        }

    async def list_nodes(
        self, auth: AuthBackend, conn: dict, target_key: str, *, changed_since: datetime | None, page_token: str | None,
    ) -> Page:
        params = None if page_token else {"limit": PAGE_SIZE, "depth": "all", "sort": "-modified-date" if changed_since else "id"}
        if params and not changed_since:
            # a full run also sees archived and trashed pages, so they leave the mirror
            params["status"] = ["current", "archived", "trashed"]
        body = _check(
            await auth.request(conn, "GET", page_token or _api(conn, f"/spaces/{target_key}/pages"), params=params),
            {200}, "confluence_pages_unavailable", "could not list Confluence pages",
        ).json()
        items = [_node(conn, raw, DocumentKind.PAGE) for raw in body.get("results", [])]
        next_token = _next(conn, body)
        if changed_since:
            fresh = [item for item in items if item["updated_at"] is None or item["updated_at"] >= changed_since]
            if len(fresh) < len(items):
                items, next_token = fresh, None
        # folders have no listing of their own: fetch the ones these pages sit in
        folders = {raw["parentId"] for raw in body.get("results", []) if raw.get("parentType") == "folder" and raw.get("parentId")}
        items.extend(await self._folders(auth, conn, folders))
        return Page(items, next_token)

    async def _folders(self, auth: AuthBackend, conn: dict, ids: set[str]) -> list[dict]:
        found: list[dict] = []
        seen: set[str] = set()
        pending = list(ids)
        while pending:
            folder_id = str(pending.pop())
            if folder_id in seen:
                continue
            seen.add(folder_id)
            resp = await auth.request(conn, "GET", _api(conn, f"/folders/{folder_id}"))
            if resp.status_code == 404:
                continue
            raw = _check(resp, {200}, "confluence_folder_unavailable", "could not read a Confluence folder").json()
            found.append(_node(conn, raw, DocumentKind.FOLDER))
            if raw.get("parentType") == "folder" and raw.get("parentId"):
                pending.append(raw["parentId"])
        return found

    async def fetch_bodies(self, auth: AuthBackend, conn: dict, external_ids: list[str], images: dict[str, str]) -> dict[str, dict]:
        bodies: dict[str, dict] = {}
        for start in range(0, len(external_ids), PAGE_SIZE):
            batch = external_ids[start:start + PAGE_SIZE]
            url: str | None = _api(conn, "/pages")
            params: dict | None = {"id": ",".join(batch), "body-format": "atlas_doc_format", "limit": PAGE_SIZE, "status": ["current", "archived"]}
            while url:
                body = _check(await auth.request(conn, "GET", url, params=params), {200}, "confluence_pages_unavailable", "could not read Confluence pages").json()
                for raw in body.get("results", []):
                    adf = _body(raw)
                    bodies[str(raw["id"])] = {
                        "title": raw.get("title") or "Untitled",
                        "content": adf_to_editor(adf, images),
                        "content_md": adf_to_markdown(adf, "image") or "",
                        "external_version": (raw.get("version") or {}).get("number"),
                    }
                url, params = _next(conn, body), None
        return bodies

    async def list_attachments(self, auth: AuthBackend, conn: dict, external_id: str) -> list[dict]:
        """A page's image files: [{external_id, media_id, filename, content_type, size, external_version}]."""
        found: list[dict] = []
        url: str | None = _api(conn, f"/pages/{external_id}/attachments")
        params: dict | None = {"limit": PAGE_SIZE, "status": "current"}
        while url:
            body = _check(await auth.request(conn, "GET", url, params=params), {200}, "confluence_attachments_unavailable", "could not list attachments").json()
            found.extend(
                {
                    "external_id": str(raw["id"]),
                    "media_id": raw.get("fileId"),
                    "filename": raw.get("title") or "image",
                    "content_type": raw.get("mediaType") or "",
                    "size": raw.get("fileSize") or 0,
                    "external_version": (raw.get("version") or {}).get("number"),
                }
                for raw in body.get("results", [])
                if (raw.get("mediaType") or "").startswith("image/")
            )
            url, params = _next(conn, body), None
        return found

    async def download_attachment(self, auth: AuthBackend, conn: dict, page_external_id: str, attachment_external_id: str) -> bytes:
        resp = await auth.request(
            conn, "GET", _v1(conn, f"/content/{page_external_id}/child/attachment/{attachment_external_id}/download"), follow_redirects=True,
        )
        return _check(resp, {200}, "confluence_attachment_unavailable", "could not download an attachment").content

    async def update_page(
        self, auth: AuthBackend, conn: dict, external_id: str, *, title: str, content: dict, base_version: int, message: str,
    ) -> int:
        resp = await auth.request(conn, "PUT", _api(conn, f"/pages/{external_id}"), json={
            "id": external_id,
            "status": "current",
            "title": title,
            "body": {"representation": "atlas_doc_format", "value": json.dumps(editor_to_adf(content))},
            "version": {"number": base_version + 1, "message": message},
        })
        if resp.status_code == 409:
            raise RemoteConflict(external_id)
        return _check(resp, {200}, "confluence_update_failed", "could not save the page to Confluence").json()["version"]["number"]

    async def create_page(self, auth: AuthBackend, conn: dict, target_key: str, parent_external_id: str | None, title: str, content: dict) -> dict:
        resp = await auth.request(conn, "POST", _api(conn, "/pages"), json={
            "spaceId": target_key,
            "status": "current",
            "title": title,
            **({"parentId": parent_external_id} if parent_external_id else {}),
            "body": {"representation": "atlas_doc_format", "value": json.dumps(editor_to_adf(content))},
        })
        return _node(conn, _check(resp, {200}, "confluence_create_failed", "could not create the page in Confluence").json(), DocumentKind.PAGE)

    async def move_page(self, auth: AuthBackend, conn: dict, external_id: str, parent_external_id: str) -> None:
        resp = await auth.request(conn, "PUT", _v1(conn, f"/content/{external_id}/move/append/{parent_external_id}"))
        _check(resp, {200}, "confluence_move_failed", "could not move the page in Confluence")

    async def archive_pages(self, auth: AuthBackend, conn: dict, external_ids: list[str]) -> None:
        for start in range(0, len(external_ids), ARCHIVE_BATCH):
            pages = [{"id": int(external_id)} for external_id in external_ids[start:start + ARCHIVE_BATCH]]
            resp = await auth.request(conn, "POST", _v1(conn, "/content/archive"), json={"pages": pages})
            _check(resp, {200, 202}, "confluence_archive_failed", "could not archive the page in Confluence")

    async def delete_page(self, auth: AuthBackend, conn: dict, external_id: str) -> None:
        """Trash the page, then purge it from the trash; a page already gone counts as deleted."""
        for params in (None, {"purge": "true"}):
            resp = await auth.request(conn, "DELETE", _api(conn, f"/pages/{external_id}"), params=params)
            if resp.status_code == 404:
                continue
            _check(resp, {204}, "confluence_delete_failed", "could not delete the page in Confluence")
