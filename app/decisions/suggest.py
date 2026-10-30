"""Suggesting decisions: the AI reads one board item or page and drafts the decisions it states.

It reads that source as S1, with up to a few related ones (linked, named by it, or closest in
meaning) as S2, S3..., and writes each decision up: background, what it means, an example, pros
and cons, citing the sources. It only drafts. Each suggestion is saved as `proposed` and stays that
way until a person settles it. Its quotes become its evidence; one not in its source word for word
is thrown away, and so is a suggestion with no quote left from S1. Citations of sources it wasn't
given are dropped. One model call per run, made only when someone asks, and never twice for the
same text.
"""

import re
from datetime import timedelta
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.constants import DecisionOrigin, DecisionStatus, EmbeddingKind, EvidenceKind, UsageFeature
from app.core.exceptions import ConflictError, ExternalServiceError, NotFoundError
from app.core.time import utcnow
from app.db import mongo
from app.decision_comments import service as decision_comments
from app.realtime import topics
from app.realtime.events import EventName, UnsubscribeReason
from app.realtime.hub import get_hub
from app.decisions import ai, leases, limits, mentions, overview, quotes, service
from app.decisions.schemas import CONTEXT, QUOTE, STATEMENT, CreateDecision, NewEvidence
from app.embeddings import service as embeddings

logger = structlog.get_logger("app.decisions")

SYSTEM_INSTRUCTION = (Path(__file__).parent / "prompts" / "suggest.md").read_text(encoding="utf-8")
MAX_SUGGESTIONS = 5
#: related sources read with the one asked about, and how much of each
MAX_RELATED = 4
RELATED_CHARS = 3000
MAX_QUOTES = 3
MAX_POINTS = 4
#: each suggestion is written up at length, so the reply takes longer than a check's
TIMEOUT_SECONDS = 120
#: longer than one model call can take, retries included
LEASE = timedelta(minutes=4)
_CITATION = re.compile(r"\s*\[(S\d+)\]")

Ref = tuple[str, str]


class Quote(BaseModel):
    source: str
    text: str = Field(**QUOTE)


