from app.db import mongo
from app.parsing.graph_builder import node_id
from app.services.graph_service import get_graph_repository
from tests.integration.test_auth_flow import login

REPO = "octocat/hello-world"


async def authed(client) -> dict:
    access = await login(client)
    return {"Authorization": f"Bearer {access}"}


async def seed_connected_repo_with_graph(user_id: str) -> None:
    await mongo.repos().insert_one({
        "user_id": user_id, "repo_full_name": REPO, "default_branch": "main",
        "webhook_status": "created", "parse_status": "done",
        "parse_error": None, "connected_at": 1, "updated_at": 1,
    })
    graph = {
        "nodes": [
            {"id": node_id(REPO, "api"), "name": "api", "kind": "module", "file_count": 2, "definition_count": 3},
            {"id": node_id(REPO, "services"), "name": "services", "kind": "module", "file_count": 1, "definition_count": 1},
        ],
        "edges": [
            {"source": node_id(REPO, "api"), "target": node_id(REPO, "services"), "type": "DEPENDS_ON", "weight": 1},
        ],
    }
    await get_graph_repository().upsert_graph(REPO, graph)


async def test_ask_endpoint_filters_hallucinated_node(client, graph_repo, fake_gemini):
    headers = await authed(client)
    user = await mongo.users().find_one({})
    await seed_connected_repo_with_graph(user["_id"])

    fake_gemini.json_response = {
        "answer": "api calls services and also the fake billing module.",
        "highlighted_node_names": ["api", "services", "billing"],
    }
    resp = await client.post(
        "/query/ask",
        json={"repo_full_name": REPO, "question": "what does api call?"},
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["highlighted_node_ids"]) == {node_id(REPO, "api"), node_id(REPO, "services")}
    assert "billing" not in str(body["highlighted_node_ids"])


async def test_flow_endpoint_returns_ordered_ids(client, graph_repo, fake_gemini):
    headers = await authed(client)
    user = await mongo.users().find_one({})
    await seed_connected_repo_with_graph(user["_id"])

    fake_gemini.json_response = {"step_node_names": ["api", "services"]}
    resp = await client.post(
        "/query/flow",
        json={"repo_full_name": REPO, "question": "trace a request"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["step_node_ids"] == [node_id(REPO, "api"), node_id(REPO, "services")]


async def test_query_requires_connected_repo(client, graph_repo, fake_gemini):
    headers = await authed(client)
    resp = await client.post(
        "/query/ask", json={"repo_full_name": "nobody/nothing", "question": "q"}, headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "repo_not_connected"


async def test_query_requires_auth(client):
    resp = await client.post("/query/ask", json={"repo_full_name": REPO, "question": "q"})
    assert resp.status_code == 401
