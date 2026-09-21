"""The repository index lock: mutual exclusion, crash recovery, no lost pushes."""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db import mongo
from app.services import repo_service
from app.workers import jobs

USER = "user-1"
REPO = "octocat/hello-world"


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _insert_repo(**overrides) -> None:
    await mongo.repos().insert_one({
        "user_id": USER,
        "repo_full_name": REPO,
        "default_branch": "main",
        "parse_status": "pending",
        "parse_error": None,
        "parse_started_at": None,
        "connected_at": _now(),
        "updated_at": _now(),
        **overrides,
    })


async def _repo_doc() -> dict:
    return await mongo.repos().find_one({"user_id": USER, "repo_full_name": REPO})


@pytest.fixture
def record_index_calls(monkeypatch):
    """Replace the real indexing work with recorders."""
    calls: list = []

    async def fake_full(user_id, repo_full_name):
        calls.append("full")

    async def fake_incremental(user_id, repo_full_name, changed, removed):
        calls.append(("incremental", tuple(changed), tuple(removed)))

    monkeypatch.setattr(jobs, "_full_index", fake_full)
    monkeypatch.setattr(jobs, "_incremental_index", fake_incremental)
    return calls


async def test_index_runs_and_releases_the_lock(record_index_calls):
    await _insert_repo()

    await jobs.run_initial_parse(USER, REPO)

    doc = await _repo_doc()
    assert record_index_calls == ["full"]
    assert doc["parse_status"] == "done"
    assert doc["parse_started_at"] is None


async def test_a_held_lock_defers_the_work_instead_of_running_it(record_index_calls):
    await _insert_repo(parse_status="in_progress", parse_started_at=_now())

    await jobs.run_incremental_reparse(USER, REPO, ["src/a.py"], ["src/gone.py"])

    doc = await _repo_doc()
    assert record_index_calls == []  # did not run concurrently
    assert doc["sync_requested_at"] is not None
    assert doc["pending_changed"] == ["src/a.py"]
    assert doc["pending_removed"] == ["src/gone.py"]


async def test_a_stale_lock_from_a_crashed_run_is_reclaimed(record_index_calls):
    stale = _now() - timedelta(hours=2)
    await _insert_repo(parse_status="in_progress", parse_started_at=stale)

    await jobs.run_initial_parse(USER, REPO)

    assert record_index_calls == ["full"]
    assert (await _repo_doc())["parse_status"] == "done"


async def test_a_lock_held_without_a_start_time_is_reclaimed(record_index_calls):
    # Written by a version that did not record start times; otherwise the
    # repository would stay wedged forever.
    await _insert_repo(parse_status="in_progress")

    await jobs.run_initial_parse(USER, REPO)

    assert record_index_calls == ["full"]


async def test_a_push_arriving_mid_index_is_drained_before_release(monkeypatch):
    await _insert_repo()
    calls: list = []

    async def fake_full(user_id, repo_full_name):
        calls.append("full")
        # A webhook lands while the index is running.
        await jobs.run_incremental_reparse(user_id, repo_full_name, ["src/late.py"], [])

    async def fake_incremental(user_id, repo_full_name, changed, removed):
        calls.append(("incremental", tuple(changed)))

    monkeypatch.setattr(jobs, "_full_index", fake_full)
    monkeypatch.setattr(jobs, "_incremental_index", fake_incremental)

    await jobs.run_initial_parse(USER, REPO)

    doc = await _repo_doc()
    assert calls == ["full", ("incremental", ("src/late.py",))]
    assert doc["parse_status"] == "done"
    assert doc["sync_requested_at"] is None


async def test_failure_releases_the_lock_and_records_the_error(monkeypatch):
    await _insert_repo()

    async def failing(user_id, repo_full_name):
        raise RuntimeError("clone exploded")

    monkeypatch.setattr(jobs, "_full_index", failing)

    await jobs.run_initial_parse(USER, REPO)

    doc = await _repo_doc()
    assert doc["parse_status"] == "failed"
    assert "clone exploded" in doc["parse_error"]
    assert doc["parse_started_at"] is None


async def test_indexing_a_real_checkout_writes_a_graph(monkeypatch, tmp_path):
    """The job runs the pipeline over a real checkout and records what it did."""
    from app.core.config import get_settings
    from app.integrations import git_cli
    from app.knowledge_graph.pipeline import IndexingPipeline
    from app.services.facts_repository import InMemoryFactsStore, use_facts_store
    from app.services.knowledge_graph_store import (
        InMemoryKnowledgeGraphStore,
        use_knowledge_graph_store,
    )

    fixtures = Path(__file__).parent.parent / "regression" / "fixtures" / "sample_repo_py"
    checkout = tmp_path / "repos" / REPO.replace("/", "__")
    checkout.parent.mkdir(parents=True)
    import shutil

    shutil.copytree(fixtures, checkout)

    monkeypatch.setenv("REPOS_DIR", str(tmp_path / "repos"))
    get_settings.cache_clear()

    async def fake_commit(_path):
        return "sha-abc"

    monkeypatch.setattr(git_cli, "current_commit", fake_commit)
    facts, graph = InMemoryFactsStore(), InMemoryKnowledgeGraphStore()
    use_facts_store(facts)
    use_knowledge_graph_store(graph)

    await _insert_repo()
    try:
        await jobs.run_initial_parse(USER, REPO)

        doc = await _repo_doc()
        assert doc["parse_status"] == "done"
        assert doc["graph_version"] == "sha-abc"
        assert doc["graph_stats"]["files"] == 4
        assert f"{REPO}:api/handlers.py" in await graph.node_ids(REPO)
        assert isinstance(IndexingPipeline(facts, graph), IndexingPipeline)
    finally:
        use_facts_store(None)
        use_knowledge_graph_store(None)
        get_settings.cache_clear()


