"""A repository's code embedded by meaning: kept in step with its index, and found from the chat."""

import asyncio
import json

import pytest
from pymongo.errors import OperationFailure

from app.core.config import get_settings
from app.core.constants import EmbedPurpose
from app.core.exceptions import ExternalServiceError
from app.db import mongo
from app.db.knowledge_graph_store import get_knowledge_graph_store
from app.embeddings import code
from app.embeddings import service as embeddings_service
from app.embeddings.service import decode, encode
from app.github import checkout as github_checkout
from app.github import service as github_service
from app.integrations.llm import ToolCall
from app.knowledge_graph.build.ids import file_id, symbol_id
from app.knowledge_graph.pipeline import IndexingPipeline
from app.query.context import open_ask_context
from app.query.tools import ALREADY_LOOKED_UP, READ_CODE, SEARCH_MEANING, Lookups
from app.query.workspace_tools import Workspace
from app.services.facts_repository import get_facts_store
from app.workers import jobs
from tests.git_fixtures import commit_all
from tests.integration.test_indexing_jobs import (
    REPO,
    _insert_repo,
    _repo_doc,
    _stub_checkout,
)

WORKSPACE = "ws-1"
ASKER = (WORKSPACE, "user-1")
REPORTS = '''"""Documents people download."""


def build_document(rows):
    """Turn rows into a PDF report a guest can export."""
    header = "report"
    body = [str(row) for row in rows]
    return header, body


def tiny():
    return 1
'''
THEME = '''def switch_colours(theme):
    """Swap the palette."""
    palette = theme.palette
    theme.palette = palette.inverse()
    return theme
'''
TEST_FILE = '''def test_build_document():
    """A PDF report for a guest."""
    assert build_document([]) is not None
    assert True
'''
DOCUMENT = symbol_id(REPO, "app/downloads.py", "build_document")
COLOURS = symbol_id(REPO, "app/theme.py", "switch_colours")


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("REPOS_DIR", str(tmp_path / "repos"))
    # the fake embedder counts shared words, so everything reads a little alike; only real overlap passes
    monkeypatch.setenv("CODE_SIMILARITY_THRESHOLD", "0.8")
    get_settings.cache_clear()
    root = tmp_path / "repos" / REPO.replace("/", "__")
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "app" / "downloads.py").write_text(REPORTS)
    (root / "app" / "theme.py").write_text(THEME)
    (root / "tests" / "test_reports.py").write_text(TEST_FILE)
    yield root
    get_settings.cache_clear()


async def index_and_sync(root) -> tuple[str, dict]:
    sha = commit_all(root)
    await IndexingPipeline(get_facts_store(), get_knowledge_graph_store()).index(REPO, root, sha, full=True)
    facts = await get_facts_store().load_all(REPO)
    return sha, await code.sync(REPO, sha, facts, include_symbols=True, workspace_id=WORKSPACE)


def call(name: str, **arguments) -> ToolCall:
    return ToolCall("c1", name, json.dumps(arguments))


async def stored() -> dict[str, dict]:
    return {doc["ref_id"]: doc async for doc in mongo.embeddings().find({"kind": "code"})}


async def test_files_and_substantial_functions_are_embedded_packed_and_charged_to_the_workspace(root, fake_embedder):
    _, counts = await index_and_sync(root)

    vectors = await stored()
    assert set(vectors) == {file_id(REPO, "app/downloads.py"), file_id(REPO, "app/theme.py"), DOCUMENT, COLOURS}
    assert counts == {"embedded": 4, "deleted": 0, "capped": 0, "deferred": 0, "no_room": 0}
    doc = vectors[DOCUMENT]
    assert (doc["repo"], doc.get("workspace_id"), doc["path"], doc["model"]) == (REPO, None, "app/downloads.py", "fake/embedder")
    assert isinstance(doc["vector"], bytes) and len(decode(doc["vector"])) == len(fake_embedder.vocabulary)
    assert {call["purpose"] for call in fake_embedder.calls} == {EmbedPurpose.DOCUMENT}
    usage = await mongo.llm_usage().find_one({"feature": "embeddings"})
    assert (usage["workspace_id"], usage["user_id"]) == (WORKSPACE, "system")


async def test_an_unchanged_repository_is_not_embedded_again(root, fake_embedder):
    await index_and_sync(root)
    calls = len(fake_embedder.calls)

    _, counts = await index_and_sync(root)

    assert len(fake_embedder.calls) == calls
    assert counts["embedded"] == 0


async def test_an_edited_function_is_embedded_again_with_its_file_but_nothing_else(root, fake_embedder):
    await index_and_sync(root)
    fake_embedder.calls.clear()

    (root / "app" / "theme.py").write_text(THEME.replace("palette.inverse()", "palette.darker()"))
    await index_and_sync(root)

    texts = [text for call in fake_embedder.calls for text in call["texts"]]
    assert sorted(text.splitlines()[1] for text in texts) == ["function switch_colours", "python"]


