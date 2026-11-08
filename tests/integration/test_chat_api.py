"""AI chat: private conversations, grounded answers, history, mentions and limits."""

import asyncio
import json
from datetime import UTC, datetime

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
from app.integrations.embeddings import use_embedder
from app.query.tools import READ_CODE, RELATED, SEARCH_GRAPH, SEARCH_MEANING
from app.query.workspace_tools import (
    DRAFTED,
    FIND_DECISIONS,
    FIND_DOCS,
    FIND_TICKETS,
    MAX_DRAFTS,
    OPEN_RECORD,
    PROPOSE_DECISION,
    PROPOSE_DOC,
    PROPOSE_TICKET,
    READ_CHARS,
    WORKSPACE_INFO,
    WORKSPACE_TOOLS,
)
from tests.fakes import written
from tests.unit.test_llm_client import WRITTEN_CALLS
from tests.integration.test_workspaces import in_workspace, join, user
from tests.git_fixtures import commit_all
from tests.graph_fixtures import GraphSeed
from tests.integration.test_auth_flow import auth_headers, login
from tests.integration.test_decisions_api import decide, page
from tests.integration.test_work_items_api import board_id, seed_jira
from tests.integration.test_work_items_api import create as create_item

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


async def test_a_used_up_monthly_allowance_answers_429_and_counts_nothing(client, owner, fake_llm, limits):
    limits(ai_workspace_monthly_limit=0)

    resp = await ask(client, owner)

    assert resp.status_code == 429
    assert resp.json()["meta"]["error_code"] == "ai_allowance_used_up"
    assert await mongo.usage_counters().count_documents({"count": {"$gt": 0}}) == 0
    assert await mongo.ai_actions().count_documents({}) == 0
    assert fake_llm.calls == []


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


async def test_before_the_graph_is_built_only_the_workspace_can_be_looked_up(client, owner, fake_llm):
    await get_knowledge_graph_store().delete_repository(REPO)
    fake_llm.json_response = {"answer": "No tickets mention it."}

    resp = await ask(client, owner)

    assert resp.status_code == 200
    assert fake_llm.calls[0]["tools"] == WORKSPACE_TOOLS


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


def offered(call: dict) -> list[str]:
    return [tool["function"]["name"] for tool in call["tools"]]


async def test_searching_code_by_meaning_is_offered_only_with_embeddings(client, owner, fake_llm, fake_embedder):
    fake_llm.json_response = GROUNDED

    await ask(client, owner)
    use_embedder(None)
    await ask(client, owner)

    with_embeddings, without = fake_llm.calls
    assert SEARCH_MEANING in offered(with_embeddings)
    assert SEARCH_MEANING not in offered(without) and SEARCH_GRAPH in offered(without)


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


async def test_tickets_from_every_tracker_are_found_and_only_those_found_are_cited(client, owner, fake_llm):
    workspace = owner["X-Workspace-Id"]
    await seed_jira(workspace)
    await mongo.work_items().update_one({"key": "OVR-1"}, {"$set": {"title": "Rate limit the public API"}})
    native = await create_item(client, owner, title="Rate limit login attempts", description="Five tries a minute.")
    fake_llm.json_response = {"answer": "Two do: [r1] and [r2]. Not [r9]."}
    fake_llm.lookups = [[lookup(FIND_TICKETS, query="rate limit")]]

    events = events_of((await ask_streaming(client, owner, "any tickets about rate limits?")).text)

    assert [d["text"] for e, d in events if e == "status"] == ["Searching tickets for “rate limit”"]
    (found,) = fake_llm.calls[-1]["rounds"][0].results
    assert found.startswith("<workspace_data>\nTrackers connected: Jira.")
    answer = events[-1][1]["answer"]
    # the stream sends what's saved as it is, so every field the app reads must be there
    assert events[-1][1]["question"]["proposals"] == answer["proposals"] == []
    assert answer["text"] == "Two do: [r1] and [r2]. Not."
    assert answer["actions"] == []
    hrefs = {record["key"]: record["href"] for record in answer["records"]}
    assert set(hrefs) == {"OVR-1", native["key"]}
    assert hrefs[native["key"]] == f"/board?board={await board_id(client, owner)}&item={native['key']}"

    # a follow-up gets the tickets back by key, since refs only mean something within one answer
    conversation = events[-1][1]["conversation"]["id"]
    await ask(client, owner, "and which is done?", conversation_id=conversation)
    assert "(OVR-1)" in fake_llm.calls[-1]["history"][-1].text


