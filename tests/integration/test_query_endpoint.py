from app.db import mongo
from app.knowledge_graph.build.ids import module_id
from app.db.knowledge_graph_store import get_knowledge_graph_store
from tests.graph_fixtures import GraphSeed
from tests.integration.test_auth_flow import auth_headers, login

REPO = "octocat/hello-world"


async def authed(client) -> dict:
    access = await login(client)
    return auth_headers(access)


async def seed_connected_repo_with_graph(user_id: str) -> None:
    await mongo.repos().insert_one({
        "workspace_id": user_id, "repo_full_name": REPO, "default_branch": "main",
        "parse_status": "done",
        "parse_error": None, "connected_at": 1, "updated_at": 1,
    })
    await GraphSeed(REPO).modules("api", "services").depends("api", "services").apply(
        get_knowledge_graph_store()
    )


async def test_flow_endpoint_returns_ordered_ids(client, graph_store, fake_gemini):
    headers = await authed(client)
    user = await mongo.users().find_one({})
    await seed_connected_repo_with_graph(user["_id"])

    # modules are listed first, so they are n1 and n2
    fake_gemini.json_response = {"step_refs": ["n1", "n2"]}
    resp = await client.post(
        "/query/flow",
        json={"repo_full_name": REPO, "question": "trace a request"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["step_node_ids"] == [module_id(REPO, "api"), module_id(REPO, "services")]


async def test_query_requires_connected_repo(client, graph_store, fake_gemini):
    headers = await authed(client)
    resp = await client.post(
        "/query/flow", json={"repo_full_name": "nobody/nothing", "question": "q"}, headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["meta"]["error_code"] == "repo_not_connected"


async def test_query_refuses_a_revoked_repo(client, graph_store, fake_gemini):
    headers = await authed(client)
    user = await mongo.users().find_one({})
    await seed_connected_repo_with_graph(user["_id"])
    await mongo.repos().update_one({"repo_full_name": REPO}, {"$set": {"access_status": "revoked"}})

    resp = await client.post("/query/flow", json={"repo_full_name": REPO, "question": "q"}, headers=headers)

    assert resp.status_code == 403
    assert resp.json()["meta"]["error_code"] == "repo_access_revoked"
    assert fake_gemini.calls == []


async def test_query_requires_auth(client):
    resp = await client.post("/query/flow", json={"repo_full_name": REPO, "question": "q"})
    assert resp.status_code == 401
