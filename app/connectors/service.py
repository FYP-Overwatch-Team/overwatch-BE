import secrets

import structlog
from pymongo.errors import DuplicateKeyError

from app.connectors import registry, repository
from app.core.constants import AuthBackendName, ConnectionStatus, ConnectorCategory, Role
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.db import oauth_tokens
from app.workspaces.service import require_member

logger = structlog.get_logger("app.connectors")


async def auth_status(conn: dict | None) -> str:
    if conn is None or conn["status"] == ConnectionStatus.DISCONNECTED:
        return ConnectionStatus.DISCONNECTED
    token = await oauth_tokens.get(conn["workspace_id"], registry.get(conn["provider"]).credential)
    return token["status"] if token else ConnectionStatus.DISCONNECTED


def can_write(conn: dict | None) -> bool:
    write_scope = conn and registry.get(conn["provider"]).write_scope
    return bool(write_scope and conn["status"] != ConnectionStatus.DISCONNECTED and write_scope in conn.get("scopes", []))


def can_manage_sprints(conn: dict | None) -> bool:
    """The grant reaches the tracker's boards and sprints (and can write back at all)."""
    sprint_scope = conn and registry.get(conn["provider"]).sprint_scope
    return bool(sprint_scope and can_write(conn) and sprint_scope in conn.get("scopes", []))


def outdated(conn: dict | None) -> bool:
    """Connected with a grant made before the connector asked for all it does now: reconnecting fixes it."""
    if conn is None or conn["status"] == ConnectionStatus.DISCONNECTED:
        return False
    return not set(registry.get(conn["provider"]).scopes) <= set(conn.get("scopes", []))


async def catalog(workspace_id: str) -> list[dict]:
    """Every connector this server offers, with this workspace's state for it."""
    connections = {c["provider"]: c for c in await repository.list_connections(workspace_id)}
    targets = await repository.list_targets(workspace_id)
    result = []
    for provider, connector in registry.CONNECTORS.items():
        if not connector.configured:
            continue
        conn = connections.get(provider)
        status = await auth_status(conn)
        result.append({
            "provider": provider,
            "name": connector.name,
            "category": connector.category,
            "status": status,
            "can_write": can_write(conn),
            "outdated": outdated(conn),
            "external_account": conn and conn.get("external_account") if status != ConnectionStatus.DISCONNECTED else None,
            "connected_by": conn and conn.get("connected_by"),
            "connected_at": conn and conn.get("connected_at"),
            "targets": [t for t in targets if t["provider"] == provider],
        })
    return result


async def start_authorization(member: dict, provider: str, return_to: str) -> tuple[str, str]:
    """(authorization_url, state). The state binds the user, workspace, landing page and product.

    Products sharing a credential share one grant, so it asks for theirs too: connecting
    Confluence must not take Jira's scopes away.
    """
    connector = registry.get(provider)
    if not connector.configured:
        raise ConflictError(f"{provider} isn't set up on this server", error_code="connector_not_configured")
    scopes = list(connector.scopes)
    for sibling in registry.siblings(provider):
        if await repository.active_connection(member["workspace_id"], sibling):
            scopes.extend(registry.get(sibling).scopes)
    state = f"{member['user_id']}.{member['workspace_id']}.{return_to}.{provider}.{secrets.token_urlsafe(24)}"
    return connector.flow.authorize_url(state, tuple(scopes)), state


async def complete_authorization(provider: str, workspace_id: str, user_id: str, code: str) -> None:
    # they may have lost the admin role during the round trip
    await require_member(workspace_id, user_id, Role.ADMIN)
    connector = registry.get(provider)
    siblings = [conn for p in registry.siblings(provider) if (conn := await repository.active_connection(workspace_id, p))]
    tokens = await connector.flow.exchange_code(code)
    known_site = next((conn["external_account"]["id"] for conn in siblings if conn.get("external_account")), None)
    account = await connector.adapter.resolve_account(tokens["access_token"], known_site)
    if connector.exclusive_account:
        await _require_unclaimed(connector, account["id"], workspace_id)
    await oauth_tokens.save(workspace_id, connector.credential, tokens)
    granted = connector.oauth.parse_scopes(tokens.get("scope", "")) or [*connector.scopes, *(s for conn in siblings for s in registry.get(conn["provider"]).scopes)]
    try:
        await repository.save_connection(
            workspace_id, provider,
            auth_backend=AuthBackendName.NATIVE_OAUTH,
            scopes=granted,
            external_account=account,
            connected_by=user_id,
        )
    except DuplicateKeyError as exc:
        # another workspace claimed the same account between the check and the save
        await oauth_tokens.forget(workspace_id, connector.credential)
        raise _claimed(connector) from exc
    # the new grant replaced the one they used
    for conn in siblings:
        await repository.set_scopes(workspace_id, conn["provider"], granted)
    # a reconnect may grant what earlier syncs couldn't read (a board's columns), so the next one reads everything;
    # chat channels aren't synced
    if connector.category != ConnectorCategory.CHAT:
        await request_full_sync(workspace_id, provider)


