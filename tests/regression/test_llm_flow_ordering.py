"""Regression pin: a fixed graph + fixed mocked Gemini response -> exact ordered id list."""

import pytest

from app.knowledge_graph.build.ids import module_id
from app.services import llm_grounding_service as grounding
from app.services.knowledge_graph_store import get_knowledge_graph_store
from tests.graph_fixtures import GraphSeed

pytestmark = pytest.mark.regression

REPO = "octocat/hello-world"

FIXED_GEMINI_RESPONSE = {"step_node_names": ["api", "services", "hallucinated-cache", "db"]}

EXPECTED_ORDERED_IDS = [
    module_id(REPO, "api"),
    module_id(REPO, "services"),
    module_id(REPO, "db"),
]


async def test_flow_ordering_pinned_exact(graph_store, fake_gemini):
    await GraphSeed(REPO).modules("api", "services", "db").depends("api", "services").depends(
        "services", "db"
    ).apply(get_knowledge_graph_store())
    fake_gemini.json_response = FIXED_GEMINI_RESPONSE

    result = await grounding.flow(REPO, "walk through a typical request")

    assert result == {"step_node_ids": EXPECTED_ORDERED_IDS}
