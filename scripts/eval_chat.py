"""Live eval of the AI chat and code search against the real models; not part of the test suite.

Runs in-process through `chat.service.ask_stream`, so access checks, limits, grounding and saving
all run as they do in production. Everything it writes is namespaced: repositories `eval/*`,
workspace `eval-ws`, database `overwatch_eval`, which it drops at the end unless `--keep`.

    uv run python scripts/eval_chat.py retrieval            # search quality, about $0.01
    uv run python scripts/eval_chat.py answers --subset     # 5 answers
    uv run python scripts/eval_chat.py redteam              # planted injection attacks
    uv run python scripts/eval_chat.py atlas                # Atlas vector search on the cluster in .env

Every command but `atlas` uses the local Mongo and Neo4j containers; `atlas` uses MONGO_URI from
.env with the `overwatch_eval` database, and never another one. A run stops once what the models
billed reaches `--budget-usd`.
"""

import argparse
import asyncio
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EVAL_DB = "overwatch_eval"
WORKSPACE, USER = "eval-ws", "eval-user"
MEMBER = {"workspace_id": WORKSPACE, "user_id": USER, "role": "owner"}
BE_REPO, LAB_REPO, OTHER_REPO = "eval/overwatch-be", "eval/injection-lab", "eval/other"
#: never copied into the eval repository: the eval's own payloads, and anything local
SKIPPED_PATHS = ("scripts/eval", "scripts/eval_chat.py", ".env")


def configure(command: str, out: Path) -> None:
    """Settings for the eval process; set before `app` is imported, so they're the ones read."""
    os.environ |= {
        "MONGO_DB_NAME": EVAL_DB,
        "REPOS_DIR": str(out / "eval-repos"),
        "NEO4J_URI": "bolt://localhost:7687", "NEO4J_USER": "neo4j", "NEO4J_PASSWORD": "localdevpassword",
        "CODE_EMBEDDING_RUN_LIMIT": "5000",
        "CHAT_USER_HOURLY_LIMIT": "1000", "CHAT_WORKSPACE_DAILY_LIMIT": "1000", "CHAT_DAILY_BUDGET": "5000",
        "AI_WORKSPACE_MONTHLY_LIMIT": "100000",
    }
    if command == "atlas":
        os.environ["VECTOR_SEARCH"] = "atlas"
    else:
        os.environ |= {"MONGO_URI": "mongodb://localhost:27017", "VECTOR_SEARCH": "local"}
    if command == "retrieval":
        # every score, so the threshold can be chosen from them
        os.environ["CODE_SIMILARITY_THRESHOLD"] = "-1"


class Budget:
    """What the models billed in the eval database so far, against the cap."""

    def __init__(self, cap: float):
        self.cap = cap

    async def spent(self) -> float:
        from app.db import mongo

        rows = mongo.llm_usage().aggregate([{"$group": {"_id": None, "cost": {"$sum": "$cost_usd"}}}])
        return round(sum([row["cost"] or 0 async for row in rows]), 4)

    async def check(self) -> None:
        if (spent := await self.spent()) >= self.cap:
            raise SystemExit(f"budget reached: ${spent} of ${self.cap}")


class RecordingLLM:
    """The real client, keeping what each call was sent."""

    def __init__(self, real):
        self.real = real
        self.calls: list[dict] = []

    def stream_text(self, system_instruction, prompt, **kwargs):
        self.calls.append({"system": system_instruction, "prompt": prompt, "rounds": kwargs.get("rounds", [])})
        return self.real.stream_text(system_instruction, prompt, **kwargs)

    def sent(self) -> str:
        """Every text the model was given in this question: prompts and lookup results."""
        rounds = self.calls[-1]["rounds"] if self.calls else []
        return "\n".join([*(call["prompt"] for call in self.calls), *(r for round_ in rounds for r in round_.results)])

    def lookups(self) -> list[str]:
        rounds = self.calls[-1]["rounds"] if self.calls else []
        return [call.name for round_ in rounds for call in round_.calls]


