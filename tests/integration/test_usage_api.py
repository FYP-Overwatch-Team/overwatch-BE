"""AI usage: every model call is recorded against its workspace, member and feature, and summed per workspace."""

from datetime import UTC, datetime, timedelta

import pytest

from app.chat import limits as chat_limits
from app.core.time import utcnow
from app.db import mongo
from app.decisions import limits as decision_limits
from tests.integration.test_chat_api import GROUNDED, ask, authed, seed_connected_repo_with_graph
from tests.integration.test_workspaces import user


@pytest.fixture
async def member(client, graph_store):
    headers = await authed(client)
    await seed_connected_repo_with_graph(headers["X-Workspace-Id"])
    return headers


def row(
    workspace_id: str, user_id: str, *, days_ago: int = 0, feature: str = "chat", cost: float | None = 0.01,
    at: datetime | None = None,
) -> dict:
    return {
        "workspace_id": workspace_id, "user_id": user_id, "feature": feature, "model": "m", "provider": None,
        "prompt_tokens": 100, "completion_tokens": 10, "cached_tokens": 5, "cost_usd": cost,
        "created_at": at or utcnow() - timedelta(days=days_ago),
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
        row(workspace_id, "u2", cost=None, feature="decision_check"),
        row(workspace_id, "u1", days_ago=2),
        row(workspace_id, "u1", days_ago=40),
    ])

    data = (await client.get("/usage?days=30", headers=member)).json()["data"]

    assert data["total"] == {
        "calls": 3, "prompt_tokens": 300, "completion_tokens": 30, "cached_tokens": 15, "cost_usd": 0.02,
    }
    assert [day["calls"] for day in data["by_day"]] == [2, 1]
    assert data["by_day"][0]["calls_by_feature"] == {"chat": 1, "decision_check": 1}
    assert data["by_day"][0]["calls_by_member"] == {"u1": 1, "u2": 1}
    assert {(f["feature"], f["calls"]) for f in data["by_feature"]} == {("chat", 2), ("decision_check", 1)}
    assert {m["user_id"]: m["calls"] for m in data["by_member"]} == {"u1": 2, "u2": 1}


async def test_days_are_counted_in_the_time_zone_asked_for(client, member):
    # 20:00 UTC yesterday is already today in Karachi (UTC+5)
    yesterday_evening = datetime.combine(utcnow().date() - timedelta(days=1), datetime.min.time(), UTC) + timedelta(hours=20)
    await mongo.llm_usage().insert_one(row(member["X-Workspace-Id"], "u1", at=yesterday_evening))

    utc = (await client.get("/usage?days=7", headers=member)).json()["data"]
    karachi = (await client.get("/usage?days=7&tz=Asia/Karachi", headers=member)).json()["data"]

    assert utc["time_zone"] == "UTC"
    assert utc["by_day"][0]["day"] == str(yesterday_evening.date())
    assert karachi["time_zone"] == "Asia/Karachi"
    assert karachi["by_day"][0]["day"] == str(yesterday_evening.date() + timedelta(days=1))


async def test_an_unknown_time_zone_counts_in_utc(client, member):
    data = (await client.get("/usage?tz=Not/AZone", headers=member)).json()["data"]

    assert data["time_zone"] == "UTC"


async def test_a_workspace_sees_only_its_own_usage(client, member):
    await mongo.llm_usage().insert_one(row(member["X-Workspace-Id"], "u1"))
    other = await user(client, "someone-else")

    data = (await client.get("/usage", headers=other)).json()["data"]

    assert data["total"]["calls"] == 0
    assert data["by_day"] == []


def by_kind(response) -> dict:
    return {limit["kind"]: limit for limit in response.json()["data"]["limits"]}


async def test_limits_show_what_today_has_taken_and_when_they_reset(client, member):
    workspace_id = member["X-Workspace-Id"]
    await decision_limits.take_suggestion(workspace_id)
    await decision_limits.take_check(workspace_id)
    await decision_limits.take_check(workspace_id)

    limits = by_kind(await client.get("/usage/limits", headers=member))

    assert (limits["decision_suggestions"]["used"], limits["decision_suggestions"]["limit"]) == (1, 20)
    assert limits["decision_checks"]["used"] == 2
    assert limits["chat_questions"]["used"] == 0
    tomorrow = utcnow().date() + timedelta(days=1)
    assert datetime.fromisoformat(limits["decision_checks"]["resets_at"]) == datetime.combine(
        tomorrow, datetime.min.time(), UTC,
    )


async def test_limits_count_only_this_workspace_and_this_member(client, member):
    other = await user(client, "someone-else")
    await chat_limits.take(other["X-Workspace-Id"], "someone")
    await chat_limits.take(member["X-Workspace-Id"], "another-member")

    limits = by_kind(await client.get("/usage/limits", headers=member))

    assert limits["chat_questions"]["used"] == 1
    assert limits["member_chat_questions"]["used"] == 0
