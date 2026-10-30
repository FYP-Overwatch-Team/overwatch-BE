"""Checking accepted decisions against what changed since, and a newly accepted one against what's there.

Every workspace is checked as often as it chose (every half hour unless it changes that, or turns
checks off), and whenever someone asks. A check reads a source with the accepted decisions it
relates to (`CheckMatch`): ones that quote it, quote an item it's linked to, are named by it or
name it, or read close to it in meaning. Only sources whose text changed since they were last read
are read. The AI reads each with those decisions; a passage contradicting one is added to that
decision as `contradicts` evidence, with why. The decision itself stays as it is: it goes to Needs
you, and a person settles it.

When a decision is accepted, the few sources closest to it (by mention, then meaning) are read
against it too, in the background, so what already contradicts it shows without anyone editing it.
"""

import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

from app.core import config
from app.core.constants import (
    CheckMatch,
    DecisionCheckInterval,
    DecisionStatus,
    EmbeddingKind,
    EvidenceKind,
    EvidenceStance,
    UsageFeature,
)
from app.core.exceptions import AppError, ConflictError, ExternalServiceError, FieldValidationError
from app.core.time import utcnow
from app.db import mongo
from app.decisions import ai, leases, limits, mentions, overview, quotes, service
from app.decisions.schemas import NOTE, QUOTE, ChangeCheckSettings, NewEvidence
from app.embeddings import service as embeddings

logger = structlog.get_logger("app.decisions")

SYSTEM_INSTRUCTION = (Path(__file__).parent / "prompts" / "check.md").read_text(encoding="utf-8")
#: who the automatic checks act as, in usage and on the evidence they add
SYSTEM_USER = "system"
#: decisions read with one source; the most strongly related, then the most recently changed
MAX_DECISIONS_PER_SOURCE = 8
#: longer than a whole check can take: a day's cap of model calls, each with its retries
LEASE = timedelta(minutes=30)
#: strongest first
MATCH_ORDER = list(CheckMatch)


class Conflict(BaseModel):
    decision: int
    quote: str = Field(**QUOTE)
    reason: str = Field(min_length=1, **NOTE)


class Reply(BaseModel):
    conflicts: list[Conflict]


Ref = tuple[str, str]
#: the decisions a source relates to: decision id -> (decision, why, what it matched on)
Related = dict[str, tuple[dict, CheckMatch, str | None]]


def _relate(related: dict[Ref, Related], ref: Ref, decision: dict, match: CheckMatch, detail: str | None = None) -> None:
    """Note that `ref` relates to `decision`, keeping the strongest reason."""
    held = related[ref].get(decision["_id"])
    if held is None or MATCH_ORDER.index(match) < MATCH_ORDER.index(held[1]):
        related[ref][decision["_id"]] = (decision, match, detail)


async def _identifiers(workspace_id: str, refs: list[Ref], found: dict[Ref, dict]) -> dict[str, Ref]:
    """How other sources can name each of `refs`: an item's key, a page's id or its Confluence id."""
    named = {found[ref]["key"]: ref for ref in refs if ref in found and "key" in found[ref]}
    page_ids = [ref_id for kind, ref_id in refs if kind == EvidenceKind.DOCUMENT]
    async for doc in mongo.documents().find({"workspace_id": workspace_id, "_id": {"$in": page_ids}}, {"external_id": 1}):
        named[doc["_id"]] = (EvidenceKind.DOCUMENT, doc["_id"])
        if doc.get("external_id"):
            named[doc["external_id"]] = (EvidenceKind.DOCUMENT, doc["_id"])
    return named


async def _quoted_and_linked(workspace_id: str, decisions: list[dict]) -> tuple[dict[Ref, Related], dict[Ref, dict]]:
    """Sources the decisions quote, items linked to quoted items, and what quoted sources name; with the quoted sources."""
    related: dict[Ref, Related] = defaultdict(dict)
    for decision in decisions:
        for evidence in decision["evidence"]:
            _relate(related, (evidence["kind"], evidence["ref_id"]), decision, CheckMatch.QUOTED)
    quoted = {ref: list(by_id.values()) for ref, by_id in related.items()}
    found = await service.sources(workspace_id, list(quoted)) if quoted else {}

    quoted_items = [ref_id for kind, ref_id in quoted if kind == EvidenceKind.WORK_ITEM]
    async for item in mongo.work_items().find({"workspace_id": workspace_id, "_id": {"$in": quoted_items}}):
        for linked in await service.linked_items(workspace_id, item):
            for decision, _, _ in quoted[(EvidenceKind.WORK_ITEM, item["_id"])]:
                _relate(related, (EvidenceKind.WORK_ITEM, linked["_id"]), decision, CheckMatch.LINKED, linked["key"])
    for ref, source in found.items():
        for named_ref, name in await mentions.named_in(workspace_id, source["text"]):
            for decision, _, _ in quoted[ref]:
                _relate(related, named_ref, decision, CheckMatch.MENTIONED, name)
    return related, found


