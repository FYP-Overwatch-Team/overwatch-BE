"""AI chat: private conversations, grounded answers, history, mentions and limits."""

import pytest

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError
from app.db import mongo
from app.db.knowledge_graph_store import get_knowledge_graph_store
from app.knowledge_graph.build.ids import module_id
from app.knowledge_graph.build.model import EdgeType, GraphDelta, GraphEdge, GraphNode, NodeLabel
from app.query.grounding import CANNOT_DETERMINE
from tests.integration.test_query_endpoint import REPO, authed, seed_connected_repo_with_graph
from tests.integration.test_workspaces import in_workspace, user

API, SERVICES = module_id(REPO, "api"), module_id(REPO, "services")
# with no symbol in the question, the context is the module overview: n1 api, n2 services
GROUNDED = {
    "answer": "api [n1] depends on services [n2].",
    "answerable": True,
    "cited_refs": ["n1", "n2"],
    "relations": [{"source": "n1", "type": "depends_on", "target": "n2"}],
}


@pytest.fixture
async def owner(client, graph_store):
    headers = await authed(client)
    await seed_connected_repo_with_graph(headers["X-Workspace-Id"])
    return headers


@pytest.fixture
def limits(monkeypatch):
    def set_limits(**values):
        for name, value in values.items():
            monkeypatch.setenv(name.upper(), str(value))
        get_settings.cache_clear()

    yield set_limits
    get_settings.cache_clear()


async def ask(client, headers, question="what does api depend on?", **body):
    return await client.post("/chat/ask", json={"repo_full_name": REPO, "question": question, **body}, headers=headers)


async def test_a_question_starts_a_conversation_with_a_cited_answer(client, owner, fake_gemini):
    fake_gemini.json_response = GROUNDED

    resp = await ask(client, owner)

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["conversation"]["title"] == "what does api depend on?"
    assert data["conversation"]["message_count"] == 2
    assert data["answer"]["text"] == "api [n1] depends on services [n2]."
    assert [(n["ref"], n["node_id"]) for n in data["answer"]["nodes"]] == [("n1", API), ("n2", SERVICES)]

    listed = await client.get(f"/chat/conversations?repo_full_name={REPO}", headers=owner)
    assert [c["id"] for c in listed.json()["data"]] == [data["conversation"]["id"]]
    full = await client.get(f"/chat/conversations/{data['conversation']['id']}", headers=owner)
    assert [m["role"] for m in full.json()["data"]["messages"]] == ["user", "assistant"]


async def test_a_follow_up_sends_the_conversation_and_what_was_cited(client, owner, fake_gemini):
    fake_gemini.json_response = GROUNDED
    first = (await ask(client, owner)).json()["data"]

    await ask(client, owner, "what uses it?", conversation_id=first["conversation"]["id"])

    history = fake_gemini.calls[-1]["history"]
    assert [(turn.role, turn.text) for turn in history] == [
        ("user", "what does api depend on?"),
        # references only mean something in their own context, so they go back as names
        ("model", "api depends on services."),
    ]
    assert "Mentioned earlier:" in fake_gemini.calls[-1]["prompt"]


async def test_an_unsupported_relationship_is_stored_as_a_refusal(client, owner, fake_gemini):
    fake_gemini.json_response = {**GROUNDED, "relations": [{"source": "n2", "type": "depends_on", "target": "n1"}]}

    answer = (await ask(client, owner)).json()["data"]["answer"]

    assert answer["text"] == CANNOT_DETERMINE
    assert answer["grounded"] is False
    assert answer["nodes"] == []


async def test_mentions_are_kept_on_the_question_and_unknown_ones_ignored(client, owner, fake_gemini):
    fake_gemini.json_response = GROUNDED

    question = (await ask(client, owner, "is @services used?", mentioned_node_ids=[SERVICES, "other/repo:x.py"])).json()["data"]["question"]

    assert question["nodes"] == [{"ref": None, "node_id": SERVICES, "module_id": SERVICES, "name": "services"}]
    assert "Mentioned in the question:" in fake_gemini.calls[-1]["prompt"]


