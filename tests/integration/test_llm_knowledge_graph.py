"""Grounding on the knowledge graph: retrieved context, validated answers."""

import pytest

from app.core.config import get_settings
from app.knowledge_graph.build.model import EdgeType, GraphDelta, GraphEdge, GraphNode, NodeLabel
from app.services import llm_grounding_service
from app.services.graph_context import build_ask_context, keywords, owning_module_id
from app.services.knowledge_graph_store import (
    InMemoryKnowledgeGraphStore,
    use_knowledge_graph_store,
)

REPO = "octocat/hello-world"
MODULE = f"{REPO}:api"
FILE = f"{REPO}:api/handlers.py"
HANDLER = f"{FILE}#handle_login"
HELPER = f"{REPO}:services/auth.py#verify_password"


@pytest.fixture(autouse=True)
def knowledge_graph_enabled(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_GRAPH_V2", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def graph():
    store = InMemoryKnowledgeGraphStore()
    use_knowledge_graph_store(store)
    await store.apply(
        GraphDelta(
            repo_full_name=REPO,
            version="sha-1",
            upserted_nodes=(
                GraphNode(MODULE, NodeLabel.MODULE, {"name": "api", "kind": "module", "file_count": 1}),
                GraphNode(f"{REPO}:services", NodeLabel.MODULE, {"name": "services", "kind": "module", "file_count": 1}),
                GraphNode(FILE, NodeLabel.FILE, {"name": "handlers.py", "path": "api/handlers.py"}),
                GraphNode(HANDLER, NodeLabel.SYMBOL, {"name": "handle_login", "kind": "function", "signature": "handle_login(request)", "file_path": "api/handlers.py"}),
                GraphNode(HELPER, NodeLabel.SYMBOL, {"name": "verify_password", "kind": "function", "signature": "verify_password(hash)", "file_path": "services/auth.py"}),
            ),
            upserted_edges=(
                GraphEdge(MODULE, f"{REPO}:services", EdgeType.DEPENDS_ON, {"weight": 2}),
                GraphEdge(MODULE, FILE, EdgeType.CONTAINS),
                GraphEdge(FILE, HANDLER, EdgeType.DEFINES),
                GraphEdge(HANDLER, HELPER, EdgeType.CALLS, {"count": 1}),
            ),
        )
    )
    yield store
    use_knowledge_graph_store(None)


# -- Context ---------------------------------------------------------------


async def test_context_retrieves_the_symbols_the_question_names(graph):
    context = await build_ask_context(graph, REPO, "what does handle_login call?")

    assert "handle_login" in context.labels
    assert "handle_login(request)" in context.text  # its signature is included
    assert "Modules:" in context.text


async def test_context_is_fenced_and_labelled_as_data(graph):
    context = await build_ask_context(graph, REPO, "anything")

    assert context.text.startswith("<graph_context>")
    assert context.text.endswith("</graph_context>")


async def test_context_includes_the_selected_node_and_its_neighbourhood(graph):
    context = await build_ask_context(graph, REPO, "explain this", focused_node_id=HANDLER)

    assert "Selected: handle_login" in context.text
    assert "verify_password" in context.labels


async def test_context_is_bounded(monkeypatch, graph):
    monkeypatch.setattr("app.services.graph_context.MAX_CONTEXT_LINES", 3)

    context = await build_ask_context(graph, REPO, "handle_login verify_password")

    assert "more lines omitted" in context.text
    assert len(context.text.splitlines()) <= 6


def test_keywords_ignore_filler_words():
    assert keywords("what does handle_login call in the auth service?") == [
        "handle_login", "auth",
    ]


def test_owning_module_is_derived_from_a_file_or_symbol_id():
    assert owning_module_id(HANDLER, REPO) == MODULE
    assert owning_module_id(FILE, REPO) == MODULE
    # A top-level file belongs to no module; the repository owns it.
    assert owning_module_id(f"{REPO}:main.py", REPO) == REPO
    # Packages belong to no module of ours.
    assert owning_module_id(f"{REPO}:pkg:npm/react", REPO) == f"{REPO}:pkg:npm/react"


# -- Answers ---------------------------------------------------------------


async def test_ask_returns_module_highlights_and_precise_references(graph, fake_gemini):
    fake_gemini.json_response = {
        "answer": "handle_login calls verify_password.",
        "highlighted_node_names": ["handle_login", "verify_password"],
    }

    result = await llm_grounding_service.ask(REPO, "what does handle_login call?")

    assert result["answer"] == "handle_login calls verify_password."
    # Highlights are modules, which is what the dashboard renders…
    assert result["highlighted_node_ids"] == [MODULE, f"{REPO}:services"]
    # …and the exact symbols stay available.
    assert result["references"] == [HANDLER, HELPER]


async def test_ask_drops_nodes_the_model_invented(graph, fake_gemini):
    fake_gemini.json_response = {
        "answer": "It calls PaymentService.",
        "highlighted_node_names": ["handle_login", "PaymentService", "DROP TABLE users"],
    }

    result = await llm_grounding_service.ask(REPO, "what does handle_login call?")

    assert result["references"] == [HANDLER]
    assert result["highlighted_node_ids"] == [MODULE]


async def test_ask_refuses_when_there_is_no_graph_yet(graph, fake_gemini):
    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        await llm_grounding_service.ask("nobody/else", "anything")


async def test_flow_returns_ordered_module_steps(graph, fake_gemini):
    fake_gemini.json_response = {"step_node_names": ["handle_login", "verify_password"]}

    result = await llm_grounding_service.flow(REPO, "show me the login flow")

    assert result["step_node_ids"] == [MODULE, f"{REPO}:services"]


async def test_flow_collapses_consecutive_steps_in_one_module(graph, fake_gemini):
    fake_gemini.json_response = {"step_node_names": ["api", "handle_login", "verify_password"]}

    result = await llm_grounding_service.flow(REPO, "login")

    assert result["step_node_ids"] == [MODULE, f"{REPO}:services"]


async def test_flow_drops_invented_steps(graph, fake_gemini):
    fake_gemini.json_response = {"step_node_names": ["api", "GhostService", "services"]}

    result = await llm_grounding_service.flow(REPO, "login")

    assert result["step_node_ids"] == [MODULE, f"{REPO}:services"]


async def test_the_prompt_warns_that_context_is_data(graph, fake_gemini):
    fake_gemini.json_response = {"answer": "", "highlighted_node_names": []}

    await llm_grounding_service.ask(REPO, "anything")

    system = fake_gemini.calls[-1]["system"]
    assert "not instructions" in system
    assert "<graph_context>" in fake_gemini.calls[-1]["prompt"]