async def _changed(workspace_id: str, since: datetime) -> tuple[dict[Ref, dict], set[Ref]]:
    """Items and live pages written since `since`, and which of them are new.

    All of them: a full tracker sync rewrites every item, so a cut would drop real edits among them.
    """
    written = {"workspace_id": workspace_id, "updated_at": {"$gte": since}}
    fields = {"created_at": 1}
    refs, new = [], set()
    for kind, collection, query in (
        (EvidenceKind.WORK_ITEM, mongo.work_items(), written),
        (EvidenceKind.DOCUMENT, mongo.documents(), written | mentions.LIVE_PAGES),
    ):
        async for doc in collection.find(query, fields).sort("updated_at", -1):
            refs.append((kind, doc["_id"]))
            if doc.get("created_at") and doc["created_at"] >= since:
                new.add((kind, doc["_id"]))
    return (await service.sources(workspace_id, refs) if refs else {}), new


async def _edited(workspace_id: str, written: dict[Ref, dict], new: set[Ref]) -> list[Ref]:
    """Which written sources' text really changed: syncs rewrite items whose text stayed the same.

    Without embeddings every written source counts, as each is still read at most once per version.
    """
    if not embeddings.enabled():
        return list(written)
    edited = []
    for kind in (EvidenceKind.WORK_ITEM, EvidenceKind.DOCUMENT):
        refs = [ref for ref in written if ref[0] == kind]
        before = await embeddings.fingerprints(workspace_id, EmbeddingKind(kind), [ref_id for _, ref_id in refs])
        for ref in refs:
            last = before.get(ref[1])
            # one never embedded is only new if it was created since; otherwise it's the backlog being filled
            if (last is not None and last != embeddings.fingerprint(written[ref]["text"])) or (last is None and ref in new):
                edited.append(ref)
    return edited


async def _embed(workspace_id: str, user_id: str, texts: dict[Ref, str]) -> dict[Ref, list[float]]:
    vectors = {}
    for kind in (EvidenceKind.WORK_ITEM, EvidenceKind.DOCUMENT):
        by_id = {ref_id: text for (k, ref_id), text in texts.items() if k == kind}
        vectors |= {(kind, ref_id): v for ref_id, v in (await embeddings.refresh(workspace_id, user_id, EmbeddingKind(kind), by_id)).items()}
    return vectors


async def _decision_vectors(workspace_id: str, user_id: str, decisions: list[dict]) -> dict[str, list[float]]:
    texts = {d["_id"]: f"{d['title']}\n{d['statement']}" for d in decisions}
    return await embeddings.refresh(workspace_id, user_id, EmbeddingKind.DECISION, texts)


async def _fill_backlog(workspace_id: str, user_id: str, room: int) -> None:
    """Embed up to `room` items and pages that have no vector yet, so meaning searches cover them.

    The embedding service being down only delays this; the next check carries on.
    """
    if not embeddings.enabled() or room <= 0:
        return
    try:
        await _embed_backlog(workspace_id, user_id, room)
    except ExternalServiceError:
        logger.warning("embedding_backlog_unavailable", workspace_id=workspace_id)


async def _embed_backlog(workspace_id: str, user_id: str, room: int) -> None:
    for kind, collection, query in (
        (EvidenceKind.WORK_ITEM, mongo.work_items(), {"workspace_id": workspace_id}),
        (EvidenceKind.DOCUMENT, mongo.documents(), {"workspace_id": workspace_id, **mentions.LIVE_PAGES}),
    ):
        ids = [doc["_id"] async for doc in collection.find(query, {"_id": 1}).sort("updated_at", -1)]
        have = await embeddings.fingerprints(workspace_id, EmbeddingKind(kind), ids)
        missing = [ref_id for ref_id in ids if ref_id not in have][:room]
        if missing:
            found = await service.sources(workspace_id, [(kind, ref_id) for ref_id in missing])
            await _embed(workspace_id, user_id, {ref: source["text"] for ref, source in found.items()})
            room -= len(missing)
        if room <= 0:
            return


def _similar(vector: list[float], decisions: list[dict], decision_vectors: dict[str, list[float]]) -> list[tuple[dict, float]]:
    settings = config.get_settings()
    scored = [(d, embeddings.cosine(vector, decision_vectors[d["_id"]])) for d in decisions if d["_id"] in decision_vectors]
    close = [(d, score) for d, score in scored if score >= settings.decision_similarity_threshold]
    return sorted(close, key=lambda row: -row[1])[:settings.decision_similar_per_source]


