"""The pipeline end to end, against a real checkout and in-memory stores."""

from pathlib import Path

import pytest

from app.knowledge_graph.pipeline import IndexingPipeline
from app.services.facts_repository import InMemoryFactsStore
from app.services.knowledge_graph_store import InMemoryKnowledgeGraphStore

REPO = "octocat/hello-world"

INITIAL_TREE = {
    "tsconfig.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./*"]}}}',
    "app/page.tsx": (
        'import { Button } from "@/components/Button";\n'
        'import { formatName } from "@/lib/format";\n\n'
        "export function Page() {\n"
        "  return <Button label={formatName('x')} />;\n"
        "}\n"
    ),
    "components/Button.tsx": "export function Button({ label }: { label: string }) { return <button>{label}</button>; }\n",
    "lib/format.ts": "export function formatName(value: string) { return value.trim(); }\n",
    "lib/unused.ts": "export function unused() { return 1; }\n",
}


def write_tree(root: Path, tree: dict[str, str]) -> Path:
    for relative_path, content in tree.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


@pytest.fixture
def checkout(tmp_path) -> Path:
    return write_tree(tmp_path / "checkout", INITIAL_TREE)


@pytest.fixture
def pipeline():
    facts, graph = InMemoryFactsStore(), InMemoryKnowledgeGraphStore()
    return IndexingPipeline(facts, graph), facts, graph


def graph_contents(store: InMemoryKnowledgeGraphStore) -> tuple[dict, dict]:
    """Nodes and edges, ignoring the version stamp that differs per commit."""
    nodes = {
        node_id: {k: v for k, v in node.items() if k != "version"}
        for node_id, node in store.nodes.items()
    }
    edges = {
        key: {k: v for k, v in edge.items() if k != "version"}
        for key, edge in store.edges.items()
    }
    return nodes, edges


async def test_full_index_builds_the_graph_and_caches_facts(pipeline, checkout):
    runner, facts, graph = pipeline

    result = await runner.index(REPO, checkout, "sha-1", full=True)

    assert result.files_indexed == 4  # tsconfig.json is not a source file
    assert result.plan["extract"] == 4
    assert await facts.fingerprints(REPO)
    node_ids = await graph.node_ids(REPO)
    assert f"{REPO}:app/page.tsx" in node_ids
    assert f"{REPO}:components/Button.tsx#Button" in node_ids


async def test_the_alias_import_becomes_a_real_dependency(pipeline, checkout):
    runner, _, graph = pipeline

    await runner.index(REPO, checkout, "sha-1", full=True)

    view = await graph.module_view(REPO)
    dependencies = {(e["source"], e["target"]) for e in view["edges"] if e["type"] == "DEPENDS_ON"}
    assert (f"{REPO}:app", f"{REPO}:components") in dependencies
    assert (f"{REPO}:app", f"{REPO}:lib") in dependencies


async def test_a_second_index_of_an_unchanged_checkout_writes_nothing(pipeline, checkout):
    runner, _, _ = pipeline
    await runner.index(REPO, checkout, "sha-1", full=True)

    result = await runner.index(REPO, checkout, "sha-2")

    assert result.plan == {"extract": 0, "unchanged": 4, "deleted": 0}
    # Only the repository node is rewritten, because it carries the commit.
    assert result.delta == {
        "nodes_upserted": 1, "edges_upserted": 0, "nodes_deleted": 0, "edges_deleted": 0,
    }


async def test_only_changed_files_are_re_extracted(pipeline, checkout):
    runner, _, _ = pipeline
    await runner.index(REPO, checkout, "sha-1", full=True)
    (checkout / "lib/format.ts").write_text(
        "export function formatName(value: string) { return value.trimEnd(); }\n"
    )

    result = await runner.index(REPO, checkout, "sha-2")

    assert result.plan["extract"] == 1
    assert result.plan["unchanged"] == 3


async def test_deleting_a_file_removes_its_nodes(pipeline, checkout):
    runner, facts, graph = pipeline
    await runner.index(REPO, checkout, "sha-1", full=True)

    (checkout / "lib/unused.ts").unlink()
    result = await runner.index(REPO, checkout, "sha-2")

    assert result.plan["deleted"] == 1
    node_ids = await graph.node_ids(REPO)
    assert f"{REPO}:lib/unused.ts" not in node_ids
    assert f"{REPO}:lib/unused.ts#unused" not in node_ids
    assert "lib/unused.ts" not in await facts.fingerprints(REPO)


async def test_renaming_a_function_updates_a_caller_that_did_not_change(pipeline, checkout):
    runner, _, graph = pipeline
    await runner.index(REPO, checkout, "sha-1", full=True)
    calls_before = {key for key in graph.edges if key[2] == "CALLS"}
    assert calls_before

    (checkout / "lib/format.ts").write_text(
        "export function formatLabel(value: string) { return value.trim(); }\n"
    )
    await runner.index(REPO, checkout, "sha-2")

    # app/page.tsx still imports `formatName`, which no longer exists, so the
    # edge must be gone even though that file was not re-read.
    remaining = {key for key in graph.edges if key[2] == "CALLS"}
    assert not any(target.endswith("#formatName") for _, target, _ in remaining)


async def test_incremental_sync_matches_a_full_index_of_the_same_tree(pipeline, checkout):
    """The correctness rule for the whole design."""
    incremental_runner, _, incremental_graph = pipeline
    await incremental_runner.index(REPO, checkout, "sha-1", full=True)

    # A realistic sequence of edits: modify, add, delete, and rename.
    (checkout / "lib/format.ts").write_text(
        "export function formatName(value: string) { return value.toUpperCase(); }\n"
    )
    (checkout / "lib/new_helper.ts").write_text(
        'import { formatName } from "@/lib/format";\n'
        "export function helper() { return formatName('y'); }\n"
    )
    (checkout / "lib/unused.ts").unlink()
    (checkout / "components/Button.tsx").write_text(
        "export function Button({ label }: { label: string }) { return <span>{label}</span>; }\n"
    )
    await incremental_runner.index(REPO, checkout, "sha-2")

    fresh_runner = IndexingPipeline(InMemoryFactsStore(), InMemoryKnowledgeGraphStore())
    await fresh_runner.index(REPO, checkout, "sha-2", full=True)

    assert graph_contents(incremental_graph) == graph_contents(fresh_runner._graph)


async def test_facts_are_not_cached_when_the_graph_write_fails(pipeline, checkout):
    """Otherwise the next sync would believe the work was already applied."""
    runner, facts, graph = pipeline

    async def failing_apply(_delta):
        raise RuntimeError("neo4j unavailable")

    graph.apply = failing_apply

    with pytest.raises(RuntimeError):
        await runner.index(REPO, checkout, "sha-1", full=True)

    assert await facts.fingerprints(REPO) == {}


async def test_skipped_files_are_reported(pipeline, checkout):
    runner, _, _ = pipeline
    (checkout / "bundle.min.js").write_text("var a=1;\n")
    (checkout / "node_modules").mkdir()
    (checkout / "node_modules" / "dep.js").write_text("module.exports = 1;\n")

    result = await runner.index(REPO, checkout, "sha-1", full=True)

    assert result.stats["skipped"]["generated"] == 1
    assert result.stats["skipped"]["ignored_directory"] == 1


async def test_stats_record_resolution_coverage(pipeline, checkout):
    runner, _, _ = pipeline

    result = await runner.index(REPO, checkout, "sha-1", full=True)

    resolution = result.stats["resolution"]
    assert resolution["internal_import_resolution"] == 1.0  # every @/ import resolved
    assert result.as_stats_document()["version"] == "sha-1"
