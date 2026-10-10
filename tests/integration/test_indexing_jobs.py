"""The repository index lock: mutual exclusion, crash recovery, no lost pushes."""

import asyncio
import shutil
import time
from datetime import timedelta
from pathlib import Path

import pytest

from app.core.time import utcnow
from app.db import mongo
from app.github import checkout
from app.integrations import git_cli
from app.integrations.github_app import InstallationGone
from app.knowledge_graph.build import GRAPH_BUILD_VERSION
from app.knowledge_graph.pipeline import IndexingPipeline, IndexResult
from app.workers import jobs

USER = "user-1"
REPO = "octocat/hello-world"


async def _insert_repo(**overrides) -> None:
    await mongo.repos().insert_one({
        "user_id": USER,
        "repo_full_name": REPO,
        "installation_id": 9,
        "default_branch": "main",
        "parse_status": "pending",
        "parse_error": None,
        "parse_started_at": None,
        "connected_at": utcnow(),
        "updated_at": utcnow(),
        **overrides,
    })


async def _repo_doc() -> dict:
    return await mongo.repos().find_one({"user_id": USER, "repo_full_name": REPO})


class _FakeApp:
    def __init__(self, error: Exception | None = None):
        self.error = error

    async def installation_token(self, installation_id):
        if self.error:
            raise self.error
        return "ghs_installation"


def _stub_checkout(monkeypatch, path: Path, app: _FakeApp | None = None) -> None:
    """No GitHub, no git: the checkout is whatever is at `path`."""
    async def update(repo_full_name, token):
        return path

    async def commit(_path):
        return "sha-abc"

    monkeypatch.setattr(jobs, "get_github_app", lambda: app or _FakeApp())
    monkeypatch.setattr(checkout, "update", update)
    monkeypatch.setattr(git_cli, "current_commit", commit)


@pytest.fixture
def record_index_calls(monkeypatch):
    """Replace the real indexing work with a recorder of the `full` flag."""
    calls: list[bool] = []

    async def fake_index(user_id, repo_full_name, *, full):
        calls.append(full)

    monkeypatch.setattr(jobs, "_index", fake_index)
    return calls


async def test_index_runs_and_releases_the_lock(record_index_calls):
    await _insert_repo()

    await jobs.run_index(USER, REPO, full=True)

    doc = await _repo_doc()
    assert record_index_calls == [True]
    assert doc["parse_status"] == "done"
    assert doc["parse_started_at"] is None


async def test_a_held_lock_defers_the_work_instead_of_running_it(record_index_calls):
    await _insert_repo(parse_status="in_progress", parse_started_at=utcnow())

    await jobs.run_index(USER, REPO)

    doc = await _repo_doc()
    assert record_index_calls == []  # did not run concurrently
    assert doc["sync_requested_at"] is not None
    assert not doc.get("pending_full")


async def test_a_deferred_full_index_is_not_downgraded(record_index_calls):
    await _insert_repo(parse_status="in_progress", parse_started_at=utcnow())

    await jobs.run_index(USER, REPO, full=True)
    await jobs.run_index(USER, REPO)

    assert (await _repo_doc())["pending_full"] is True


async def test_a_stale_lock_from_a_crashed_run_is_reclaimed(record_index_calls):
    await _insert_repo(parse_status="in_progress", parse_started_at=utcnow() - timedelta(hours=2))

    await jobs.run_index(USER, REPO, full=True)

    assert record_index_calls == [True]
    assert (await _repo_doc())["parse_status"] == "done"


async def test_a_lock_held_without_a_start_time_is_reclaimed(record_index_calls):
    # written by a version that didn't record start times; otherwise wedged forever
    await _insert_repo(parse_status="in_progress")

    await jobs.run_index(USER, REPO, full=True)

    assert record_index_calls == [True]


async def test_a_push_arriving_mid_index_is_drained_before_release(monkeypatch):
    await _insert_repo()
    calls: list[bool] = []

    async def fake_index(user_id, repo_full_name, *, full):
        calls.append(full)
        if len(calls) == 1:
            await jobs.run_index(user_id, repo_full_name)  # a webhook lands mid-index

    monkeypatch.setattr(jobs, "_index", fake_index)

    await jobs.run_index(USER, REPO, full=True)

    doc = await _repo_doc()
    assert calls == [True, False]
    assert doc["parse_status"] == "done"
    assert doc["sync_requested_at"] is None