async def test_conversations_are_private_to_their_member(client, owner, fake_gemini):
    fake_gemini.json_response = GROUNDED
    conversation_id = (await ask(client, owner)).json()["data"]["conversation"]["id"]
    teammate = await user(client, "teammate")
    await mongo.workspace_members().insert_one({
        "_id": "m-teammate", "workspace_id": owner["X-Workspace-Id"], "user_id": "id-teammate", "role": "admin",
    })
    teammate = in_workspace(teammate, owner["X-Workspace-Id"])

    listed = await client.get(f"/chat/conversations?repo_full_name={REPO}", headers=teammate)
    fetched = await client.get(f"/chat/conversations/{conversation_id}", headers=teammate)
    continued = await ask(client, teammate, conversation_id=conversation_id)
    deleted = await client.delete(f"/chat/conversations/{conversation_id}", headers=teammate)

    assert listed.json()["data"] == []
    assert [r.status_code for r in (fetched, continued, deleted)] == [404, 404, 404]
    assert continued.json()["meta"]["error_code"] == "conversation_not_found"


async def test_the_owner_can_delete_a_conversation(client, owner, fake_gemini):
    fake_gemini.json_response = GROUNDED
    conversation_id = (await ask(client, owner)).json()["data"]["conversation"]["id"]

    assert (await client.delete(f"/chat/conversations/{conversation_id}", headers=owner)).status_code == 200
    assert (await client.get(f"/chat/conversations/{conversation_id}", headers=owner)).status_code == 404


async def test_a_revoked_repo_is_refused_before_any_ai_call(client, owner, fake_gemini):
    await mongo.repos().update_one({"repo_full_name": REPO}, {"$set": {"access_status": "revoked"}})

    resp = await ask(client, owner)

    assert resp.status_code == 403
    assert fake_gemini.calls == []


async def test_a_failed_ai_call_stores_nothing(client, owner, fake_gemini, monkeypatch):
    async def fail(*args, **kwargs):
        raise ExternalServiceError("Gemini request failed", error_code="gemini_unavailable")

    monkeypatch.setattr(fake_gemini, "generate_json", fail)

    resp = await ask(client, owner)

    assert resp.status_code == 502
    assert await mongo.chat_conversations().count_documents({}) == 0


async def test_the_hourly_limit_answers_429(client, owner, fake_gemini, limits):
    limits(chat_user_hourly_limit=1)
    fake_gemini.json_response = GROUNDED

    assert (await ask(client, owner)).status_code == 200
    resp = await ask(client, owner)

    assert resp.status_code == 429
    assert resp.json()["meta"]["error_code"] == "rate_limited"
    assert len(fake_gemini.calls) == 1


async def test_a_spent_daily_budget_answers_503_and_counts_nothing(client, owner, fake_gemini, limits):
    limits(chat_daily_budget=0)

    resp = await ask(client, owner)

    assert resp.status_code == 503
    assert resp.json()["meta"]["error_code"] == "llm_quota_exhausted"
    # the member's and workspace's windows were given back
    assert await mongo.usage_counters().count_documents({"count": {"$gt": 0}}) == 0


async def test_a_full_conversation_is_refused(client, owner, fake_gemini):
    fake_gemini.json_response = GROUNDED
    conversation_id = (await ask(client, owner)).json()["data"]["conversation"]["id"]
    await mongo.chat_conversations().update_one({"_id": conversation_id}, {"$set": {"message_count": 99}})

    resp = await ask(client, owner, conversation_id=conversation_id)

    assert resp.status_code == 409
    assert resp.json()["meta"]["error_code"] == "conversation_full"


async def test_questions_are_bounded(client, owner, fake_gemini):
    assert (await ask(client, owner, "x" * 2_001)).status_code == 422
    assert (await ask(client, owner, "   ")).status_code == 422
    assert fake_gemini.calls == []


async def test_no_graph_yet_is_a_clear_error(client, owner, fake_gemini):
    await get_knowledge_graph_store().delete_repository(REPO)

    resp = await ask(client, owner)

    assert resp.status_code == 404
    assert resp.json()["meta"]["error_code"] == "graph_not_ready"
    # nothing was asked of Gemini, so nothing counts against the limits
    assert await mongo.usage_counters().count_documents({}) == 0


