import json

import pytest

from app.core.exceptions import AppError, NotFoundError
from app.parsing.graph_builder import node_id
from app.services import llm_grounding_service as grounding
from app.services.graph_service import get_graph_repository

REPO = "octocat/hello-world"


async def seed_graph():
    graph = {
        "nodes": [
            {"id": node_id(REPO, "api"), "name": "api", "kind": "module", "file_count": 2, "definition_count": 3},
            {"id": node_id(REPO, "services"), "name": "services", "kind": "module", "file_count": 1, "definition_count": 1},
            {"id": node_id(REPO, "__external__"), "name": "external dependencies", "kind": "external", "file_count": 0, "definition_count": 0},
        ],
        "edges": [
            {"source": node_id(REPO, "api"), "target": node_id(REPO, "services"), "type": "DEPENDS_ON", "weight": 1},
            {"source": node_id(REPO, "api"), "target": node_id(REPO, "__external__"), "type": "EXTERNAL_DEPENDENCY", "weight": 2},
        ],
    }
    await get_graph_repository().upsert_graph(REPO, graph)


async def test_ask_resolves_valid_node_names_to_ids(graph_repo, fake_gemini):
    await seed_graph()
    fake_gemini.json_response = {
        "answer": "The api module depends on services.",
        "highlighted_node_names": ["api", "services"],
    }
    result = await grounding.ask(REPO, "what does api depend on?")
    assert result["answer"] == "The api module depends on services."
    assert set(result["highlighted_node_ids"]) == {node_id(REPO, "api"), node_id(REPO, "services")}


async def test_ask_drops_hallucinated_node_names(graph_repo, fake_gemini):
    await seed_graph()
    fake_gemini.json_response = {
        "answer": "The payments service handles this.",
        "highlighted_node_names": ["api", "payments"],  # "payments" doesn't exist
    }
    result = await grounding.ask(REPO, "who handles payments?")
    assert result["highlighted_node_ids"] == [node_id(REPO, "api")]


async def test_ask_with_no_graph_raises_not_found(graph_repo, fake_gemini):
    with pytest.raises(NotFoundError) as exc:
        await grounding.ask(REPO, "anything")
    assert exc.value.error_code == "graph_not_ready"


async def test_ask_focused_node_uses_neighborhood(graph_repo, fake_gemini):
    await seed_graph()
    fake_gemini.json_response = {"answer": "ok", "highlighted_node_names": []}
    await grounding.ask(REPO, "what connects to api?", focused_node_id=node_id(REPO, "api"))
    prompt = fake_gemini.calls[0]["prompt"]
    assert "api" in prompt and "services" in prompt


async def test_ask_unknown_focused_node_raises(graph_repo, fake_gemini):
    await seed_graph()
    with pytest.raises(NotFoundError) as exc:
        await grounding.ask(REPO, "q", focused_node_id="not-a-real-id")
    assert exc.value.error_code == "node_not_found"


async def test_flow_returns_ordered_ids_and_drops_hallucinations(graph_repo, fake_gemini):
    await seed_graph()
    fake_gemini.json_response = {"step_node_names": ["api", "made-up", "services"]}
    result = await grounding.flow(REPO, "walk me through a request")
    assert result["step_node_ids"] == [node_id(REPO, "api"), node_id(REPO, "services")]


async def test_malformed_llm_json_raises_clean_error(graph_repo, fake_gemini):
    await seed_graph()
    fake_gemini.json_response = None
    fake_gemini.generate_json = lambda *a, **kw: _bad_json()  # type: ignore
    with pytest.raises(AppError) as exc:
        await grounding.ask(REPO, "q")
    assert exc.value.error_code == "llm_bad_output"


async def _bad_json():
    return "not valid json {{"