async def _check_source(workspace_id: str, user_id: str, ref: Ref, text: str, related: list[tuple[dict, CheckMatch, str | None]]) -> int:
    """Read one source against the decisions it relates to; how many contradictions were added."""
    decisions = [decision for decision, _, _ in related]
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
        decision, match, detail = related[conflict.decision - 1]
        if quotes.fingerprint(conflict.quote) in {quotes.fingerprint(e["quote"]) for e in decision["evidence"]}:
            continue
        evidence = NewEvidence(
            kind=ref[0], ref_id=ref[1], quote=conflict.quote, stance=EvidenceStance.CONTRADICTS, note=conflict.reason,
        )
        try:
            await service.add_evidence(member, decision["_id"], evidence, {"reason": match, "detail": detail})
        except FieldValidationError:
            continue  # the decision holds all the passages it can
        added += 1
    return added


def _strongest(related: Related, recency: dict[str, int]) -> list[tuple[dict, CheckMatch, str | None]]:
    """The decisions to read a source with: most strongly related first, then most recently changed."""
    ordered = sorted(related.values(), key=lambda row: (MATCH_ORDER.index(row[1]), recency[row[0]["_id"]]))
    return ordered[:MAX_DECISIONS_PER_SOURCE]


async def check_workspace(workspace_id: str, user_id: str = SYSTEM_USER) -> dict:
    """Check what changed since the last check; `capped` when the day's checks ran out on the way.

    One check runs per workspace at a time; another meanwhile raises `check_in_progress`.
    """
    busy = ConflictError("A check is already running for this workspace", error_code="check_in_progress")
    async with leases.lease(mongo.decision_check_settings(), {"_id": workspace_id}, LEASE, busy):
        return await _check(workspace_id, user_id)


async def _check(workspace_id: str, user_id: str) -> dict:
    started = utcnow()
    await service.refresh_attention(workspace_id)
    settings_doc = await mongo.decision_check_settings().find_one({"_id": workspace_id}) or {}
    interval = timedelta(minutes=settings_doc.get("interval_minutes", DecisionCheckInterval.HALF_HOUR))
    since = settings_doc.get("scanned_until") or started - interval

    decisions = await mongo.decisions().find({"workspace_id": workspace_id, "status": DecisionStatus.ACCEPTED}).sort("updated_at", -1).to_list(None)
    recency = {d["_id"]: n for n, d in enumerate(decisions)}
    related, found = await _quoted_and_linked(workspace_id, decisions)
    written, new = await _changed(workspace_id, since)
    found |= written
    edited = await _edited(workspace_id, written, new)

    # sources that changed and name something a decision quotes, or read close to a decision
    named = await _identifiers(workspace_id, [ref for ref, by_id in related.items() if any(m == CheckMatch.QUOTED for _, m, _ in by_id.values())], found)
    try:
        vectors = await _embed(workspace_id, user_id, {ref: written[ref]["text"] for ref in edited})
        decision_vectors = await _decision_vectors(workspace_id, user_id, decisions) if vectors else {}
    except ExternalServiceError:
        # meaning is an extra: quoted, linked and mentioned sources are still read without it
        logger.warning("decision_check_embeddings_unavailable", workspace_id=workspace_id)
        vectors, decision_vectors = {}, {}
    for ref in edited:
        text = written[ref]["text"]
        for name in mentions.keys(text) | mentions.page_ids(text):
            quoted = named.get(name)
            if quoted and quoted != ref:
                for decision, match, _ in list(related[quoted].values()):
                    if match == CheckMatch.QUOTED:
                        _relate(related, ref, decision, CheckMatch.MENTIONED, name)
        if ref in vectors:
            for decision, score in _similar(vectors[ref], decisions, decision_vectors):
                _relate(related, ref, decision, CheckMatch.SIMILAR, f"{score:.2f}")
    await _fill_backlog(workspace_id, user_id, config.get_settings().embedding_batch_limit - len(vectors))

    unread = [ref for ref in related if ref not in found]
    found |= await service.sources(workspace_id, unread) if unread else {}
    last_read = {doc["ref_id"]: doc["fingerprint"] async for doc in mongo.decision_checks().find({"workspace_id": workspace_id})}
    checked = conflicts = 0
    capped = False
    # strongest reasons first, so a day's cap is spent on them before sources that only read alike
    for ref in sorted(related, key=lambda r: min(MATCH_ORDER.index(m) for _, m, _ in related[r].values())):
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
        conflicts += await _check_source(workspace_id, user_id, ref, source["text"], _strongest(related[ref], recency))
        checked += 1
        await mongo.decision_checks().update_one(
            {"workspace_id": workspace_id, "ref_id": ref[1]},
            {"$set": {"kind": ref[0], "fingerprint": fingerprint, "checked_at": utcnow()}}, upsert=True,
        )
    result = {"checked": checked, "conflicts": conflicts, "capped": capped}
    done = {"last_run_at": utcnow(), "last_result": result}
    if not capped:
        # what's left unread when capped is picked up from the same point next time
        done["scanned_until"] = started
    await mongo.decision_check_settings().update_one({"_id": workspace_id}, {"$set": done}, upsert=True)
    logger.info("decision_check", workspace_id=workspace_id, by=user_id, **result)
    return result


