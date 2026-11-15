"""Load and cost benchmark of the Slack decision pipeline; not part of the test suite.

Runs against a local Mongo database of its own, `overwatch_bench`, which it drops at the end unless
`--keep`, and refuses any other. `replay` and `load` never call a model; `live` calls the real one
and stops once what it billed reaches `--budget-usd`.

    uv run python scripts/bench_slack.py replay --out <dir> --label before
    uv run python scripts/bench_slack.py load --out <dir> --label before
    uv run python scripts/bench_slack.py live --out <dir> --label low --budget-usd 0.25
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import random
import statistics
import sys
import time
import tracemalloc
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BENCH_DB, BENCH_URI = "overwatch_bench", "mongodb://localhost:27017"
WORKSPACE, TEAM = "bench-ws", "T0BENCH"
SECRET = "bench-signing-secret"
VOLUMES = [(500, 1), (2000, 3), (10000, 10)]
TICK, RETENTION, READS_CAP = 300, 3 * 3600, 100
UNCAPPED = 10**9
RUNS = 5


def configure() -> None:
    """Settings for this process; set before `app` is imported, so they're the ones read."""
    os.environ |= {
        "MONGO_DB_NAME": BENCH_DB, "MONGO_URI": BENCH_URI, "SLACK_SIGNING_SECRET": SECRET, "EMBEDDING_MODEL": "",
        "SLACK_DECISION_SCANS_PER_DAY": "1000000", "CHAT_DAILY_BUDGET": "1000000", "AI_WORKSPACE_MONTHLY_LIMIT": "1000000",
    }


def replay_once(posted, cap: int) -> dict:
    """The real grouping and readiness rules over a simulated day of 5-minute ticks; no database, no model."""
    from app.slack import decisions as d

    message = {m.ts: d.Message(m.ts, m.user, m.text, m.thread_ts) for m in posted}
    start = float(posted[0].ts)
    end = float(posted[-1].ts) + RETENTION + TICK
    settles = hasattr(d, "settled_without_a_read")
    totals = {"ticks": 0, "ticks_loading": 0, "messages_loaded": 0, "reads": 0, "forced_reads": 0, "capped_ticks": 0, "chars_read": 0}
    read_ids: set[str] = set()
    used, tick = 0, start + TICK
    while tick <= end:
        totals["ticks"] += 1
        alive = [m for m in message.values() if tick - float(m.ts) < RETENTION and float(m.ts) <= tick]
        waiting = any(m.read_ref is None and tick - float(m.ts) >= d.QUIET_SECONDS for m in alive)
        if settles and (used >= cap or not waiting):
            totals["capped_ticks"] += used >= cap
            tick += TICK
            continue
        alive = sorted(alive, key=lambda m: float(m.ts), reverse=True)[: d.MAX_MESSAGES]
        if alive:
            totals["ticks_loading"] += 1
            totals["messages_loaded"] += len(alive)
        channel_of = {m.ts: m.channel for m in posted}
        by_channel: dict[str, list] = {}
        for m in alive:
            by_channel.setdefault(channel_of[m.ts], []).append(m)
        limited = False
        for channel, messages in by_channel.items():
            for c in d.conversations(channel, messages):
                if settles and d.settled_without_a_read(c, tick):
                    for m in c.messages:
                        message[m.ts] = replace(m, read_ref=c.root)
                    continue
                if not c.is_ready(tick):
                    continue
                if used >= cap:
                    limited = True
                    continue
                used += 1
                totals["reads"] += 1
                totals["forced_reads"] += tick - float(c.messages[-1].ts) < d.QUIET_SECONDS
                totals["chars_read"] += sum(len(m.text) + 8 for m in c.messages)
                read_ids.add(f"{channel}:{c.root}")
                for m in c.messages:
                    message[m.ts] = replace(m, read_ref=c.root)
        totals["capped_ticks"] += limited
        tick += TICK
    every = [c for ch in {m.channel for m in posted} for c in d.conversations(ch, [message[m.ts] for m in posted if m.channel == ch])]
    needed = lambda c: len(c.messages) >= (d.MIN_THREAD_MESSAGES if c.is_thread else d.MIN_MESSAGES) and len({m.user for m in c.messages}) >= d.MIN_PEOPLE
    discussions = [c for c in every if needed(c)]
    return totals | {
        "conversations": len(every),
        "threads": sum(c.is_thread for c in every),
        "largest_run": max((len(c.messages) for c in every if not c.is_thread), default=0),
        "discussions": len(discussions),
        "discussions_read": len(read_ids),
        "discussions_never_read": sum(all(m.read_ref is None for m in c.messages) for c in discussions),
        "messages_expired_unread_in_discussions": sum(m.read_ref is None for c in discussions for m in c.messages),
    }


