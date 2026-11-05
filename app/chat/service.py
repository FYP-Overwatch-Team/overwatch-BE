"""The AI chat: saved conversations about one repository, answered from its graph.

A question is only stored together with its answer, so a failed call leaves
nothing behind and the client can simply resend.
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict

from app.chat import limits, repository
from app.chat.schemas import AskRequest
from app.core.constants import UsageFeature
from app.core.exceptions import AppError, ConflictError, NotFoundError
from app.core.time import utcnow
from app.github.repository import get_readable
from app.integrations.llm import Turn
from app.query import service as query
from app.query.grounding import (
    Citation,
    ClarifyingQuestion,
    CodeCitation,
    FlowStep,
    ask_markdown,
    plain_text,
    steps_markdown,
)
from app.usage import service as usage

#: Earlier messages sent with a question, oldest first.
HISTORY_MESSAGES = 6
#: Code ranges the previous answer cited, shown again so a follow-up can build on them.
MAX_EARLIER_RANGES = 4
TITLE_CHARS = 80


def to_summary(doc: dict) -> dict:
    return {**doc, "id": doc["_id"]}


async def ask(member: dict, body: AskRequest) -> dict:
    """The saved question and its checked answer."""
    async for event, data in ask_stream(member, body):
        if event == "done":
            return data
    raise AssertionError("ask_stream ends with its answer")


async def ask_stream(member: dict, body: AskRequest) -> AsyncIterator[tuple[str, dict]]:
    """`(event, data)` as the answer is written: `started`, then `thinking`, `status` (a lookup) and `answer` pieces, then `done`.

    Everything that can refuse the question (access, a full or unknown conversation,
    no graph yet, rate limits) raises before `started`. `done` carries what `ask`
    returns, saved; text streamed before it is a draft that `done` replaces.
    """
    workspace_id, user_id = member["workspace_id"], member["user_id"]
    repo = await get_readable(workspace_id, body.repo_full_name)

    conversation = None
    if body.conversation_id:
        conversation = await repository.get(workspace_id, user_id, body.conversation_id, last_messages=HISTORY_MESSAGES)
        if conversation is None or conversation["repo_full_name"] != body.repo_full_name:
            raise NotFoundError("conversation not found", error_code="conversation_not_found")
        if conversation["message_count"] + 2 > repository.MAX_MESSAGES:
            raise ConflictError("this conversation is full; start a new one", error_code="conversation_full")

    earlier = conversation["messages"] if conversation else []
    last_answer = next((m for m in reversed(earlier) if m["role"] == "assistant"), None)
    events = query.answer_stream(
        body.repo_full_name,
        body.question,
        selected_node_id=body.focused_node_id,
        mentioned_node_ids=body.mentioned_node_ids,
        earlier_node_ids=[node["node_id"] for node in last_answer["nodes"]] if last_answer else [],
        open_folder_ids=body.open_folder_ids,
        # only ranges read at the current commit: after a re-index the same numbers may be other lines
        earlier_ranges=[
            (c["path"], c["start_line"], c["end_line"])
            for c in (last_answer or {}).get("citations", [])
            if c["commit_sha"] == repo.get("graph_version")
        ][:MAX_EARLIER_RANGES],
        # the commit the graph was built from: its line numbers are that commit's
        commit=repo.get("graph_version"),
        history=[_turn(message) for message in earlier],
        # charged only once there's a graph to answer from, right before the model is called
        before_model_call=lambda: limits.take(workspace_id, user_id),
        before_extra_model_call=limits.take_model_call,
        # a conversation's calls share their opening, so they're routed to one provider's cache
        session=body.conversation_id or uuid.uuid4().hex,
        on_usage=usage.recorder(workspace_id, user_id, UsageFeature.CHAT),
        # never two questions back in a row
        may_ask=not (last_answer or {}).get("ask"),
    )
    result = None
    async for event in events:
        if isinstance(event, query.Started):
            yield "started", {}
        elif isinstance(event, query.Thinking):
            yield "thinking", {"text": event.text}
        elif isinstance(event, query.Looking):
            yield "status", {"text": event.text}
        elif isinstance(event, query.AnswerText):
            yield "answer", {"text": event.text}
        else:
            result = event.result
    if result is None:
        raise AppError("The AI reply ended without an answer", error_code="llm_failed", status_code=502)

    now = utcnow()
    # a mention is shown as a chip, not an inline reference
    mentions = [{**asdict(mention), "ref": None} for mention in result.mentions]
    question = _message("user", body.question, mentions, True, now)
    answer = _message("assistant", result.text, [asdict(c) for c in result.citations], result.grounded, now)
    answer["citations"] = [asdict(c) for c in result.code_citations]
    answer["steps"] = [asdict(step) for step in result.steps]
    answer["follow_ups"] = list(result.follow_ups)
    answer["ask"] = asdict(result.ask) if result.ask else None
    if conversation is None:
        saved = await repository.create(
            workspace_id, user_id, body.repo_full_name, body.question[:TITLE_CHARS], [question, answer],
        )
    else:
        saved = await repository.append(workspace_id, user_id, conversation["_id"], [question, answer])
        if saved is None:  # filled up, or deleted, while the answer was being written
            raise ConflictError("this conversation is full; start a new one", error_code="conversation_full")
    yield "done", {"conversation": to_summary(saved), "question": question, "answer": answer}


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
    return {
        "id": uuid.uuid4().hex, "role": role, "text": text, "nodes": nodes, "steps": [], "follow_ups": [], "ask": None,
        "grounded": grounded, "created_at": created_at,
    }


def _turn(message: dict) -> Turn:
    if message["role"] == "user":
        return Turn("user", message["text"])
    citations = [Citation(**node) for node in message["nodes"] if node.get("ref")]
    code_citations = [CodeCitation(**citation) for citation in message.get("citations", [])]
    text = message["text"]
    if steps := [FlowStep(**step) for step in message.get("steps", [])]:
        text = f"{text}\n\n{steps_markdown(steps)}"
    if ask := message.get("ask"):  # so a picked option reads as the answer to it
        text = f"{text}\n\n{ask_markdown(ClarifyingQuestion(ask['question'], tuple(ask['options'])))}"
    return Turn("model", plain_text(text, citations, code_citations))
