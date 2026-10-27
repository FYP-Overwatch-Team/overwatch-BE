"""The product overview: a docs page listing the accepted decisions by area, each linked to where it's decided.

It's built from the decisions, not written by the AI, so it says only what was agreed. It's an
ordinary page that can be read, moved and shared like any other; each update replaces its content.
"""

from urllib.parse import urlencode

from app.connectors.adf import adf_to_editor, markdown_to_adf
from app.core.constants import DecisionStatus, DocumentKind, EvidenceKind, EvidenceStance
from app.core.time import utcnow
from app.db import mongo
from app.documents import service as documents
from app.documents.schemas import CreateDocument, UpdateDocument

TITLE = "Product overview"
#: marks the page as the overview on its document, so updates find it again
PURPOSE = "product_overview"
OTHER_AREA = "Other"


def _link_text(text: str) -> str:
    return text.replace("[", "").replace("]", "") or "Untitled"


def _source_href(evidence: dict) -> str:
    # where the app opens a page, and a board item on its board
    if evidence["kind"] == EvidenceKind.DOCUMENT:
        return f"/documents/{evidence['ref_id']}"
    params = {"board": evidence.get("board_id"), "item": evidence.get("item_key")}
    return f"/board?{urlencode({key: value for key, value in params.items() if value})}"


def markdown(decisions: list[dict], day: str) -> str:
    """The overview's text: one section per area (Other last), a line per decision with its sources."""
    lines = [
        f"Built from the decisions accepted in Decisions, last updated {day}. Each update replaces this page,"
        " so change the decisions rather than this page.",
    ]
    if not decisions:
        lines += ["", "No decisions have been accepted yet."]
    by_area: dict[str, list[dict]] = {}
    for decision in decisions:
        by_area.setdefault(decision.get("area") or OTHER_AREA, []).append(decision)
    for area in sorted(by_area, key=lambda name: (name == OTHER_AREA, name.casefold())):
        lines += ["", f"## {area}", ""]
        for decision in by_area[area]:
            line = f"- **[{_link_text(decision['title'])}](/decisions/{decision['_id']})**: {decision['statement']}"
            sources = [
                f"[{_link_text(e['title'])}]({_source_href(e)})"
                for e in decision["evidence"] if e["stance"] == EvidenceStance.SUPPORTS
            ]
            if sources:
                line += f" Sources: {', '.join(sources)}."
            lines.append(line)
    return "\n".join(lines)


async def update(member: dict) -> str:
    """Write the workspace's product overview page, creating it the first time; its id."""
    workspace_id = member["workspace_id"]
    cursor = mongo.decisions().find({"workspace_id": workspace_id, "status": DecisionStatus.ACCEPTED}).sort("title", 1)
    md = markdown(await cursor.to_list(None), utcnow().strftime("%d %b %Y"))
    content = adf_to_editor(markdown_to_adf(md))

    page = await mongo.documents().find_one(
        {"workspace_id": workspace_id, "purpose": PURPOSE, "archived_at": None}, {"version": 1},
    )
    if page:
        await documents.update(member, page["_id"], UpdateDocument(content=content, content_md=md, base_version=page["version"]))
        return page["_id"]
    created = await documents.create(member, CreateDocument(kind=DocumentKind.PAGE, title=TITLE, content=content, content_md=md))
    await mongo.documents().update_one({"_id": created["id"]}, {"$set": {"purpose": PURPOSE}})
    return created["id"]
