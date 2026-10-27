"""Suggesting decisions: the AI reads one board item or page and drafts the decisions it states.

It only drafts. Each suggestion is saved as `proposed` and stays that way until a person
settles it, and a suggestion whose quote isn't in the source word for word is thrown away.
One model call per run, made only when someone asks, and never twice for the same text.
"""

import json
from pathlib import Path

import structlog
from pydantic import BaseModel, Field, ValidationError

from app.core.constants import DecisionOrigin, DecisionStatus, EvidenceKind, UsageFeature
from app.core.exceptions import ConflictError, ExternalServiceError, NotFoundError
from app.core.time import utcnow
from app.db import mongo
from app.decisions import limits, quotes, service
from app.decisions.schemas import QUOTE, STATEMENT, CreateDecision, NewEvidence
from app.integrations.llm import Delta, get_llm_client
from app.usage import service as usage

logger = structlog.get_logger("app.decisions")

SYSTEM_INSTRUCTION = (Path(__file__).parent / "prompts" / "suggest.md").read_text(encoding="utf-8")
#: how much of a source the model reads; longer pages are cut here
MAX_SOURCE_CHARS = 12_000
MAX_SUGGESTIONS = 8
TIMEOUT_SECONDS = 60


class Suggestion(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    statement: str = Field(**STATEMENT)
    quote: str = Field(**QUOTE)


class Reply(BaseModel):
    decisions: list[Suggestion]


def _read_reply(text: str) -> list[Suggestion]:
    """The suggestions in the model's JSON reply, which may come wrapped in a code fence."""
    start, end = text.find("{"), text.rfind("}")
    try:
        return Reply.model_validate(json.loads(text[start:end + 1])).decisions[:MAX_SUGGESTIONS]
    except (ValueError, ValidationError) as exc:
        logger.warning("decision_suggest_unreadable", error=type(exc).__name__, reply=text[:200])
        raise ExternalServiceError("The AI's reply couldn't be read; try again", error_code="llm_bad_reply") from exc


async def _ask(member: dict, text: str) -> str:
    # the source's text starts with its title
    prompt = f"<source>\n{text[:MAX_SOURCE_CHARS]}\n</source>"
    reply = []
    stream = get_llm_client().stream_text(
        SYSTEM_INSTRUCTION, prompt, timeout=TIMEOUT_SECONDS,
        on_usage=usage.recorder(member["workspace_id"], member["user_id"], UsageFeature.DECISION_SUGGEST),
    )
    async for item in stream:
        if isinstance(item, Delta) and item.kind == "text":
            reply.append(item.text)
    return "".join(reply)


async def suggest(member: dict, kind: EvidenceKind, ref_id: str) -> list[dict]:
    """Draft the decisions one board item or page states, as `proposed` decisions quoting it."""
    workspace_id = member["workspace_id"]
    source = (await service.sources(workspace_id, [(kind, ref_id)])).get((kind, ref_id))
    if source is None:
        raise NotFoundError("that item or page wasn't found", error_code="evidence_source_not_found")
    fingerprint = quotes.fingerprint(source["text"])
    scan = await mongo.decision_scans().find_one({"workspace_id": workspace_id, "ref_id": ref_id}) or {}
    if scan.get("fingerprint") == fingerprint:
        raise ConflictError("Nothing has changed here since it was last checked", error_code="source_unchanged")

    await limits.take_suggestion(workspace_id)
    suggestions = _read_reply(await _ask(member, source["text"]))

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