async def test_indexing_does_not_block_the_event_loop(monkeypatch, tmp_path):
    """Indexing is CPU-bound, so it must run in a worker thread."""
    from app.integrations import git_cli
    from app.knowledge_graph import pipeline as pipeline_module

    await _insert_repo()
    (tmp_path / "app.py").write_text("x = 1\n")

    def slow_hash(files):
        time.sleep(0.4)  # stands in for the CPU-bound stages
        return {}

    async def fake_commit(_path):
        return "sha-abc"

    monkeypatch.setattr(pipeline_module, "_hash_files", slow_hash)
    monkeypatch.setattr(git_cli, "current_commit", fake_commit)
    monkeypatch.setattr(repo_service, "repo_workdir", lambda repo_full_name: tmp_path)

    stalls: list[float] = []

    async def heartbeat():
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            stalls.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await jobs.run_initial_parse(USER, REPO)
    beat.cancel()

    # Inline parsing would stall the loop for the full 0.4s.
    assert stalls and max(stalls) < 0.25
    assert (await _repo_doc())["parse_status"] == "done"


async def test_a_graph_from_an_older_builder_is_rebuilt_not_patched(monkeypatch):
    """A shape change invalidates the whole graph, not just the changed files.

    An incremental sync diffs the new snapshot against one rebuilt from cached
    facts. If the *builder* changed, the old graph holds nodes the new
    snapshot has no opinion about, so they survive the diff and the stored
    graph keeps its old shape forever. The only correct answer is to rebuild.
    """
    from app.knowledge_graph.build import GRAPH_BUILD_VERSION

    calls: list[bool] = []

    async def fake_pipeline_index(self, repo_full_name, checkout, version, *, full):
        calls.append(full)
        from app.knowledge_graph.pipeline import IndexResult

        return IndexResult(repo_full_name=repo_full_name, version=version, files_indexed=0)

    from app.integrations import git_cli
    from app.knowledge_graph.pipeline import IndexingPipeline

    monkeypatch.setattr(IndexingPipeline, "index", fake_pipeline_index)
    monkeypatch.setattr(git_cli, "current_commit", lambda _path: _resolved("sha-1"))
    monkeypatch.setattr(repo_service, "repo_workdir", lambda repo_full_name: Path("."))
    # An incremental sync pulls the checkout forward first; neither GitHub nor
    # the filesystem is what this test is about.
    monkeypatch.setattr(repo_service, "get_github_token", lambda user_id: _resolved("t"))
    monkeypatch.setattr(
        repo_service, "update_workdir", lambda repo, token: _resolved(Path(".")),
    )

    await _insert_repo()

    # Stored by a builder that predates the current shape.
    await mongo.repos().update_one(
        {"user_id": USER, "repo_full_name": REPO},
        {"$set": {"graph_build_version": GRAPH_BUILD_VERSION - 1}},
    )
    await jobs._index(USER, REPO, full=False)
    assert calls == [True], "an out-of-date shape must force a full rebuild"

    # Now it is current, so an incremental sync stays incremental.
    calls.clear()
    await jobs._index(USER, REPO, full=False)
    assert calls == [False]

    doc = await _repo_doc()
    assert doc["graph_build_version"] == GRAPH_BUILD_VERSION


async def test_a_graph_with_no_recorded_shape_is_rebuilt(monkeypatch):
    """Every graph written before the version existed is of unknown shape."""
    calls: list[bool] = []

    async def fake_pipeline_index(self, repo_full_name, checkout, version, *, full):
        calls.append(full)
        from app.knowledge_graph.pipeline import IndexResult

        return IndexResult(repo_full_name=repo_full_name, version=version, files_indexed=0)

    from app.integrations import git_cli
    from app.knowledge_graph.pipeline import IndexingPipeline

    monkeypatch.setattr(IndexingPipeline, "index", fake_pipeline_index)
    monkeypatch.setattr(git_cli, "current_commit", lambda _path: _resolved("sha-1"))
    monkeypatch.setattr(repo_service, "repo_workdir", lambda repo_full_name: Path("."))

    await _insert_repo()
    await jobs._index(USER, REPO, full=False)

    assert calls == [True]


def _resolved(value):
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    future.set_result(value)
    return future
