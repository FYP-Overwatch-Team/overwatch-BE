"""Signing an AI tool in over OAuth: the tool starts it, the person picks a workspace, the tool gets tokens for it."""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import respx

from app.db import mongo
from app.mcp import grants
from tests.integration.test_mcp_server import rpc, running  # noqa: F401  (the fixture runs the MCP server)
from tests.integration.test_workspaces import bearer, join, team, user

VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
CALLBACK = "https://tool.example/callback"
ORIGIN = {"Origin": "http://localhost:3000"}


async def registered(client, redirect_uris: list[str] | None = None) -> str:
    resp = await client.post("/oauth/register", json={"client_name": "Claude", "redirect_uris": redirect_uris or [CALLBACK]})
    assert resp.status_code == 201, resp.json()
    return resp.json()["client_id"]


async def started(client, client_id: str, **changes) -> httpx.Response:
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "s1",
        "code_challenge": CHALLENGE, "code_challenge_method": "S256", "resource": "http://localhost:8000/mcp", **changes,
    }
    return await client.get("/oauth/authorize", params={name: value for name, value in params.items() if value is not None})


def query(response_or_url) -> dict:
    url = response_or_url if isinstance(response_or_url, str) else response_or_url.headers["location"]
    return {name: values[0] for name, values in parse_qs(urlparse(url).query).items()}


async def approved(client, headers: dict, workspace_id: str, client_id: str, **body) -> str:
    """The code the tool is sent back with once the person approves."""
    request_id = query(await started(client, client_id))["request"]
    resp = await client.post(f"/oauth/requests/{request_id}/approve", json={"workspace_id": workspace_id, **body}, headers={**bearer(headers), **ORIGIN})
    assert resp.status_code == 200, resp.json()
    return query(resp.json()["data"]["redirect_to"])["code"]


async def traded(client, client_id: str, code: str, **changes) -> httpx.Response:
    form = {"grant_type": "authorization_code", "client_id": client_id, "code": code, "redirect_uri": CALLBACK, "code_verifier": VERIFIER, **changes}
    return await client.post("/oauth/token", data=form)


async def test_a_tool_finds_how_to_sign_in_from_the_servers_metadata(client):
    described = (await client.get("/.well-known/oauth-authorization-server")).json()

    assert described["issuer"] == "http://localhost:8000"
    assert described["authorization_endpoint"] == "http://localhost:8000/oauth/authorize"
    assert described["code_challenge_methods_supported"] == ["S256"]
    assert described["client_id_metadata_document_supported"] is True
    resource = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert resource["authorization_servers"] == [described["issuer"]]


async def test_a_person_signs_a_tool_in_to_one_workspace_and_it_can_call_the_server(client):
    alice = await user(client, "alice")
    acme = await team(client, alice)
    client_id = await registered(client)

    sent = await started(client, client_id)
    request_id = query(sent)["request"]
    shown = (await client.get(f"/oauth/requests/{request_id}", headers=bearer(alice))).json()["data"]
    answer = await client.post(f"/oauth/requests/{request_id}/approve", json={"workspace_id": acme}, headers={**bearer(alice), **ORIGIN})
    back = query(answer.json()["data"]["redirect_to"])
    issued = (await traded(client, client_id, back["code"])).json()

    assert sent.status_code == 302 and sent.headers["location"].startswith("http://localhost:3000/connect/authorize?request=")
    assert (shown["client_name"], shown["redirect_host"], shown["on_this_device"]) == ("Claude", "tool.example", False)
    assert (back["state"], back["iss"]) == ("s1", "http://localhost:8000")
    assert (issued["token_type"], issued["expires_in"], issued["scope"]) == ("Bearer", 3600, "decisions:read decisions:record")
    assert (await rpc(client, issued["access_token"], "tools/list")).status_code == 200
    # the refresh token only gets the next tokens: it doesn't call the server
    assert (await rpc(client, issued["refresh_token"], "tools/list")).status_code == 401
    [tool] = (await client.get("/ai-tools", headers=bearer(alice))).json()["data"]
    assert (tool["kind"], tool["label"], tool["workspace_id"], tool["expires_at"]) == ("oauth", "Claude", acme, None)


