"""Suggesting decisions: the AI reads one board item or page and drafts the decisions it states.

It only drafts. Each suggestion is saved as `proposed` and stays that way until a person
settles it, and a suggestion whose quote isn't in the source word for word is thrown away.
One model call per run, made only when someone asks, and never twice for the same text.
"""

from datetime import timedelta
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

from app.core.constants import DecisionOrigin, DecisionStatus, EvidenceKind, UsageFeature
from app.core.exceptions import ConflictError, NotFoundError
from app.core.time import utcnow
from app.db import mongo
from app.decisions import ai, leases, limits, quotes, service
from app.decisions.schemas import QUOTE, STATEMENT, CreateDecision, NewEvidence

logger = structlog.get_logger("app.decisions")

SYSTEM_INSTRUCTION = (Path(__file__).parent / "prompts" / "suggest.md").read_text(encoding="utf-8")
MAX_SUGGESTIONS = 8
#: longer than one model call can take, retries included
LEASE = timedelta(minutes=3)


class Suggestion(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    statement: str = Field(**STATEMENT)
    quote: str = Field(**QUOTE)


class Reply(BaseModel):
    decisions: list[Suggestion]


async def suggest(member: dict, kind: EvidenceKind, ref_id: str) -> list[dict]:
    """Draft the decisions one board item or page states, as `proposed` decisions quoting it."""
    workspace_id = member["workspace_id"]
    source = (await service.sources(workspace_id, [(kind, ref_id)])).get((kind, ref_id))
    if source is None:
        raise NotFoundError("that item or page wasn't found", error_code="evidence_source_not_found")
    key = {"workspace_id": workspace_id, "ref_id": ref_id}
    busy = ConflictError("Suggestions for this are already being drafted", error_code="suggest_in_progress")
    async with leases.lease(mongo.decision_scans(), key, LEASE, busy):
        return await _suggest(member, kind, ref_id, source)


async def _suggest(member: dict, kind: EvidenceKind, ref_id: str, source: dict) -> list[dict]:
    workspace_id = member["workspace_id"]
    fingerprint = quotes.fingerprint(source["text"])
    scan = await mongo.decision_scans().find_one({"workspace_id": workspace_id, "ref_id": ref_id}) or {}
    if scan.get("fingerprint") == fingerprint:
        raise ConflictError("Nothing has changed here since it was last checked", error_code="source_unchanged")

    await limits.take_suggestion(workspace_id)
    reply = await ai.ask_json(
        Reply, SYSTEM_INSTRUCTION, ai.fenced(source["text"]),
        workspace_id=workspace_id, user_id=member["user_id"], feature=UsageFeature.DECISION_SUGGEST,
    )
    suggestions = reply.decisions[:MAX_SUGGESTIONS]

    # nothing already quoted from here, and nothing someone said isn't a decision
    known = set(scan.get("dismissed_quotes", []))
    async for decision in mongo.decisions().find({"workspace_id": workspace_id, "evidence.ref_id": ref_id}, {"evidence": 1}):
        known.update(quotes.fingerprint(e["quote"]) for e in decision["evidence"] if e["ref_id"] == ref_id)
    created = []
    for suggestion in suggestions:
        quote_print = quotes.fingerprint(suggestion.quote)
        if quote_print in known or not quotes.contains(source["text"], suggestion.quote):
            continue
        known.add(quote_print)
        body = CreateDecision(
            title=suggestion.title, statement=suggestion.statement, status=DecisionStatus.PROPOSED,
            evidence=[NewEvidence(kind=kind, ref_id=ref_id, quote=suggestion.quote)],
        )
        created.append(await service.create(member, body, DecisionOrigin.SUGGESTED))
    logger.info("decision_suggest", ref_id=ref_id, suggested=len(suggestions), kept=len(created))

    await mongo.decision_scans().update_one(
        {"workspace_id": workspace_id, "ref_id": ref_id},
        {"$set": {"kind": kind, "fingerprint": fingerprint, "scanned_at": utcnow()}}, upsert=True,
    )
    return created


async def dismiss(member: dict, decision_id: str) -> None:
    """"Not a decision": the suggestion goes, and its quote isn't suggested from that source again."""
    workspace_id = member["workspace_id"]
    decision = await mongo.decisions().find_one_and_delete({
        "_id": decision_id, "workspace_id": workspace_id,
        "origin": DecisionOrigin.SUGGESTED, "status": DecisionStatus.PROPOSED,
    })
    if decision is None:
        raise NotFoundError("no suggestion waiting to be reviewed", error_code="suggestion_not_found")
    for evidence in decision["evidence"]:
        await mongo.decision_scans().update_one(
            {"workspace_id": workspace_id, "ref_id": evidence["ref_id"]},
            {"$addToSet": {"dismissed_quotes": quotes.fingerprint(evidence["quote"])}}, upsert=True,
        )