class RecordingEmbedder:
    """The real embedder, keeping every text it was sent."""

    def __init__(self, real):
        self.real = real
        self.model = real.model
        self.texts: list[str] = []

    async def embed(self, texts, purpose, **kwargs):
        self.texts += list(texts)
        return await self.real.embed(texts, purpose, **kwargs)


async def prepare(repo: str, files: dict[str, str] | None = None) -> str:
    """Check out `repo` (overwatch-BE's own tracked files, or `files`), index and embed it; its commit."""
    from app.db import mongo
    from app.db.knowledge_graph_store import get_knowledge_graph_store
    from app.embeddings import code
    from app.github.checkout import repo_workdir
    from app.knowledge_graph.pipeline import IndexingPipeline
    from app.services.facts_repository import get_facts_store
    from tests.git_fixtures import commit_all

    root = repo_workdir(repo)
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    if files is None:
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        files = {
            path: (ROOT / path).read_text(encoding="utf-8", errors="replace") for path in listed
            if (ROOT / path).is_file() and not path.startswith(SKIPPED_PATHS)
        }
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    sha = commit_all(root, "eval")

    result = await IndexingPipeline(get_facts_store(), get_knowledge_graph_store()).index(repo, root, sha, full=True)
    await mongo.repos().update_one(
        {"workspace_id": WORKSPACE, "repo_full_name": repo},
        {"$set": {"default_branch": "main", "parse_status": "done", "access_status": "active", "graph_version": sha,
                  "installation_id": 0, "connected_at": 1, "updated_at": 1}},
        upsert=True,
    )
    facts = await get_facts_store().load_all(repo)
    counts = await code.sync(
        repo, sha, facts, include_symbols=result.stats.get("symbol_layer") == "included", workspace_id=WORKSPACE,
    )
    print(f"{repo}: indexed at {sha[:8]}, vectors {counts}")
    return sha


async def ask(question: str, repo: str, llm: RecordingLLM) -> dict:
    """One question through the chat as a member would ask it, with what it cost and what was sent."""
    from app.chat.schemas import AskRequest
    from app.chat.service import ask_stream
    from app.core.exceptions import AppError

    llm.calls.clear()
    started = time.monotonic()
    done = None
    try:
        async for event, data in ask_stream(MEMBER, AskRequest(repo_full_name=repo, question=question)):
            if event == "done":
                done = data
    except AppError as exc:
        return {"question": question, "error": exc.error_code, "seconds": round(time.monotonic() - started, 1)}
    return {
        "question": question, "answer": done["answer"], "seconds": round(time.monotonic() - started, 1),
        "lookups": llm.lookups(), "model_calls": len(llm.calls), "sent": llm.sent(),
    }


async def citations_match(answer: dict, repo: str) -> bool:
    """Every code citation shows the file's own lines at its commit."""
    from app.query.source import read_file

    for citation in answer["citations"]:
        text = await read_file(repo, citation["commit_sha"], citation["path"])
        if text is None:
            return False
        lines = text.splitlines()[citation["start_line"] - 1:citation["end_line"]]
        if "\n".join(line.rstrip() for line in lines) != citation["code"]:
            return False
    return True


async def retrieval(args, report: dict) -> None:
    from app.db.knowledge_graph_store import get_knowledge_graph_store
    from scripts.eval import retrieval as search_quality

    await prepare(BE_REPO)
    report["retrieval"] = await search_quality.run(get_knowledge_graph_store(), BE_REPO, (WORKSPACE, USER))


async def answers(args, report: dict, llm: RecordingLLM, budget: Budget) -> None:
    from scripts.eval.cases import ANSWERS, UNANSWERABLE

    sha = await prepare(BE_REPO)
    questions = ANSWERS[:5] if args.subset else ANSWERS
    results = []
    for question in questions:
        await budget.check()
        result = await ask(question, BE_REPO, llm)
        answer = result.get("answer")
        if answer:
            text = answer["text"]
            result |= {
                "grounded": answer["grounded"], "nodes": len(answer["nodes"]), "code_citations": len(answer["citations"]),
                "citations_match": await citations_match(answer, BE_REPO),
                "used_meaning_search": "search_code_by_meaning" in result["lookups"],
                "should_refuse": question in UNANSWERABLE, "text": text[:600],
            }
        result.pop("sent", None)
        result.pop("answer", None)
        results.append(result)
        print(f"answer: {question[:60]} -> {result.get('error') or ('grounded' if result['grounded'] else 'not grounded')}")
    report["answers"] = {"commit": sha, "results": results}


