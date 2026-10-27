"""Checking accepted decisions against what changed since.

Every workspace is checked every half hour unless it turns checks off, and whenever someone asks. Each
check looks at the board items and pages its accepted decisions quote, and the items linked
to those quoted items, and reads only the ones whose text changed since they were last read.
The AI reads each with the decisions it relates to; a passage contradicting one is added to
that decision as `contradicts` evidence, with why. The decision itself stays as it is: it
goes to Needs you, and a person settles it.
"""

from collections import defaultdict
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

from app.core.constants import DecisionStatus, EvidenceKind, EvidenceStance, UsageFeature
from app.core.exceptions import AppError, FieldValidationError
from app.core.time import utcnow
from app.db import mongo
from app.decisions import ai, limits, quotes, service
from app.decisions.schemas import NOTE, QUOTE, NewEvidence

logger = structlog.get_logger("app.decisions")

SYSTEM_INSTRUCTION = (Path(__file__).parent / "prompts" / "check.md").read_text(encoding="utf-8")
#: who the automatic checks act as, in usage and on the evidence they add
SYSTEM_USER = "system"
#: decisions read with one source; the most recently changed when there are more
MAX_DECISIONS_PER_SOURCE = 8


class Conflict(BaseModel):
    decision: int
    quote: str = Field(**QUOTE)
    reason: str = Field(min_length=1, **NOTE)


class Reply(BaseModel):
    conflicts: list[Conflict]


Ref = tuple[str, str]


async def _related(workspace_id: str) -> dict[Ref, list[dict]]:
    """The accepted decisions each item or page relates to: it's quoted by them, or linked to an item they quote."""
    related: dict[Ref, dict[str, dict]] = defaultdict(dict)
    cursor = mongo.decisions().find({"workspace_id": workspace_id, "status": DecisionStatus.ACCEPTED}).sort("updated_at", -1)
    async for decision in cursor:
        for evidence in decision["evidence"]:
            related[(evidence["kind"], evidence["ref_id"])][decision["_id"]] = decision

    quoted_items = [ref_id for kind, ref_id in list(related) if kind == EvidenceKind.WORK_ITEM]
    async for item in mongo.work_items().find({"workspace_id": workspace_id, "_id": {"$in": quoted_items}}):
        # its parent, subtasks and issue links, which are named by key
        keys = [linked.get("key") for linked in [item.get("parent") or {}, *item.get("subtasks", []), *item.get("links", [])]]
        query = {"workspace_id": workspace_id, "source": item["source"], "key": {"$in": [key for key in keys if key]}}
        async for linked in mongo.work_items().find(query, {"_id": 1}):
            related[(EvidenceKind.WORK_ITEM, linked["_id"])].update(related[(EvidenceKind.WORK_ITEM, item["_id"])])
    return {ref: list(decisions.values())[:MAX_DECISIONS_PER_SOURCE] for ref, decisions in related.items()}


async def _check_source(workspace_id: str, user_id: str, ref: Ref, text: str, decisions: list[dict]) -> int:
    """Read one changed source against its decisions; how many contradictions were added."""
    listing = "\n".join(f"[{n}] {d['title']}: {d['statement']}" for n, d in enumerate(decisions, 1))
    reply = await ai.ask_json(
        Reply, SYSTEM_INSTRUCTION, f"Accepted decisions:\n{listing}\n\n{ai.fenced(text)}",
        workspace_id=workspace_id, user_id=user_id, feature=UsageFeature.DECISION_CHECK,
    )
    member = {"workspace_id": workspace_id, "user_id": user_id}
    added = 0
    for conflict in reply.conflicts:
        if not 1 <= conflict.decision <= len(decisions) or not quotes.contains(text, conflict.quote):
            continue
        decision = decisions[conflict.decision - 1]
        if quotes.fingerprint(conflict.quote) in {quotes.fingerprint(e["quote"]) for e in decision["evidence"]}:
            continue
        evidence = NewEvidence(
            kind=ref[0], ref_id=ref[1], quote=conflict.quote, stance=EvidenceStance.CONTRADICTS, note=conflict.reason,
        )
        try:
            await service.add_evidence(member, decision["_id"], evidence)
        except FieldValidationError:
            continue  # the decision holds all the passages it can
        added += 1
    return added


async def check_workspace(workspace_id: str, user_id: str = SYSTEM_USER) -> dict:
    """Check what changed since the last check; `capped` when the day's checks ran out on the way."""
    related = await _related(workspace_id)
    found = await service.sources(workspace_id, list(related)) if related else {}
    last_read = {doc["ref_id"]: doc["fingerprint"] async for doc in mongo.decision_checks().find({"workspace_id": workspace_id})}
    checked = conflicts = 0
    capped = False
    for ref, decisions in related.items():
        source = found.get(ref)
        if source is None:
            continue  # gone: its evidence already says so
        fingerprint = quotes.fingerprint(source["text"])
        quoted_as_is = any(e["ref_id"] == ref[1] and e["fingerprint"] == fingerprint for d in decisions for e in d["evidence"])
        if last_read.get(ref[1], fingerprint if quoted_as_is else None) == fingerprint:
            continue  # unchanged since it was last read, or since it was quoted
        try:
            await limits.take_check(workspace_id)
        except AppError:
            capped = True
            break
        conflicts += await _check_source(workspace_id, user_id, ref, source["text"], decisions)
        checked += 1
        await mongo.decision_checks().update_one(
            {"workspace_id": workspace_id, "ref_id": ref[1]},
            {"$set": {"kind": ref[0], "fingerprint": fingerprint, "checked_at": utcnow()}}, upsert=True,
        )
    result = {"checked": checked, "conflicts": conflicts, "capped": capped}
    await mongo.decision_check_settings().update_one(
        {"_id": workspace_id}, {"$set": {"last_run_at": utcnow(), "last_result": result}}, upsert=True,
    )
    logger.info("decision_check", workspace_id=workspace_id, by=user_id, **result)
    return result


async def get_settings(workspace_id: str) -> dict:
    doc = await mongo.decision_check_settings().find_one({"_id": workspace_id}) or {}
    # on unless the workspace turned it off
    return {"enabled": doc.get("enabled", True), "last_run_at": doc.get("last_run_at"), "last_result": doc.get("last_result")}


async def set_enabled(workspace_id: str, enabled: bool) -> dict:
    await mongo.decision_check_settings().update_one({"_id": workspace_id}, {"$set": {"enabled": enabled}}, upsert=True)
    return await get_settings(workspace_id)


async def run_enabled() -> None:
    """One pass of the background check over every workspace with accepted decisions that hasn't turned checks off."""
    off = {doc["_id"] async for doc in mongo.decision_check_settings().find({"enabled": False}, {"_id": 1})}
    for workspace_id in await mongo.decisions().distinct("workspace_id", {"status": DecisionStatus.ACCEPTED}):
        if workspace_id in off:
            continue
        try:
            await check_workspace(workspace_id)
        except Exception:
            # one workspace's failure (the AI being down, say) doesn't stop the others
            logger.exception("decision_check_failed", workspace_id=workspace_id)
