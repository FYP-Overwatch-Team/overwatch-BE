"""Tickets, pages and decisions the AI chat drafted (`chat_proposals`), created only when the asker approves.

The model can only draft (`query.workspace_tools`): a draft is saved here with the answer and
nothing else happens until the member who asked approves it, with their own edits. It's then made
by the same create service the app uses, as that member, so their role and the board's
editability are checked as usual. Approving takes the proposal in one conditional update, so two
clicks or two tabs create it once, and an approval that fails hands it back to try again.
"""

import uuid
from datetime import timedelta

from pymongo import ReturnDocument

from app.boards import service as boards
from app.chat.repository import RETENTION
from app.chat.schemas import ProposalFields
from app.connectors.adf import adf_to_editor, markdown_to_adf
from app.core.constants import ChatRecordKind, DocumentKind, ProposalStatus
from app.core.exceptions import ConflictError, FieldValidationError, NotFoundError
from app.core.links import decision_href, document_href, work_item_href
from app.core.time import utcnow
from app.db import mongo
from app.decisions import service as decisions
from app.decisions.schemas import CreateDecision
from app.documents import service as documents
from app.documents.schemas import CreateDocument
from app.work_items import service as work_items
from app.work_items.schemas import CreateWorkItem

#: how long a draft can be approved for; after that it's shown as expired
EXPIRY = timedelta(hours=24)


def to_proposal(doc: dict) -> dict:
    expired = doc["status"] == ProposalStatus.PENDING and doc["expires_at"] <= utcnow()
    return {**doc, "id": doc["_id"], "status": ProposalStatus.EXPIRED if expired else doc["status"]}


async def save(member: dict, conversation_id: str, message_id: str, drafts: list[dict]) -> list[dict]:
    """Store an answer's drafts; a ticket goes on the first board that can take it unless the asker picks another."""
    if not drafts:
        return []
    boards_open = [
        {"id": board["id"], "name": board["name"]}
        for board in await boards.list_boards(member["workspace_id"]) if board["editable"]
    ]
    now = utcnow()
    docs = []
    for draft in drafts:
        ticket = draft["kind"] == ChatRecordKind.WORK_ITEM
        docs.append({
            "_id": uuid.uuid4().hex, "workspace_id": member["workspace_id"], "user_id": member["user_id"],
            "conversation_id": conversation_id, "message_id": message_id, "kind": draft["kind"],
            "fields": {**draft["fields"], "board_id": boards_open[0]["id"] if ticket and boards_open else None},
            "boards": boards_open if ticket else [], "status": ProposalStatus.PENDING, "result": None,
            "created_at": now, "expires_at": now + EXPIRY, "purge_at": now + RETENTION,
        })
    await mongo.chat_proposals().insert_many(docs)
    return [to_proposal(doc) for doc in docs]


async def by_message(member: dict, conversation_id: str) -> dict[str, list[dict]]:
    """A conversation's proposals, by the answer that drafted them."""
    found: dict[str, list[dict]] = {}
    cursor = mongo.chat_proposals().find(
        {"workspace_id": member["workspace_id"], "user_id": member["user_id"], "conversation_id": conversation_id},
    ).sort("created_at", 1)
    async for doc in cursor:
        found.setdefault(doc["message_id"], []).append(to_proposal(doc))
    return found


async def apply(member: dict, proposal_id: str, fields: ProposalFields) -> dict:
    """Create what was drafted, with the asker's edits; approving one already created returns it again."""
    owned = {"_id": proposal_id, "workspace_id": member["workspace_id"], "user_id": member["user_id"]}
    claimed = await mongo.chat_proposals().find_one_and_update(
        {**owned, "status": ProposalStatus.PENDING, "expires_at": {"$gt": utcnow()}},
        {"$set": {"status": ProposalStatus.APPLYING, "fields": fields.model_dump()}},
        return_document=ReturnDocument.AFTER,
    )
    if claimed is None:
        doc = await _owned(owned)
        if doc["status"] == ProposalStatus.APPLIED:
            return to_proposal(doc)
        raise ConflictError(f"This draft is {to_proposal(doc)['status']}", error_code="proposal_not_pending")

    try:
        result = await _create(member, claimed["kind"], fields)
    except Exception:
        await mongo.chat_proposals().update_one(owned, {"$set": {"status": ProposalStatus.PENDING}})
        raise
    return to_proposal(await mongo.chat_proposals().find_one_and_update(
        owned, {"$set": {"status": ProposalStatus.APPLIED, "result": result}}, return_document=ReturnDocument.AFTER,
    ))


async def cancel(member: dict, proposal_id: str) -> dict:
    owned = {"_id": proposal_id, "workspace_id": member["workspace_id"], "user_id": member["user_id"]}
    cancelled = await mongo.chat_proposals().find_one_and_update(
        {**owned, "status": ProposalStatus.PENDING}, {"$set": {"status": ProposalStatus.CANCELLED}},
        return_document=ReturnDocument.AFTER,
    )
    if cancelled is None:
        raise ConflictError(f"This draft is {to_proposal(await _owned(owned))['status']}", error_code="proposal_not_pending")
    return to_proposal(cancelled)


async def _owned(owned: dict) -> dict:
    """The asker's proposal; someone else's is simply not found."""
    doc = await mongo.chat_proposals().find_one(owned)
    if doc is None:
        raise NotFoundError("draft not found", error_code="proposal_not_found")
    return doc


async def _create(member: dict, kind: ChatRecordKind, fields: ProposalFields) -> dict:
    """Make it with the app's own create service, as the member; `{kind, id, title, href}` of what was made."""
    if kind == ChatRecordKind.WORK_ITEM:
        if not fields.board_id:
            raise FieldValidationError({"board_id": "Pick a board for the ticket."})
        item = await work_items.create(
            member, CreateWorkItem(title=fields.title, description=fields.body or None, board_id=fields.board_id),
        )
        return {"kind": kind, "id": item["id"], "title": item["title"], "href": work_item_href(fields.board_id, item["key"])}
    if kind == ChatRecordKind.DOCUMENT:
        page = await documents.create(member, CreateDocument(
            kind=DocumentKind.PAGE, title=fields.title, content=adf_to_editor(markdown_to_adf(fields.body)),
            content_md=fields.body,
        ))
        return {"kind": kind, "id": page["id"], "title": page["title"], "href": document_href(page["id"])}
    if not fields.statement:
        raise FieldValidationError({"statement": "A decision needs a statement."})
    decision = await decisions.create(
        member, CreateDecision(title=fields.title, statement=fields.statement, context_md=fields.body or None),
    )
    return {"kind": kind, "id": decision["id"], "title": decision["title"], "href": decision_href(decision["id"])}
