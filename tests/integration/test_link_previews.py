"""Link previews: a page's own description, a provider's oEmbed, only public addresses, cached."""

import pytest
import respx
from httpx import Response

from app.link_previews import service
from tests.integration.test_auth_flow import auth_headers, login

PAGE = """<html><head><title>Fallback</title>
<meta property="og:title" content="Release notes"><meta name="description" content="What shipped">
<meta property="og:image" content="/cover.png"><link rel="shortcut icon" href="/fav.png"></head></html>"""
ADDRESSES = {"docs.example.com": ["93.184.216.34"], "www.loom.com": ["104.18.1.1"], "intranet.example.com": ["10.0.0.5"]}


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    async def resolve(host: str) -> list[str]:
        return ADDRESSES.get(host, ["127.0.0.1"])

    monkeypatch.setattr(service, "_resolve", resolve)


async def get(client, headers: dict, url: str):
    return await client.get("/link-previews", params={"url": url}, headers=headers)


async def test_a_page_previews_from_its_open_graph_and_is_cached(client):
    headers = auth_headers(await login(client))
    url = "https://docs.example.com/notes"

    with respx.mock as router:
        page = router.get(url).mock(return_value=Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"}))
        first = await get(client, headers, url + "#top")
        second = await get(client, headers, url)

    assert first.json()["data"] == {
        "url": url, "title": "Release notes", "description": "What shipped", "site_name": None,
        "icon_url": "https://docs.example.com/fav.png", "image_url": "https://docs.example.com/cover.png",
    }
    assert second.json()["data"] == first.json()["data"]
    assert page.call_count == 1


async def test_a_provider_with_oembed_is_read_from_it(client):
    headers = auth_headers(await login(client))
    url = "https://www.loom.com/share/abc"

    with respx.mock as router:
        router.get("https://www.loom.com/v1/oembed").mock(return_value=Response(200, json={
            "title": "How to interact with a video", "provider_name": "Loom", "thumbnail_url": "https://cdn.loom.com/abc.gif",
        }))
        found = (await get(client, headers, url)).json()["data"]

    assert (found["title"], found["site_name"], found["image_url"]) == ("How to interact with a video", "Loom", "https://cdn.loom.com/abc.gif")


async def test_private_addresses_are_refused_even_through_a_redirect(client):
    headers = auth_headers(await login(client))

    with respx.mock as router:
        router.get("https://docs.example.com/go").mock(return_value=Response(302, headers={"location": "https://intranet.example.com/admin"}))
        redirected = await get(client, headers, "https://docs.example.com/go")
        direct = await get(client, headers, "http://localhost/secret")
        odd_port = await get(client, headers, "https://docs.example.com:8443/x")
        not_web = await get(client, headers, "file:///etc/passwd")

    assert [r.json()["meta"]["error_code"] for r in (redirected, direct, odd_port, not_web)] == ["link_not_allowed"] * 4


async def test_a_site_that_fails_gives_an_empty_preview_and_previews_need_a_workspace(client):
    headers = auth_headers(await login(client))

    with respx.mock as router:
        router.get("https://docs.example.com/down").mock(return_value=Response(503))
        down = await get(client, headers, "https://docs.example.com/down")
    anonymous = await client.get("/link-previews", params={"url": "https://docs.example.com/down"})

    assert down.json()["data"]["title"] is None
    assert anonymous.status_code == 401
