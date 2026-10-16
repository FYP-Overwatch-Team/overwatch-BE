"""Joining the waitlist: stored once per email, one friendly email, nothing revealed about who's already on it."""

from app.core.config import get_settings
from app.db import mongo
from app.waitlist.service import JOINS_PER_EMAIL, JOINS_PER_IP

EMAIL = "ada@example.com"
ENTRY = {"name": "Ada Lovelace", "email": EMAIL, "heard_from": "friend"}


async def join(client, **changes):
    return await client.post("/waitlist", json=ENTRY | changes)


async def test_joining_stores_the_entry_and_sends_one_email(client, outbox):
    resp = await join(client, email="  Ada@Example.com ", name="  Ada Lovelace ")

    assert resp.status_code == 202
    entry = await mongo.waitlist().find_one({"email": EMAIL})
    assert entry["name"] == "Ada Lovelace" and entry["heard_from"] == "friend" and entry["product"] == "overwatch"
    [email] = outbox.to(EMAIL)
    assert email.subject == "You're on the Overwatch waitlist"
    assert "Hi Ada," in email.text and "Hi Ada," in email.html and "hit reply" in email.text


async def test_joining_again_answers_the_same_and_sends_nothing(client, outbox):
    first = await join(client)
    again = await join(client, email="ADA@example.com", name="Someone Else")

    assert again.status_code == first.status_code == 202 and again.json() == first.json()
    assert await mongo.waitlist().count_documents({}) == 1
    assert (await mongo.waitlist().find_one({"email": EMAIL}))["name"] == "Ada Lovelace"
    assert len(outbox.to(EMAIL)) == 1


async def test_too_many_joins_from_one_address_are_refused(client, outbox):
    for n in range(JOINS_PER_IP.count):
        assert (await join(client, email=f"person{n}@example.com")).status_code == 202

    resp = await join(client)

    assert resp.status_code == 429 and resp.json()["meta"]["error_code"] == "rate_limited"
    assert outbox.to(EMAIL) == []


async def test_too_many_joins_for_one_email_are_refused(client, outbox):
    for _ in range(JOINS_PER_EMAIL.count):
        assert (await join(client)).status_code == 202

    resp = await join(client)

    assert resp.status_code == 429 and resp.json()["meta"]["error_code"] == "rate_limited"


async def test_invalid_entries_name_the_field(client, outbox):
    bad_email = await join(client, email="not-an-email")
    bad_source = await join(client, heard_from="carrier pigeon")
    blank_name = await join(client, name="   ")

    for resp in (bad_email, bad_source, blank_name):
        assert resp.status_code == 422 and resp.json()["meta"]["error_code"] == "validation_error"
    assert "email" in bad_email.json()["meta"]["fields"]
    assert "heard_from" in bad_source.json()["meta"]["fields"]
    assert "name" in blank_name.json()["meta"]["fields"]
    assert await mongo.waitlist().count_documents({}) == 0 and outbox.sent == []


async def test_the_name_is_escaped_in_the_email(client, outbox):
    await join(client, name="<script>alert(1)</script> Lovelace")

    html = outbox.to(EMAIL)[0].html
    assert "<script>" not in html and "&lt;script&gt;" in html


async def test_the_email_uses_the_configured_product_name(client, outbox, monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "bearings")
    get_settings.cache_clear()

    await join(client)

    email = outbox.to(EMAIL)[0]
    assert email.subject == "You're on the Bearings waitlist" and "Overwatch" not in email.html
    assert (await mongo.waitlist().find_one({"email": EMAIL}))["product"] == "bearings"


async def test_other_sites_cant_add_people(client, outbox):
    resp = await client.post("/waitlist", json=ENTRY, headers={"Origin": "https://evil.example"})

    assert resp.status_code == 403 and resp.json()["meta"]["error_code"] == "bad_origin"
    assert await mongo.waitlist().count_documents({}) == 0
