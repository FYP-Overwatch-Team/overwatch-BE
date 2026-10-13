"""Regression pin: a fixed graph + fixed mocked Gemini response -> exact ordered id list."""

import pytest

from app.knowledge_graph.build.ids import module_id
from app.query import service as grounding
from app.db.knowledge_graph_store import get_knowledge_graph_store
from tests.graph_fixtures import GraphSeed

pytestmark = pytest.mark.regression

REPO = "octocat/hello-world"

# modules are referenced in name order: n1 api, n2 db, n3 services
FIXED_GEMINI_RESPONSE = {"step_refs": ["n1", "n3", "[n99]", "n2"]}

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