async def _mentioning(workspace_id: str, named: dict[str, Ref], limit: int) -> list[tuple[Ref, str]]:
    """Items and pages whose text names an item by key or links a page in `named`, with the name found."""
    if not named:
        return []
    # a key anywhere; a page id only in a link to it, so a number that happens to match isn't one
    names = [
        re.escape(name) if ref[0] == EvidenceKind.WORK_ITEM else rf"(?<=/documents/){re.escape(name)}|(?<=/pages/){re.escape(name)}"
        for name, ref in named.items()
    ]
    pattern = re.compile(r"\b(" + "|".join(names) + r")\b")
    found = []
    for kind, collection, field, query in (
        (EvidenceKind.WORK_ITEM, mongo.work_items(), "description_md", {"workspace_id": workspace_id}),
        (EvidenceKind.DOCUMENT, mongo.documents(), "content_md", {"workspace_id": workspace_id, **mentions.LIVE_PAGES}),
    ):
        async for doc in collection.find(query | {field: {"$regex": pattern.pattern}}, {field: 1}).limit(limit):
            if match := pattern.search(doc.get(field) or ""):
                found.append(((kind, doc["_id"]), match.group(1)))
    return found


async def scan_accepted(workspace_id: str, decision_id: str, user_id: str) -> dict:
    """Read the sources closest to a newly accepted decision against it: those naming what it quotes, then by meaning."""
    decision = await mongo.decisions().find_one({"_id": decision_id, "workspace_id": workspace_id, "status": DecisionStatus.ACCEPTED})
    if decision is None:
        return {"checked": 0, "conflicts": 0, "capped": False}
    settings = config.get_settings()
    limit = settings.decision_accept_scan_limit
    quoted = {(e["kind"], e["ref_id"]) for e in decision["evidence"]}
    quoted_sources = await service.sources(workspace_id, list(quoted)) if quoted else {}
    named = await _identifiers(workspace_id, list(quoted), quoted_sources)

    candidates: dict[Ref, tuple[CheckMatch, str | None]] = {}
    for ref, name in await _mentioning(workspace_id, named, limit):
        candidates.setdefault(ref, (CheckMatch.MENTIONED, name))
    await _fill_backlog(workspace_id, user_id, settings.embedding_batch_limit)
    try:
        vector = await embeddings.embed_query(workspace_id, user_id, f"{decision['title']}\n{decision['statement']}")
    except ExternalServiceError:
        logger.warning("decision_accept_scan_embeddings_unavailable", workspace_id=workspace_id)
        vector = None
    if vector is not None:
        kinds = [EmbeddingKind.WORK_ITEM, EmbeddingKind.DOCUMENT]
        for kind, ref_id, score in await embeddings.nearest(workspace_id, vector, kinds, limit + len(quoted) + 1):
            if score >= settings.decision_similarity_threshold:
                candidates.setdefault((kind, ref_id), (CheckMatch.SIMILAR, f"{score:.2f}"))
    overview_page = await mongo.documents().find_one({"workspace_id": workspace_id, "purpose": overview.PURPOSE}, {"_id": 1})
    skip = quoted | ({(EvidenceKind.DOCUMENT, overview_page["_id"])} if overview_page else set())
    refs = [ref for ref in candidates if ref not in skip][:limit]
    found = await service.sources(workspace_id, refs) if refs else {}

    checked = conflicts = 0
    capped = False
    for ref in refs:
        if ref not in found:
            continue
        try:
            await limits.take_check(workspace_id)
        except AppError:
            capped = True
            break
        match, detail = candidates[ref]
        conflicts += await _check_source(workspace_id, user_id, ref, found[ref]["text"], [(decision, match, detail)])
        checked += 1
    result = {"checked": checked, "conflicts": conflicts, "capped": capped}
    logger.info("decision_accept_scan", workspace_id=workspace_id, decision_id=decision_id, by=user_id, **result)
    return result


async def scan_accepted_in_background(workspace_id: str, decision_id: str, user_id: str) -> None:
    """`scan_accepted` after the request answered; a failure is logged, the decision stays accepted."""
    try:
        await scan_accepted(workspace_id, decision_id, user_id)
    except Exception:
        logger.exception("decision_accept_scan_failed", workspace_id=workspace_id, decision_id=decision_id)


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
