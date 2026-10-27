"""Decision checks: accepted decisions read against what changed since, on by default, within a daily cap."""

import json
from datetime import timedelta

import pytest

from app.core.config import get_settings
from app.core.time import utcnow
from app.db import mongo
from app.decisions import checks
from tests.integration.test_decisions_api import PRD, decide, page, quoting
from tests.integration.test_work_items_api import create as create_item
from tests.integration.test_work_items_api import signed_in
from tests.integration.test_workspaces import user

CHANGED = PRD + "\n\nGuests may export a PDF of their own reports."


def reply(*conflicts: tuple[int, str]) -> str:
    return json.dumps({"conflicts": [{"decision": n, "quote": quote, "reason": "They disagree."} for n, quote in conflicts]})


async def accepted(client, headers: dict, ref: dict, quote: str, kind: str = "document") -> dict:
    return await decide(client, headers, status="accepted", evidence=[quoting(ref, quote, kind=kind)])


async def run(client, headers: dict) -> dict:
    resp = await client.post("/decisions/checks/run", headers=headers)
    assert resp.status_code == 200, resp.json()
    return resp.json()["data"]


async def edit(page_id: str, md: str) -> None:
    await mongo.documents().update_one({"_id": page_id}, {"$set": {"content_md": md}})


@pytest.fixture
def check_limit(monkeypatch):
    def set_limit(value: int):
        monkeypatch.setenv("DECISION_CHECK_WORKSPACE_DAILY_LIMIT", str(value))
        get_settings.cache_clear()

    yield set_limit
    get_settings.cache_clear()


async def test_checks_are_on_every_half_hour_by_default_and_can_be_changed(client):
    headers = await signed_in(client)

    before = (await client.get("/decisions/checks", headers=headers)).json()["data"]
    daily = (await client.patch("/decisions/checks", json={"interval_minutes": 1440}, headers=headers)).json()["data"]
    off = (await client.patch("/decisions/checks", json={"enabled": False}, headers=headers)).json()["data"]
    odd = await client.patch("/decisions/checks", json={"interval_minutes": 45}, headers=headers)

    assert (before["enabled"], before["interval_minutes"], before["last_run_at"]) == (True, 30, None)
    assert (daily["enabled"], daily["interval_minutes"]) == (True, 1440)
    assert (off["enabled"], off["interval_minutes"]) == (False, 1440)
    assert odd.status_code == 422


async def test_a_source_unchanged_since_it_was_quoted_isnt_read(client, fake_llm):
    headers = await signed_in(client)
    prd = await page(client, headers)
    await accepted(client, headers, prd, "Guests can't export reports.")

    assert await run(client, headers) == {"checked": 0, "conflicts": 0, "capped": False}
    assert fake_llm.calls == []


async def test_a_changed_source_is_read_once_and_a_contradiction_is_added_as_evidence(client, fake_llm):
    headers = await signed_in(client)
    prd = await page(client, headers)
    made = await accepted(client, headers, prd, "Guests can't export reports.")
    await edit(prd["id"], CHANGED)
    fake_llm.stream_response = reply((1, "Guests may export a PDF of their own reports."), (1, "Guests can export anything."), (5, "x"))

    first = await run(client, headers)
    again = await run(client, headers)

    assert (first, again["checked"]) == ({"checked": 1, "conflicts": 1, "capped": False}, 0)
    assert len(fake_llm.calls) == 1
    assert "[1] Guest export: Guests can't export." in fake_llm.calls[0]["prompt"]
    evidence = (await client.get(f"/decisions/{made['id']}", headers=headers)).json()["data"]["evidence"]
    assert [(e["stance"], e["note"]) for e in evidence] == [("supports", None), ("contradicts", "They disagree.")]
    settings = (await client.get("/decisions/checks", headers=headers)).json()["data"]
    # the last run is the one shown
    assert settings["last_result"] == again


async def test_items_linked_to_a_quoted_item_are_read_too(client, fake_llm):
    headers = await signed_in(client)
    quoted = await create_item(client, headers, title="Export", description="Guests can't export reports.")
    linked = await create_item(client, headers, title="PDF export", description="Let guests download a PDF.")
    await create_item(client, headers, title="Unrelated", description="Dark mode.")
    await mongo.work_items().update_one({"_id": quoted["id"]}, {"$set": {"links": [{"key": linked["key"]}]}})
    await accepted(client, headers, quoted, "Guests can't export reports.", kind="work_item")
    fake_llm.stream_response = reply()

    result = await run(client, headers)

    assert result["checked"] == 1
    assert "Let guests download a PDF." in fake_llm.calls[0]["prompt"]


async def test_only_accepted_decisions_are_checked(client, fake_llm):
    headers = await signed_in(client)
    prd = await page(client, headers)
    await decide(client, headers, evidence=[quoting(prd, "Guests can't export reports.")])
    await edit(prd["id"], CHANGED)

    assert (await run(client, headers))["checked"] == 0
    assert fake_llm.calls == []


async def test_checks_stop_at_the_daily_cap_and_pick_up_the_rest_later(client, fake_llm, check_limit):
    check_limit(1)
    headers = await signed_in(client)
    first, second = await page(client, headers), await page(client, headers)
    await accepted(client, headers, first, "Guests can't export reports.")
    await accepted(client, headers, second, "Guests can't export reports.")
    await edit(first["id"], CHANGED)
    await edit(second["id"], CHANGED)
    fake_llm.stream_response = reply()

    assert await run(client, headers) == {"checked": 1, "conflicts": 0, "capped": True}


async def test_the_background_pass_checks_workspaces_that_are_due_and_on(client, fake_llm):
    due, off, daily = await signed_in(client), await user(client, "someone-else"), await user(client, "third")
    for headers in (due, off, daily):
        prd = await page(client, headers)
        await accepted(client, headers, prd, "Guests can't export reports.")
        await edit(prd["id"], CHANGED)
    await client.patch("/decisions/checks", json={"enabled": False}, headers=off)
    await client.patch("/decisions/checks", json={"interval_minutes": 1440}, headers=daily)
    # checked an hour ago: due again on a half-hour interval, not on a daily one
    hour_ago = utcnow() - timedelta(hours=1)
    for headers in (due, daily):
        await mongo.decision_check_settings().update_one(
            {"_id": headers["X-Workspace-Id"]}, {"$set": {"last_run_at": hour_ago}}, upsert=True,
        )
    fake_llm.stream_response = reply()

    await checks.run_due()

    assert len(fake_llm.calls) == 1
    recorded = await mongo.llm_usage().find_one({})
    assert (recorded["workspace_id"], recorded["user_id"], recorded["feature"]) == (due["X-Workspace-Id"], "system", "decision_check")
