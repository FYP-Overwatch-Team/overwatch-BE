"""Extraction must be bounded: bad files cost a worker, never the service."""

from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path

from app.knowledge_graph.discovery import DiscoveredFile, discover
from app.knowledge_graph.extract import pool as extract_pool
from app.knowledge_graph.extract.pool import (
    ExtractionPool,
    extract_file,
    extract_files,
    worker_count,
)
from app.knowledge_graph.limits import DEFAULT_LIMITS, SkipReason


def make_repo(tmp_path: Path, count: int) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for index in range(count):
        (repo / f"module_{index:03}.py").write_text(
            f"def function_{index}():\n    return {index}\n"
        )
    return repo


def test_extract_file_returns_facts(tmp_path):
    repo = make_repo(tmp_path, 1)
    file = discover(repo).files[0]

    outcome = extract_file(file)

    assert outcome.skip_reason is None
    assert outcome.facts.definitions[0].name == "function_0"


def test_extract_file_reports_a_skip_instead_of_raising(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "bundle.js").write_bytes(b"var a=1;" * 2000)  # one enormous line
    file = DiscoveredFile(
        path=repo / "bundle.js", relative_path="bundle.js",
        language="javascript", size_bytes=16000,
    )

    outcome = extract_file(file)

    assert outcome.facts is None
    assert outcome.skip_reason is SkipReason.MINIFIED


def test_extract_file_survives_a_vanished_file(tmp_path):
    file = DiscoveredFile(
        path=tmp_path / "gone.py", relative_path="gone.py", language="python", size_bytes=10,
    )

    assert extract_file(file).skip_reason is SkipReason.UNREADABLE


def test_extract_files_preserves_order(tmp_path):
    files = discover(make_repo(tmp_path, 5)).files

    outcomes = extract_files(files)

    assert [o.relative_path for o in outcomes] == [f.relative_path for f in files]


def test_worker_count_is_bounded(monkeypatch):
    assert worker_count(requested=3) == 3
    assert worker_count(requested=0) == 1

    monkeypatch.setattr(extract_pool, "_available_memory_mb", lambda: 512)
    limits = replace(DEFAULT_LIMITS, worker_memory_mb=1024)
    # Half of 512MB cannot fit a single 1GB worker, so it floors at one.
    assert worker_count(limits) == 1

    monkeypatch.setattr(extract_pool, "_available_memory_mb", lambda: 64_000)
    assert 1 <= worker_count(limits) <= 8


def test_small_batches_run_in_process(tmp_path, monkeypatch):
    files = discover(make_repo(tmp_path, 3)).files

    def fail_if_started():
        raise AssertionError("small batches must not start worker processes")

    monkeypatch.setattr(ExtractionPool, "_executor_or_start", lambda self: fail_if_started())

    with ExtractionPool(max_workers=2) as pool:
        outcomes = pool.extract_all(files)

    assert [o.facts.definitions[0].name for o in outcomes] == [
        "function_0", "function_1", "function_2",
    ]


def test_large_batches_run_in_worker_processes(tmp_path):
    files = discover(make_repo(tmp_path, 30)).files

    with ExtractionPool(max_workers=2) as pool:
        outcomes = pool.extract_all(files)

    assert len(outcomes) == 30
    assert all(o.facts is not None for o in outcomes)
    assert [o.relative_path for o in outcomes] == [f.relative_path for f in files]


def test_a_batch_that_overruns_is_reported_and_the_pool_recycled(tmp_path, monkeypatch):
    files = discover(make_repo(tmp_path, 30)).files
    recycled: list[bool] = []

    class NeverFinishingExecutor:
        def submit(self, *_args, **_kwargs):
            return Future()  # never resolved

    pool = ExtractionPool(max_workers=2)
    monkeypatch.setattr(ExtractionPool, "_executor_or_start", lambda self: NeverFinishingExecutor())
    monkeypatch.setattr(ExtractionPool, "_batch_budget", lambda self, size: 0.05)
    monkeypatch.setattr(ExtractionPool, "_recycle", lambda self: recycled.append(True))

    outcomes = pool.extract_all(files)

    assert len(outcomes) == 30
    assert all(o.skip_reason is SkipReason.PARSE_TIMEOUT for o in outcomes)
    assert recycled == [True]  # workers were replaced, not left running


def test_batch_budget_scales_with_batch_size():
    pool = ExtractionPool(max_workers=2)

    assert pool._batch_budget(1) == 30.0  # floor for small batches
    assert pool._batch_budget(400) > 30.0
