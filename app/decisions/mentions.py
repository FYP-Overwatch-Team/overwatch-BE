"""Exact mentions in a source's text: tracker keys it names and docs pages it links. No AI, nothing guessed."""

import re

from app.core.constants import DocumentKind, EvidenceKind
from app.db import mongo
from app.decisions import overview

#: the pages decisions read: live ones, but not the product overview, which is written from the decisions
LIVE_PAGES = {"kind": DocumentKind.PAGE, "archived_at": None, "purpose": {"$ne": overview.PURPOSE}}

#: a tracker key such as PROJ-123: capitals, then a dash and a number
_KEY = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")
#: a docs page opened here: /documents/<id>
_NATIVE_PAGE = re.compile(r"/documents/([0-9a-f]{32})\b")
#: a Confluence page: .../wiki/spaces/<space>/pages/<id>
_CONFLUENCE_PAGE = re.compile(r"/wiki/spaces/[^/\s]+/pages/(\d+)")


def keys(text: str) -> set[str]:
    return set(_KEY.findall(text))


def page_ids(text: str) -> set[str]:
    """Ids of the docs pages the text links: our own page ids and Confluence's."""
    return set(_NATIVE_PAGE.findall(text)) | set(_CONFLUENCE_PAGE.findall(text))


async def named_in(workspace_id: str, text: str) -> list[tuple[tuple[str, str], str]]:
    """The items and pages a text names by key or links, as `(kind, ref_id)`, with the key or page title that named them."""
    found = []
    if names := keys(text):
        async for item in mongo.work_items().find({"workspace_id": workspace_id, "key": {"$in": list(names)}}, {"key": 1}):
            found.append(((EvidenceKind.WORK_ITEM, item["_id"]), item["key"]))
    if ids := page_ids(text):
        query = {
            "workspace_id": workspace_id, **LIVE_PAGES,
            "$or": [{"_id": {"$in": list(ids)}}, {"external_id": {"$in": list(ids)}}],
        }
        async for doc in mongo.documents().find(query, {"title": 1}):
            found.append(((EvidenceKind.DOCUMENT, doc["_id"]), doc["title"]))
    return found