def replay() -> dict:
    from scripts.bench.corpus import MIXES, generate

    rows = []
    for messages, channels in VOLUMES:
        for mix in MIXES:
            posted = generate(messages, channels, mix)
            rows.append({"messages": messages, "channels": channels, "mix": mix, "capped": replay_once(posted, READS_CAP), "uncapped": replay_once(posted, UNCAPPED)})
            print(f"replay {messages}/{channels}/{mix}: reads {rows[-1]['capped']['reads']} of {rows[-1]['uncapped']['reads']}")
    return {"rows": rows}


async def fresh_database():
    from app.core.config import get_settings
    from app.db import mongo

    settings = get_settings()
    if settings.mongo_db_name != BENCH_DB or not settings.mongo_uri.startswith("mongodb://localhost"):
        raise SystemExit("refusing to run against anything but the local benchmark database")
    db = mongo.connect()
    await db.client.drop_database(BENCH_DB)
    await mongo.ensure_indexes()
    return db


def signed(body: bytes) -> dict:
    stamp = str(int(time.time()))
    signature = hmac.new(SECRET.encode(), b"v0:" + stamp.encode() + b":" + body, hashlib.sha256).hexdigest()
    return {"x-slack-request-timestamp": stamp, "x-slack-signature": f"v0={signature}", "content-type": "application/json"}


async def seed(channels: int) -> None:
    from app.connectors import repository as connections
    from app.db import mongo

    await mongo.workspaces().insert_one({"_id": WORKSPACE, "name": "Bench", "deleted_at": None})
    await connections.save_connection(
        WORKSPACE, "slack", auth_backend="native_oauth", scopes=[], connected_by="bench",
        external_account={"id": TEAM, "name": "Bench", "url": "https://bench.slack.com/"},
    )
    for n in range(channels):
        await connections.add_target(WORKSPACE, "slack", f"C{n:03d}", f"#c{n:03d}")


def event_of(m) -> bytes:
    event = {"type": "message", "channel": m.channel, "user": m.user, "text": m.text, "ts": m.ts}
    if m.thread_ts:
        event["thread_ts"] = m.thread_ts
    return json.dumps({"type": "event_callback", "team_id": TEAM, "event": event}).encode()


async def ingest(posted) -> dict:
    """Signed events through the real endpoint and buffer; application cost only, no network."""
    import httpx

    from app.db import mongo
    from app.main import create_app
    from app.slack.messages import FLUSH_AT, buffer

    bodies = [event_of(m) for m in posted]
    transport = httpx.ASGITransport(app=create_app(manage_db=False))
    flush_times, statuses = [], set()
    began = time.perf_counter()
    async with httpx.AsyncClient(transport=transport, base_url="http://bench") as client:
        for body in bodies:
            statuses.add((await client.post("/webhooks/slack/events", content=body, headers=signed(body))).status_code)
            if len(buffer) >= FLUSH_AT:
                at = time.perf_counter()
                await buffer.flush()
                flush_times.append(time.perf_counter() - at)
        at = time.perf_counter()
        await buffer.flush()
        flush_times.append(time.perf_counter() - at)
    seconds = time.perf_counter() - began
    stored = await mongo.channel_messages().count_documents({"workspace_id": WORKSPACE})
    replies = await mongo.channel_messages().count_documents({"workspace_id": WORKSPACE, "thread_ts": {"$type": "string"}})
    return {
        "events": len(bodies), "statuses": sorted(statuses), "seconds": round(seconds, 3),
        "events_per_second": round(len(bodies) / seconds), "flushes": len(flush_times),
        "flush_ms_median": round(statistics.median(flush_times) * 1000, 1),
        "stored": stored, "stored_replies": replies, "expected_replies": sum(m.thread_ts is not None for m in posted),
    }


async def fill(rows: list[dict]) -> None:
    from app.core.time import utcnow
    from app.db import mongo

    await mongo.channel_messages().delete_many({})
    await mongo.channel_messages().insert_many([{"workspace_id": WORKSPACE, "provider": "slack", "team_id": TEAM, "received_at": utcnow(), **row} for row in rows])


async def reset_reads() -> None:
    from app.db import mongo

    await mongo.channel_messages().update_many({}, {"$unset": {"read_ref": ""}})
    await mongo.chat_threads().delete_many({})
    await mongo.decision_scans().delete_many({})
    await mongo.usage_counters().delete_many({})
    await mongo.llm_usage().delete_many({})