async def test_with_no_tracker_and_no_tickets_the_answer_offers_to_connect_or_create(client, owner, fake_llm):
    fake_llm.json_response = {"answer": "There are no tickets yet."}
    fake_llm.lookups = [[lookup(FIND_TICKETS, sprint="current")]]

    answer = (await ask(client, owner, "what's in the current sprint?")).json()["data"]["answer"]

    assert fake_llm.calls[-1]["rounds"][0].results == (
        "<workspace_data>\nNo tracker is connected; only the app's own board was searched.\nNo tickets matched.\n</workspace_data>",
    )
    assert answer["actions"] == [
        {"kind": "connect_tracker", "label": "Connect a tracker", "href": "/settings/integrations"},
        {"kind": "create_ticket", "label": "Create a ticket", "href": "/board?new=1"},
    ]


async def test_the_current_sprint_holds_only_its_tickets(client, owner, fake_llm):
    native = await board_id(client, owner)
    sprint = (await client.post(f"/boards/{native}/sprints", json={}, headers=owner)).json()["data"]
    await client.post(f"/sprints/{sprint['id']}/start", json={}, headers=owner)
    planned = await create_item(client, owner, title="In the sprint")
    await create_item(client, owner, title="In the backlog")
    await mongo.work_items().update_one({"_id": planned["id"]}, {"$set": {"sprint_id": sprint["id"]}})
    fake_llm.json_response = {"answer": "One."}
    fake_llm.lookups = [[lookup(FIND_TICKETS, sprint="current", status="open")]]

    await ask(client, owner, "what's in the current sprint?")

    (found,) = fake_llm.calls[-1]["rounds"][0].results
    assert "In the sprint" in found and "In the backlog" not in found


async def test_another_workspaces_tickets_docs_and_decisions_are_never_found(client, owner, fake_llm):
    other = await user(client, "someone-else")
    await create_item(client, other, title="Export rules")
    await page(client, other, md="Export rules.")
    await decide(client, other, title="Export rules")
    fake_llm.json_response = {"answer": "None."}
    fake_llm.lookups = [[
        lookup(FIND_TICKETS, query="export"), lookup(FIND_DOCS, query="export"), lookup(FIND_DECISIONS, query="export"),
    ]]

    await ask(client, owner, "anything about export?")

    tickets, docs, decisions = fake_llm.calls[-1]["rounds"][0].results
    assert "No tickets matched." in tickets and "No docs pages matched." in docs and "No decisions matched." in decisions


async def test_a_long_ticket_is_read_whole_in_parts(client, owner, fake_llm):
    item = await create_item(client, owner, title="Spec")
    description = "".join(f"line {n} of the spec\n" for n in range(2_500))
    await mongo.work_items().update_one({"_id": item["id"]}, {"$set": {"description_md": description}})
    fake_llm.json_response = {"answer": "Read it: [r1]."}
    fake_llm.lookups = [
        [lookup(FIND_TICKETS, query="spec")],
        [lookup(OPEN_RECORD, ref="r1"), lookup(OPEN_RECORD, ref="r9")],
        [lookup(OPEN_RECORD, ref="r1", offset=READ_CHARS)],
        [lookup(OPEN_RECORD, ref="r1", offset=2 * READ_CHARS)],
    ]

    await ask(client, owner, "what does the spec say?")

    rounds = fake_llm.calls[-1]["rounds"]
    first, unknown = rounds[1].results
    assert first.endswith(f"read on with offset {READ_CHARS}")
    assert unknown == "error: unknown ref; use one a lookup gave, like r1"
    last = rounds[3].results[0]
    assert last.endswith("</workspace_data>")
    parts = [result.split("\n</workspace_data>")[0].removeprefix("<workspace_data>\n") for result in (first, rounds[2].results[0], last)]
    assert "".join(parts).endswith(f"\n\n{description}")


