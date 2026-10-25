"""AI chat: private conversations, grounded answers, history, mentions and limits."""

import json

import pytest

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError
from app.db import mongo
from app.db.knowledge_graph_store import get_knowledge_graph_store
from app.integrations.llm import Delta, ToolCall
from app.knowledge_graph.build.ids import module_id
from app.knowledge_graph.build.model import EdgeType, GraphDelta, GraphEdge, GraphNode, NodeLabel
from app.query.grounding import CANNOT_DETERMINE
from app.query.service import ANSWER_NOW, ANSWER_WITHOUT_ASKING, LOOKUPS_RAN_OUT
from app.query.tools import READ_CODE, RELATED, SEARCH_GRAPH
from tests.fakes import written
from tests.unit.test_llm_client import WRITTEN_CALLS
from tests.integration.test_workspaces import in_workspace, user
from tests.git_fixtures import commit_all
from tests.graph_fixtures import GraphSeed
from tests.integration.test_auth_flow import auth_headers, login

REPO = "octocat/hello-world"


async def authed(client) -> dict:
    return auth_headers(await login(client))


async def seed_connected_repo_with_graph(workspace_id: str) -> None:
    await mongo.repos().insert_one({
        "workspace_id": workspace_id, "repo_full_name": REPO, "default_branch": "main",
        "parse_status": "done", "parse_error": None, "connected_at": 1, "updated_at": 1,
    })
    await GraphSeed(REPO).modules("api", "services").depends("api", "services").apply(get_knowledge_graph_store())


