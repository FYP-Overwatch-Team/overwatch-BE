"""Slack's OAuth callback (the redirect URI registered with the Slack app), and the daily digest's settings.

Connecting goes through the generic connector API (`POST /connections/slack/authorize`);
Slack sends the user back here.
"""

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse

from app.api.deps import workspace_member
from app.connectors.router import oauth_callback
from app.connectors.service import require_connection
from app.core.constants import Provider, Role
from app.core.responses import api_router
from app.slack import digest
from app.slack.schemas import DigestSettings, UpdateDigest

router = api_router(prefix="/slack", tags=["slack"])


@router.get("/callback")
async def slack_callback(
    request: Request, state: str = "", code: str | None = None, error: str | None = None,
) -> RedirectResponse:
    return await oauth_callback(Provider.SLACK, request, state, code, error)


@router.get("/digest")
async def get_digest(member: dict = Depends(workspace_member())) -> DigestSettings:
    return digest.settings_of(await require_connection(member["workspace_id"], Provider.SLACK))


@router.patch("/digest")
async def update_digest(body: UpdateDigest, member: dict = Depends(workspace_member(Role.ADMIN))) -> DigestSettings:
    await require_connection(member["workspace_id"], Provider.SLACK)
    return await digest.update_settings(member["workspace_id"], body.model_dump(exclude_unset=True))