async def test_a_question_without_a_repository_is_answered_from_the_workspace(client, fake_llm):
    headers = await authed(client)
    fake_llm.json_response = {"answer": "No tickets yet."}

    resp = await client.post("/chat/ask", json={"question": "any tickets?"}, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["data"]["conversation"]["repo_full_name"] is None
    assert fake_llm.calls[0]["tools"] == WORKSPACE_TOOLS
    listed = (await client.get("/chat/conversations", headers=headers)).json()["data"]
    assert [c["id"] for c in listed] == [resp.json()["data"]["conversation"]["id"]]


async def test_create_is_offered_only_when_no_search_found_a_ticket(client, owner, fake_llm):
    await create_item(client, owner, title="Rate limit login attempts")
    fake_llm.json_response = {"answer": "Found it on the second try."}
    fake_llm.lookups = [[lookup(FIND_TICKETS, query="throttle")], [lookup(FIND_TICKETS, query="rate limit")]]

    answer = (await ask(client, owner, "anything about throttling?")).json()["data"]["answer"]

    # no tracker is connected, so that's still offered
    assert [action["kind"] for action in answer["actions"]] == ["connect_tracker"]


async def test_docs_are_found_and_read_whole_but_archived_ones_never(client, owner, fake_llm):
    live = await page(client, owner, md="Guests can export reports as PDF.")
    archived = await page(client, owner, md="Guests can export everything.")
    await mongo.documents().update_one({"_id": archived["id"]}, {"$set": {"archived_at": archived["created_at"]}})
    fake_llm.json_response = {"answer": "The Export PRD [r1] says so."}
    fake_llm.lookups = [[lookup(FIND_DOCS, query="guests export")], [lookup(OPEN_RECORD, ref="r1")]]

    answer = (await ask(client, owner, "can guests export?")).json()["data"]["answer"]

    found, read = (round_.results[0] for round_ in fake_llm.calls[-1]["rounds"])
    assert found.count("[r") == 1
    assert read.startswith("<workspace_data>\nExport PRD\nSource: the app") and "Guests can export reports as PDF." in read
    assert [(r["kind"], r["href"]) for r in answer["records"]] == [("document", f"/documents/{live['id']}")]
    # pages found, but no ticket searched for: nothing to offer
    assert answer["actions"] == []


async def test_decisions_are_found_by_status_and_read_with_their_evidence(client, owner, fake_llm):
    prd = await page(client, owner, md="Guests can export reports as PDF.")
    agreed = await decide(
        client, owner, title="PDF export for guests", statement="Guests export as PDF only.",
        evidence=[{"kind": "document", "ref_id": prd["id"], "quote": "Guests can export reports as PDF."}],
    )
    await decide(client, owner, title="CSV export", statement="Maybe CSV later.")
    await client.post(f"/decisions/{agreed['id']}/status", json={"status": "accepted"}, headers=owner)
    fake_llm.json_response = {"answer": "Yes: [r1]."}
    fake_llm.lookups = [[lookup(FIND_DECISIONS, query="export", status="accepted")], [lookup(OPEN_RECORD, ref="r1")]]

    answer = (await ask(client, owner, "what did we decide about export?")).json()["data"]["answer"]

    found, read = (round_.results[0] for round_ in fake_llm.calls[-1]["rounds"])
    assert "PDF export for guests" in found and "CSV export" not in found
    assert 'supports "Guests can export reports as PDF." from Export PRD' in read
    assert answer["records"][0]["href"] == f"/decisions/{agreed['id']}"


async def test_workspace_info_says_who_is_asking_and_what_is_connected(client, owner, fake_llm):
    fake_llm.json_response = {"answer": "You're the owner."}
    fake_llm.lookups = [[lookup(WORKSPACE_INFO)]]

    await ask(client, owner, "what's my role here?")

    (info,) = fake_llm.calls[-1]["rounds"][0].results
    assert "You: Octo Cat (owner)" in info
    assert f"- {REPO} · indexing done" in info
    assert "Connections: none" in info



async def drafted(client, headers, fake_llm, *calls: ToolCall) -> dict:
    """An answer whose lookups drafted `calls`."""
    fake_llm.json_response = {"answer": "I drafted it below for you to check."}
    fake_llm.lookups = [list(calls)]
    return (await ask(client, headers, "draft it")).json()["data"]


async def approve(client, headers, proposal: dict, **edits):
    return await client.post(f"/chat/proposals/{proposal['id']}/apply", json={**proposal["fields"], **edits}, headers=headers)


async def test_a_drafted_ticket_is_created_only_once_approved_with_the_askers_edits(client, owner, fake_llm):
    data = await drafted(client, owner, fake_llm, lookup(PROPOSE_TICKET, title="Fix login", description="It **fails**."))
    (proposal,) = data["answer"]["proposals"]
    assert fake_llm.calls[-1]["rounds"][0].results == (DRAFTED,)
    assert (proposal["kind"], proposal["status"]) == ("work_item", "pending")
    native = await board_id(client, owner)
    assert proposal["boards"][0]["id"] == proposal["fields"]["board_id"] == native
    assert await mongo.work_items().count_documents({}) == 0

    resp = await approve(client, owner, proposal, title="Fix login on Safari")

    assert resp.status_code == 200, resp.json()
    applied = resp.json()["data"]
    assert applied["status"] == "applied"
    item = await mongo.work_items().find_one({})
    asker = (await mongo.chat_proposals().find_one({"_id": proposal["id"]}))["user_id"]
    # made as the member who asked, by the board's own create
    assert (item["title"], item["description"], item["created_by"]) == ("Fix login on Safari", "It **fails**.", asker)
    assert applied["result"]["href"] == f"/board?board={native}&item={item['key']}"
    # approving again returns what was made, and makes nothing more
    again = await approve(client, owner, proposal)
    assert again.json()["data"]["result"] == applied["result"]
    assert await mongo.work_items().count_documents({}) == 1
    full = await client.get(f"/chat/conversations/{data['conversation']['id']}", headers=owner)
    assert full.json()["data"]["messages"][-1]["proposals"][0]["status"] == "applied"


async def test_pages_and_decisions_are_drafted_and_a_decision_starts_open(client, owner, fake_llm):
    data = await drafted(
        client, owner, fake_llm, lookup(PROPOSE_DOC, title="Login runbook", content="# Steps\n\nRestart it."),
        lookup(PROPOSE_DECISION, title="OAuth only", statement="We sign in with OAuth only.", background="Passwords leak."),
    )
    page, decision = data["answer"]["proposals"]

    made_page = (await approve(client, owner, page)).json()["data"]["result"]
    made_decision = (await approve(client, owner, decision)).json()["data"]["result"]

    stored = await mongo.documents().find_one({"_id": made_page["id"]})
    assert (stored["title"], stored["content_md"]) == ("Login runbook", "# Steps\n\nRestart it.")
    stored = await mongo.decisions().find_one({"_id": made_decision["id"]})
    assert (stored["status"], stored["statement"], stored["context_md"]) == ("open", "We sign in with OAuth only.", "Passwords leak.")
    assert made_decision["href"] == f"/decisions/{made_decision['id']}"


async def test_only_the_member_who_asked_can_approve_or_cancel(client, owner, fake_llm):
    (proposal,) = (await drafted(client, owner, fake_llm, lookup(PROPOSE_TICKET, title="Fix login")))["answer"]["proposals"]
    teammate = await user(client, "teammate")
    await join(client, owner, teammate, owner["X-Workspace-Id"])
    teammate = in_workspace(teammate, owner["X-Workspace-Id"])

    assert (await approve(client, teammate, proposal)).status_code == 404
    assert (await client.post(f"/chat/proposals/{proposal['id']}/cancel", headers=teammate)).status_code == 404
    assert await mongo.work_items().count_documents({}) == 0


async def test_a_cancelled_or_expired_draft_cant_be_approved(client, owner, fake_llm):
    cancelled, expired = (await drafted(
        client, owner, fake_llm, lookup(PROPOSE_TICKET, title="One"), lookup(PROPOSE_TICKET, title="Two"),
    ))["answer"]["proposals"]
    assert (await client.post(f"/chat/proposals/{cancelled['id']}/cancel", headers=owner)).json()["data"]["status"] == "cancelled"
    await mongo.chat_proposals().update_one({"_id": expired["id"]}, {"$set": {"expires_at": datetime(2020, 1, 1, tzinfo=UTC)}})

    for proposal, status in ((cancelled, "cancelled"), (expired, "expired")):
        resp = await approve(client, owner, proposal)
        assert resp.status_code == 409 and resp.json()["meta"]["error_code"] == "proposal_not_pending"
        assert status in resp.json()["meta"]["message"]
    assert await mongo.work_items().count_documents({}) == 0


async def test_a_failed_approval_can_be_tried_again(client, owner, fake_llm):
    (proposal,) = (await drafted(client, owner, fake_llm, lookup(PROPOSE_TICKET, title="Fix login")))["answer"]["proposals"]

    assert (await approve(client, owner, proposal, board_id="no-such-board")).status_code == 404
    assert (await approve(client, owner, proposal)).json()["data"]["status"] == "applied"


async def test_two_approvals_at_once_create_it_once(client, owner, fake_llm):
    (proposal,) = (await drafted(client, owner, fake_llm, lookup(PROPOSE_TICKET, title="Fix login")))["answer"]["proposals"]

    responses = await asyncio.gather(approve(client, owner, proposal), approve(client, owner, proposal))

    assert sorted(r.status_code for r in responses) in ([200, 200], [200, 409])
    assert await mongo.work_items().count_documents({}) == 1


async def test_an_answer_drafts_at_most_a_few_and_a_decision_needs_a_statement(client, owner, fake_llm):
    tickets = [lookup(PROPOSE_TICKET, title=f"Ticket {n}") for n in range(MAX_DRAFTS + 1)]
    data = await drafted(client, owner, fake_llm, lookup(PROPOSE_DECISION, title="No statement"), *tickets)

    results = fake_llm.calls[-1]["rounds"][0].results
    assert results[0] == "error: give it a title and a statement"
    assert results[-1] == f"error: at most {MAX_DRAFTS} drafts per answer"
    assert len(data["answer"]["proposals"]) == MAX_DRAFTS


async def test_a_secret_in_the_code_never_reaches_the_model_but_its_citation_shows_the_file(
    client, owner, fake_llm, tmp_path, monkeypatch,
):
    monkeypatch.setenv("REPOS_DIR", str(tmp_path / "repos"))
    get_settings.cache_clear()
    sha = await with_checkout(tmp_path, {"api/keys.py": 'def keys():\n    return "AKIAABCDEFGHIJKLMNOP"\n'})
    node = f"{REPO}:api/keys.py#keys"
    await get_knowledge_graph_store().apply(GraphDelta(
        repo_full_name=REPO, version=sha,
        upserted_nodes=(GraphNode(node, NodeLabel.SYMBOL, {
            "name": "keys", "kind": "function", "file_path": "api/keys.py", "start_line": 1, "end_line": 2,
        }),),
    ))
    fake_llm.json_response = {"answer": "It returns a key [s1:2].", "answerable": True, "relations": []}

    resp = await ask(client, owner, "what does keys return?", focused_node_id=node)

    prompt = fake_llm.calls[-1]["prompt"]
    assert "AKIAABCDEFGHIJKLMNOP" not in prompt
    assert '      2 |     return "[REDACTED]"' in prompt
    assert resp.json()["data"]["answer"]["citations"][0]["code"] == '    return "AKIAABCDEFGHIJKLMNOP"'
    get_settings.cache_clear()


async def test_lookup_results_come_back_fenced_as_data(client, owner, fake_llm):
    fake_llm.json_response = GROUNDED
    fake_llm.lookups = [[lookup(SEARCH_GRAPH, query="services")]]

    await ask_streaming(client, owner)

    (result,) = fake_llm.calls[-1]["rounds"][0].results
    assert result.startswith("<graph_context>\n") and result.endswith("\n</graph_context>")


async def test_a_tool_that_wasnt_offered_never_runs(client, owner, fake_llm):
    fake_llm.json_response = GROUNDED
    # without embeddings, searching by meaning isn't offered
    fake_llm.lookups = [[lookup(SEARCH_MEANING, query="where a guest exports a report")]]

    await ask_streaming(client, owner)

    assert fake_llm.calls[-1]["rounds"][0].results == (f"error: there is no tool called {SEARCH_MEANING}",)


async def test_a_rate_limited_question_never_pays_for_a_meaning_search(client, owner, fake_llm, fake_embedder, limits):
    limits(chat_user_hourly_limit=1)
    await mongo.repos().update_many({"repo_full_name": REPO}, {"$set": {"graph_version": "a" * 40}})
    fake_llm.json_response = GROUNDED
    assert (await ask(client, owner)).status_code == 200
    assert fake_embedder.calls
    fake_embedder.calls.clear()

    resp = await ask(client, owner)

    assert resp.status_code == 429
    assert fake_embedder.calls == []


async def test_before_the_graph_is_built_nothing_is_searched_by_meaning(client, owner, fake_llm, fake_embedder):
    fake_llm.json_response = GROUNDED

    await ask(client, owner)

    assert fake_embedder.calls == []


async def test_a_drafts_links_and_images_are_stripped_before_it_is_kept(client, owner, fake_llm):
    body = "See [the docs](https://attacker.test/?d=1) and ![x](https://attacker.test/p.png)."
    data = await drafted(client, owner, fake_llm, lookup(PROPOSE_DECISION, title="Use X", statement=body, background=body))

    (proposal,) = data["answer"]["proposals"]
    assert "attacker.test" not in json.dumps(proposal["fields"])
    assert "See the docs and x." in json.dumps(proposal["fields"])