API, SERVICES = module_id(REPO, "api"), module_id(REPO, "services")
# with no symbol in the question, the context is the module overview: n1 api, n2 services
GROUNDED = {
    "answer": "api [n1] depends on services [n2].",
    "answerable": True,
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


async def with_checkout(tmp_path, files: dict[str, str]) -> str:
    """Commit `files` into the repo's checkout and mark that commit as the one the graph was built from."""
    root = tmp_path / "repos" / "octocat__hello-world"
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    sha = commit_all(root)
    await mongo.repos().update_many({"repo_full_name": REPO}, {"$set": {"graph_version": sha}})
    return sha


async def ask(client, headers, question="what does api depend on?", **body):
    return await client.post("/chat/ask", json={"repo_full_name": REPO, "question": question, **body}, headers=headers)


async def test_a_question_starts_a_conversation_with_a_cited_answer(client, owner, fake_llm):
    fake_llm.json_response = GROUNDED

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


async def test_a_follow_up_sends_the_conversation_and_what_was_cited(client, owner, fake_llm):
    fake_llm.json_response = GROUNDED
    first = (await ask(client, owner)).json()["data"]

    await ask(client, owner, "what uses it?", conversation_id=first["conversation"]["id"])

    history = fake_llm.calls[-1]["history"]
    assert [(turn.role, turn.text) for turn in history] == [
        ("user", "what does api depend on?"),
        # references only mean something in their own context, so they go back as names
        ("model", "api depends on services."),
    ]
    assert "Mentioned earlier:" in fake_llm.calls[-1]["prompt"]


async def test_an_unsupported_relationship_is_stored_as_a_refusal(client, owner, fake_llm):
    fake_llm.json_response = {**GROUNDED, "relations": [{"source": "n2", "type": "depends_on", "target": "n1"}]}

    answer = (await ask(client, owner)).json()["data"]["answer"]

    assert answer["text"] == CANNOT_DETERMINE
    assert answer["grounded"] is False
    assert answer["nodes"] == []


async def test_mentions_are_kept_on_the_question_and_unknown_ones_ignored(client, owner, fake_llm):
    fake_llm.json_response = GROUNDED

    question = (await ask(client, owner, "is @services used?", mentioned_node_ids=[SERVICES, "other/repo:x.py"])).json()["data"]["question"]

    assert question["nodes"] == [{"ref": None, "node_id": SERVICES, "module_id": SERVICES, "name": "services"}]
    assert "Mentioned in the question:" in fake_llm.calls[-1]["prompt"]


async def test_conversations_are_private_to_their_member(client, owner, fake_llm):
    fake_llm.json_response = GROUNDED
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


async def test_the_owner_can_delete_a_conversation(client, owner, fake_llm):
    fake_llm.json_response = GROUNDED
    conversation_id = (await ask(client, owner)).json()["data"]["conversation"]["id"]

    assert (await client.delete(f"/chat/conversations/{conversation_id}", headers=owner)).status_code == 200
    assert (await client.get(f"/chat/conversations/{conversation_id}", headers=owner)).status_code == 404


async def test_a_revoked_repo_is_refused_before_any_ai_call(client, owner, fake_llm):
    await mongo.repos().update_one({"repo_full_name": REPO}, {"$set": {"access_status": "revoked"}})

    resp = await ask(client, owner)

    assert resp.status_code == 403
    assert fake_llm.calls == []


async def test_a_failed_ai_call_stores_nothing(client, owner, fake_llm, monkeypatch):
    async def fail(*args, **kwargs):
        raise ExternalServiceError("The AI request failed", error_code="llm_failed")
        yield  # a generator, like the real stream

    monkeypatch.setattr(fake_llm, "stream_text", fail)

    resp = await ask(client, owner)

    assert resp.status_code == 502
    assert await mongo.chat_conversations().count_documents({}) == 0


async def test_the_hourly_limit_answers_429(client, owner, fake_llm, limits):
    limits(chat_user_hourly_limit=1)
    fake_llm.json_response = GROUNDED

    assert (await ask(client, owner)).status_code == 200
    resp = await ask(client, owner)

    assert resp.status_code == 429
    assert resp.json()["meta"]["error_code"] == "rate_limited"
    assert len(fake_llm.calls) == 1


async def test_a_spent_daily_budget_answers_503_and_counts_nothing(client, owner, fake_llm, limits):
    limits(chat_daily_budget=0)

    resp = await ask(client, owner)

    assert resp.status_code == 503
    assert resp.json()["meta"]["error_code"] == "llm_quota_exhausted"
    # the member's and workspace's windows were given back
    assert await mongo.usage_counters().count_documents({"count": {"$gt": 0}}) == 0


async def test_a_full_conversation_is_refused(client, owner, fake_llm):
    fake_llm.json_response = GROUNDED
    conversation_id = (await ask(client, owner)).json()["data"]["conversation"]["id"]
    await mongo.chat_conversations().update_one({"_id": conversation_id}, {"$set": {"message_count": 99}})

    resp = await ask(client, owner, conversation_id=conversation_id)

    assert resp.status_code == 409
    assert resp.json()["meta"]["error_code"] == "conversation_full"


async def test_questions_are_bounded(client, owner, fake_llm):
    assert (await ask(client, owner, "x" * 2_001)).status_code == 422
    assert (await ask(client, owner, "   ")).status_code == 422
    assert fake_llm.calls == []


async def test_no_graph_yet_is_a_clear_error(client, owner, fake_llm):
    await get_knowledge_graph_store().delete_repository(REPO)

    resp = await ask(client, owner)

    assert resp.status_code == 404
    assert resp.json()["meta"]["error_code"] == "graph_not_ready"
    # nothing was asked of the model, so nothing counts against the limits
    assert await mongo.usage_counters().count_documents({}) == 0


async def test_open_folders_reach_the_context(client, owner, fake_llm):
    await get_knowledge_graph_store().apply(GraphDelta(
        repo_full_name=REPO, version="sha-2",
        upserted_nodes=(GraphNode(f"{REPO}:api/routes.py", NodeLabel.FILE, {"name": "routes.py", "path": "api/routes.py"}),),
        upserted_edges=(GraphEdge(API, f"{REPO}:api/routes.py", EdgeType.CONTAINS),),
    ))
    fake_llm.json_response = GROUNDED

    await ask(client, owner, "can you see it now?", open_folder_ids=[API])

    assert "--contains--> [n2] routes.py (file)" in fake_llm.calls[-1]["prompt"]


async def test_only_the_latest_messages_are_sent_as_history(client, owner, fake_llm):
    fake_llm.json_response = GROUNDED
    conversation_id = (await ask(client, owner, "question 0")).json()["data"]["conversation"]["id"]
    for index in range(1, 5):
        await ask(client, owner, f"question {index}", conversation_id=conversation_id)

    history = fake_llm.calls[-1]["history"]
    assert [turn.text for turn in history if turn.role == "user"] == ["question 1", "question 2", "question 3"]


async def test_the_selected_functions_code_reaches_the_prompt(client, owner, fake_llm, tmp_path, monkeypatch):
    monkeypatch.setenv("REPOS_DIR", str(tmp_path / "repos"))
    get_settings.cache_clear()
    await with_checkout(tmp_path, {"api/auth.py": "def login():\n    return True\n"})
    login = f"{REPO}:api/auth.py#login"
    await get_knowledge_graph_store().apply(GraphDelta(
        repo_full_name=REPO, version="sha-2",
        upserted_nodes=(GraphNode(login, NodeLabel.SYMBOL, {
            "name": "login", "kind": "function", "file_path": "api/auth.py", "start_line": 1, "end_line": 2,
        }),),
    ))
    fake_llm.json_response = GROUNDED

    await ask(client, owner, "how does it work?", focused_node_id=login)

    assert "      2 |     return True" in fake_llm.calls[-1]["prompt"]
    get_settings.cache_clear()


async def test_every_mention_is_kept(client, owner, fake_llm):
    files = [f"{REPO}:api/f{index}.py" for index in range(7)]
    await get_knowledge_graph_store().apply(GraphDelta(
        repo_full_name=REPO, version="sha-2",
        upserted_nodes=tuple(GraphNode(node_id, NodeLabel.FILE, {"name": f"f{i}.py", "path": f"api/f{i}.py"}) for i, node_id in enumerate(files)),
    ))
    fake_llm.json_response = GROUNDED
    question = " ".join(f"@f{index}.py" for index in range(7))

    resp = await ask(client, owner, question, mentioned_node_ids=files)

    assert resp.status_code == 200
    assert [node["node_id"] for node in resp.json()["data"]["question"]["nodes"]] == files
    assert fake_llm.calls[-1]["prompt"].count("Mentioned in the question:") == 1


async def test_a_mention_shows_its_name_as_the_repository_has_it(client, owner, fake_llm):
    route = f"{REPO}:app/[slug]"
    await get_knowledge_graph_store().apply(GraphDelta(
        repo_full_name=REPO, version="sha-2",
        upserted_nodes=(GraphNode(route, NodeLabel.MODULE, {"name": "[slug]", "kind": "module", "path": "app/[slug]"}),),
    ))
    fake_llm.json_response = GROUNDED

    question = (await ask(client, owner, "what is @[slug]?", mentioned_node_ids=[route])).json()["data"]["question"]

    assert question["nodes"][0]["name"] == "[slug]"
    # the prompt still gets the escaped form
    assert "(slug)" in fake_llm.calls[-1]["prompt"]


@pytest.fixture
async def login_code(tmp_path, monkeypatch):
    """`login` in api/auth.py, committed, with the graph built from that commit."""
    monkeypatch.setenv("REPOS_DIR", str(tmp_path / "repos"))
    get_settings.cache_clear()
    sha = await with_checkout(tmp_path, {"api/auth.py": "def login(user):\n    check(user)\n    return True\n"})
    login = f"{REPO}:api/auth.py#login"
    await get_knowledge_graph_store().apply(GraphDelta(
        repo_full_name=REPO, version=sha,
        upserted_nodes=(GraphNode(login, NodeLabel.SYMBOL, {
            "name": "login", "kind": "function", "file_path": "api/auth.py", "start_line": 1, "end_line": 3,
        }),),
    ))
    yield sha, login
    get_settings.cache_clear()


async def test_code_citations_carry_the_files_own_lines(client, owner, fake_llm, login_code):
    sha, login = login_code
    # the model "quotes" different code; only the line range it points at is used
    fake_llm.json_response = {
        "answer": "It checks the user first [s1:2] and then succeeds [s1:3-3]. Fake [s1:9-12].",
        "answerable": True, "relations": [],
    }

    resp = await ask(client, owner, "how does login work?", focused_node_id=login)

    answer = resp.json()["data"]["answer"]
    assert answer["text"] == "It checks the user first [c1] and then succeeds [c2]. Fake."
    assert answer["citations"] == [
        {"ref": "c1", "path": "api/auth.py", "commit_sha": sha, "start_line": 2, "end_line": 2,
         "code": "    check(user)", "language": "python", "node_id": login},
        {"ref": "c2", "path": "api/auth.py", "commit_sha": sha, "start_line": 3, "end_line": 3,
         "code": "    return True", "language": "python", "node_id": login},
    ]
    full = await client.get(f"/chat/conversations/{resp.json()['data']['conversation']['id']}", headers=owner)
    assert full.json()["data"]["messages"][1]["citations"] == answer["citations"]


async def test_a_follow_up_sees_the_cited_code_again_by_path(client, owner, fake_llm, login_code):
    _, login = login_code
    fake_llm.json_response = {
        "answer": "It checks the user [s1:2].", "answerable": True, "relations": [],
    }
    first = await ask(client, owner, "how does login work?", focused_node_id=login)

    await ask(client, owner, "and then?", conversation_id=first.json()["data"]["conversation"]["id"])

    call = fake_llm.calls[-1]
    assert call["history"][-1].text == "It checks the user (api/auth.py:L2-2)."
    assert "Source s1 (api/auth.py:2-2), cited earlier:" in call["prompt"]


async def test_code_cited_at_an_older_commit_isnt_shown_again(client, owner, fake_llm, login_code, tmp_path):
    _, login = login_code
    fake_llm.json_response = {
        "answer": "It checks the user [s1:2].", "answerable": True, "relations": [],
    }
    first = await ask(client, owner, "how does login work?", focused_node_id=login)
    # re-indexed since: line 2 is now something else
    await with_checkout(tmp_path, {"api/auth.py": "def login(user):\n    audit(user)\n    check(user)\n"})

    await ask(client, owner, "and then?", conversation_id=first.json()["data"]["conversation"]["id"])

    assert "cited earlier" not in fake_llm.calls[-1]["prompt"]


async def test_answers_saved_before_code_citations_still_load(client, owner, fake_llm):
    fake_llm.json_response = GROUNDED
    resp = await ask(client, owner)
    conversation_id = resp.json()["data"]["conversation"]["id"]
    doc = await mongo.chat_conversations().find_one({"_id": conversation_id})
    messages = [{k: v for k, v in m.items() if k != "citations"} for m in doc["messages"]]
    await mongo.chat_conversations().update_one({"_id": conversation_id}, {"$set": {"messages": messages}})

    full = await client.get(f"/chat/conversations/{conversation_id}", headers=owner)

    assert full.status_code == 200
    assert [m["citations"] for m in full.json()["data"]["messages"]] == [[], []]


FLOW = {**GROUNDED, "answer": "api [n1] hands off to services [n2].\n\nSTEPS:\n1. [n1] takes the request\n2. [n2] does the work"}


async def test_a_flow_answer_stores_its_steps_from_one_ai_call(client, owner, fake_llm):
    fake_llm.json_response = FLOW

    data = (await ask(client, owner, "how does a request flow?")).json()["data"]

    assert len(fake_llm.calls) == 1
    assert data["answer"]["text"] == "api [n1] hands off to services [n2]."
    expected = [
        {"ref": "n1", "node_id": API, "text": "takes the request", "citation_ref": None},
        {"ref": "n2", "node_id": SERVICES, "text": "does the work", "citation_ref": None},
    ]
    assert data["answer"]["steps"] == expected
    full = await client.get(f"/chat/conversations/{data['conversation']['id']}", headers=owner)
    assert [m["steps"] for m in full.json()["data"]["messages"]] == [[], expected]


async def test_a_follow_up_sees_the_steps_by_name(client, owner, fake_llm):
    fake_llm.json_response = FLOW
    first = (await ask(client, owner, "how does a request flow?")).json()["data"]

    await ask(client, owner, "and then?", conversation_id=first["conversation"]["id"])

    assert fake_llm.calls[-1]["history"][-1].text == (
        "api hands off to services.\n\nSTEPS:\n1. api takes the request\n2. services does the work"
    )


async def test_answers_saved_before_steps_still_load(client, owner, fake_llm):
    fake_llm.json_response = FLOW
    conversation_id = (await ask(client, owner)).json()["data"]["conversation"]["id"]
    doc = await mongo.chat_conversations().find_one({"_id": conversation_id})
    messages = [{k: v for k, v in m.items() if k != "steps"} for m in doc["messages"]]
    await mongo.chat_conversations().update_one({"_id": conversation_id}, {"$set": {"messages": messages}})

    full = await client.get(f"/chat/conversations/{conversation_id}", headers=owner)

    assert [m["steps"] for m in full.json()["data"]["messages"]] == [[], []]


async def test_a_cited_file_can_be_read_at_its_commit(client, owner, login_code):
    sha, _ = login_code

    resp = await client.get(f"/graph/source?repo_full_name={REPO}&path=api/auth.py&commit_sha={sha}", headers=owner)

    assert resp.status_code == 200
    assert resp.json()["data"] == {
        "path": "api/auth.py", "commit_sha": sha, "language": "python",
        "content": "def login(user):\n    check(user)\n    return True", "line_count": 3, "truncated": False,
        "is_current": True,
    }


async def test_a_file_that_cant_be_read_is_a_clear_404(client, owner, login_code):
    for query in ("path=api/missing.py", "path=../../etc/passwd", f"path=api/auth.py&commit_sha={'a' * 40}"):
        resp = await client.get(f"/graph/source?repo_full_name={REPO}&{query}", headers=owner)
        assert resp.status_code == 404, query
        assert resp.json()["meta"]["error_code"] == "source_unavailable"
    bad_sha = await client.get(f"/graph/source?repo_full_name={REPO}&path=api/auth.py&commit_sha=HEAD", headers=owner)
    assert bad_sha.status_code == 422


async def test_source_is_only_served_to_workspaces_that_connected_the_repo(client, owner, login_code):
    stranger = await user(client, "stranger")  # signed in, in their own workspace

    resp = await client.get(f"/graph/source?repo_full_name={REPO}&path=api/auth.py", headers=stranger)

    assert resp.status_code == 404


def events_of(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


async def ask_streaming(client, headers, question="what does api depend on?", **body):
    return await client.post(
        "/chat/ask/stream", json={"repo_full_name": REPO, "question": question, **body}, headers=headers,
    )


async def test_a_streamed_answer_shows_thinking_and_text_then_the_saved_answer(client, owner, fake_llm):
    fake_llm.json_response = GROUNDED
    fake_llm.thoughts = ["Looking at ", "the modules."]

    resp = await ask_streaming(client, owner)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = events_of(resp.text)
    kinds = [event for event, _ in events]
    assert kinds[0] == "started" and kinds[-1] == "done"
    assert "".join(d["text"] for e, d in events if e == "thinking") == "Looking at the modules."
    # the header is never streamed, only the answer
    assert "".join(d["text"] for e, d in events if e == "answer") == "api [n1] depends on services [n2]."
    done = events[-1][1]
    assert done["answer"]["text"] == "api [n1] depends on services [n2]."
    assert await mongo.chat_conversations().count_documents({}) == 1


async def test_an_unsupported_relationship_is_refused_before_any_answer_text(client, owner, fake_llm):
    fake_llm.json_response = {**GROUNDED, "relations": [{"source": "n2", "type": "depends_on", "target": "n1"}]}

    events = events_of((await ask_streaming(client, owner)).text)

    assert [e for e, _ in events if e == "answer"] == []
    assert events[-1][1]["answer"]["text"] == CANNOT_DETERMINE
    assert events[-1][1]["answer"]["grounded"] is False


async def test_a_refused_question_is_an_ordinary_error_before_the_stream(client, owner, fake_llm):
    await mongo.repos().update_one({"repo_full_name": REPO}, {"$set": {"access_status": "revoked"}})

    resp = await ask_streaming(client, owner)

    assert resp.status_code == 403
    assert resp.json()["meta"]["error_code"] == "repo_access_revoked"
    assert fake_llm.calls == []


async def test_a_failure_mid_stream_is_an_error_event_and_stores_nothing(client, owner, fake_llm, monkeypatch):
    async def fail_midway(*args, **kwargs):
        yield Delta("thought", "Hmm")
        raise ExternalServiceError("The AI service is busy", error_code="llm_unavailable", status_code=503)

    monkeypatch.setattr(fake_llm, "stream_text", fail_midway)

    events = events_of((await ask_streaming(client, owner)).text)

    assert events[-1] == ("error", {"error_code": "llm_unavailable", "message": "The AI service is busy"})
    assert await mongo.chat_conversations().count_documents({}) == 0


def lookup(name: str, **arguments) -> ToolCall:
    return ToolCall(f"call-{name}", name, json.dumps(arguments))


async def test_the_model_can_look_further_before_answering(client, owner, fake_llm):
    fake_llm.json_response = GROUNDED
    fake_llm.lookups = [[lookup(RELATED, ref="n1", direction="out"), lookup(SEARCH_GRAPH, query="services")]]

    events = events_of((await ask_streaming(client, owner)).text)

    assert [d["text"] for e, d in events if e == "status"] == ["Following what api uses", "Searching for “services”"]
    assert events[-1][0] == "done" and events[-1][1]["answer"]["text"] == "api [n1] depends on services [n2]."
    first, second = fake_llm.calls
    assert first["tool_choice"] == "auto" and first["rounds"] == []
    (looked,) = second["rounds"]
    assert [call.name for call in looked.calls] == [RELATED, SEARCH_GRAPH]
    assert "--depends_on--> [n2] services" in looked.results[0]


async def test_a_lookup_of_an_unknown_ref_gets_an_error_back(client, owner, fake_llm):
    fake_llm.json_response = GROUNDED
    fake_llm.lookups = [[lookup(READ_CODE, ref="n99")]]

    await ask_streaming(client, owner)

    assert fake_llm.calls[-1]["rounds"][0].results == ("error: unknown ref; use one from the context or an earlier lookup",)


async def test_looking_stops_at_the_cap_and_the_model_must_answer(client, owner, fake_llm, limits):
    limits(chat_max_lookups=2)
    fake_llm.json_response = GROUNDED
    fake_llm.lookups = [[lookup(SEARCH_GRAPH, query="api")]] * 10

    events = events_of((await ask_streaming(client, owner)).text)

    assert events[-1][0] == "done"
    assert [call["tool_choice"] for call in fake_llm.calls] == ["auto", "auto", "none"]


async def test_each_extra_model_call_counts_against_the_daily_budget(client, owner, fake_llm, limits):
    limits(chat_daily_budget=2)
    fake_llm.json_response = GROUNDED
    fake_llm.lookups = [[lookup(SEARCH_GRAPH, query="api")]] * 10

    events = events_of((await ask_streaming(client, owner)).text)

    # the question is the first call; one more fits the budget, then it has to answer
    assert events[-1][0] == "done"
    assert [call["tool_choice"] for call in fake_llm.calls] == ["auto", "auto", "none"]
    budget = await mongo.usage_counters().find_one({"_id": {"$regex": "^chat:global:"}})
    assert budget["count"] == 2


async def test_the_call_that_must_answer_after_lookups_counts_too(client, owner, fake_llm, limits):
    limits(chat_max_lookups=1)
    fake_llm.json_response = GROUNDED
    fake_llm.lookups = [[lookup(SEARCH_GRAPH, query="api")]]

    await ask_streaming(client, owner)

    assert [call["tool_choice"] for call in fake_llm.calls] == ["auto", "none"]
    budget = await mongo.usage_counters().find_one({"_id": {"$regex": "^chat:global:"}})
    assert budget["count"] == 2


async def test_without_lookups_no_tools_are_offered(client, owner, fake_llm, limits):
    limits(chat_max_lookups=0)
    fake_llm.json_response = GROUNDED

    await ask(client, owner)

    assert fake_llm.calls[0]["tools"] == []


async def test_calls_a_model_writes_as_text_are_run_as_lookups(client, owner, fake_llm):
    fake_llm.replies = [WRITTEN_CALLS, written(GROUNDED)]

    events = events_of((await ask_streaming(client, owner)).text)

    assert [d["text"] for e, d in events if e == "status"] == ["Searching the code for “socket.emit”", "Reading a file"]
    assert events[-1][1]["answer"]["text"] == "api [n1] depends on services [n2]."
    assert [call.name for call in fake_llm.calls[1]["rounds"][0].calls] == ["grep_code", "read_code"]


async def test_markup_is_never_shown_as_the_answer(client, owner, fake_llm, limits):
    limits(chat_max_lookups=1)
    fake_llm.lookups = [[lookup(SEARCH_GRAPH, query="api")]]
    fake_llm.replies = [WRITTEN_CALLS]

    events = events_of((await ask_streaming(client, owner)).text)

    final = fake_llm.calls[-1]
    assert final["tool_choice"] == "none" and final["closing"] == ANSWER_NOW
    answer = events[-1][1]["answer"]
    assert answer["text"] == LOOKUPS_RAN_OUT and answer["grounded"] is False
    assert not any("DSML" in d.get("text", "") for e, d in events if e == "answer")


async def test_once_its_time_for_looking_is_up_the_model_must_answer(client, owner, fake_llm, monkeypatch):
    monkeypatch.setattr("app.query.service.LOOKING_SECONDS", 0)
    fake_llm.json_response = GROUNDED
    fake_llm.lookups = [[lookup(SEARCH_GRAPH, query="api")]] * 3

    await ask(client, owner)

    assert [call["tool_choice"] for call in fake_llm.calls] == ["none"]


async def test_follow_ups_are_stored_with_the_answer(client, owner, fake_llm):
    fake_llm.json_response = {**GROUNDED, "answer": "api [n1] depends on services [n2].\n\nFOLLOW_UPS:\n- What does services do?"}

    data = (await ask(client, owner)).json()["data"]

    assert data["answer"]["follow_ups"] == ["What does services do?"]
    full = await client.get(f"/chat/conversations/{data['conversation']['id']}", headers=owner)
    assert full.json()["data"]["messages"][1]["follow_ups"] == ["What does services do?"]


ASKED = {**GROUNDED, "answer": "Two match: api [n1] and services [n2].\n\nASK: Which one?\n- The api\n- The services"}


async def test_a_question_back_is_stored_and_the_next_answer_cant_ask_again(client, owner, fake_llm):
    fake_llm.json_response = ASKED
    first = (await ask(client, owner)).json()["data"]

    second = (await ask(client, owner, "The api", conversation_id=first["conversation"]["id"])).json()["data"]

    assert first["answer"]["ask"] == {"question": "Which one?", "options": ["The api", "The services"]}
    assert second["answer"]["ask"] is None
    assert "Which one?\n\n- The api" in second["answer"]["text"]
    call = fake_llm.calls[-1]
    assert call["history"][-1].text.endswith("ASK: Which one?\n- The api\n- The services")
    assert call["prompt"].endswith(f"({ANSWER_WITHOUT_ASKING})")


async def test_answers_saved_before_follow_ups_and_questions_back_still_load(client, owner, fake_llm):
    fake_llm.json_response = GROUNDED
    conversation_id = (await ask(client, owner)).json()["data"]["conversation"]["id"]
    doc = await mongo.chat_conversations().find_one({"_id": conversation_id})
    messages = [{k: v for k, v in m.items() if k not in ("follow_ups", "ask")} for m in doc["messages"]]
    await mongo.chat_conversations().update_one({"_id": conversation_id}, {"$set": {"messages": messages}})

    full = await client.get(f"/chat/conversations/{conversation_id}", headers=owner)

    assert [(m["follow_ups"], m["ask"]) for m in full.json()["data"]["messages"]] == [([], None), ([], None)]


async def test_a_conversations_calls_share_a_session(client, owner, fake_llm):
    fake_llm.json_response = GROUNDED
    first = (await ask(client, owner)).json()["data"]

    await ask(client, owner, "and then?", conversation_id=first["conversation"]["id"])

    assert fake_llm.calls[-1]["session"] == first["conversation"]["id"]


async def test_once_lookups_have_no_room_left_the_model_must_answer(client, owner, fake_llm, monkeypatch):
    monkeypatch.setattr("app.query.context.MAX_LOOKUP_LINES", 0)
    fake_llm.json_response = GROUNDED
    fake_llm.lookups = [[lookup(SEARCH_GRAPH, query="api")]]

    await ask(client, owner)

    assert [call["tool_choice"] for call in fake_llm.calls] == ["none"]
