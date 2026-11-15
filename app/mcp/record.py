"""Recording what an AI tool and its user settled, as a proposal for people to review.

Nothing a tool records is decided: a decision lands as `proposed` in everyone's Needs you, and a
question as `open`, owned by the person the tool works for. The same wording is recorded once,
whichever tool sends it and however often, so a retry after a dropped connection is safe.
"""

import re
from datetime import timedelta

from pymongo.errors import DuplicateKeyError

from app.core.config import get_settings
from app.core.constants import (
    CAPTURE_REF,
    CaptureKind,
    DecisionOrigin,
    DecisionStatus,
    EmbeddingKind,
    EmbedPurpose,
    EvidenceKind,
)
from app.core.exceptions import ExternalServiceError, FieldValidationError, NotFoundError
from app.core.rate_limit import Limit, allow
from app.core.secrets import find_secret
from app.core.time import utcnow
from app.db import mongo
from app.decisions import quotes
from app.decisions import service as decisions
from app.decisions import suggest
from app.decisions.schemas import CodeContext, CreateDecision, NewEvidence
from app.embeddings import service as embeddings
from app.query.workspace_tools import words

#: how many of a statement's longest words another decision must share to be shown as possibly the same
SIMILAR_WORDS = 5
_WORD = re.compile(r"\w{4,}")
MAX_SIMILAR = 5
LIVE = {"$ne": DecisionStatus.REJECTED}
#: how long after recording something its recorder's tools may take it back
WITHDRAW_WINDOW = timedelta(days=7)

ASK_FIRST = (
    "Not recorded yet. Ask the user whether they'd like this posted to Bearings as a {what}, "
    "mention any similar ones listed here, and call again with user_approved=true only if they say yes."
)


class Refused(Exception):
    """Why nothing was recorded, as `code: what to do about it`, for the tool to act on."""


def fingerprint(title: str, statement: str) -> str:
    return quotes.fingerprint(f"{title}\n{statement}")


def _text(title: str, statement: str) -> str:
    """A decision as it's embedded everywhere: its title, then its statement."""
    return f"{title}\n{statement}"


async def _same_words(workspace_id: str, statement: str) -> list[dict]:
    """Decisions that share the statement's most telling words: a cheap look for the same thing said before."""
    longest = sorted(set(_WORD.findall(quotes.normalise(statement))), key=len, reverse=True)
    if len(longest) < SIMILAR_WORDS:
        return []
    query = words({"query": " ".join(longest[:SIMILAR_WORDS])}, ("title", "statement"))
    cursor = mongo.decisions().find({"workspace_id": workspace_id, "status": LIVE, **query}).sort("updated_at", -1)
    return await cursor.limit(MAX_SIMILAR).to_list(None)


async def _same_meaning(grant: dict, title: str, statement: str) -> list[dict]:
    """Decisions that say much the same in other words, closest first; none when embeddings are off or unreachable."""
    workspace_id = grant["workspace_id"]
    try:
        vector = await embeddings.embed_query(workspace_id, grant["user_id"], _text(title, statement), EmbedPurpose.DOCUMENT)
    except ExternalServiceError:
        return []
    if vector is None:
        return []
    threshold = get_settings().mcp_dedupe_similarity
    close = [
        ref_id for _, ref_id, score in await embeddings.nearest(workspace_id, vector, [EmbeddingKind.DECISION], MAX_SIMILAR)
        if score >= threshold
    ]
    found = {
        doc["_id"]: doc
        async for doc in mongo.decisions().find({"workspace_id": workspace_id, "_id": {"$in": close}, "status": LIVE})
    }
    return [found[ref_id] for ref_id in close if ref_id in found]


async def _similar(grant: dict, title: str, statement: str) -> list[dict]:
    by_id = {doc["_id"]: doc for doc in [*await _same_meaning(grant, title, statement), *await _same_words(grant["workspace_id"], statement)]}
    return list(by_id.values())[:MAX_SIMILAR]


async def _remember_meaning(grant: dict, decision: dict) -> None:
    """Embed a new record, so the next one saying the same in other words is caught; a failure only delays that."""
    try:
        await embeddings.refresh(
            grant["workspace_id"], grant["user_id"], EmbeddingKind.DECISION,
            {decision["id"]: _text(decision["title"], decision["statement"])},
        )
    except ExternalServiceError:
        pass


async def _take_write(grant: dict) -> None:
    settings = get_settings()
    per_tool = Limit("mcp_writes", settings.mcp_writes_per_connection_hourly, timedelta(hours=1))
    per_workspace = Limit("mcp_workspace_writes", settings.mcp_writes_per_workspace_daily, timedelta(days=1))
    if not await allow(per_tool, grant["_id"]):
        raise Refused("rate_limited: this tool has recorded as much as it may this hour; try again later.")
    if not await allow(per_workspace, grant["workspace_id"]):
        raise Refused("rate_limited: this workspace's AI tools have recorded as much as they may today.")