async def measure(fake, *, resets: bool, prepare=None) -> dict:
    from app.db import mongo
    from app.slack import decisions as d

    times, reads = [], []
    for _ in range(RUNS):
        if resets:
            await reset_reads()
        if prepare:
            await prepare()
        before = len(fake.calls)
        at = time.perf_counter()
        await d.scan_workspace(WORKSPACE)
        times.append(time.perf_counter() - at)
        reads.append(len(fake.calls) - before)
    if resets:
        await reset_reads()
    if prepare:
        await prepare()
    tracemalloc.start()
    await d.scan_workspace(WORKSPACE)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    copies = await mongo.chat_threads().count_documents({})
    return {
        "ms_median": round(statistics.median(times) * 1000, 1), "ms_min": round(min(times) * 1000, 1), "ms_max": round(max(times) * 1000, 1),
        "model_calls_each_run": reads, "peak_mb": round(peak / 1_048_576, 2), "copies_left": copies,
    }


def _stages(stage: dict) -> list[dict]:
    children = [stage["inputStage"]] if "inputStage" in stage else stage.get("inputStages", [])
    return [stage, *(found for child in children for found in _stages(child))]


async def ticks(fake) -> dict:
    from app.core.time import utcnow
    from app.db import mongo
    from app.slack import decisions as d

    old = time.time() - 40 * 60
    lone = [{"channel": f"C{i % 3:03d}", "ts": f"{old - i * 700:.6f}", "user": f"U{i:04d}", "text": "word " * 12, "thread_ts": None} for i in range(5000)]

    def thread(n: int) -> list[dict]:
        root = f"{old - n * 2000:.6f}"
        return [
            {"channel": "C000", "ts": root, "user": "U1", "text": "word " * 12, "thread_ts": None},
            {"channel": "C000", "ts": f"{old - n * 2000 + 1:.6f}", "user": "U2", "text": "word " * 12, "thread_ts": root},
        ]

    await fill([{**row, "read_ref": "seen"} for row in lone])
    results = {"idle_5000_read": await measure(fake, resets=False)}
    channels = ["C000", "C001", "C002"]
    gate = {"workspace_id": WORKSPACE, "channel": {"$in": channels}, "read_ref": None, "ts": {"$lte": f"{old:.6f}"}}
    plan = await mongo.channel_messages().find(gate, {"_id": 1}).limit(1).explain()
    results["gate_plan"] = {
        "index": [s.get("indexName") for s in _stages(plan["queryPlanner"]["winningPlan"]) if s.get("indexName")],
        "docs_examined": plan["executionStats"]["totalDocsExamined"], "keys_examined": plan["executionStats"]["totalKeysExamined"],
    }
    await fill(lone)
    results["noise_5000_unread"] = await measure(fake, resets=False)

    await fill([row for n in range(100) for row in thread(n)])
    results["backlog_100_threads"] = await measure(fake, resets=True)

    async def at_cap() -> None:
        [window, *_] = d._scan_windows(WORKSPACE, utcnow())
        await mongo.usage_counters().update_one({"_id": window.key}, {"$set": {"count": window.limit}}, upsert=True)

    results["capped_100_threads"] = await measure(fake, resets=True, prepare=at_cap)
    await reset_reads()
    return results


async def load() -> dict:
    from app.db import mongo
    from app.integrations import llm
    from scripts.bench.corpus import generate
    from tests.fakes import FakeLLMClient

    db = await fresh_database()
    if os.environ.get("BENCH_NO_GATE_INDEX"):
        await mongo.channel_messages().drop_index("workspace_id_1_read_ref_1_ts_1")
    await seed(3)
    fake = FakeLLMClient()
    fake.stream_response = json.dumps({"decisions": []})
    llm.use_llm_client(fake)
    try:
        posted = generate(5000, 3, "medium")
        result = {"ingest": await ingest(posted), "ticks": await ticks(fake)}
    finally:
        llm.use_llm_client(None)
    await db.client.drop_database(BENCH_DB)
    mongo.close()
    return result


def wilson(hits: int, total: int) -> list[float]:
    if not total:
        return [0.0, 0.0]
    z, p = 1.96, hits / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    spread = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / (1 + z * z / total)
    return [round(max(0.0, centre - spread), 3), round(min(1.0, centre + spread), 3)]


def bootstrap_mean(values: list[float]) -> list[float]:
    rng = random.Random(7)
    means = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(2000))
    return [round(means[50], 6), round(means[1949], 6)]


def matched(expected: dict, quotes: list[str]) -> bool:
    return any(want in got or got in want for want in expected["quotes"] for got in quotes if got)


def score(rows: list[dict]) -> dict:
    expected = sum(len(r["expected"]) for r in rows)
    found = sum(len(r["found"]) for r in rows)
    recalled = sum(sum(any(matched(e, f["quotes"]) for f in r["found"]) for e in r["expected"]) for r in rows)
    correct = sum(sum(any(matched(e, f["quotes"]) for e in r["expected"]) for f in r["found"]) for r in rows)
    wanted = [r for r in rows if r["open_question"]]
    return {
        "conversations": len(rows), "expected": expected, "found": found,
        "recall": round(recalled / expected, 3) if expected else None, "recall_ci": wilson(recalled, expected),
        "precision": round(correct / found, 3) if found else None, "precision_ci": wilson(correct, found),
        "false_proposals": found - correct, "open_questions_flagged": sum(r["questions"] > 0 for r in wanted), "open_questions": len(wanted),
    }


