"""The AI chat: saved conversations about one repository, answered from its graph.

A question is only stored together with its answer, so a failed call leaves
nothing behind and the client can simply resend.
"""

import uuid
from dataclasses import asdict

from app.chat import limits, repository
from app.chat.schemas import AskRequest
from app.core.exceptions import ConflictError, NotFoundError
from app.core.time import utcnow
from app.github.repository import get_readable
from app.integrations.gemini_client import Turn
from app.query import service as query
from app.query.grounding import Citation, plain_text

#: Earlier messages sent with a question, oldest first.
HISTORY_MESSAGES = 6
TITLE_CHARS = 80


def to_summary(doc: dict) -> dict:
    return {**doc, "id": doc["_id"]}


async def ask(member: dict, body: AskRequest) -> dict:
    workspace_id, user_id = member["workspace_id"], member["user_id"]
    await get_readable(workspace_id, body.repo_full_name)

    conversation = None
    if body.conversation_id:
        conversation = await repository.get(workspace_id, user_id, body.conversation_id, last_messages=HISTORY_MESSAGES)
        if conversation is None or conversation["repo_full_name"] != body.repo_full_name:
            raise NotFoundError("conversation not found", error_code="conversation_not_found")
        if conversation["message_count"] + 2 > repository.MAX_MESSAGES:
            raise ConflictError("this conversation is full; start a new one", error_code="conversation_full")

    earlier = conversation["messages"] if conversation else []
    last_answer = next((m for m in reversed(earlier) if m["role"] == "assistant"), None)
    result = await query.answer(
        body.repo_full_name,
        body.question,
        selected_node_id=body.focused_node_id,
        mentioned_node_ids=body.mentioned_node_ids,
        earlier_node_ids=[node["node_id"] for node in last_answer["nodes"]] if last_answer else [],
        open_folder_ids=body.open_folder_ids,
        history=[_turn(message) for message in earlier],
        # charged only once there's a graph to answer from, right before Gemini is called
        before_model_call=lambda: limits.take(workspace_id, user_id),
    )

    now = utcnow()
    # a mention is shown as a chip, not an inline reference
    mentions = [{**asdict(mention), "ref": None} for mention in result.mentions]
    question = _message("user", body.question, mentions, True, now)
    answer = _message("assistant", result.text, [asdict(c) for c in result.citations], result.grounded, now)
    if conversation is None:
        saved = await repository.create(
            workspace_id, user_id, body.repo_full_name, body.question[:TITLE_CHARS], [question, answer],
        )
    else:
        saved = await repository.append(workspace_id, user_id, conversation["_id"], [question, answer])
        if saved is None:  # filled up, or deleted, while the answer was being written
            raise ConflictError("this conversation is full; start a new one", error_code="conversation_full")
    return {"conversation": to_summary(saved), "question": question, "answer": answer}


async def list_conversations(member: dict, repo_full_name: str) -> list[dict]:
    return [to_summary(doc) for doc in await repository.list_for(member["workspace_id"], member["user_id"], repo_full_name)]


async def get_conversation(member: dict, conversation_id: str) -> dict:
    doc = await repository.get(member["workspace_id"], member["user_id"], conversation_id)
    if doc is None:
        raise NotFoundError("conversation not found", error_code="conversation_not_found")
    return to_summary(doc)


async def delete_conversation(member: dict, conversation_id: str) -> None:
    if not await repository.delete(member["workspace_id"], member["user_id"], conversation_id):
        raise NotFoundError("conversation not found", error_code="conversation_not_found")


def _message(role: str, text: str, nodes: list[dict], grounded: bool, created_at) -> dict:
    return {"id": uuid.uuid4().hex, "role": role, "text": text, "nodes": nodes, "grounded": grounded, "created_at": created_at}


def _turn(message: dict) -> Turn:
    if message["role"] == "user":
        return Turn("user", message["text"])
    citations = [Citation(**node) for node in message["nodes"] if node.get("ref")]
    return Turn("model", plain_text(message["text"], citations))
