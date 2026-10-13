"""The sync every work-item connector shares; the adapter only fetches and normalises.

Polling, not webhooks (Jira's dynamic webhooks need a public URL and expire every 30 days).
Most runs are incremental, reading only items updated since the last sync; a full run,
which also drops items deleted at the source, happens first and then hourly.
"""

import math
from datetime import timedelta

import structlog

from app.boards import repository as board_store
from app.boards import service as boards
from app.connectors import registry, repository
from app.connectors.service import can_manage_sprints
from app.core.constants import JobStatus
from app.core.exceptions import ExternalServiceError
from app.core.time import utcnow
from app.sprints import service as sprints
from app.work_items import repository as work_items

logger = structlog.get_logger("app.connectors.sync")

FULL_SYNC_EVERY = timedelta(hours=1)
# re-read a few minutes before the last sync, so clock skew or a slow page can't drop an update
OVERLAP_MINUTES = 5


async def run_work_item_sync(workspace_id: str, provider: str, target_key: str) -> None:
    target = await repository.claim_target(workspace_id, provider, target_key)
    if target is None:
        logger.info("sync_skipped_already_running", provider=provider, target=target_key)
        return

    started = utcnow()
    last_full = target.get("last_full_sync_at")
    full = target.get("last_synced_at") is None or last_full is None or started - last_full >= FULL_SYNC_EVERY
    try:
        count = await _sync(workspace_id, provider, target_key, target, full)
        await repository.finish_target(
            workspace_id, provider, target_key, sync_status=JobStatus.DONE, sync_error=None, last_synced_at=started,
            **({"last_full_sync_at": started} if full else {}),
        )
        logger.info("sync_done", provider=provider, target=target_key, items=count, full=full)
    except Exception as exc:
        logger.exception("sync_failed", provider=provider, target=target_key)
        await repository.finish_target(workspace_id, provider, target_key, sync_status=JobStatus.FAILED, sync_error=str(exc))


async def _sync(workspace_id: str, provider: str, target_key: str, target: dict, full: bool) -> int:
    conn = await repository.active_connection(workspace_id, provider)
    if conn is None:
        raise ExternalServiceError(f"{provider} is not connected", error_code=f"{provider}_not_connected")
    adapter, auth = registry.get(provider).adapter, registry.auth_for(conn)
    since = None if full else math.ceil((utcnow() - target["last_synced_at"]).total_seconds() / 60) + OVERLAP_MINUTES

    board = await board_store.ensure(workspace_id, provider, target_key, target.get("name"))
    # columns, board details and sprints are extra: a tracker that won't share them still syncs its items
    if full:
        board = await _read_board(adapter, auth, conn, board)
    if board.get("external_id") and can_manage_sprints(conn):
        try:
            # every run: starting or completing a sprint changes no issue, so issue syncs alone would miss it
            await sprints.mirror(board, await adapter.list_sprints(auth, conn, board["external_id"]))
        except Exception:
            logger.warning("sprints_unavailable", provider=provider, target=target_key, exc_info=True)
    sprint_ids: dict[str, str] = {}

    seen: list[str] = []
    seen_tokens: set[str] = set()
    token = None
    while True:
        page = await adapter.fetch_page(auth, conn, target_key, since_minutes=since, page_token=token, board=board)
        for raw in page.items:
            fields = boards.place(board, adapter.normalise(conn, target_key, raw, board))
            fields = await sprints.apply_tracker_sprint(board, fields, sprint_ids)
            await work_items.upsert_synced(workspace_id, fields)
            seen.append(fields["external_id"])
        token = page.next_token
        if not token:
            break
        if token in seen_tokens:
            raise ExternalServiceError(f"{provider} returned a repeated page token", error_code=f"{provider}_search_failed")
        seen_tokens.add(token)

    if full:
        await work_items.sweep(workspace_id, provider, target_key, seen)
    return len(seen)


async def _read_board(adapter, auth, conn: dict, board: dict) -> dict:
    """The tracker's columns (offered or followed), board id, kind and field ids, on a full sync."""
    provider, target_key = board["source"], board["target_key"]
    try:
        board = await boards.offer_columns(board, await adapter.board_columns(auth, conn, target_key))
    except Exception:
        logger.warning("board_columns_unavailable", provider=provider, target=target_key, exc_info=True)
    try:
        details = await adapter.board_details(auth, conn, target_key)
    except Exception:
        logger.warning("board_details_unavailable", provider=provider, target=target_key, exc_info=True)
        return board
    # a grant that can't see the board leaves its kind as the sprints found on items made it
    changes = {"external_id": details["external_id"], "fields": details["fields"], **({"kind": details["kind"]} if details["kind"] else {})}
    return await board_store.update(board["_id"], changes)