async def test_open_folders_reach_the_context(client, owner, fake_gemini):
    await get_knowledge_graph_store().apply(GraphDelta(
        repo_full_name=REPO, version="sha-2",
        upserted_nodes=(GraphNode(f"{REPO}:api/routes.py", NodeLabel.FILE, {"name": "routes.py", "path": "api/routes.py"}),),
        upserted_edges=(GraphEdge(API, f"{REPO}:api/routes.py", EdgeType.CONTAINS),),
    ))
    fake_gemini.json_response = GROUNDED

    await ask(client, owner, "can you see it now?", open_folder_ids=[API])

    assert "--contains--> [n2] routes.py (file)" in fake_gemini.calls[-1]["prompt"]


async def test_only_the_latest_messages_are_sent_as_history(client, owner, fake_gemini):
    fake_gemini.json_response = GROUNDED
    conversation_id = (await ask(client, owner, "question 0")).json()["data"]["conversation"]["id"]
    for index in range(1, 5):
        await ask(client, owner, f"question {index}", conversation_id=conversation_id)

    history = fake_gemini.calls[-1]["history"]
    assert [turn.text for turn in history if turn.role == "user"] == ["question 1", "question 2", "question 3"]


async def test_the_selected_functions_code_reaches_the_prompt(client, owner, fake_gemini, tmp_path, monkeypatch):
    monkeypatch.setenv("REPOS_DIR", str(tmp_path / "repos"))
    get_settings.cache_clear()
    (tmp_path / "repos" / "octocat__hello-world" / "api").mkdir(parents=True)
    (tmp_path / "repos" / "octocat__hello-world" / "api" / "auth.py").write_text("def login():\n    return True\n")
    login = f"{REPO}:api/auth.py#login"
    await get_knowledge_graph_store().apply(GraphDelta(
        repo_full_name=REPO, version="sha-2",
        upserted_nodes=(GraphNode(login, NodeLabel.SYMBOL, {
            "name": "login", "kind": "function", "file_path": "api/auth.py", "start_line": 1, "end_line": 2,
        }),),
    ))
    fake_gemini.json_response = GROUNDED

    await ask(client, owner, "how does it work?", focused_node_id=login)

    assert "      2 |     return True" in fake_gemini.calls[-1]["prompt"]
    get_settings.cache_clear()


async def test_every_mention_is_kept(client, owner, fake_gemini):
    files = [f"{REPO}:api/f{index}.py" for index in range(7)]
    await get_knowledge_graph_store().apply(GraphDelta(
        repo_full_name=REPO, version="sha-2",
        upserted_nodes=tuple(GraphNode(node_id, NodeLabel.FILE, {"name": f"f{i}.py", "path": f"api/f{i}.py"}) for i, node_id in enumerate(files)),
    ))
    fake_gemini.json_response = GROUNDED
    question = " ".join(f"@f{index}.py" for index in range(7))

    resp = await ask(client, owner, question, mentioned_node_ids=files)

    assert resp.status_code == 200
    assert [node["node_id"] for node in resp.json()["data"]["question"]["nodes"]] == files
    assert fake_gemini.calls[-1]["prompt"].count("Mentioned in the question:") == 1


async def test_a_mention_shows_its_name_as_the_repository_has_it(client, owner, fake_gemini):
    route = f"{REPO}:app/[slug]"
    await get_knowledge_graph_store().apply(GraphDelta(
        repo_full_name=REPO, version="sha-2",
        upserted_nodes=(GraphNode(route, NodeLabel.MODULE, {"name": "[slug]", "kind": "module", "path": "app/[slug]"}),),
    ))
    fake_gemini.json_response = GROUNDED

    question = (await ask(client, owner, "what is @[slug]?", mentioned_node_ids=[route])).json()["data"]["question"]

    assert question["nodes"][0]["name"] == "[slug]"
    # the prompt still gets the escaped form
    assert "(slug)" in fake_gemini.calls[-1]["prompt"]
