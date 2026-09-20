"""Running extraction with bounded time and memory.

Parsing is the one place where we execute logic over input we do not control,
and the binding offers no parser-level timeout. So the bound comes from the
process boundary:

* each worker caps its own address space, so a pathological file raises
  MemoryError in a worker instead of the kernel killing the API;
* a batch that overruns its budget is abandoned, the workers are killed, and
  the pool is rebuilt — the files still running are reported as timeouts
  rather than silently missing;
* workers are recycled periodically so parser allocations cannot accumulate.

Small batches (an incremental push) skip the pool entirely: spawning a process
costs more than parsing a handful of files.
"""

import os
import resource
import signal
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor, wait
from dataclasses import dataclass
from multiprocessing import get_context
from pathlib import Path

import structlog

from app.knowledge_graph.discovery import DiscoveredFile, read_source
from app.knowledge_graph.extract.extractor import UnsupportedLanguage, extract
from app.knowledge_graph.facts import FileFacts
from app.knowledge_graph.limits import DEFAULT_LIMITS, Limits, SkipReason

logger = structlog.get_logger("app.knowledge_graph.extract")

#: Files sent to the pool at once. Large enough to amortise IPC, small enough
#: that one hung file does not take a big batch down with it.
CHANGE_BATCH_SIZE = 200
#: Below this, parsing in-process is faster than starting workers.
IN_PROCESS_THRESHOLD = 24
#: Recycle workers after this many files to bound memory drift.
MAX_TASKS_PER_CHILD = 500


@dataclass(frozen=True, slots=True)
class ExtractionOutcome:
    """What happened to one file. Exactly one of facts/skip_reason is set."""

    relative_path: str
    facts: FileFacts | None = None
    skip_reason: SkipReason | None = None


def extract_file(file: DiscoveredFile, limits: Limits = DEFAULT_LIMITS) -> ExtractionOutcome:
    """Read and extract a single file. Never raises for a bad input file."""
    source, reason = read_source(file, limits)
    if source is None:
        return ExtractionOutcome(file.relative_path, skip_reason=reason)
    try:
        return ExtractionOutcome(file.relative_path, facts=extract(source, limits))
    except UnsupportedLanguage:
        return ExtractionOutcome(file.relative_path, skip_reason=SkipReason.UNSUPPORTED_LANGUAGE)
    except (MemoryError, RecursionError, ValueError, OSError):
        logger.warning("extract_failed", path=file.relative_path, exc_info=True)
        return ExtractionOutcome(file.relative_path, skip_reason=SkipReason.PARSE_FAILED)


def extract_files(
    files: Sequence[DiscoveredFile], limits: Limits = DEFAULT_LIMITS,
) -> list[ExtractionOutcome]:
    """Extract in this process. Blocking: call from a worker thread."""
    return [extract_file(file, limits) for file in files]


def worker_count(limits: Limits = DEFAULT_LIMITS, requested: int | None = None) -> int:
    """How many workers this machine can afford, from CPU and free memory."""
    if requested is not None:
        return max(1, requested)

    cpus = os.cpu_count() or 1
    # Leave a core for the API itself.
    by_cpu = max(1, min(cpus - 1, 8))
    available_mb = _available_memory_mb()
    if available_mb is None:
        return by_cpu
    # Never plan to use more than half of what is free.
    by_memory = max(1, int(available_mb * 0.5) // max(limits.worker_memory_mb, 1))
    return max(1, min(by_cpu, by_memory))


def _available_memory_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _init_worker(memory_mb: int) -> None:
    """Cap this worker's memory and stop it reacting to the parent's signals."""
    limit_bytes = memory_mb * 1024 * 1024
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        ceiling = limit_bytes if hard == resource.RLIM_INFINITY else min(limit_bytes, hard)
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, hard))
    except (ValueError, OSError):  # not supported on this platform
        logger.info("worker_memory_limit_unavailable")
    signal.signal(signal.SIGINT, signal.SIG_IGN)


class ExtractionPool:
    """Worker pool for full-repository indexing.

    Use as a context manager; workers are started on first use and always torn
    down, including after a timeout.
    """

    def __init__(self, limits: Limits = DEFAULT_LIMITS, max_workers: int | None = None):
        self._limits = limits
        self._workers = worker_count(limits, max_workers)
        self._executor: ProcessPoolExecutor | None = None

    def __enter__(self) -> "ExtractionPool":
        return self

    def __exit__(self, *_exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def extract_all(self, files: Sequence[DiscoveredFile]) -> list[ExtractionOutcome]:
        """Extract every file, in the order given. Blocking: run in a thread."""
        if len(files) <= IN_PROCESS_THRESHOLD:
            return extract_files(files, self._limits)

        outcomes: list[ExtractionOutcome] = []
        for start in range(0, len(files), CHANGE_BATCH_SIZE):
            outcomes.extend(self._extract_batch(files[start:start + CHANGE_BATCH_SIZE]))
        return outcomes

    # -- internals ---------------------------------------------------------

    def _executor_or_start(self) -> ProcessPoolExecutor:
        if self._executor is None:
            self._executor = ProcessPoolExecutor(
                max_workers=self._workers,
                # "spawn": a worker must not inherit the API's event loop or
                # open database sockets.
                mp_context=get_context("spawn"),
                initializer=_init_worker,
                initargs=(self._limits.worker_memory_mb,),
                max_tasks_per_child=MAX_TASKS_PER_CHILD,
            )
        return self._executor

    def _batch_budget(self, batch_size: int) -> float:
        per_worker = batch_size / self._workers
        return max(30.0, per_worker * self._limits.parse_timeout_seconds)

    def _extract_batch(self, batch: Sequence[DiscoveredFile]) -> list[ExtractionOutcome]:
        executor = self._executor_or_start()
        futures = {
            executor.submit(extract_file, file, self._limits): file for file in batch
        }
        done, not_done = wait(futures, timeout=self._batch_budget(len(batch)))

        by_path: dict[str, ExtractionOutcome] = {}
        for future in done:
            file = futures[future]
            try:
                by_path[file.relative_path] = future.result()
            except Exception:  # a worker died: OOM, segfault, anything
                logger.warning("extract_worker_lost", path=file.relative_path, exc_info=True)
                by_path[file.relative_path] = ExtractionOutcome(
                    file.relative_path, skip_reason=SkipReason.PARSE_FAILED,
                )

        if not_done:
            timed_out = [futures[future].relative_path for future in not_done]
            logger.warning(
                "extract_batch_timeout", files=len(timed_out), sample=sorted(timed_out)[:5],
            )
            for path in timed_out:
                by_path[path] = ExtractionOutcome(path, skip_reason=SkipReason.PARSE_TIMEOUT)
            # The workers are still busy on files we have given up on: replace
            # the pool rather than letting them compete with the next batch.
            self._recycle()

        return [by_path[file.relative_path] for file in batch]

    def _recycle(self) -> None:
        executor, self._executor = self._executor, None
        if executor is not None:
            for process in list(getattr(executor, "_processes", {}).values()):
                process.kill()
            executor.shutdown(wait=False, cancel_futures=True)


__all__ = [
    "ExtractionOutcome",
    "ExtractionPool",
    "extract_file",
    "extract_files",
    "worker_count",
]