async def test_the_person_chooses_what_the_tool_may_do(client):
    alice = await user(client, "alice")
    acme = await team(client, alice)
    client_id = await registered(client)

    code = await approved(client, alice, acme, client_id, read_only=True, ask_first=False)
    issued = (await traded(client, client_id, code)).json()

    grant = await grants.authenticate(issued["access_token"])
    assert (issued["scope"], grant["ask_first"]) == ("decisions:read", False)


async def test_a_code_works_once_and_only_with_the_verifier_it_was_asked_with(client):
    alice = await user(client, "alice")
    acme = await team(client, alice)
    client_id = await registered(client)

    wrong = await traded(client, client_id, await approved(client, alice, acme, client_id), code_verifier="w" * 64)
    code = await approved(client, alice, acme, client_id)
    first, second = await traded(client, client_id, code), await traded(client, client_id, code)

    assert (wrong.status_code, wrong.json()["error"]) == (400, "invalid_grant")
    assert (first.status_code, second.status_code, second.json()["error"]) == (200, 400, "invalid_grant")
    assert await mongo.mcp_grants().count_documents({}) == 1


async def test_a_code_is_refused_to_another_client_or_redirect_address(client):
    alice = await user(client, "alice")
    acme = await team(client, alice)
    client_id, other = await registered(client), await registered(client)

    by_other = await traded(client, other, await approved(client, alice, acme, client_id))
    elsewhere = await traded(client, client_id, await approved(client, alice, acme, client_id), redirect_uri="https://evil.example/cb")

    assert [by_other.json()["error"], elsewhere.json()["error"]] == ["invalid_grant"] * 2


async def test_a_request_to_an_unregistered_address_never_leaves_the_server(client):
    client_id = await registered(client)

    elsewhere = await started(client, client_id, redirect_uri="https://evil.example/cb")
    unknown = await started(client, "nope")

    assert (elsewhere.status_code, unknown.status_code) == (400, 400)
    assert "location" not in elsewhere.headers and "location" not in unknown.headers


async def test_a_bad_request_from_a_known_client_is_sent_back_with_the_error(client):
    client_id = await registered(client)

    plain = await started(client, client_id, code_challenge_method="plain")
    no_pkce = await started(client, client_id, code_challenge=None)
    other_resource = await started(client, client_id, resource="https://another.example/mcp")

    assert [query(r)["error"] for r in (plain, no_pkce, other_resource)] == ["invalid_request", "invalid_request", "invalid_target"]
    assert all(r.headers["location"].startswith(CALLBACK) and query(r)["state"] == "s1" for r in (plain, no_pkce, other_resource))


async def test_a_tool_on_the_persons_machine_may_come_back_on_any_port(client):
    client_id = await registered(client, ["http://localhost:1234/callback"])

    same_path = await started(client, client_id, redirect_uri="http://localhost:53999/callback")
    other_path = await started(client, client_id, redirect_uri="http://localhost:53999/elsewhere")

    assert same_path.status_code == 302 and "request" in query(same_path)
    assert other_path.status_code == 400


async def test_only_https_or_local_redirect_addresses_can_be_registered(client):
    refused = await client.post("/oauth/register", json={"client_name": "X", "redirect_uris": ["http://tool.example/cb"]})
    none = await client.post("/oauth/register", json={"client_name": "X"})

    assert [refused.json()["error"], none.json()["error"]] == ["invalid_redirect_uri"] * 2


