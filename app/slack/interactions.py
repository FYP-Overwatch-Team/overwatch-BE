"""Buttons in the daily digest: Slack calls this endpoint when someone clicks one.

Slack signs these requests like events, and wants an answer within 3 seconds, so the click is
acknowledged at once and handled afterwards. The answer goes back through the click's
`response_url`: the digest line is replaced with who acted, or only the clicker sees why nothing
happened. The person is found in Bearings by their Slack email (which Slack verifies) and must be
a member of the workspace the question belongs to.

"Create a decision" makes an open decision of the question, owned by whoever clicked, quoting the
conversation; "We'll settle it here" marks it as being settled in the thread, where the next read
finds the decision. Each question is acted on once: the status change is claimed atomically first.
"""

import json
from urllib.parse import parse_qs, urlparse

import structlog
from fastapi import BackgroundTasks, Request, Response

from app.auth.repository import find_user_by_email
from app.connectors import registry
from app.connectors.oauth import http
from app.connectors.repository import ACTIVE
from app.core.constants import ACTIVE_QUESTION_STATUSES, ChatQuestionStatus, DecisionOrigin, DecisionStatus, EvidenceKind, Provider
from app.core.exceptions import AppError, FieldValidationError, NotFoundError, UnauthorizedError
from app.core.responses import api_router
from app.core.time import utcnow
from app.db import mongo
from app.decisions import service as decisions
from app.decisions.schemas import CreateDecision, NewEvidence
from app.integrations.http_log import log_call
from app.slack import questions
from app.slack.digest import CREATE_ACTION, SETTLE_ACTION, question_block_id
from app.slack.events import read_body, verify_signature
from app.workspaces.service import require_member

router = api_router(prefix="/webhooks/slack", tags=["webhooks"])
logger = structlog.get_logger("app.slack")

# where Slack's response_url points; anything else in a payload is refused (it's signed, but cheap to check)
RESPONSE_HOST = "hooks.slack.com"
TITLE_CHARS = 200


@router.post("/interactions")
async def slack_interactions(request: Request, background_tasks: BackgroundTasks) -> Response:
    raw_body = await read_body(request)
    if not verify_signature(raw_body, request.headers.get("x-slack-request-timestamp"), request.headers.get("x-slack-signature")):
        logger.warning("slack_interaction_bad_signature")
        raise UnauthorizedError("invalid Slack signature", error_code="slack_bad_signature")
    try:
        payload = json.loads(parse_qs(raw_body.decode())["payload"][0])
    except (KeyError, IndexError, ValueError, UnicodeDecodeError) as exc:
        raise AppError("unreadable Slack interaction", error_code="slack_bad_payload") from exc
    action = (payload.get("actions") or [{}])[0]
    if payload.get("type") == "block_actions" and action.get("action_id") in {CREATE_ACTION, SETTLE_ACTION}:
        background_tasks.add_task(handle, payload, action)
    return Response(status_code=200)


async def handle(payload: dict, action: dict) -> None:
    response_url = payload.get("response_url") or ""
    try:
        reply = await _act(payload, action)
    except Exception:
        logger.exception("slack_interaction_failed", action=action.get("action_id"))
        reply = _only_you("Something went wrong on our side. Try again in a moment.")
    await _respond(response_url, reply)