async def record(
    grant: dict, *, kind: CaptureKind, title: str, statement: str, rationale: str | None, area: str | None,
    user_words: str | None, user_approved: bool, evidence: list[dict], code: CodeContext | None, ticket: str | None,
    agent_model: str | None, force: bool,
) -> dict:
    """What happened (`result`), and the stored decision when there is one. Raises `Refused` when it can't be recorded."""
    workspace_id = grant["workspace_id"]
    if not recording_enabled(grant["workspace"]):
        raise Refused("recording_disabled: an admin turned off recording by AI tools in this workspace. Reading still works.")
    texts = {"title": title, "statement": statement, "rationale": rationale, "user_words": user_words}
    for name, text in texts.items():
        if text and (found := find_secret(text)):
            raise Refused(f"secret_detected: `{name}` contains {found}. Remove it and record again; nothing was stored.")

    wording = fingerprint(title, statement)
    existing = await mongo.decisions().find_one({"workspace_id": workspace_id, "capture.fingerprint": wording})
    if existing:
        return {"result": "already_recorded", "decision": existing}
    dismissed = await mongo.decision_scans().find_one(
        {"workspace_id": workspace_id, "ref_id": CAPTURE_REF, "dismissed_quotes": wording}, {"_id": 1},
    )
    if dismissed and not force:
        return {"result": "previously_dismissed"}
    similar = [] if force else await _similar(grant, title, statement)
    if similar and user_approved:
        # they agreed before knowing of these: nothing is written until they've seen them
        return {"result": "similar_exists", "similar": similar}
    if not user_approved and grant.get("ask_first", True):
        return {"result": "confirmation_needed", "similar": similar}
    if similar:
        return {"result": "similar_exists", "similar": similar}

    await _take_write(grant)
    question = kind == CaptureKind.OPEN_QUESTION
    body = CreateDecision(
        title=title, statement=statement, context_md=rationale, area=area,
        status=DecisionStatus.OPEN if question else DecisionStatus.PROPOSED,
        # a question is someone's to settle: the person the tool works for
        owner_id=grant["user_id"] if question else None,
        evidence=[
            NewEvidence(kind=EvidenceKind.WORK_ITEM if e["kind"] == "ticket" else EvidenceKind.DOCUMENT, ref_id=e["id"], quote=e["quote"])
            for e in evidence
        ],
    )
    capture = {
        "grant_id": grant["_id"], "client": grant["label"], "user_approved": user_approved, "user_words": user_words,
        "agent_model": agent_model, "ticket": ticket, "code": code.model_dump() if code else None,
        "fingerprint": wording, "recorded_at": utcnow(),
    }
    member = {"workspace_id": workspace_id, "user_id": grant["user_id"]}
    try:
        decision = await decisions.create(member, body, DecisionOrigin.AGENT, capture=capture)
    except DuplicateKeyError:
        # another tool recorded the same wording between the look and the write
        existing = await mongo.decisions().find_one({"workspace_id": workspace_id, "capture.fingerprint": wording})
        return {"result": "already_recorded", "decision": existing}
    except NotFoundError:
        raise Refused("evidence_not_found: a ticket or page given as evidence isn't in this workspace; find it with search_workspace.")
    except FieldValidationError as error:
        raise Refused(f"quote_not_in_source: {'; '.join(f'{name}: {why}' for name, why in error.meta['fields'].items())}")
    await mongo.mcp_grants().update_one({"_id": grant["_id"]}, {"$inc": {"records_count": 1}})
    await _remember_meaning(grant, decision)
    return {"result": "recorded", "decision": decision}


def recording_enabled(workspace: dict) -> bool:
    """AI tools may record in a workspace unless an admin turned that off."""
    return not workspace.get("mcp_recording_off", False)


async def withdraw(grant: dict, key: str) -> dict:
    """Take back a record nobody settled yet: one this person's tools made, in the last few days. Returns it as it was."""
    try:
        decision = await decisions.found_by_key(grant["workspace_id"], key)
    except NotFoundError:
        raise Refused(f"decision_not_found: this workspace has no decision {key}.")
    mine = decision["origin"] == DecisionOrigin.AGENT and decision["created_by"] == grant["user_id"]
    recent = mine and decision["capture"]["recorded_at"] > utcnow() - WITHDRAW_WINDOW
    if not recent or decision["status"] != DecisionStatus.PROPOSED:
        raise Refused(
            "not_withdrawable: only a proposal this person's AI tools recorded in the last "
            f"{WITHDRAW_WINDOW.days} days, and nobody settled yet, can be withdrawn. Ask them to change it in Bearings.",
        )
    try:
        # not remembered as dismissed: a corrected version of it may be recorded again
        await suggest.dismiss({"workspace_id": grant["workspace_id"], "user_id": grant["user_id"]}, decision["_id"], remember=False)
    except NotFoundError:
        raise Refused("not_withdrawable: someone settled or dismissed it just now.")
    return decision