async def test_lines_moving_only_re_embed_the_file(root, fake_embedder):
    await index_and_sync(root)
    fake_embedder.calls.clear()

    (root / "app" / "theme.py").write_text("\n\n\n" + THEME)
    await index_and_sync(root)

    texts = [text for call in fake_embedder.calls for text in call["texts"]]
    assert [text.splitlines()[0:2] for text in texts] == [["app/theme.py", "python"]]
    vectors = await stored()
    assert vectors[COLOURS]["file_hash"] == vectors[file_id(REPO, "app/theme.py")]["file_hash"]


async def test_a_removed_function_loses_its_vector(root, fake_embedder):
    await index_and_sync(root)

    (root / "app" / "theme.py").write_text("THEME = 'light'\n")
    _, counts = await index_and_sync(root)

    assert COLOURS not in await stored()
    assert counts["deleted"] == 1


async def test_another_models_vectors_are_replaced(root, fake_embedder):
    await mongo.embeddings().insert_one({"repo": REPO, "kind": "code", "ref_id": "old", "model": "old/model", "vector": [1.0]})

    await index_and_sync(root)

    assert "old" not in await stored()


async def test_a_repository_keeps_its_most_worthwhile_code_under_its_limit(root, fake_embedder, monkeypatch):
    monkeypatch.setenv("CODE_EMBEDDING_REPO_LIMIT", "3")
    get_settings.cache_clear()

    _, counts = await index_and_sync(root)

    # files first, then the bigger function
    assert set(await stored()) == {file_id(REPO, "app/downloads.py"), file_id(REPO, "app/theme.py"), DOCUMENT}
    assert counts["capped"] == 1


async def test_a_run_embeds_at_most_its_limit_and_the_next_carries_on(root, fake_embedder, monkeypatch):
    monkeypatch.setenv("CODE_EMBEDDING_RUN_LIMIT", "3")
    get_settings.cache_clear()

    _, first = await index_and_sync(root)
    _, second = await index_and_sync(root)

    assert (first["embedded"], first["deferred"], second["embedded"]) == (3, 1, 1)
    assert len(await stored()) == 4


async def test_no_new_vectors_once_every_repository_together_is_at_the_limit(root, fake_embedder, monkeypatch):
    monkeypatch.setenv("CODE_EMBEDDING_TOTAL_LIMIT", "2")
    get_settings.cache_clear()

    _, counts = await index_and_sync(root)

    assert len(await stored()) == 2
    assert counts["no_room"] == 2


async def test_without_embeddings_nothing_is_written(root):
    _, counts = await index_and_sync(root)

    assert counts == {}
    assert await mongo.embeddings().count_documents({}) == 0


async def test_code_close_in_meaning_is_found_only_in_its_own_repository(root, fake_embedder):
    await index_and_sync(root)
    await mongo.embeddings().insert_one({
        "repo": "someone/else", "kind": "code", "ref_id": "someone/else:x.py", "model": "fake/embedder",
        "vector": encode([3.0, 3.0, 3.0, 3.0, 0, 0, 0, 0]),
    })

    hits = await code.similar(WORKSPACE, "user-1", REPO, "How does a guest export a PDF report?", 5)

    assert {node_id for node_id, _ in hits[:2]} == {file_id(REPO, "app/downloads.py"), DOCUMENT}
    assert all(node_id.startswith(REPO) for node_id, _ in hits)
    assert COLOURS not in dict(hits)
    assert fake_embedder.calls[-1]["purpose"] == EmbedPurpose.CODE_QUERY


async def test_the_chat_shows_code_close_in_meaning_with_a_reference_and_its_lines(
    root, fake_embedder, graph_store, monkeypatch,
):
    sha, _ = await index_and_sync(root)

    async def nothing_by_keyword(*args, **kwargs):
        return []

    # keyword search would also find these words in the docstring; only meaning search is checked here
    monkeypatch.setattr(graph_store, "search_text", nothing_by_keyword)
    builder = await open_ask_context(
        graph_store, REPO, "How does a guest export a PDF report?", commit=sha, asked_by=ASKER,
    )
    context = builder.build()

    assert "Close in meaning to the question" in context.text
    ref = builder.refs[DOCUMENT]
    assert f"[{ref}] build_document" in context.text
    assert any(shown.path == "app/downloads.py" for shown in context.sources.values())


async def test_without_an_asker_nothing_is_searched_by_meaning(root, fake_embedder, graph_store):
    sha, _ = await index_and_sync(root)
    fake_embedder.calls.clear()

    builder = await open_ask_context(graph_store, REPO, "How does a guest get a pdf?", commit=sha)

    assert "Close in meaning" not in builder.build().text
    assert fake_embedder.calls == []