async def live(budget: float, limit: int | None) -> dict:
    from app.connectors import repository as connections
    from app.core.time import utcnow
    from app.db import mongo
    from app.slack import decisions as d

    db = await fresh_database()
    await seed(0)
    labelled = json.loads((ROOT / "scripts/bench/labelled.json").read_text())["conversations"]
    labelled = labelled[:limit] if limit else labelled
    base, rows, spent, stopped = time.time() - 40 * 60, [], 0.0, None
    for number, item in enumerate(labelled):
        if spent >= budget:
            stopped = f"budget reached at ${spent:.4f}"
            break
        channel = f"L{number:03d}"
        await connections.add_target(WORKSPACE, "slack", channel, f"#{item['id']}")
        stamps = [f"{base + i:.6f}" for i in range(len(item["messages"]))]
        await mongo.channel_messages().insert_many([{
            "workspace_id": WORKSPACE, "channel": channel, "ts": stamps[i], "user": f"U0{user}", "text": text, "provider": "slack", "team_id": TEAM,
            "thread_ts": stamps[0] if item["shape"] == "thread" and i else None, "received_at": utcnow(),
        } for i, (user, text) in enumerate(item["messages"])])
        at = time.perf_counter()
        await d.scan_workspace(WORKSPACE)
        seconds = time.perf_counter() - at
        usage = [u async for u in mongo.llm_usage().find({})]
        if any(u.get("cost_usd") is None for u in usage):
            stopped = "a call reported no cost"
        found = [{"title": x["title"], "quotes": [e["quote"] for e in x.get("evidence", [])]} async for x in mongo.decisions().find({"workspace_id": WORKSPACE})]
        questions = await mongo.chat_questions().count_documents({"workspace_id": WORKSPACE})
        rows.append({
            "id": item["id"], "shape": item["shape"], "group": item["id"][:2], "holdout": item["holdout"], "open_question": item["open_question"],
            "expected": item["decisions"], "found": found, "questions": questions, "calls": len(usage), "seconds": round(seconds, 1),
            "prompt_tokens": sum(u["prompt_tokens"] or 0 for u in usage), "completion_tokens": sum(u["completion_tokens"] or 0 for u in usage),
            "cost_usd": sum(u.get("cost_usd") or 0 for u in usage),
        })
        spent += rows[-1]["cost_usd"]
        print(f"{item['id']}: calls {len(usage)} found {len(found)} questions {questions} ${rows[-1]['cost_usd']:.5f} total ${spent:.4f}", flush=True)
        for collection in (mongo.decisions(), mongo.chat_questions(), mongo.chat_threads(), mongo.decision_scans(), mongo.llm_usage(), mongo.channel_messages(), mongo.usage_counters()):
            await collection.delete_many({})
        if stopped:
            break
    await db.client.drop_database(BENCH_DB)
    mongo.close()
    costs = [r["cost_usd"] for r in rows if r["calls"]]
    groups = {
        group: {
            "n": len(of), "mean_cost": round(statistics.mean(r["cost_usd"] for r in of), 6),
            "mean_prompt": round(statistics.mean(r["prompt_tokens"] for r in of)), "mean_completion": round(statistics.mean(r["completion_tokens"] for r in of)),
        }
        for group in sorted({r["group"] for r in rows}) if (of := [r for r in rows if r["group"] == group and r["calls"]])
    }
    return {
        "spent_usd": round(spent, 5), "stopped": stopped, "failed_reads": sum(r["calls"] != 1 for r in rows),
        "mean_cost": round(statistics.mean(costs), 6) if costs else None, "mean_cost_ci": bootstrap_mean(costs) if costs else None,
        "mean_seconds": round(statistics.mean(r["seconds"] for r in rows), 1), "by_shape": groups,
        "all": score(rows), "tuning": score([r for r in rows if not r["holdout"]]), "holdout": score([r for r in rows if r["holdout"]]),
        "two_message_threads": score([r for r in rows if r["group"] == "t2"]), "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["replay", "load", "live"])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--budget-usd", type=float, default=0.25)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--effort", help="overrides MODEL_REASONING_EFFORT for a live run")
    args = parser.parse_args()
    configure()
    if args.effort is not None:
        os.environ["MODEL_REASONING_EFFORT"] = args.effort
    args.out.mkdir(parents=True, exist_ok=True)
    if args.command == "replay":
        result = replay()
    elif args.command == "load":
        result = asyncio.run(load())
    else:
        result = asyncio.run(live(args.budget_usd, args.limit))
    path = args.out / f"{args.command}-{args.label}.json"
    path.write_text(json.dumps(result, indent=1))
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