class Suggestion(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    statement: str = Field(**STATEMENT)
    background: str = ""
    meaning: str = ""
    example: str = ""
    pros: list[str] = []
    cons: list[str] = []
    quotes: list[Quote] = []


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


async def _related(workspace_id: str, user_id: str, ref: Ref, text: str) -> list[Ref]:
    """Up to `MAX_RELATED` sources related to `ref`: its linked items, what it names, then the closest in meaning."""
    found: list[Ref] = []
    if ref[0] == EvidenceKind.WORK_ITEM:
        item = await mongo.work_items().find_one({"_id": ref[1], "workspace_id": workspace_id})
        found += [(EvidenceKind.WORK_ITEM, linked["_id"]) for linked in await service.linked_items(workspace_id, item)]
    found += [named for named, _ in await mentions.named_in(workspace_id, text)]
    try:
        vector = (await embeddings.refresh(workspace_id, user_id, EmbeddingKind(ref[0]), {ref[1]: text})).get(ref[1])
    except ExternalServiceError:
        logger.warning("decision_suggest_embeddings_unavailable", workspace_id=workspace_id)
        vector = None
    if vector is not None:
        kinds = [EmbeddingKind.WORK_ITEM, EmbeddingKind.DOCUMENT]
        threshold = get_settings().decision_similarity_threshold
        nearest = await embeddings.nearest(workspace_id, vector, kinds, MAX_RELATED + 2)
        found += [(kind, ref_id) for kind, ref_id, score in nearest if score >= threshold]
    overview_page = await mongo.documents().find_one({"workspace_id": workspace_id, "purpose": overview.PURPOSE}, {"_id": 1})
    skip = {ref} | ({(EvidenceKind.DOCUMENT, overview_page["_id"])} if overview_page else set())
    return [r for r in dict.fromkeys(found) if r not in skip][:MAX_RELATED]


def _write_up(suggestion: Suggestion, titles: dict[str, str], quoted: set[str]) -> tuple[str, list[str]]:
    """The suggestion's details as Markdown, keeping only citations of sources the AI was given, and the
    sources it quotes or cites (`S1`, `S2`...) in order."""

    def cited(text: str) -> str:
        return _CITATION.sub(lambda m: m.group(0) if m.group(1) in titles else "", text).strip()

    parts = [
        f"### {heading}\n{cited(text)}"
        for heading, text in (("Background", suggestion.background), ("What it means", suggestion.meaning), ("Example", suggestion.example))
        if text.strip()
    ]
    for heading, points in (("Pros", suggestion.pros), ("Cons and trade-offs", suggestion.cons)):
        kept = [cited(point) for point in points[:MAX_POINTS] if point.strip()]
        if kept:
            parts.append(f"### {heading}\n" + "\n".join(f"- {point}" for point in kept))
    used = quoted | {sid for part in parts for sid in _CITATION.findall(part)}
    body = "\n\n".join(parts)
    if len(body) > CONTEXT["max_length"]:
        body = body[: CONTEXT["max_length"] - 1].rstrip() + "…"
    return body, sorted(used, key=lambda sid: int(sid[1:]))


async def _known(workspace_id: str, ref_ids: list[str]) -> set[tuple[str, str]]:
    """Quotes (as `(ref_id, fingerprint)`) already recorded from these sources, or dismissed as not a decision."""
    known = {
        (scan["ref_id"], dismissed)
        async for scan in mongo.decision_scans().find({"workspace_id": workspace_id, "ref_id": {"$in": ref_ids}})
        for dismissed in scan.get("dismissed_quotes", [])
    }
    async for decision in mongo.decisions().find({"workspace_id": workspace_id, "evidence.ref_id": {"$in": ref_ids}}, {"evidence": 1}):
        known |= {(e["ref_id"], quotes.fingerprint(e["quote"])) for e in decision["evidence"] if e["ref_id"] in ref_ids}
    return known


async def _suggest(member: dict, kind: EvidenceKind, ref_id: str, source: dict) -> list[dict]:
    workspace_id = member["workspace_id"]
    fingerprint = quotes.fingerprint(source["text"])
    scan = await mongo.decision_scans().find_one({"workspace_id": workspace_id, "ref_id": ref_id}) or {}
    if scan.get("fingerprint") == fingerprint:
        raise ConflictError("Nothing has changed here since it was last checked", error_code="source_unchanged")

    await limits.take_suggestion(workspace_id)
    related = await _related(workspace_id, member["user_id"], (kind, ref_id), source["text"])
    found = await service.sources(workspace_id, related) if related else {}
    # S1 is the source asked about; the related ones follow in order
    sources = {"S1": ((kind, ref_id), source)} | {
        f"S{n}": (ref, found[ref]) for n, ref in enumerate([r for r in related if r in found], 2)
    }
    prompt = "\n\n".join(
        ai.fenced(src["text"], source_id=sid, limit=ai.MAX_SOURCE_CHARS if sid == "S1" else RELATED_CHARS)
        for sid, (_, src) in sources.items()
    )
    reply = await ai.ask_json(
        Reply, SYSTEM_INSTRUCTION, prompt, workspace_id=workspace_id, user_id=member["user_id"],
        feature=UsageFeature.DECISION_SUGGEST, timeout=TIMEOUT_SECONDS,
    )
    suggestions = reply.decisions[:MAX_SUGGESTIONS]

    # nothing already quoted from here, and nothing someone said isn't a decision
    known = await _known(workspace_id, [ref[1] for ref, _ in sources.values()])
    titles = {sid: src["title"] for sid, (_, src) in sources.items()}
    created = []
    for suggestion in suggestions:
        evidence: list[NewEvidence] = []
        quoted: set[str] = set()
        marks: set[tuple[str, str]] = set()
        for quote in suggestion.quotes:
            if quote.source not in sources or len(evidence) == MAX_QUOTES:
                continue
            (quote_kind, quote_ref), quoted_source = sources[quote.source]
            mark = (quote_ref, quotes.fingerprint(quote.text))
            if mark in known | marks or not quotes.contains(quoted_source["text"], quote.text):
                continue
            marks.add(mark)
            quoted.add(quote.source)
            evidence.append(NewEvidence(kind=quote_kind, ref_id=quote_ref, quote=quote.text))
        if "S1" not in quoted:
            continue  # what S1 records is the point; a decision only the related sources state isn't one
        # only a kept suggestion's quotes are used up, so a dropped one leaves them for the next
        known |= marks
        context_md, used = _write_up(suggestion, titles, quoted)
        body = CreateDecision(
            # the title and statement are what's shown and checked against: citations belong in the details
            title=_CITATION.sub("", suggestion.title).strip(), statement=_CITATION.sub("", suggestion.statement).strip(),
            status=DecisionStatus.PROPOSED, context_md=context_md, evidence=evidence,
        )
        # numbered as the write-up cites them, so [S2] in it still means the same source
        cited = [
            {"id": sid, "kind": sources[sid][0][0], "ref_id": sources[sid][0][1], "title": titles[sid]} for sid in used
        ]
        created.append(await service.create(member, body, DecisionOrigin.SUGGESTED, cited=cited))
    logger.info("decision_suggest", ref_id=ref_id, related=len(sources) - 1, suggested=len(suggestions), kept=len(created))

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
    await decision_comments.delete_for_decision(workspace_id, decision_id)
    # anyone viewing it learns it's gone, and stops listening for it
    topic = topics.name("decision", decision_id)
    get_hub().publish(topic, EventName.DECISION_DELETED, {"id": decision_id})
    get_hub().close_topic(topic, UnsubscribeReason.GONE)
    for evidence in decision["evidence"]:
        await mongo.decision_scans().update_one(
            {"workspace_id": workspace_id, "ref_id": evidence["ref_id"]},
            {"$addToSet": {"dismissed_quotes": quotes.fingerprint(evidence["quote"])}}, upsert=True,
        )