async def test_the_meaning_lookup_hands_out_references_that_can_be_read(root, fake_embedder, graph_store):
    sha, _ = await index_and_sync(root)
    builder = await open_ask_context(graph_store, REPO, "unrelated", commit=sha)
    lookups = Lookups(builder, graph_store, REPO, sha, Workspace(*ASKER))
    search = call(SEARCH_MEANING, query="guest PDF report export")

    result = await lookups.run(search)
    ref = builder.refs[DOCUMENT]
    again = await lookups.run(search)
    read = await lookups.run(call(READ_CODE, ref=ref))

    assert f"[{ref}] build_document" in result
    assert again == ALREADY_LOOKED_UP
    assert "Turn rows into a PDF report" in read


async def test_the_meaning_lookup_needs_a_workspace_to_charge(root, fake_embedder, graph_store):
    sha, _ = await index_and_sync(root)
    builder = await open_ask_context(graph_store, REPO, "unrelated", commit=sha)
    search = call(SEARCH_MEANING, query="guest PDF report")

    assert (await Lookups(builder, graph_store, REPO, sha).run(search)).startswith("error: there is no tool")


async def test_purging_a_repository_drops_its_vectors(root, fake_embedder, monkeypatch):
    await index_and_sync(root)

    async def remove(repo_full_name):
        return None

    monkeypatch.setattr(github_checkout, "remove", remove)
    await github_service._purge(REPO)

    assert await mongo.embeddings().count_documents({"repo": REPO}) == 0


async def test_an_embedding_outage_never_fails_the_index(monkeypatch, tmp_path, fake_embedder):
    async def failing_sync(*args, **kwargs):
        raise ExternalServiceError("down", error_code="embedding_unavailable", status_code=503)

    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "a.py").write_text("def run():\n    return 1\n")
    _stub_checkout(monkeypatch, tmp_path / "repo")
    monkeypatch.setattr(code, "sync", failing_sync)
    await _insert_repo()

    await jobs.run_index(REPO, full=True)

    doc = await _repo_doc()
    assert doc["parse_status"] == "done"
    assert "embeddings" not in doc["graph_stats"]


async def test_a_vector_store_error_never_fails_the_index(monkeypatch, tmp_path, fake_embedder):
    async def over_quota(*args, **kwargs):
        raise OperationFailure("you are over your space quota")

    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "a.py").write_text("def run():\n    return 1\n")
    _stub_checkout(monkeypatch, tmp_path / "repo")
    monkeypatch.setattr(code, "sync", over_quota)
    await _insert_repo()

    await jobs.run_index(REPO, full=True)

    doc = await _repo_doc()
    assert doc["parse_status"] == "done"
    assert "embeddings" not in doc["graph_stats"]


async def test_an_index_records_what_embedding_did(monkeypatch, tmp_path, fake_embedder):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "a.py").write_text("def run():\n    return 1\n")
    _stub_checkout(monkeypatch, tmp_path / "repo")
    await _insert_repo()

    await jobs.run_index(REPO, full=True)

    # the stubbed checkout has no git history to read code from, so nothing is embedded, but it was run
    assert (await _repo_doc())["graph_stats"]["embeddings"]["embedded"] == 0


async def test_a_slow_embedding_provider_never_holds_up_the_chat(root, fake_embedder, graph_store, monkeypatch):
    sha, _ = await index_and_sync(root)
    monkeypatch.setenv("CODE_SIMILAR_TIMEOUT_SECONDS", "0.05")
    get_settings.cache_clear()

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(fake_embedder, "embed", slow)

    async with asyncio.timeout(1):
        builder = await open_ask_context(graph_store, REPO, "How does a guest export a PDF report?", commit=sha, asked_by=ASKER)

    assert "Close in meaning" not in builder.build().text


async def test_a_failing_vector_search_leaves_the_chat_and_the_lookup_working(root, fake_embedder, graph_store, monkeypatch):
    sha, _ = await index_and_sync(root)

    async def rebuilding(*args, **kwargs):
        raise OperationFailure("the search index is being rebuilt")

    monkeypatch.setattr(embeddings_service, "nearest_code", rebuilding)

    builder = await open_ask_context(graph_store, REPO, "How does a guest export a PDF report?", commit=sha, asked_by=ASKER)
    result = await Lookups(builder, graph_store, REPO, sha, Workspace(*ASKER)).run(call(SEARCH_MEANING, query="guest PDF"))

    assert "Close in meaning" not in builder.build().text
    assert result.startswith("(nothing close in meaning")


async def test_vectors_written_after_a_repository_was_disconnected_are_dropped(monkeypatch, tmp_path, fake_embedder):
    async def sync_racing_a_disconnect(*args, **kwargs):
        # the index's last write lands after the purge removed every vector
        await mongo.embeddings().insert_one({"repo": REPO, "kind": "code", "ref_id": f"{REPO}:a.py", "model": "fake/embedder"})
        await mongo.repos().delete_many({"repo_full_name": REPO})
        return {}

    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "a.py").write_text("def run():\n    return 1\n")
    _stub_checkout(monkeypatch, tmp_path / "repo")
    monkeypatch.setattr(code, "sync", sync_racing_a_disconnect)
    await _insert_repo()

    await jobs.run_index(REPO, full=True)

    assert await mongo.embeddings().count_documents({"repo": REPO}) == 0
