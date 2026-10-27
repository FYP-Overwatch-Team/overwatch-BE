"""The product overview: a docs page built from the accepted decisions, by area, updated in place."""

from app.db import mongo
from tests.integration.test_decisions_api import decide, page, quoting
from tests.integration.test_work_items_api import signed_in


async def write(client, headers: dict) -> dict:
    resp = await client.post("/decisions/overview", headers=headers)
    assert resp.status_code == 200, resp.json()
    return (await client.get(f"/documents/{resp.json()['data']['page_id']}", headers=headers)).json()["data"]


async def test_the_overview_lists_accepted_decisions_by_area_with_their_sources(client):
    headers = await signed_in(client)
    prd = await page(client, headers)
    export = await decide(client, headers, status="accepted", area="Billing", evidence=[quoting(prd, "Guests can't export reports.")])
    await decide(client, headers, title="Dark mode", statement="Ship a dark theme.", status="accepted")
    await decide(client, headers, title="Still open", statement="Not decided yet.")

    got = await write(client, headers)

    md = got["content_md"]
    assert got["title"] == "Product overview"
    assert md.index("## Billing") < md.index("## Other")
    assert f"- **[Guest export](/decisions/{export['id']})**: Guests can't export. Sources: [Export PRD](/documents/{prd['id']})." in md
    assert "Dark mode" in md and "Still open" not in md
    assert got["content"]["content"][0]["type"] == "paragraph"


async def test_updates_replace_the_same_page_until_it_is_archived(client):
    headers = await signed_in(client)
    first = await write(client, headers)
    await decide(client, headers, title="Dark mode", statement="Ship a dark theme.", status="accepted")

    second = await write(client, headers)
    await client.post(f"/documents/{first['id']}/archive", json={}, headers=headers)
    third = await write(client, headers)

    assert second["id"] == first["id"]
    assert (first["version"], second["version"]) == (0, 1)
    assert "Dark mode" in second["content_md"]
    assert third["id"] != first["id"]
    assert await mongo.documents().count_documents({"purpose": "product_overview"}) == 2
