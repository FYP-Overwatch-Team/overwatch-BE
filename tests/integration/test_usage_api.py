"""AI usage: every model call and every action taken from the monthly allowance is recorded, and summed per workspace."""

from datetime import UTC, datetime, timedelta

import pytest

from app.chat import limits as chat_limits
from app.core.time import utcnow
from app.db import mongo
from app.decisions import limits as decision_limits
from app.usage.allowance import next_month
from tests.integration.test_chat_api import GROUNDED, ask, authed, seed_connected_repo_with_graph
from tests.integration.test_workspaces import user


@pytest.fixture
async def member(client, graph_store):
    headers = await authed(client)
    await seed_connected_repo_with_graph(headers["X-Workspace-Id"])
    return headers


def action(workspace_id: str, user_id: str, *, days_ago: int = 0, feature: str = "chat") -> dict:
    return {"workspace_id": workspace_id, "user_id": user_id, "feature": feature, "created_at": utcnow() - timedelta(days=days_ago)}


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
    assert (total["actions"], total["calls"], total["cost_usd"]) == (1, 1, 0.001)


async def test_usage_is_split_by_day_and_member_within_the_window(client, member):
    workspace_id = member["X-Workspace-Id"]
    await mongo.ai_actions().insert_many([
        action(workspace_id, "u1"), action(workspace_id, "u3", days_ago=1), action(workspace_id, "u1", days_ago=40),
    ])
    await mongo.llm_usage().insert_many([
        row(workspace_id, "u1"),
        row(workspace_id, "u2", cost=None, feature="decision_check"),
        row(workspace_id, "u1", days_ago=2),
        row(workspace_id, "u1", days_ago=40),
    ])

    data = (await client.get("/usage?days=30", headers=member)).json()["data"]

    assert data["total"] == {
        "actions": 2, "calls": 3, "prompt_tokens": 300, "completion_tokens": 30, "cached_tokens": 15, "cost_usd": 0.02,
    }
    # a day with actions but no model calls still gets its own entry
    assert [(day["actions"], day["calls"]) for day in data["by_day"]] == [(1, 2), (1, 0), (0, 1)]
    assert {m["user_id"]: (m["actions"], m["calls"]) for m in data["by_member"]} == {
        "u1": (1, 2), "u2": (0, 1), "u3": (1, 0),
    }


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
    await decision_limits.take_suggestion(workspace_id, "u1")
    await decision_limits.take_check(workspace_id, "u1")
    await decision_limits.take_check(workspace_id, "u1")

    limits = by_kind(await client.get("/usage/limits", headers=member))

    assert (limits["decision_suggestions"]["used"], limits["decision_suggestions"]["limit"]) == (1, 20)
    assert limits["decision_checks"]["used"] == 2
    assert limits["chat_questions"]["used"] == 0
    tomorrow = utcnow().date() + timedelta(days=1)
    assert datetime.fromisoformat(limits["decision_checks"]["resets_at"]) == datetime.combine(
        tomorrow, datetime.min.time(), UTC,
    )


async def test_questions_suggestions_and_sources_checked_each_take_one_action_from_the_month(client, member):
    workspace_id = member["X-Workspace-Id"]
    await chat_limits.take(workspace_id, "u1")
    await decision_limits.take_suggestion(workspace_id, "u1")
    await decision_limits.take_check(workspace_id, "system")
    await decision_limits.take_check(workspace_id, "system")

    limits = by_kind(await client.get("/usage/limits", headers=member))
    total = (await client.get("/usage", headers=member)).json()["data"]["total"]

    assert (limits["ai_actions"]["used"], limits["ai_actions"]["limit"]) == (4, 1000)
    assert datetime.fromisoformat(limits["ai_actions"]["resets_at"]) == next_month(utcnow())
    # the page's history counts the same actions the allowance does
    assert total["actions"] == 4
    assert {(a["user_id"], a["feature"]) async for a in mongo.ai_actions().find({})} == {
        ("u1", "chat"), ("u1", "decision_suggest"), ("system", "decision_check"),
    }


def test_the_month_resets_on_the_first_of_the_next_one_and_december_rolls_over():
    assert next_month(datetime(2026, 11, 15, 9, tzinfo=UTC)) == datetime(2026, 12, 1, tzinfo=UTC)
    assert next_month(datetime(2026, 12, 31, 23, tzinfo=UTC)) == datetime(2027, 1, 1, tzinfo=UTC)


async def test_limits_count_only_this_workspace_and_this_member(client, member):
    other = await user(client, "someone-else")
    await chat_limits.take(other["X-Workspace-Id"], "someone")
    await chat_limits.take(member["X-Workspace-Id"], "another-member")

    limits = by_kind(await client.get("/usage/limits", headers=member))

    assert limits["chat_questions"]["used"] == 1
    assert limits["member_chat_questions"]["used"] == 0
