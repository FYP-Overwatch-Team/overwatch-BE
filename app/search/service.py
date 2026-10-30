"""Finding board items and docs pages by key, title or the text inside them, for the ⌘K menu.

Items from every connected tracker and pages from every docs source are searched alike, by what
the workspace can see: native items plus those of connected targets, and pages not archived. The
query is matched as plain text, case-insensitively. An exact key comes first, then keys starting
with the query, then title matches, then matches in the body; the most recently changed first
within each.
"""

import re

from app.boards import repository as boards
from app.core.constants import DocumentKind, SearchResultKind
from app.db import mongo
from app.work_items import service as work_items

#: matches read per kind before ranking, title and body separately
CANDIDATES = 50
#: characters of body text shown either side of a match
SNIPPET_RADIUS = 60
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKS = re.compile(r"[*_`~#>|]+")
_SPACE = re.compile(r"\s+")


def snippet(text: str, query: str) -> str | None:
    """The plain text around the first match of `query`, with ellipses where it's cut; None when it isn't there."""
    plain = _SPACE.sub(" ", _MARKS.sub(" ", _LINK.sub(r"\1", text))).strip()
    at = plain.lower().find(query.lower())
    if at < 0:
        return None
    start, end = max(at - SNIPPET_RADIUS, 0), min(at + len(query) + SNIPPET_RADIUS, len(plain))
    return f"{'…' if start else ''}{plain[start:end].strip()}{'…' if end < len(plain) else ''}"


def _rank(query: str, key: str | None, title: str) -> int:
    q = query.lower()
    if key and key.lower() == q:
        return 0
    if key and key.lower().startswith(q):
        return 1
    return 2 if q in title.lower() else 3


async def _matches(collection, scope: dict, matches: list[dict], body_field: str) -> list[dict]:
    """Docs for each of `matches` in turn, up to `CANDIDATES` each, newest first, none twice."""
    fields = {"key": 1, "title": 1, body_field: 1, "source": 1, "target_key": 1, "updated_at": 1}
    found: list[dict] = []
    for match in matches:
        # $and, not a merge: the scope can hold its own $or (which trackers are visible)
        query = {"$and": [scope, match, {"_id": {"$nin": [doc["_id"] for doc in found]}}]}
        found += await collection.find(query, fields).sort("updated_at", -1).limit(CANDIDATES).to_list(None)
    return found


async def search(workspace_id: str, query: str, limit: int) -> list[dict]:
    """Up to `limit` board items, then up to `limit` pages, best match first."""
    pattern = {"$regex": re.escape(query), "$options": "i"}
    items = await _matches(
        mongo.work_items(), {"workspace_id": workspace_id, **await work_items.visibility(workspace_id)},
        [
            # on its own: an exact key can be older than every newer key starting with it
            {"key": {"$regex": f"^{re.escape(query)}$", "$options": "i"}},
            {"$or": [{"key": pattern}, {"title": pattern}]},
            {"description_md": pattern},
        ],
        "description_md",
    )
    pages = await _matches(
        mongo.documents(), {"workspace_id": workspace_id, "kind": DocumentKind.PAGE, "archived_at": None},
        [{"title": pattern}, {"content_md": pattern}], "content_md",
    )
    # sorted is stable: within a rank, the newest stays first
    items = sorted(items, key=lambda doc: _rank(query, doc["key"], doc["title"]))[:limit]
    pages = sorted(pages, key=lambda doc: _rank(query, None, doc["title"]))[:limit]

    board_ids: dict[tuple[str, str | None], str | None] = {}
    for item in items:
        place = (item["source"], item.get("target_key"))
        if place not in board_ids:
            board = await boards.find(workspace_id, *place)
            board_ids[place] = board["_id"] if board else None
    results = [
        {
            "kind": SearchResultKind.WORK_ITEM, "id": item["_id"], "title": item["title"], "source": item["source"],
            "key": item["key"], "board_id": board_ids[(item["source"], item.get("target_key"))],
            "snippet": snippet(item.get("description_md") or "", query) if _rank(query, item["key"], item["title"]) == 3 else None,
        }
        for item in items
    ]
    results += [
        {
            "kind": SearchResultKind.DOCUMENT, "id": page["_id"], "title": page["title"], "source": page["source"],
            "snippet": snippet(page.get("content_md") or "", query) if _rank(query, None, page["title"]) == 3 else None,
        }
        for page in pages
    ]
    return results
