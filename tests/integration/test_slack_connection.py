"""Connecting Slack and choosing the channels it listens to."""

import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import Response

from app.core.config import get_settings
from app.db import mongo, oauth_tokens
from app.workers import jobs
from tests.integration.test_auth_flow import auth_headers, login

TOKEN_URL = "https://slack.com/api/oauth.v2.access"
AUTH_TEST_URL = "https://slack.com/api/auth.test"
CHANNELS_URL = "https://slack.com/api/conversations.list"


@pytest.fixture
def slack_app(monkeypatch):
    monkeypatch.setenv("SLACK_CLIENT_ID", "test-slack-client-id")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "test-slack-client-secret")
    get_settings.cache_clear()


def mock_slack_install(router: respx.Router) -> None:
    # Slack returns the bot token with comma-separated scopes, and no expiry
    router.post(TOKEN_URL).mock(return_value=Response(200, json={
        "ok": True, "access_token": "xoxb-test", "token_type": "bot", "scope": "channels:read,groups:read",
        "bot_user_id": "U0BOT", "app_id": "A0APP", "team": {"id": "T0ACME", "name": "Acme"},
    }))
    router.post(AUTH_TEST_URL).mock(return_value=Response(200, json={
        "ok": True, "team_id": "T0ACME", "team": "Acme", "url": "https://acme.slack.com/",
    }))


async def authorize(client, headers: dict) -> str:
    """The Slack authorize URL the flow starts at."""
    start = await client.post("/connections/slack/authorize", json={"return_to": "settings"}, headers=headers)
    return start.json()["data"]["authorization_url"]


async def connect(client, headers: dict) -> Response:
    state = parse_qs(urlparse(await authorize(client, headers)).query)["state"][0]
    with respx.mock as router:
        mock_slack_install(router)
        return await client.get(f"/slack/callback?code=fake&state={state}")


async def slack(client, headers: dict) -> dict | None:
    catalog = (await client.get("/connections", headers=headers)).json()["data"]
    return next((c for c in catalog if c["provider"] == "slack"), None)


async def test_slack_is_not_offered_until_the_server_has_its_app(client, monkeypatch):
    # blank, not unset: a developer's .env may hold real Slack credentials
    monkeypatch.setenv("SLACK_CLIENT_ID", "")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "")
    get_settings.cache_clear()
    headers = auth_headers(await login(client))

    started = await client.post("/connections/slack/authorize", json={"return_to": "settings"}, headers=headers)

    assert await slack(client, headers) is None
    assert started.status_code == 409
    assert started.json()["meta"]["error_code"] == "connector_not_configured"


async def test_connecting_stores_the_slack_workspace_and_its_scopes(client, slack_app):
    headers = auth_headers(await login(client))
    workspace_id = headers["X-Workspace-Id"]

    url = urlparse(await authorize(client, headers))
    params = parse_qs(url.query)
    callback = await connect(client, headers)

    assert (url.netloc, url.path) == ("slack.com", "/oauth/v2/authorize")
    assert params["scope"] == ["channels:read,groups:read,channels:history,groups:history,chat:write,users:read,users:read.email"]
    assert params["redirect_uri"][0].endswith("/slack/callback")
    assert callback.headers["location"].endswith(f"/settings/integrations?slack=connected&w={workspace_id}")
    state = await slack(client, headers)
    assert (state["category"], state["status"], state["can_write"]) == ("chat", "active", False)
    assert state["external_account"] == {"id": "T0ACME", "name": "Acme", "url": "https://acme.slack.com/"}
    conn = await mongo.connections().find_one({"workspace_id": workspace_id, "provider": "slack"})
    assert conn["scopes"] == ["channels:read", "groups:read"]
    token = await oauth_tokens.get(workspace_id, "slack")
    assert (token["status"], token["expires_at"]) == ("active", None)


async def test_the_token_request_uses_basic_auth_and_a_form_body(client, slack_app):
    headers = auth_headers(await login(client))
    state = parse_qs(urlparse(await authorize(client, headers)).query)["state"][0]

    with respx.mock as router:
        mock_slack_install(router)
        await client.get(f"/slack/callback?code=fake&state={state}")
        exchange = router.routes[0].calls[0].request

    credentials = base64.b64encode(b"test-slack-client-id:test-slack-client-secret").decode()
    form = parse_qs(exchange.content.decode())
    assert exchange.headers["Authorization"] == f"Basic {credentials}"
    assert exchange.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert (form["code"], form["redirect_uri"][0].endswith("/slack/callback")) == (["fake"], True)
    assert "client_secret" not in form


