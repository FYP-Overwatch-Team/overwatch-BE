"""Decisions: recorded with quoted evidence, settled by people, and showing when their evidence moves on."""

from app.db import mongo
from app.decisions.schemas import MAX_EVIDENCE
from tests.integration.test_documents_api import create as create_page
from tests.integration.test_documents_api import text_doc
from tests.integration.test_work_items_api import create as create_item
from tests.integration.test_work_items_api import signed_in
from tests.integration.test_workspaces import user

PRD = "Guests can't export reports.\n\nAdmins export as **CSV** or PDF."


async def page(client, headers: dict, md: str = PRD) -> dict:
    return await create_page(client, headers, title="Export PRD", content=text_doc(md), content_md=md)


async def decide(client, headers: dict, **body) -> dict:
    resp = await client.post("/decisions", json={"title": "Guest export", "statement": "Guests can't export.", **body}, headers=headers)
    assert resp.status_code == 201, resp.json()
    return resp.json()["data"]


def quoting(ref: dict, quote: str, kind: str = "document", **extra) -> dict:
    return {"kind": kind, "ref_id": ref["id"], "quote": quote, **extra}


async def test_a_decision_keeps_its_quoted_evidence_and_starts_its_history(client):
    headers = await signed_in(client)
    prd = await page(client, headers)

    # copied from the rendered page: no Markdown marks, other spacing and case
    made = await decide(client, headers, area="Billing", evidence=[quoting(prd, "admins export as csv   or PDF")])

    [evidence] = made["evidence"]
    assert (made["status"], made["origin"], made["area"]) == ("open", "manual", "Billing")
    assert (evidence["title"], evidence["stance"], evidence["state"]) == ("Export PRD", "supports", "current")
    assert [h["status"] for h in made["history"]] == ["open"]


async def test_a_quote_that_isnt_in_its_source_is_refused(client):
    headers = await signed_in(client)
    prd = await page(client, headers)

    resp = await client.post(
        "/decisions", json={"title": "t", "statement": "s", "evidence": [quoting(prd, "Guests can export reports.")]}, headers=headers,
    )

    assert resp.status_code == 422
    assert resp.json()["meta"]["fields"] == {"evidence.0.quote": "This passage isn't in that item or page."}
    assert await mongo.decisions().count_documents({}) == 0


async def test_evidence_shows_when_its_source_changes_drops_the_quote_or_goes_away(client):
    headers = await signed_in(client)
    edited, rewritten, archived = [await page(client, headers) for _ in range(3)]
    item = await create_item(client, headers, title="Export button", description="Hide export for guests")
    made = await decide(client, headers, evidence=[
        quoting(edited, "Guests can't export reports."), quoting(rewritten, "Guests can't export reports."),
        quoting(archived, "Guests can't export reports."), quoting(item, "hide export for guests", kind="work_item"),
    ])

    await mongo.documents().update_one({"_id": edited["id"]}, {"$set": {"content_md": PRD + "\n\nAlso JSON."}})
    await mongo.documents().update_one({"_id": rewritten["id"]}, {"$set": {"content_md": "Guests can export reports."}})
    await client.post(f"/documents/{archived['id']}/archive", json={}, headers=headers)

    got = (await client.get(f"/decisions/{made['id']}", headers=headers)).json()["data"]
    assert [e["state"] for e in got["evidence"]] == ["changed", "quote_missing", "source_gone", "current"]
    # the snapshot still reads after its source is gone
    assert got["evidence"][2]["quote"] == "Guests can't export reports."


async def test_status_changes_are_recorded_and_superseding_needs_the_replacement(client):
    headers = await signed_in(client)
    old, new = await decide(client, headers), await decide(client, headers, title="Guest export v2")

    accepted = await client.post(f"/decisions/{old['id']}/status", json={"status": "accepted", "note": "agreed in standup"}, headers=headers)
    missing = await client.post(f"/decisions/{old['id']}/status", json={"status": "superseded"}, headers=headers)
    replaced = await client.post(f"/decisions/{old['id']}/status", json={"status": "superseded", "superseded_by": new["id"]}, headers=headers)

    assert accepted.json()["data"]["history"][-1]["note"] == "agreed in standup"
    assert missing.status_code == 422
    data = replaced.json()["data"]
    assert (data["status"], data["superseded_by"]) == ("superseded", new["id"])
    assert [h["status"] for h in data["history"]] == ["open", "accepted", "superseded"]


