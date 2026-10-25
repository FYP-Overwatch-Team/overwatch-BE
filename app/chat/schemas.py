from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.query.context import MAX_OPEN_FOLDERS

MAX_QUESTION_CHARS = 2_000


class AskRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    repo_full_name: str
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    #: omitted to start a new conversation
    conversation_id: str | None = None
    #: the node selected on the canvas, if any
    focused_node_id: str | None = None
    #: nodes the question @-mentions; every mention takes at least `@x ` of the question, so this bound never cuts one
    mentioned_node_ids: list[str] = Field(default_factory=list, max_length=MAX_QUESTION_CHARS // 3)
    #: folders open on the canvas, most recently opened first
    open_folder_ids: list[str] = Field(default_factory=list, max_length=MAX_OPEN_FOLDERS)


class NodeRef(BaseModel):
    """A node a message points at: an answer's `[nX]` citation, or a question's @-mention."""

    #: the `nX` used inline in an answer; null for a mention
    ref: str | None = None
    node_id: str
    #: the module that owns it, which the diagram can always draw
    module_id: str
    name: str


class CodeCitation(BaseModel):
    """Lines of the repository an answer points at with `[cN]`, copied from the file, never from the model."""

    #: the `cN` used inline in the answer
    ref: str
    path: str
    #: the commit the lines were read at
    commit_sha: str
    start_line: int
    end_line: int
    code: str
    #: what to highlight it as; null when unknown
    language: str | None = None
    #: the node the code belongs to; null for a range an earlier answer cited
    node_id: str | None = None


class FlowStep(BaseModel):
    """One step of a flow an answer walks through."""

    #: the step's `nX`; its node is among the message's nodes
    ref: str
    node_id: str
    #: Markdown; may hold further `[nX]` and `[cN]`
    text: str
    #: a `cN` among the message's citations
    citation_ref: str | None = None


class ClarifyingQuestion(BaseModel):
    """A question back, asked only when the question could mean different things."""

    question: str
    options: list[str]


class ChatMessage(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    text: str
    nodes: list[NodeRef]
    #: code an answer cites; empty for questions and answers saved before code was cited
    citations: list[CodeCitation] = []
    #: in order; empty for questions, answers not about a flow and answers saved before steps
    steps: list[FlowStep] = []
    #: questions to go deeper; empty for questions and answers saved before them
    follow_ups: list[str] = []
    #: a question back instead of an answer; null almost always
    ask: ClarifyingQuestion | None = None
    #: false when the answer was refused or the graph couldn't answer it
    grounded: bool = True
    created_at: datetime


class ConversationSummary(BaseModel):
    id: str
    repo_full_name: str
    title: str
    message_count: int
    created_at: datetime
    updated_at: datetime


class Conversation(ConversationSummary):
    messages: list[ChatMessage]


class AskResult(BaseModel):
    conversation: ConversationSummary
    question: ChatMessage
    answer: ChatMessage