async def test_channels_are_listed_across_pages(client, slack_app):
    headers = auth_headers(await login(client))
    await connect(client, headers)

    with respx.mock as router:
        listing = router.get(CHANNELS_URL).mock(side_effect=[
            Response(200, json={
                "ok": True, "channels": [{"id": "C0RANDOM", "name": "random"}],
                "response_metadata": {"next_cursor": "page-2"},
            }),
            Response(200, json={
                "ok": True, "channels": [{"id": "C0GENERAL", "name": "general"}], "response_metadata": {"next_cursor": ""},
            }),
        ])
        available = await client.get("/connections/slack/targets/available", headers=headers)

    assert available.json()["data"] == [
        {"key": "C0GENERAL", "name": "#general", "id": "C0GENERAL"},
        {"key": "C0RANDOM", "name": "#random", "id": "C0RANDOM"},
    ]
    first, second = listing.calls
    assert first.request.headers["Authorization"] == "Bearer xoxb-test"
    assert "cursor" not in first.request.url.params
    assert second.request.url.params["cursor"] == "page-2"


async def test_a_revoked_install_asks_to_reconnect(client, slack_app):
    headers = auth_headers(await login(client))
    await connect(client, headers)

    with respx.mock as router:
        # Slack reports a revoked token with HTTP 200
        router.get(CHANNELS_URL).mock(return_value=Response(200, json={"ok": False, "error": "token_revoked"}))
        available = await client.get("/connections/slack/targets/available", headers=headers)

    assert available.status_code == 401
    assert available.json()["meta"]["error_code"] == "slack_needs_reauth"
    assert (await slack(client, headers))["status"] == "needs_reauth"


async def test_channels_are_listened_to_not_synced(client, slack_app, monkeypatch):
    headers = auth_headers(await login(client))
    await connect(client, headers)
    synced: list[str] = []

    async def record(workspace_id, provider, target_key):
        synced.append(target_key)

    monkeypatch.setattr(jobs.sync, "run_work_item_sync", record)
    monkeypatch.setattr(jobs.doc_sync, "run_document_sync", record)

    added = await client.post("/connections/slack/targets", json={"key": "C0GENERAL", "name": "#general"}, headers=headers)
    sync_now = await client.post("/connections/slack/sync", headers=headers)
    await jobs.run_connector_sync(headers["X-Workspace-Id"], "slack", "C0GENERAL")

    assert added.status_code == 201
    assert [(t["key"], t["name"]) for t in (await slack(client, headers))["targets"]] == [("C0GENERAL", "#general")]
    assert sync_now.json()["meta"]["error_code"] == "nothing_to_sync"
    assert synced == []


def logged(capsys, event: str) -> list[dict]:
    """Log lines with this event; the app logs JSON to stdout, and its loggers are cached, so capture_logs can miss them."""
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("event") == event]


async def test_a_failed_install_logs_why(client, slack_app, capsys):
    headers = auth_headers(await login(client))
    state = parse_qs(urlparse(await authorize(client, headers)).query)["state"][0]

    with respx.mock as router:
        router.post(TOKEN_URL).mock(return_value=Response(200, json={"ok": False, "error": "invalid_code"}))
        failed = await client.get(f"/slack/callback?code=stale&state={state}")
    [failure] = logged(capsys, "oauth_callback_failed")
    client.cookies.clear()
    mismatched = await client.get(f"/slack/callback?code=fake&state={state}")
    [mismatch] = logged(capsys, "oauth_callback_state_mismatch")

    assert failed.headers["location"].endswith("/settings/integrations?slack=error")
    assert (failure["provider"], failure["error_code"]) == ("slack", "external_service_error")
    assert failure["reason"] == "slack token exchange failed: invalid_code"
    assert mismatched.headers["location"].endswith("/settings/integrations?slack=error")
    assert (mismatch["provider"], mismatch["has_cookie"]) == ("slack", False)