async def test_the_list_filters_by_status_text_and_quoted_source(client):
    headers = await signed_in(client)
    prd = await page(client, headers)
    quoted = await decide(client, headers, evidence=[quoting(prd, "Guests can't export reports.")])
    await decide(client, headers, title="Dark mode", statement="Ship a dark theme.", status="accepted")

    async def titles(**params) -> list[str]:
        return [d["title"] for d in (await client.get("/decisions", params=params, headers=headers)).json()["data"]]

    assert await titles() == ["Dark mode", "Guest export"]
    assert await titles(status="accepted") == ["Dark mode"]
    assert await titles(q="THEME") == ["Dark mode"]
    assert await titles(ref_id=prd["id"]) == [quoted["title"]]


async def test_evidence_can_be_added_and_removed(client):
    headers = await signed_in(client)
    prd = await page(client, headers)
    made = await decide(client, headers)

    added = await client.post(
        f"/decisions/{made['id']}/evidence", json=quoting(prd, "Guests can't export reports.", stance="contradicts"), headers=headers,
    )
    [evidence] = added.json()["data"]["evidence"]
    removed = await client.delete(f"/decisions/{made['id']}/evidence/{evidence['id']}", headers=headers)
    again = await client.delete(f"/decisions/{made['id']}/evidence/{evidence['id']}", headers=headers)

    assert (added.status_code, evidence["stance"]) == (201, "contradicts")
    assert removed.json()["data"]["evidence"] == []
    assert again.json()["meta"]["error_code"] == "evidence_not_found"


async def test_edits_keep_the_title_and_check_the_owner(client):
    headers = await signed_in(client)
    made = await decide(client, headers)

    edited = await client.patch(
        f"/decisions/{made['id']}", json={"title": None, "area": "Auth", "review_by": "2026-11-30"}, headers=headers,
    )
    stranger = await client.patch(f"/decisions/{made['id']}", json={"owner_id": "nobody"}, headers=headers)

    assert (edited.json()["data"]["title"], edited.json()["data"]["area"]) == ("Guest export", "Auth")
    assert edited.json()["data"]["review_by"] == "2026-11-30"
    assert stranger.status_code == 422


async def test_a_new_decision_cant_start_out_superseded(client):
    headers = await signed_in(client)

    resp = await client.post("/decisions", json={"title": "t", "statement": "s", "status": "superseded"}, headers=headers)

    assert resp.json()["meta"]["fields"] == {"status": "A new decision can't start out replaced."}


async def test_decisions_and_their_sources_stay_inside_the_workspace(client):
    headers = await signed_in(client)
    prd = await page(client, headers)
    made = await decide(client, headers)
    other = await user(client, "someone-else")

    assert (await client.get(f"/decisions/{made['id']}", headers=other)).status_code == 404
    assert (await client.get("/decisions", headers=other)).json()["data"] == []
    # another workspace's page can't be quoted
    resp = await client.post(
        "/decisions", json={"title": "t", "statement": "s", "evidence": [quoting(prd, "Guests can't export reports.")]}, headers=other,
    )
    assert resp.json()["meta"]["error_code"] == "evidence_source_not_found"


async def test_a_decision_holds_a_limited_number_of_passages(client):
    headers = await signed_in(client)
    prd = await page(client, headers)
    full = await decide(client, headers, evidence=[quoting(prd, "Guests can't export reports.")] * MAX_EVIDENCE)

    resp = await client.post(f"/decisions/{full['id']}/evidence", json=quoting(prd, "Guests can't export reports."), headers=headers)

    assert resp.status_code == 422
    assert len((await client.get(f"/decisions/{full['id']}", headers=headers)).json()["data"]["evidence"]) == MAX_EVIDENCE
