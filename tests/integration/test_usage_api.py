"""AI usage: every model call is recorded against its workspace, member and feature, and summed per workspace."""

from datetime import timedelta

import pytest

from app.core.time import utcnow
from app.db import mongo
from tests.integration.test_chat_api import GROUNDED, ask, authed, seed_connected_repo_with_graph
from tests.integration.test_workspaces import user


@pytest.fixture
async def member(client, graph_store):
    headers = await authed(client)
    await seed_connected_repo_with_graph(headers["X-Workspace-Id"])
    return headers


def row(workspace_id: str, user_id: str, *, days_ago: int = 0, feature: str = "chat", cost: float | None = 0.01) -> dict:
    return {
        "workspace_id": workspace_id, "user_id": user_id, "feature": feature, "model": "m", "provider": None,
        "prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 5, "cost_usd": cost,
        "created_at": utcnow() - timedelta(days=days_ago),
    }


async def test_a_chat_question_records_what_its_model_call_used(client, member, fake_llm):
    fake_llm.json_response = GROUNDED

    assert (await ask(client, member)).status_code == 200

    recorded = await mongo.llm_usage().find_one({})
    assert recorded["workspace_id"] == member["X-Workspace-Id"]
    assert (recorded["feature"], recorded["prompt_tokens"], recorded["completion_tokens"]) == ("chat", 100, 20)
    total = (await client.get("/usage", headers=member)).json()["data"]["total"]
    assert (total["calls"], total["cost_usd"]) == (1, 0.001)


async def test_usage_is_split_by_day_feature_and_member_within_the_window(client, member):
    workspace_id = member["X-Workspace-Id"]
    await mongo.llm_usage().insert_many([
        row(workspace_id, "u1"),
        row(workspace_id, "u2", cost=None),
        row(workspace_id, "u1", days_ago=2),
        row(workspace_id, "u1", days_ago=40),
    ])

    data = (await client.get("/usage?days=30", headers=member)).json()["data"]

    assert data["total"] == {
        "calls": 3, "prompt_tokens": 300, "completion_tokens": 30, "cached_tokens": 15, "cost_usd": 0.02,
    }
    assert [day["calls"] for day in data["by_day"]] == [2, 1]
    assert [(f["feature"], f["calls"]) for f in data["by_feature"]] == [("chat", 3)]
    assert {m["user_id"]: m["calls"] for m in data["by_member"]} == {"u1": 2, "u2": 1}


async def test_a_workspace_sees_only_its_own_usage(client, member):
    await mongo.llm_usage().insert_one(row(member["X-Workspace-Id"], "u1"))
    other = await user(client, "someone-else")

    data = (await client.get("/usage", headers=other)).json()["data"]

    assert data["total"]["calls"] == 0
    assert data["by_day"] == []
