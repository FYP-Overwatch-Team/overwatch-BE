"""Decisions: what the team agreed, and the passages from board items and docs it rests on.

Each piece of evidence keeps its quote, its source's title and a fingerprint of the source's
text, so it still reads after the source changes or goes away. Every read compares the
quote with the source as it is now (`EvidenceState`), which is how a decision shows it may
be out of date.
"""

import re
import uuid
from datetime import datetime

from app.boards import repository as boards
from app.core.constants import (
    NATIVE_SOURCE,
    DecisionOrigin,
    DecisionStatus,
    DocumentKind,
    EvidenceKind,
    EvidenceState,
)
from app.core.exceptions import FieldValidationError, NotFoundError
from app.core.time import utcnow
from app.db import mongo
from app.decisions import quotes
from app.decisions.schemas import MAX_EVIDENCE, ChangeStatus, CreateDecision, NewEvidence, UpdateDecision
from app.workspaces import repository as workspaces



def _item_text(item: dict) -> str:
    return f"{item['title']}\n{item.get('description_md') or item.get('description') or ''}"


def _page_text(doc: dict) -> str:
    return f"{doc['title']}\n{doc.get('content_md') or ''}"


async def sources(workspace_id: str, refs: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """The board items and live docs pages behind `(kind, ref_id)` pairs, with their text, by pair."""
    item_ids = [ref_id for kind, ref_id in refs if kind == EvidenceKind.WORK_ITEM]
    page_ids = [ref_id for kind, ref_id in refs if kind == EvidenceKind.DOCUMENT]
    found = {}
    if item_ids:
        async for item in mongo.work_items().find({"workspace_id": workspace_id, "_id": {"$in": item_ids}}):
            found[(EvidenceKind.WORK_ITEM, item["_id"])] = {
                "title": f"{item['key']} {item['title']}", "external_url": item.get("external_url"), "text": _item_text(item),
                "key": item["key"], "source": item["source"], "target_key": item.get("target_key"),
            }
    if page_ids:
        query = {"workspace_id": workspace_id, "_id": {"$in": page_ids}, "kind": DocumentKind.PAGE, "archived_at": None}
        async for doc in mongo.documents().find(query):
            found[(EvidenceKind.DOCUMENT, doc["_id"])] = {
                "title": doc["title"], "external_url": doc.get("external_url"), "text": _page_text(doc),
                "source": doc["source"], "target_key": doc.get("target_key"),
            }
    return found


async def _synced_at(workspace_id: str, found: dict[tuple[str, str], dict]) -> dict[tuple[str, str], datetime]:
    """When each tracker or docs source behind the found items and pages was last read, by `(provider, key)`."""
    mirrored = {(source["source"], source["target_key"]) for source in found.values() if source["source"] != NATIVE_SOURCE}
    if not mirrored:
        return {}
    query = {"workspace_id": workspace_id, "provider": {"$in": [provider for provider, _ in mirrored]}}
    return {
        (target["provider"], target["key"]): target["last_synced_at"]
        async for target in mongo.sync_targets().find(query, {"provider": 1, "key": 1, "last_synced_at": 1})
        if (target["provider"], target["key"]) in mirrored and target.get("last_synced_at")
    }


def _state(evidence: dict, source: dict | None) -> EvidenceState:
    if source is None:
        return EvidenceState.SOURCE_GONE
    if not quotes.contains(source["text"], evidence["quote"]):
        return EvidenceState.QUOTE_MISSING
    if quotes.fingerprint(source["text"]) != evidence["fingerprint"]:
        return EvidenceState.CHANGED
    return EvidenceState.CURRENT


async def _to_decisions(workspace_id: str, docs: list[dict]) -> list[dict]:
    """Decisions as the API returns them, each piece of evidence with its state now."""
    refs = [(e["kind"], e["ref_id"]) for doc in docs for e in doc["evidence"]]
    found = await sources(workspace_id, refs) if refs else {}
    synced = await _synced_at(workspace_id, found)

    def with_state(evidence: dict) -> dict:
        source = found.get((evidence["kind"], evidence["ref_id"]))
        # how current the comparison is: a mirrored source is only as fresh as its last sync
        synced_at = synced.get((source["source"], source["target_key"])) if source else None
        return {**evidence, "state": _state(evidence, source), "source_synced_at": synced_at}

    return [{**doc, "id": doc["_id"], "evidence": [with_state(e) for e in doc["evidence"]]} for doc in docs]


async def _found(workspace_id: str, decision_id: str) -> dict:
    doc = await mongo.decisions().find_one({"_id": decision_id, "workspace_id": workspace_id})
    if doc is None:
        raise NotFoundError("decision not found", error_code="decision_not_found")
    return doc


async def _evidence(workspace_id: str, user_id: str, new: list[NewEvidence]) -> list[dict]:
    """Stored evidence for `new`; each quote must be found in its source."""
    found = await sources(workspace_id, [(e.kind, e.ref_id) for e in new])
    now = utcnow()
    stored = []
    for index, evidence in enumerate(new):
        source = found.get((evidence.kind, evidence.ref_id))
        if source is None:
            raise NotFoundError("that item or page wasn't found", error_code="evidence_source_not_found")
        if not quotes.contains(source["text"], evidence.quote):
            raise FieldValidationError({f"evidence.{index}.quote": "This passage isn't in that item or page."})
        # where a board item opens: its board and key; pages open by their id
        board = await boards.find(workspace_id, source["source"], source["target_key"]) if "key" in source else None
        stored.append({
            **evidence.model_dump(), "id": uuid.uuid4().hex, "title": source["title"],
            "external_url": source["external_url"], "fingerprint": quotes.fingerprint(source["text"]),
            "board_id": board["_id"] if board else None, "item_key": source.get("key"),
            "added_by": user_id, "added_at": now,
        })
    return stored


async def _check_owner(workspace_id: str, owner_id: str | None) -> None:
    if owner_id and not await workspaces.membership(workspace_id, owner_id):
        raise FieldValidationError({"owner_id": "The owner must be a member of this workspace."})


def _review_by(fields: dict) -> dict:
    # stored as an ISO date, like a work item's due date
    if fields.get("review_by"):
        fields["review_by"] = fields["review_by"].isoformat()
    return fields


async def list_decisions(
    workspace_id: str, *, status: DecisionStatus | None, q: str | None, ref_id: str | None, offset: int, limit: int,
) -> tuple[list[dict], int]:
    """Newest change first; `ref_id` is a board item or page the decision quotes."""
    query: dict = {"workspace_id": workspace_id}
    if status:
        query["status"] = status
    if ref_id:
        query["evidence.ref_id"] = ref_id
    if q:
        pattern = {"$regex": re.escape(q), "$options": "i"}
        query["$or"] = [{"title": pattern}, {"statement": pattern}]
    total = await mongo.decisions().count_documents(query)
    cursor = mongo.decisions().find(query).sort([("updated_at", -1), ("_id", 1)]).skip(offset).limit(limit)
    return await _to_decisions(workspace_id, [doc async for doc in cursor]), total


async def get(workspace_id: str, decision_id: str) -> dict:
    return (await _to_decisions(workspace_id, [await _found(workspace_id, decision_id)]))[0]


async def create(
    member: dict, body: CreateDecision, origin: DecisionOrigin = DecisionOrigin.MANUAL,
) -> dict:
    workspace_id, user_id = member["workspace_id"], member["user_id"]
    if body.status == DecisionStatus.SUPERSEDED:
        raise FieldValidationError({"status": "A new decision can't start out replaced."})
    await _check_owner(workspace_id, body.owner_id)
    now = utcnow()
    doc = {
        "_id": uuid.uuid4().hex, "workspace_id": workspace_id,
        **_review_by(body.model_dump(exclude={"evidence"})),
        "origin": origin, "superseded_by": None,
        "evidence": await _evidence(workspace_id, user_id, body.evidence),
        "history": [{"status": body.status, "by": user_id, "at": now, "note": None}],
        "created_by": user_id, "created_at": now, "updated_at": now,
    }
    await mongo.decisions().insert_one(doc)
    return await get(workspace_id, doc["_id"])


async def update(member: dict, decision_id: str, body: UpdateDecision) -> dict:
    workspace_id = member["workspace_id"]
    await _found(workspace_id, decision_id)
    fields = _review_by(body.model_dump(exclude_unset=True))
    if "owner_id" in fields:
        await _check_owner(workspace_id, fields["owner_id"])
    # title and statement can't be cleared
    for required in ("title", "statement"):
        if required in fields and fields[required] is None:
            del fields[required]
    await mongo.decisions().update_one({"_id": decision_id}, {"$set": {**fields, "updated_at": utcnow()}})
    return await get(workspace_id, decision_id)


async def change_status(member: dict, decision_id: str, body: ChangeStatus) -> dict:
    workspace_id, user_id = member["workspace_id"], member["user_id"]
    await _found(workspace_id, decision_id)
    superseded_by = None
    if body.status == DecisionStatus.SUPERSEDED:
        if not body.superseded_by or body.superseded_by == decision_id:
            raise FieldValidationError({"superseded_by": "Choose the decision that replaces this one."})
        superseded_by = (await _found(workspace_id, body.superseded_by))["_id"]
    now = utcnow()
    await mongo.decisions().update_one({"_id": decision_id}, {
        "$set": {"status": body.status, "superseded_by": superseded_by, "updated_at": now},
        "$push": {"history": {"status": body.status, "by": user_id, "at": now, "note": body.note}},
    })
    return await get(workspace_id, decision_id)


async def add_evidence(member: dict, decision_id: str, body: NewEvidence) -> dict:
    workspace_id = member["workspace_id"]
    await _found(workspace_id, decision_id)
    [evidence] = await _evidence(workspace_id, member["user_id"], [body])
    # only while there's room, checked in the same update so two adds can't pass the limit together
    added = await mongo.decisions().update_one(
        {"_id": decision_id, f"evidence.{MAX_EVIDENCE - 1}": {"$exists": False}},
        {"$push": {"evidence": evidence}, "$set": {"updated_at": utcnow()}},
    )
    if not added.matched_count:
        raise FieldValidationError({"quote": f"A decision holds up to {MAX_EVIDENCE} passages."})
    return await get(workspace_id, decision_id)


async def remove_evidence(member: dict, decision_id: str, evidence_id: str) -> dict:
    workspace_id = member["workspace_id"]
    await _found(workspace_id, decision_id)
    removed = await mongo.decisions().update_one(
        {"_id": decision_id, "evidence.id": evidence_id},
        {"$pull": {"evidence": {"id": evidence_id}}, "$set": {"updated_at": utcnow()}},
    )
    if not removed.matched_count:
        raise NotFoundError("evidence not found", error_code="evidence_not_found")
    return await get(workspace_id, decision_id)