async def test_failure_releases_the_lock_and_records_the_error(monkeypatch):
    await _insert_repo()

    async def failing(user_id, repo_full_name, *, full):
        raise RuntimeError("clone exploded")

    monkeypatch.setattr(jobs, "_index", failing)

    await jobs.run_index(USER, REPO, full=True)

    doc = await _repo_doc()
    assert doc["parse_status"] == "failed"
    assert "clone exploded" in doc["parse_error"]
    assert doc["parse_started_at"] is None


async def test_an_uninstalled_app_marks_the_repo_revoked(monkeypatch, tmp_path):
    await _insert_repo()
    _stub_checkout(monkeypatch, tmp_path, _FakeApp(error=InstallationGone("gone")))

    await jobs.run_index(USER, REPO, full=True)

    doc = await _repo_doc()
    assert doc["parse_status"] == "failed"
    assert doc["access_status"] == "revoked"


async def test_a_repo_disconnected_while_queued_is_skipped(monkeypatch, tmp_path):
    _stub_checkout(monkeypatch, tmp_path)
    await jobs._index(USER, REPO, full=True)  # no repo doc: nothing to do, no error


async def test_indexing_a_real_checkout_writes_a_graph(monkeypatch, tmp_path, graph_store):
    """The job runs the pipeline over a real checkout and records what it did."""
    fixtures = Path(__file__).parent.parent / "regression" / "fixtures" / "sample_repo_py"
    shutil.copytree(fixtures, tmp_path / "repo")
    _stub_checkout(monkeypatch, tmp_path / "repo")
    await _insert_repo()

    await jobs.run_index(USER, REPO, full=True)

    doc = await _repo_doc()
    assert doc["parse_status"] == "done"
    assert doc["graph_version"] == "sha-abc"
    assert doc["graph_stats"]["files"] == 4
    assert f"{REPO}:api/handlers.py" in await graph_store.node_ids(REPO)


async def test_indexing_does_not_block_the_event_loop(monkeypatch, tmp_path):
    """Indexing is CPU-bound, so it must run in a worker thread."""
    from app.knowledge_graph import pipeline as pipeline_module

    await _insert_repo()
    (tmp_path / "app.py").write_text("x = 1\n")

    def slow_hash(files):
        time.sleep(0.4)  # stands in for the CPU-bound stages
        return {}

    monkeypatch.setattr(pipeline_module, "_hash_files", slow_hash)
    _stub_checkout(monkeypatch, tmp_path)

    stalls: list[float] = []

    async def heartbeat():
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.02)
            now = time.perf_counter()
            stalls.append(now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await jobs.run_index(USER, REPO, full=True)
    beat.cancel()

    # inline parsing would stall the loop for the full 0.4s
    assert stalls and max(stalls) < 0.25
    assert (await _repo_doc())["parse_status"] == "done"


@pytest.fixture
def record_pipeline_full_flag(monkeypatch, tmp_path):
    calls: list[bool] = []

    async def fake_pipeline_index(self, repo_full_name, checkout, version, *, full):
        calls.append(full)
        return IndexResult(repo_full_name=repo_full_name, version=version, files_indexed=0)

    monkeypatch.setattr(IndexingPipeline, "index", fake_pipeline_index)
    _stub_checkout(monkeypatch, tmp_path)
    return calls


async def test_a_graph_from_an_older_builder_is_rebuilt_not_patched(record_pipeline_full_flag):
    """A builder change leaves nodes the new snapshot has no opinion about, so an
    incremental diff would keep the old shape forever. Only a rebuild is correct."""
    await _insert_repo(graph_build_version=GRAPH_BUILD_VERSION - 1)

    await jobs._index(USER, REPO, full=False)
    assert record_pipeline_full_flag == [True], "an out-of-date shape must force a full rebuild"

    # now current, so an incremental sync stays incremental
    record_pipeline_full_flag.clear()
    await jobs._index(USER, REPO, full=False)
    assert record_pipeline_full_flag == [False]
    assert (await _repo_doc())["graph_build_version"] == GRAPH_BUILD_VERSION


async def test_a_graph_with_no_recorded_shape_is_rebuilt(record_pipeline_full_flag):
    await _insert_repo()
    await jobs._index(USER, REPO, full=False)
    assert record_pipeline_full_flag == [True]
