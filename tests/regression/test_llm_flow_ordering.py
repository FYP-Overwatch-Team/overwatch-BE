"""Regression pin: a fixed graph + fixed mocked Gemini response -> exact ordered id list."""

import pytest

from app.parsing.graph_builder import node_id
from app.services import llm_grounding_service as grounding
from app.services.graph_service import get_graph_repository

pytestmark = pytest.mark.regression

REPO = "octocat/hello-world"

FIXED_GRAPH = {
    "nodes": [
        {"id": node_id(REPO, "api"), "name": "api", "kind": "module", "file_count": 2, "definition_count": 3},
        {"id": node_id(REPO, "services"), "name": "services", "kind": "module", "file_count": 1, "definition_count": 1},
        {"id": node_id(REPO, "db"), "name": "db", "kind": "module", "file_count": 1, "definition_count": 1},
    ],
    "edges": [
        {"source": node_id(REPO, "api"), "target": node_id(REPO, "services"), "type": "DEPENDS_ON", "weight": 1},
        {"source": node_id(REPO, "services"), "target": node_id(REPO, "db"), "type": "DEPENDS_ON", "weight": 1},
    ],
}

FIXED_GEMINI_RESPONSE = {"step_node_names": ["api", "services", "hallucinated-cache", "db"]}

EXPECTED_ORDERED_IDS = [
    node_id(REPO, "api"),
    node_id(REPO, "services"),
    node_id(REPO, "db"),
]


async def test_flow_ordering_pinned_exact(graph_repo, fake_gemini):
    await get_graph_repository().upsert_graph(REPO, FIXED_GRAPH)
    fake_gemini.json_response = FIXED_GEMINI_RESPONSE

    result = await grounding.flow(REPO, "walk through a typical request")

    assert result == {"step_node_ids": EXPECTED_ORDERED_IDS}