async def redteam(args, report: dict, llm: RecordingLLM, embedder: RecordingEmbedder, budget: Budget) -> None:
    from app.query.service import ASK_SYSTEM_INSTRUCTION
    from scripts.eval.redteam import ATTACKS, LAB_FILES, OTHER_FILES, problems_with

    lab_sha = await prepare(LAB_REPO, LAB_FILES)
    await prepare(OTHER_REPO, OTHER_FILES)
    embedded_at_index = list(embedder.texts)
    attacks = ATTACKS[:args.limit] if args.limit else ATTACKS
    results = []
    for attack in attacks:
        runs = []
        # a failure is asked twice more, to tell a flaky one from a real one
        for _ in range(3):
            await budget.check()
            embedder.texts.clear()
            result = await ask(attack.question, LAB_REPO, llm)
            found = problems_with(
                attack, result, system=ASK_SYSTEM_INSTRUCTION, repo=LAB_REPO, commit=lab_sha,
                embedded=[*embedded_at_index, *embedder.texts],
            )
            runs.append({"problems": found, "lookups": result.get("lookups"), "text": result.get("answer", {}).get("text", "")})
            if not found:
                break
        passed = not runs[-1]["problems"]
        results.append({"attack": attack.name, "passed": passed, "runs": len(runs), "detail": runs})
        print(f"redteam: {attack.name} -> {'pass' if passed else 'FAIL'} ({len(runs)} run(s)) {runs[-1]['problems']}")
    report["redteam"] = {
        "passed": sum(result["passed"] for result in results), "total": len(results), "results": results,
        "first_try_failures": sum(result["runs"] > 1 for result in results),
    }