async def _require_unclaimed(connector, account_id: str, workspace_id: str) -> None:
    taken = await repository.connection_for_account(connector.provider, account_id)
    if taken and taken["workspace_id"] != workspace_id:
        raise _claimed(connector)


def _claimed(connector) -> ConflictError:
    return ConflictError(
        f"that {connector.name} workspace is already connected to another Bearings workspace",
        error_code="account_already_connected",
    )


async def disconnect(workspace_id: str, provider: str) -> None:
    connector = registry.get(provider)
    conn = await repository.active_connection(workspace_id, provider)
    if conn and connector.category == ConnectorCategory.CHAT:
        try:
            await connector.adapter.revoke(registry.auth_for(conn), conn)
        except AppError as exc:
            # the token may already be gone (the app was removed on the provider's side): forget it anyway
            logger.warning("connector_revoke_failed", provider=provider, error_code=exc.error_code)
    if connector.on_disconnect is not None:
        await connector.on_disconnect(workspace_id)
    await repository.disconnect(workspace_id, provider)
    # a shared token stays while another product still uses it
    if not any([await repository.active_connection(workspace_id, p) for p in registry.siblings(provider)]):
        if connector.category == ConnectorCategory.CHAT:
            await oauth_tokens.forget(workspace_id, connector.credential)
        else:
            await oauth_tokens.set_status(workspace_id, connector.credential, ConnectionStatus.DISCONNECTED)


async def require_connection(workspace_id: str, provider: str) -> dict:
    registry.get(provider)
    conn = await repository.active_connection(workspace_id, provider)
    if conn is None:
        raise ConflictError(f"{provider} is not connected", error_code=f"{provider}_not_connected")
    return conn


async def list_available_targets(workspace_id: str, provider: str) -> list[dict]:
    conn = await require_connection(workspace_id, provider)
    return await registry.get(provider).adapter.list_targets(registry.auth_for(conn), conn)


async def list_assignees(workspace_id: str, provider: str, key: str) -> list[dict]:
    conn = await require_connection(workspace_id, provider)
    return await registry.work_item_source(provider).list_assignees(registry.auth_for(conn), conn, key)


async def add_target(workspace_id: str, provider: str, key: str, name: str | None) -> dict:
    conn = await require_connection(workspace_id, provider)
    if await repository.find_target(workspace_id, provider, key):
        raise ConflictError("already connected", error_code="target_already_connected")
    if registry.get(provider).category == ConnectorCategory.CHAT:
        # a channel is watched only if the app can really hear it there, never one picked by id alone
        offered = {t["key"]: t for t in await registry.get(provider).adapter.list_targets(registry.auth_for(conn), conn)}
        channel = offered.get(key)
        if channel is None:
            raise NotFoundError("that channel isn't one the app can see", error_code="target_not_available")
        if channel.get("hint"):
            raise ConflictError(f"{channel['name']}: {channel['hint']}", error_code="channel_not_joined")
        name = channel["name"]
    return await repository.add_target(workspace_id, provider, key, name)


async def remove_target(workspace_id: str, provider: str, key: str) -> None:
    if not await repository.remove_target(workspace_id, provider, key):
        raise NotFoundError("not connected", error_code="target_not_found")


async def request_full_sync(workspace_id: str, provider: str) -> list[str]:
    """Targets to resync now, each made to run in full (so deletions show up too)."""
    if registry.get(provider).category == ConnectorCategory.CHAT:
        raise ConflictError(f"{provider} sends messages as they're posted; there's nothing to sync", error_code="nothing_to_sync")
    await require_connection(workspace_id, provider)
    targets = await repository.list_targets(workspace_id, provider)
    for target in targets:
        await repository.finish_target(workspace_id, provider, target["key"], last_full_sync_at=None)
    return [t["key"] for t in targets]
