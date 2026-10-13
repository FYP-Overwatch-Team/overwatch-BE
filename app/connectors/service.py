import secrets

from app.connectors import registry, repository
from app.core.constants import AuthBackendName, ConnectionStatus, Role
from app.core.exceptions import ConflictError, NotFoundError
from app.db import oauth_tokens
from app.workspaces.service import require_member


async def auth_status(conn: dict | None) -> str:
    if conn is None or conn["status"] == ConnectionStatus.DISCONNECTED:
        return ConnectionStatus.DISCONNECTED
    token = await oauth_tokens.get(conn["workspace_id"], conn["provider"])
    return token["status"] if token else ConnectionStatus.DISCONNECTED


def can_write(conn: dict | None) -> bool:
    write_scope = conn and registry.get(conn["provider"]).write_scope
    return bool(write_scope and conn["status"] != ConnectionStatus.DISCONNECTED and write_scope in conn.get("scopes", []))


def can_manage_sprints(conn: dict | None) -> bool:
    """The grant reaches the tracker's boards and sprints (and can write back at all)."""
    sprint_scope = conn and registry.get(conn["provider"]).sprint_scope
    return bool(sprint_scope and can_write(conn) and sprint_scope in conn.get("scopes", []))


async def catalog(workspace_id: str) -> list[dict]:
    """Every supported connector with this workspace's state for it."""
    connections = {c["provider"]: c for c in await repository.list_connections(workspace_id)}
    targets = await repository.list_targets(workspace_id)
    result = []
    for provider, connector in registry.CONNECTORS.items():
        conn = connections.get(provider)
        status = await auth_status(conn)
        result.append({
            "provider": provider,
            "name": connector.name,
            "status": status,
            "can_write": can_write(conn),
            "external_account": conn and conn.get("external_account") if status != ConnectionStatus.DISCONNECTED else None,
            "connected_by": conn and conn.get("connected_by"),
            "connected_at": conn and conn.get("connected_at"),
            "targets": [t for t in targets if t["provider"] == provider],
        })
    return result


def start_authorization(member: dict, provider: str, return_to: str) -> tuple[str, str]:
    """(authorization_url, state). The state binds the user, workspace and landing page."""
    state = f"{member['user_id']}.{member['workspace_id']}.{return_to}.{secrets.token_urlsafe(24)}"
    return registry.get(provider).flow.authorize_url(state), state


async def complete_authorization(provider: str, workspace_id: str, user_id: str, code: str) -> None:
    # they may have lost the admin role during the round trip
    await require_member(workspace_id, user_id, Role.ADMIN)
    connector = registry.get(provider)
    tokens = await connector.flow.exchange_code(code)
    account = await connector.adapter.resolve_account(tokens["access_token"])
    await oauth_tokens.save(workspace_id, provider, tokens)
    await repository.save_connection(
        workspace_id, provider,
        auth_backend=AuthBackendName.NATIVE_OAUTH,
        scopes=tokens.get("scope", "").split() or list(connector.oauth.scopes),
        external_account=account,
        connected_by=user_id,
    )


async def disconnect(workspace_id: str, provider: str) -> None:
    registry.get(provider)
    await oauth_tokens.set_status(workspace_id, provider, ConnectionStatus.DISCONNECTED)
    await repository.disconnect(workspace_id, provider)


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
    return await registry.get(provider).adapter.list_assignees(registry.auth_for(conn), conn, key)


async def add_target(workspace_id: str, provider: str, key: str, name: str | None) -> dict:
    await require_connection(workspace_id, provider)
    if await repository.find_target(workspace_id, provider, key):
        raise ConflictError("already connected", error_code="target_already_connected")
    return await repository.add_target(workspace_id, provider, key, name)


async def remove_target(workspace_id: str, provider: str, key: str) -> None:
    if not await repository.remove_target(workspace_id, provider, key):
        raise NotFoundError("not connected", error_code="target_not_found")


async def request_full_sync(workspace_id: str, provider: str) -> list[str]:
    """Targets to resync now, each made to run in full (so deletions show up too)."""
    await require_connection(workspace_id, provider)
    targets = await repository.list_targets(workspace_id, provider)
    for target in targets:
        await repository.finish_target(workspace_id, provider, target["key"], last_full_sync_at=None)
    return [t["key"] for t in targets]
