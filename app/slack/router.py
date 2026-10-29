"""Slack's OAuth callback: the redirect URI registered with the Slack app.

Connecting goes through the generic connector API (`POST /connections/slack/authorize`);
Slack sends the user back here.
"""

from fastapi import Request
from fastapi.responses import RedirectResponse

from app.connectors.router import oauth_callback
from app.core.constants import Provider
from app.core.responses import api_router

router = api_router(prefix="/slack", tags=["slack"])


@router.get("/callback")
async def slack_callback(
    request: Request, state: str = "", code: str | None = None, error: str | None = None,
) -> RedirectResponse:
    return await oauth_callback(Provider.SLACK, request, state, code, error)
