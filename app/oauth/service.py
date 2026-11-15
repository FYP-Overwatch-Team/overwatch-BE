"""The OAuth 2.1 authorization server AI tools sign in through (claude.ai, ChatGPT, Gemini, coding agents).

A tool sends the person to `/oauth/authorize`; they choose a workspace on our consent page; the
tool trades the code it gets back for tokens of a grant tied to that person and workspace
(`mcp.grants`). Only public clients with PKCE (S256): no client holds a secret. A client is
either registered by the tool itself, or described by a document at the client id's URL, which
is only fetched from hosts we name.
"""

import base64
import hashlib
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlencode, urlparse

import httpx

from app.core.config import get_settings
from app.core.constants import McpScope, OAuthRegistration
from app.core.rate_limit import Limit, allow
from app.core.security import hash_token
from app.core.time import utcnow
from app.db import mongo
from app.mcp import grants
from app.workspaces.service import require_member

#: how long the person has to approve, and the tool to trade its code
REQUEST_TTL = timedelta(minutes=10)
CODE_TTL = timedelta(minutes=2)
#: a client metadata document is read again after this long, and never past this size
DOCUMENT_TTL = timedelta(days=1)
DOCUMENT_MAX_BYTES = 16 * 1024
DOCUMENT_TIMEOUT_SECONDS = 5
LOOPBACK = {"localhost", "127.0.0.1", "[::1]"}
NAME_MAX = 80
MAX_REDIRECT_URIS = 10


class OAuthError(Exception):
    """An error in OAuth's own shape (RFC 6749 section 5.2): a code from the spec and what went wrong."""

    def __init__(self, code: str, description: str, status: int = 400):
        super().__init__(description)
        self.code, self.description, self.status = code, description, status


def issuer() -> str:
    return get_settings().mcp_url.removesuffix("/mcp")


def metadata() -> dict:
    """What a tool reads to find its way (RFC 8414)."""
    base = issuer()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": list(McpScope),
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    }


def _loopback(uri: str) -> bool:
    parsed = urlparse(uri)
    return parsed.scheme == "http" and parsed.hostname in LOOPBACK | {"::1"}


def _acceptable(uri: object) -> bool:
    """A redirect address a public client may use: https, or the tool's own machine."""
    if not isinstance(uri, str) or len(uri) > 500 or "#" in uri:
        return False
    parsed = urlparse(uri)
    return bool(parsed.hostname) and (parsed.scheme == "https" or _loopback(uri))


def _registered(client: dict, uri: str) -> bool:
    """Exactly a registered address; a tool on the person's machine may use any port (RFC 8252 section 7.3)."""
    if uri in client["redirect_uris"]:
        return True
    if not _loopback(uri):
        return False
    wanted = urlparse(uri)
    return any(
        _loopback(known) and (urlparse(known).hostname, urlparse(known).path) == (wanted.hostname, wanted.path)
        for known in client["redirect_uris"]
    )


def _client_fields(document: dict) -> dict:
    uris = document.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > MAX_REDIRECT_URIS or not all(_acceptable(uri) for uri in uris):
        raise OAuthError("invalid_redirect_uri", "redirect_uris must be https addresses, or http ones on localhost")
    name = document.get("client_name")
    return {"name": (name if isinstance(name, str) and name.strip() else "An AI tool").strip()[:NAME_MAX], "redirect_uris": uris}


