"""Checking accepted decisions against what changed since.

Every workspace is checked as often as it chose (every half hour unless it changes that, or turns
checks off), and whenever someone asks. Each
check looks at the board items and pages its accepted decisions quote, and the items linked
to those quoted items, and reads only the ones whose text changed since they were last read.
The AI reads each with the decisions it relates to; a passage contradicting one is added to
that decision as `contradicts` evidence, with why. The decision itself stays as it is: it
goes to Needs you, and a person settles it.
"""

from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

from app.core.constants import DecisionCheckInterval, DecisionStatus, EvidenceKind, EvidenceStance, UsageFeature
from app.core.exceptions import AppError, ConflictError, FieldValidationError
from app.core.time import utcnow
from app.db import mongo
from app.decisions import ai, leases, limits, quotes, service
from app.decisions.schemas import NOTE, QUOTE, ChangeCheckSettings, NewEvidence

logger = structlog.get_logger("app.decisions")

SYSTEM_INSTRUCTION = (Path(__file__).parent / "prompts" / "check.md").read_text(encoding="utf-8")
#: who the automatic checks act as, in usage and on the evidence they add
SYSTEM_USER = "system"
#: decisions read with one source; the most recently changed when there are more
MAX_DECISIONS_PER_SOURCE = 8
#: longer than a whole check can take: a day's cap of model calls, each with its retries
LEASE = timedelta(minutes=30)


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
    """Check what changed since the last check; `capped` when the day's checks ran out on the way.

    One check runs per workspace at a time; another meanwhile raises `check_in_progress`.
    """
    busy = ConflictError("A check is already running for this workspace", error_code="check_in_progress")
    async with leases.lease(mongo.decision_check_settings(), {"_id": workspace_id}, LEASE, busy):
        return await _check(workspace_id, user_id)


async def _check(workspace_id: str, user_id: str) -> dict:
    await service.refresh_attention(workspace_id)
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
    return {
        # on, every half hour, unless the workspace changed that
        "enabled": doc.get("enabled", True),
        "interval_minutes": doc.get("interval_minutes", DecisionCheckInterval.HALF_HOUR),
        "last_run_at": doc.get("last_run_at"), "last_result": doc.get("last_result"),
    }


async def change_settings(workspace_id: str, body: ChangeCheckSettings) -> dict:
    fields = body.model_dump(exclude_none=True)
    if fields:
        await mongo.decision_check_settings().update_one({"_id": workspace_id}, {"$set": fields}, upsert=True)
    return await get_settings(workspace_id)


async def run_due() -> None:
    """Check every workspace with accepted decisions whose checks are on and due."""
    now = utcnow()
    settings = {doc["_id"]: doc async for doc in mongo.decision_check_settings().find()}
    for workspace_id in await mongo.decisions().distinct("workspace_id", {"status": DecisionStatus.ACCEPTED}):
        doc = settings.get(workspace_id, {})
        every = timedelta(minutes=doc.get("interval_minutes", DecisionCheckInterval.HALF_HOUR))
        last = doc.get("last_run_at")
        if not doc.get("enabled", True) or (last and now - last < every):
            continue
        try:
            await check_workspace(workspace_id)
        except ConflictError:
            continue  # someone pressed Check now; that run covers it
        except Exception:
            # one workspace's failure (the AI being down, say) doesn't stop the others
            logger.exception("decision_check_failed", workspace_id=workspace_id)