async def test_a_person_can_only_sign_a_tool_in_to_a_workspace_they_belong_to(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    request_id = query(await started(client, await registered(client)))["request"]

    outsider = await client.post(f"/oauth/requests/{request_id}/approve", json={"workspace_id": acme}, headers={**bearer(bob), **ORIGIN})
    cross_site = await client.post(f"/oauth/requests/{request_id}/approve", json={"workspace_id": acme}, headers={**bearer(alice), "Origin": "https://evil.example"})
    signed_out = await client.get(f"/oauth/requests/{request_id}")

    assert [outsider.status_code, cross_site.status_code, signed_out.status_code] == [404, 403, 401]


async def test_declining_sends_the_tool_back_without_a_code(client):
    alice = await user(client, "alice")
    request_id = query(await started(client, await registered(client)))["request"]

    declined = await client.post(f"/oauth/requests/{request_id}/deny", headers={**bearer(alice), **ORIGIN})
    again = await client.get(f"/oauth/requests/{request_id}", headers=bearer(alice))

    back = query(declined.json()["data"]["redirect_to"])
    assert (back["error"], back["state"], "code" in back) == ("access_denied", "s1", False)
    assert again.status_code == 404


async def test_a_refresh_token_works_once_and_a_copy_of_it_ends_the_connection(client):
    alice = await user(client, "alice")
    acme = await team(client, alice)
    client_id = await registered(client)
    issued = (await traded(client, client_id, await approved(client, alice, acme, client_id))).json()
    form = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": issued["refresh_token"]}

    renewed = await client.post("/oauth/token", data=form)
    assert renewed.status_code == 200 and renewed.json()["refresh_token"] != issued["refresh_token"]
    assert (await rpc(client, renewed.json()["access_token"], "tools/list")).status_code == 200

    copied = await client.post("/oauth/token", data=form)

    assert (copied.status_code, copied.json()["error"]) == (400, "invalid_grant")
    assert (await rpc(client, renewed.json()["access_token"], "tools/list")).status_code == 401
    assert (await client.get("/ai-tools", headers=bearer(alice))).json()["data"] == []


async def test_a_refresh_token_is_refused_to_another_client(client):
    alice = await user(client, "alice")
    acme = await team(client, alice)
    client_id, other = await registered(client), await registered(client)
    issued = (await traded(client, client_id, await approved(client, alice, acme, client_id))).json()

    stolen = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": other, "refresh_token": issued["refresh_token"]})

    assert stolen.json()["error"] == "invalid_grant"


async def test_a_signed_in_tool_stops_working_when_its_member_leaves(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    await join(client, alice, bob, acme)
    client_id = await registered(client)
    issued = (await traded(client, client_id, await approved(client, bob, acme, client_id))).json()

    await client.delete(f"/workspaces/{acme}/members/id-bob", headers=bearer(alice))

    assert (await rpc(client, issued["access_token"], "tools/list")).status_code == 401
    renewed = await client.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": issued["refresh_token"]})
    assert renewed.json()["error"] == "invalid_grant"


@respx.mock
async def test_a_client_named_by_a_url_is_read_from_its_document_on_a_trusted_host_only(client):
    trusted, untrusted = "https://claude.ai/oauth/mcp-client.json", "https://evil.example/client.json"
    document = respx.get(trusted).mock(return_value=httpx.Response(200, json={"client_id": trusted, "client_name": "Claude", "redirect_uris": [CALLBACK]}))
    elsewhere = respx.get(untrusted).mock(return_value=httpx.Response(200, json={"client_id": untrusted, "redirect_uris": [CALLBACK]}))

    known = await started(client, trusted)
    again = await started(client, trusted)
    refused = await started(client, untrusted)

    assert known.status_code == again.status_code == 302 and "request" in query(known)
    assert refused.status_code == 400
    # read once and kept; the untrusted address is never fetched
    assert (document.call_count, elsewhere.call_count) == (1, 0)


@respx.mock
async def test_a_document_that_names_another_client_is_refused(client):
    url = "https://claude.ai/oauth/mcp-client.json"
    respx.get(url).mock(return_value=httpx.Response(200, json={"client_id": "https://claude.ai/other.json", "redirect_uris": [CALLBACK]}))

    assert (await started(client, url)).status_code == 400