async def _act(payload: dict, action: dict) -> dict:
    question = await mongo.chat_questions().find_one({"_id": action.get("value")})
    team_id = (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id")
    conn = question and await mongo.connections().find_one({
        "workspace_id": question["workspace_id"], "provider": Provider.SLACK, "external_account.id": team_id, **ACTIVE,
    })
    if conn is None:
        return _only_you("This question isn't from a workspace connected to this Slack.")
    if question["status"] not in ACTIVE_QUESTION_STATUSES:
        return _only_you("This question has already been tracked, settled, or it expired.")

    slack_user = (payload.get("user") or {}).get("id", "")
    email = await registry.get(Provider.SLACK).adapter.user_email(registry.auth_for(conn), conn, slack_user)
    user = email and await find_user_by_email(email)
    try:
        if not user:
            raise LookupError
        await require_member(question["workspace_id"], user["_id"])
    except (LookupError, AppError):
        return _only_you("Sign in to Bearings with your Slack email, and join this workspace, to do this from Slack.")

    if action["action_id"] == SETTLE_ACTION:
        return await _settle(payload, question, slack_user, user["_id"])
    return await _track(payload, question, slack_user, user["_id"])


async def _claim(question: dict, allowed: list[str], status: str, user_id: str) -> bool:
    claimed = await mongo.chat_questions().find_one_and_update(
        {"_id": question["_id"], "status": {"$in": allowed}},
        {"$set": {"status": status, "acted_by": user_id, "updated_at": utcnow()}},
    )
    return claimed is not None


async def _track(payload: dict, question: dict, slack_user: str, user_id: str) -> dict:
    if not await _claim(question, list(ACTIVE_QUESTION_STATUSES), ChatQuestionStatus.TRACKED, user_id):
        return _only_you("Someone else acted on this question just now.")
    member = {"workspace_id": question["workspace_id"], "user_id": user_id}
    body = CreateDecision(
        title=_title(question["question"]), statement=question["question"], status=DecisionStatus.OPEN, owner_id=user_id,
        context_md="Raised in Slack and not settled there; tracked from the channel's daily digest.",
        evidence=[NewEvidence(kind=EvidenceKind.CHAT_THREAD, ref_id=question["thread_ref"], quote=question["quote"])],
    )
    try:
        decision = await decisions.create(member, body, DecisionOrigin.SUGGESTED)
    except (NotFoundError, FieldValidationError):
        # the message it quotes was deleted or edited in Slack since: there's nothing to track any more
        await mongo.chat_questions().update_one({"_id": question["_id"]}, {"$set": {"status": question["status"]}})
        # checked against what the conversation says now: its other questions may still stand
        thread = await mongo.chat_threads().find_one(
            {"_id": question["thread_ref"], "workspace_id": question["workspace_id"]}, {"text": 1},
        )
        await questions.withdraw_unquoted(question["workspace_id"], question["thread_ref"], thread and thread["text"])
        return _only_you("The message this question came from was changed or deleted in Slack, so it can't be tracked.")
    except Exception:
        # put it back, so it can be tried again
        await mongo.chat_questions().update_one({"_id": question["_id"]}, {"$set": {"status": question["status"]}})
        raise
    await mongo.chat_questions().update_one({"_id": question["_id"]}, {"$set": {"decision_id": decision["id"]}})
    return _replace(payload, question["_id"], f":memo: Tracked in Bearings as an open decision by <@{slack_user}>")


async def _settle(payload: dict, question: dict, slack_user: str, user_id: str) -> dict:
    allowed = [ChatQuestionStatus.PENDING, ChatQuestionStatus.POSTED]
    if not await _claim(question, allowed, ChatQuestionStatus.SETTLING, user_id):
        return _only_you("Someone else acted on this question just now.")
    return _replace(payload, question["_id"], f":speech_balloon: <@{slack_user}> will settle this in the thread")


def _title(question: str) -> str:
    text = " ".join(question.split()).rstrip("?").strip() or "Unsettled question"
    return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1] + "…"


def _only_you(text: str) -> dict:
    return {"response_type": "ephemeral", "replace_original": False, "text": text}


def _replace(payload: dict, question_id: str, line: str) -> dict:
    """The digest as it was, with this question's buttons swapped for who acted."""
    message = payload.get("message") or {}
    target = question_block_id(question_id)
    updated = [
        {"type": "context", "block_id": target, "elements": [{"type": "mrkdwn", "text": line}]} if block.get("block_id") == target else block
        for block in message.get("blocks") or []
    ]
    return {"replace_original": True, "text": message.get("text") or line, "blocks": updated}


async def _respond(response_url: str, body: dict) -> None:
    parsed = urlparse(response_url)
    if parsed.scheme != "https" or parsed.hostname != RESPONSE_HOST:
        logger.warning("slack_interaction_bad_response_url")
        return
    try:
        resp = await http.post(response_url, json=body)
        log_call(logger, "slack_api_call", "POST", "/response_url", resp)
    except Exception:
        logger.exception("slack_interaction_respond_failed")