async def register(document: dict, ip: str) -> dict:
    """A tool registers itself (RFC 7591); it gets a client id and no secret."""
    per_ip = Limit("mcp_registrations", get_settings().mcp_registrations_per_ip_daily, timedelta(days=1))
    if not await allow(per_ip, ip):
        raise OAuthError("temporarily_unavailable", "too many registrations from this address today", 429)
    client = {
        "_id": uuid.uuid4().hex, **_client_fields(document), "registration": OAuthRegistration.DYNAMIC,
        "created_at": utcnow(), "fetched_at": None,
    }
    await mongo.oauth_clients().insert_one(client)
    return {
        "client_id": client["_id"], "client_name": client["name"], "redirect_uris": client["redirect_uris"],
        "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    }


async def _document_client(url: str) -> dict:
    """The client a metadata document describes; fetched from named hosts only, and kept for a day."""
    parsed = urlparse(url)
    hosts = {host.strip() for host in get_settings().mcp_client_document_hosts.split(",") if host.strip()}
    if parsed.scheme != "https" or parsed.hostname not in hosts:
        raise OAuthError("invalid_client", "this client isn't known here; register it at the registration endpoint")
    known = await mongo.oauth_clients().find_one({"_id": url})
    if known and known["fetched_at"] > utcnow() - DOCUMENT_TTL:
        return known
    try:
        async with httpx.AsyncClient(timeout=DOCUMENT_TIMEOUT_SECONDS, follow_redirects=False) as http:
            response = await http.get(url, headers={"Accept": "application/json"})
        if response.status_code != 200 or len(response.content) > DOCUMENT_MAX_BYTES:
            raise ValueError("unusable response")
        document = response.json()
        if not isinstance(document, dict) or document.get("client_id") != url:
            raise ValueError("the document names another client")
    except (httpx.HTTPError, ValueError):
        if known:
            return known
        raise OAuthError("invalid_client", "the client's metadata document couldn't be read")
    client = {**_client_fields(document), "registration": OAuthRegistration.METADATA_DOCUMENT, "fetched_at": utcnow()}
    await mongo.oauth_clients().update_one({"_id": url}, {"$set": client, "$setOnInsert": {"created_at": utcnow()}}, upsert=True)
    return {"_id": url, **client}


async def client_of(client_id: str) -> dict:
    if client_id.startswith("https://"):
        return await _document_client(client_id)
    client = await mongo.oauth_clients().find_one({"_id": client_id, "registration": OAuthRegistration.DYNAMIC})
    if client is None:
        raise OAuthError("invalid_client", "unknown client")
    return client


def _back(redirect_uri: str, **params: str | None) -> str:
    """The tool's redirect address with our answer; `iss` says who answered (RFC 9207)."""
    query = urlencode({name: value for name, value in {**params, "iss": issuer()}.items() if value is not None})
    return f"{redirect_uri}{'&' if '?' in redirect_uri else '?'}{query}"


async def start(params: dict[str, str]) -> str:
    """Check an authorization request and keep it; returns where to send the person: our consent page, or
    back to the tool with an error. Raises when the client or its redirect address can't be trusted, as then
    there is nowhere safe to send them."""
    client = await client_of(params.get("client_id", ""))
    redirect_uri = params.get("redirect_uri", "")
    if not _registered(client, redirect_uri):
        raise OAuthError("invalid_request", "redirect_uri isn't one this client registered")
    state = params.get("state")
    problem = None
    if params.get("response_type") != "code":
        problem = ("unsupported_response_type", "only the authorization code flow is supported")
    elif not params.get("code_challenge") or params.get("code_challenge_method") != "S256":
        problem = ("invalid_request", "PKCE with code_challenge_method=S256 is required")
    elif params.get("resource") and params["resource"].rstrip("/") != get_settings().mcp_url:
        problem = ("invalid_target", "tokens are only issued for this MCP server")
    if problem:
        return _back(redirect_uri, error=problem[0], error_description=problem[1], state=state)
    request = {
        "_id": uuid.uuid4().hex, "client_id": client["_id"], "client_name": client["name"], "redirect_uri": redirect_uri,
        "state": state, "code_challenge": params["code_challenge"], "code_hash": None, "expires_at": utcnow() + REQUEST_TTL,
    }
    await mongo.oauth_requests().insert_one(request)
    return f"{get_settings().frontend_origin}/connect/authorize?{urlencode({'request': request['_id']})}"


async def _waiting(request_id: str) -> dict:
    request = await mongo.oauth_requests().find_one({"_id": request_id, "code_hash": None, "expires_at": {"$gt": utcnow()}})
    if request is None:
        raise OAuthError("invalid_request", "This sign-in request has expired. Go back to your AI tool and connect again.", 404)
    return request


async def pending(request_id: str) -> dict:
    """What the consent page shows: which tool asks, and where the person is sent back to."""
    request = await _waiting(request_id)
    return {
        "id": request["_id"], "client_name": request["client_name"],
        "redirect_host": urlparse(request["redirect_uri"]).netloc, "on_this_device": _loopback(request["redirect_uri"]),
    }


async def approve(user_id: str, request_id: str, workspace_id: str, *, read_only: bool, ask_first: bool) -> str:
    """The person agreed, for one workspace they're a member of; returns the tool's address with a one-time code."""
    await require_member(workspace_id, user_id)
    code = secrets.token_urlsafe(32)
    request = await mongo.oauth_requests().find_one_and_update(
        {"_id": request_id, "code_hash": None, "expires_at": {"$gt": utcnow()}},
        {"$set": {
            "code_hash": hash_token(code), "user_id": user_id, "workspace_id": workspace_id, "read_only": read_only,
            "ask_first": ask_first, "expires_at": utcnow() + CODE_TTL,
        }},
    )
    if request is None:
        await _waiting(request_id)  # raises: expired, or already answered
    return _back(request["redirect_uri"], code=code, state=request["state"])


async def deny(request_id: str) -> str:
    request = await mongo.oauth_requests().find_one_and_delete({"_id": request_id, "code_hash": None})
    if request is None:
        await _waiting(request_id)
    return _back(request["redirect_uri"], error="access_denied", error_description="the person declined", state=request["state"])


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


async def tokens(form: dict[str, str]) -> dict:
    """The token endpoint: trade a code for a grant's first tokens, or a refresh token for the next ones."""
    client_id = form.get("client_id", "")
    if form.get("grant_type") == "refresh_token":
        issued = await grants.refresh(form.get("refresh_token", ""), client_id)
        if issued is None:
            raise OAuthError("invalid_grant", "the refresh token is no longer valid; sign in again")
        return issued
    if form.get("grant_type") != "authorization_code":
        raise OAuthError("unsupported_grant_type", "use authorization_code or refresh_token")
    # a code works once: it's gone whether or not the rest checks out
    request = await mongo.oauth_requests().find_one_and_delete(
        {"code_hash": hash_token(form.get("code", "")), "expires_at": {"$gt": utcnow()}},
    )
    if request is None or request["client_id"] != client_id or request["redirect_uri"] != form.get("redirect_uri"):
        raise OAuthError("invalid_grant", "the code is wrong, used or expired")
    if not secrets.compare_digest(_challenge(form.get("code_verifier", "")), request["code_challenge"]):
        raise OAuthError("invalid_grant", "code_verifier doesn't match the code_challenge")
    if form.get("resource") and form["resource"].rstrip("/") != get_settings().mcp_url:
        raise OAuthError("invalid_target", "tokens are only issued for this MCP server")
    member = {"user_id": request["user_id"], "workspace_id": request["workspace_id"]}
    client = {"_id": request["client_id"], "name": request["client_name"]}
    grant = await grants.create_signed_in(member, client, read_only=request["read_only"], ask_first=request["ask_first"])
    return await grants.issue(grant)
