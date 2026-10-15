"""What a link shows as a card: its title, description, icon and picture.

Read from the provider's oEmbed where it has one, else from the page itself (Open Graph, then `<title>`).
The server fetches, so viewers' browsers don't each call out to every site, and it only reaches public
addresses: each hop of a redirect is checked before it's followed. Results are cached for a while.
"""

import asyncio
import ipaddress
import json
import socket
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import urldefrag, urlencode, urljoin, urlsplit

import httpx

from app.core.exceptions import AppError
from app.core.time import utcnow
from app.db import mongo

MAX_URL = 2048
MAX_BYTES = 1_000_000
MAX_REDIRECTS = 3
TIMEOUT = httpx.Timeout(5.0)
FRESH_FOR = timedelta(days=7)
# a site that was down or blocked us is tried again sooner
RETRY_AFTER = timedelta(hours=1)
USER_AGENT = "OverwatchLinkPreview/1.0"
# providers whose oEmbed describes a link better than its page does, by host
OEMBED = {
    "loom.com": "https://www.loom.com/v1/oembed",
    "youtube.com": "https://www.youtube.com/oembed",
    "youtu.be": "https://www.youtube.com/oembed",
    "vimeo.com": "https://vimeo.com/api/oembed.json",
    "figma.com": "https://www.figma.com/api/oembed",
}
FIELDS = ("title", "description", "site_name", "icon_url", "image_url")


class LinkNotAllowed(AppError):
    error_code = "link_not_allowed"


def normalise(url: str) -> str:
    url = urldefrag(url.strip())[0]
    parts = urlsplit(url)
    if len(url) > MAX_URL or parts.scheme not in ("http", "https") or not parts.hostname:
        raise LinkNotAllowed("only web links can be previewed")
    if parts.port not in (None, 80, 443):
        raise LinkNotAllowed("only web links can be previewed")
    return url


async def _resolve(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _check_public(url: str) -> None:
    host = urlsplit(url).hostname or ""
    try:
        addresses = await _resolve(host)
    except OSError as exc:
        raise LinkNotAllowed("that address doesn't resolve") from exc
    if not addresses or not all(ipaddress.ip_address(address).is_global for address in addresses):
        raise LinkNotAllowed("only public addresses can be previewed")


async def _get(client: httpx.AsyncClient, url: str) -> tuple[str, bytes, str]:
    """The final address, body (up to MAX_BYTES) and content type, following redirects by hand."""
    for _ in range(MAX_REDIRECTS + 1):
        await _check_public(url)
        async with client.stream("GET", url) as response:
            if response.is_redirect and response.headers.get("location"):
                url = normalise(urljoin(url, response.headers["location"]))
                continue
            response.raise_for_status()
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) >= MAX_BYTES:
                    break
            return url, body[:MAX_BYTES], response.headers.get("content-type", "")
    raise LinkNotAllowed("too many redirects")


class _HeadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.icon: str | None = None
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            if key and values.get("content"):
                self.meta.setdefault(key, values["content"].strip())
        elif tag == "link" and "icon" in values.get("rel", "").lower().split() and values.get("href"):
            self.icon = self.icon or values["href"]
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def _web(base: str, link: str | None) -> str | None:
    if not link:
        return None
    url = urljoin(base, link)
    return url if urlsplit(url).scheme in ("http", "https") else None


def _from_page(url: str, html: str) -> dict:
    parser = _HeadParser()
    parser.feed(html)
    meta = parser.meta
    return {
        "title": meta.get("og:title") or meta.get("twitter:title") or parser.title.strip() or None,
        "description": meta.get("og:description") or meta.get("twitter:description") or meta.get("description"),
        "site_name": meta.get("og:site_name"),
        "icon_url": _web(url, parser.icon or "/favicon.ico"),
        "image_url": _web(url, meta.get("og:image") or meta.get("twitter:image")),
    }


def _oembed_endpoint(url: str) -> str | None:
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    endpoint = OEMBED.get(host) or next((OEMBED[known] for known in OEMBED if host.endswith(f".{known}")), None)
    return endpoint and f"{endpoint}?{urlencode({'url': url, 'format': 'json'})}"


async def _fetch(url: str) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json;q=0.9"}
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers, follow_redirects=False) as client:
        if endpoint := _oembed_endpoint(url):
            try:
                _, body, _ = await _get(client, endpoint)
                data = json.loads(body)
                return {
                    "title": data.get("title"),
                    "description": data.get("description"),
                    "site_name": data.get("provider_name"),
                    "icon_url": _web(url, "/favicon.ico"),
                    "image_url": _web(url, data.get("thumbnail_url")),
                }
            except (httpx.HTTPError, ValueError, LinkNotAllowed):
                pass  # the page itself may still say enough
        final, body, content_type = await _get(client, url)
        if "html" not in content_type:
            return {}
        return _from_page(final, body.decode("utf-8", "replace"))


async def preview(url: str) -> dict:
    url = normalise(url)
    now = utcnow()
    cached = await mongo.link_previews().find_one({"_id": url, "expires_at": {"$gt": now}})
    if cached:
        return {"url": url, **cached["data"]}
    try:
        found = await _fetch(url)
    except (httpx.HTTPError, ValueError):
        found = {}
    data = {field: found.get(field) for field in FIELDS}
    expires = now + (FRESH_FOR if data["title"] else RETRY_AFTER)
    await mongo.link_previews().replace_one({"_id": url}, {"data": data, "expires_at": expires}, upsert=True)
    return {"url": url, **data}