async def atlas(args, report: dict) -> None:
    """$vectorSearch against the local cosine ranking, filters, the packing migration and an index rebuild."""
    from bson.binary import Binary

    from app.core.config import get_settings
    from app.core.constants import EmbeddingKind
    from app.db import mongo
    from app.db.knowledge_graph_store import InMemoryKnowledgeGraphStore
    from app.embeddings import service as embeddings
    from app.integrations.embeddings import get_embedder
    from app.query.context import similar_nodes
    from scripts.migrate_embedding_vectors import migrate

    settings = get_settings()
    db = mongo.get_db()
    if db.name != EVAL_DB or settings.vector_search != "atlas":
        raise SystemExit("refusing: atlas runs only against the overwatch_eval database with VECTOR_SEARCH=atlas")
    client = db.client

    in_use = []
    for name in await client.list_database_names():
        if name in ("admin", "local", "config"):
            continue
        for collection in await client[name].list_collection_names():
            try:
                in_use += [f"{name}.{collection}:{index['name']}" async for index in client[name][collection].list_search_indexes()]
            except Exception:  # noqa: BLE001 - a view or system collection can't hold one
                continue
    stats = await db.command("dbStats")
    print(f"search indexes on the cluster: {in_use}")
    report["atlas"] = {"search_indexes_before": in_use, "eval_db_bytes_before": stats.get("storageSize")}
    if len([index for index in in_use if not index.startswith(f"{EVAL_DB}.")]) >= 3:
        raise SystemExit("no free search index slot on the cluster; not touching dev's own index")

    await mongo.ensure_indexes()
    await _await_index(mongo, "READY")

    dims, model = settings.embedding_dimensions, get_embedder().model
    rng = random.Random(7)

    def vector() -> list[float]:
        return [rng.gauss(0, 1) for _ in range(dims)]

    seeded: list[dict] = []
    for index in range(60):
        repo = "eval/a" if index < 40 else "eval/b"
        seeded.append({"kind": EmbeddingKind.CODE, "repo": repo, "ref_id": f"{repo}:f{index}.py", "model": model, "raw": vector()})
    for index in range(30):
        workspace = "eval-w1" if index < 20 else "eval-w2"
        seeded.append({"kind": EmbeddingKind.DECISION, "workspace_id": workspace, "ref_id": f"d{index}", "model": model, "raw": vector()})
    await mongo.embeddings().insert_many([
        {**{k: v for k, v in doc.items() if k != "raw"}, "vector": doc["raw"] if i % 2 else embeddings.encode(doc["raw"])}
        for i, doc in enumerate(seeded)
    ])
    report["atlas"]["seeded"] = {"lists": sum(i % 2 for i in range(len(seeded))), "packed": len(seeded) // 2 + len(seeded) % 2}

    queries = [vector() for _ in range(5)]
    scopes = {
        "code eval/a": {"repo": "eval/a", "kind": EmbeddingKind.CODE, "model": model},
        "decisions eval-w1": {"workspace_id": "eval-w1", "kind": {"$in": [EmbeddingKind.DECISION]}, "model": model},
    }

    def local(scope: dict, query: list[float], limit: int) -> list[tuple[str, float]]:
        wanted = [
            doc for doc in seeded
            if all(doc.get(key) == value or (isinstance(value, dict) and doc.get(key) in value["$in"]) for key, value in scope.items())
        ]
        scored = sorted(((doc["ref_id"], embeddings.cosine(query, doc["raw"])) for doc in wanted), key=lambda row: -row[1])
        return scored[:limit]

    async def compare(label: str) -> dict:
        outcome = {"same_order": 0, "max_score_gap": 0.0, "leaks": 0, "counts": []}
        for name, scope in scopes.items():
            for query in queries:
                found = [(ref_id, score) for _, ref_id, score in await embeddings._nearest(scope, query, 10)]
                expected = local(scope, query, 10)
                outcome["counts"].append(len(found))
                outcome["same_order"] += [r for r, _ in found] == [r for r, _ in expected]
                gaps = [abs(a - b) for (_, a), (_, b) in zip(found, expected, strict=False)]
                outcome["max_score_gap"] = round(max([outcome["max_score_gap"], *gaps]), 6)
                outcome["leaks"] += sum(
                    not ref_id.startswith("eval/a:") if name.startswith("code") else int(ref_id[1:]) >= 20
                    for ref_id, _ in found
                )
        outcome["of"] = len(scopes) * len(queries)
        print(f"atlas {label}: {outcome}")
        return outcome

    async def settled() -> None:
        """Atlas indexes new writes a moment later; wait until every seeded vector can be found."""
        for _ in range(60):
            found = await embeddings._nearest(scopes["code eval/a"], queries[0], 40)
            if len(found) == 40:
                return
            await asyncio.sleep(2)
        print("atlas: not every vector became searchable within 2 minutes")

    await settled()
    report["atlas"]["mixed_lists_and_packed"] = await compare("mixed")

    dry = await migrate(apply=False)
    await migrate(apply=True)
    remaining = await mongo.embeddings().count_documents({"vector": {"$type": "array"}})
    await settled()
    report["atlas"]["migration"] = {"dry_run": dry, "lists_left": remaining, "after": await compare("after migration")}
    packed = await mongo.embeddings().find_one({}, {"vector": 1})
    report["atlas"]["stored_as_vector_binary"] = isinstance(packed["vector"], Binary) and packed["vector"].subtype == 9

    # an index rebuild while questions arrive: a filter field missing from the index makes the search fail
    async def fixed_query(*args, **kwargs):
        return queries[0]

    embeddings.embed_query = fixed_query
    store = InMemoryKnowledgeGraphStore()
    definition = {"fields": [
        {"type": "vector", "path": "vector", "numDimensions": dims, "similarity": "cosine"},
        {"type": "filter", "path": "workspace_id"}, {"type": "filter", "path": "kind"}, {"type": "filter", "path": "model"},
    ]}
    await mongo.embeddings().update_search_index(mongo.VECTOR_INDEX, definition)
    failures, empty, errors = 0, 0, 0
    started = time.monotonic()
    while time.monotonic() - started < 90:
        try:
            await embeddings._nearest(scopes["code eval/a"], queries[0], 5)
        except Exception:  # noqa: BLE001 - counting what a raw search raises during the rebuild
            failures += 1
        try:
            rows = await similar_nodes(store, "eval/a", "anything", (WORKSPACE, USER))
            empty += rows == []
        except Exception:  # noqa: BLE001 - this is what H1 must prevent
            errors += 1
        await asyncio.sleep(3)
    report["atlas"]["rebuild"] = {
        "raw_search_failures": failures, "chat_search_errors": errors, "chat_search_empty": empty,
    }
    print(f"atlas rebuild: {report['atlas']['rebuild']}")
    await mongo.ensure_indexes()  # back to the real definition before it's dropped


async def _await_index(mongo, status: str) -> None:
    for _ in range(90):
        found = [index async for index in mongo.embeddings().list_search_indexes(mongo.VECTOR_INDEX)]
        if found and found[0].get("status") == status and found[0].get("queryable"):
            return
        await asyncio.sleep(4)
    raise SystemExit(f"the search index never became {status}")


async def cleanup(keep: bool) -> None:
    from app.db import mongo
    from app.db.knowledge_graph_store import get_knowledge_graph_store

    if keep:
        print(f"kept the {EVAL_DB} database and the eval repositories")
        return
    db = mongo.get_db()
    if db.name != EVAL_DB:
        raise SystemExit("refusing to drop a database that isn't the eval one")
    if os.environ.get("VECTOR_SEARCH") != "atlas":
        store = get_knowledge_graph_store()
        for repo in (BE_REPO, LAB_REPO, OTHER_REPO):
            await store.delete_repository(repo)
    await db.client.drop_database(EVAL_DB)
    print(f"dropped {EVAL_DB}")


async def main(args) -> None:
    from app.core.config import get_settings
    from app.db import mongo
    from app.db.knowledge_graph_store import get_knowledge_graph_store
    from app.db.neo4j_client import get_graph_store
    from app.integrations import embeddings as embedder_module
    from app.query import service as query_service

    mongo.connect()
    if mongo.get_db().name != EVAL_DB:
        raise SystemExit("refusing: the eval must run against its own database")
    if args.command != "atlas":  # atlas creates them itself, once it has checked there's room for the search index
        await mongo.ensure_indexes()
        await get_knowledge_graph_store().ensure_schema()
        await get_graph_store().run("CALL db.awaitIndexes(120)")  # keyword search needs its index built

    llm = RecordingLLM(query_service.get_llm_client())
    query_service.get_llm_client = lambda: llm
    real_embedder = embedder_module.get_embedder()
    if real_embedder is None:
        raise SystemExit("EMBEDDING_MODEL is empty; the eval needs the real embedder")
    embedder = RecordingEmbedder(real_embedder)
    embedder_module.use_embedder(embedder)

    budget = Budget(args.budget_usd)
    report: dict = {"command": args.command, "model": get_settings().model_name, "embedding_model": embedder.model}
    try:
        if args.command == "retrieval":
            await retrieval(args, report)
        elif args.command == "answers":
            await answers(args, report, llm, budget)
        elif args.command == "redteam":
            await redteam(args, report, llm, embedder, budget)
        elif args.command == "atlas":
            await atlas(args, report)
    finally:
        report["cost_usd"] = await budget.spent()
        out = Path(args.out) / f"eval-{args.command}.json"
        out.write_text(json.dumps(report, indent=2, default=str))
        print(f"report: {out} (cost ${report['cost_usd']})")
        await cleanup(args.keep)
        mongo.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["retrieval", "answers", "redteam", "atlas"])
    parser.add_argument("--subset", action="store_true", help="answers: only the first five questions")
    parser.add_argument("--limit", type=int, default=0, help="redteam: only the first N attacks")
    parser.add_argument("--budget-usd", type=float, default=0.5)
    parser.add_argument("--out", required=True, help="directory for the report and the eval checkouts")
    parser.add_argument("--keep", action="store_true", help="keep the eval database and graph afterwards")
    parsed = parser.parse_args()
    Path(parsed.out).mkdir(parents=True, exist_ok=True)
    configure(parsed.command, Path(parsed.out))
    asyncio.run(main(parsed))
