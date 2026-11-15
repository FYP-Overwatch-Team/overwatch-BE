from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import ChatActionKind, ChatRecordKind, ProposalStatus
from app.query.context import MAX_OPEN_FOLDERS

MAX_QUESTION_CHARS = 2_000


class AskRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    #: omitted to ask about the workspace alone
    repo_full_name: str | None = None
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


class RecordRef(BaseModel):
    """A workspace record an answer cites with `[rN]`: a ticket, page or decision, as it was when the answer was written."""

    #: the `rN` used inline in the answer
    ref: str
    kind: ChatRecordKind
    id: str
    title: str
    #: the ticket's key, like DEV-12
    key: str | None = None
    #: the tracker or docs source it's from, or `native`; null for a decision
    source: str | None = None
    status: str | None = None
    #: where the app opens it
    href: str


class ChatAction(BaseModel):
    """A button under an answer, from what its lookups found: connect a tracker, create a ticket."""

    kind: ChatActionKind
    label: str
    href: str


class ProposalFields(BaseModel):
    """A drafted ticket, page or decision as the asker approves it, edits included."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    #: a ticket's description, a page's text or a decision's background, in Markdown
    body: str = Field(default="", max_length=20_000)
    #: a decision's statement; empty for the others
    statement: str = Field(default="", max_length=2_000)
    #: the board a ticket goes on; null for the others
    board_id: str | None = None


class ProposalBoard(BaseModel):
    id: str
    name: str


class ProposalResult(BaseModel):
    """What approving a proposal created."""

    kind: ChatRecordKind
    id: str
    title: str
    href: str


class Proposal(BaseModel):
    """A ticket, page or decision the AI drafted; it exists only once the asker approves it."""

    id: str
    kind: ChatRecordKind
    fields: ProposalFields
    #: the boards a ticket can go on, for picking one; empty for pages and decisions
    boards: list[ProposalBoard]
    status: ProposalStatus
    #: what was created, once approved
    result: ProposalResult | None
    expires_at: datetime


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
    #: workspace records the answer cites; empty for questions and older answers
    records: list[RecordRef] = []
    actions: list[ChatAction] = []
    #: what the answer drafted for the asker to approve; empty almost always
    proposals: list[Proposal] = []
    #: false when the answer was refused or the graph couldn't answer it
    grounded: bool = True
    created_at: datetime


class ConversationSummary(BaseModel):
    id: str
    #: null for a conversation about the workspace alone
    repo_full_name: str | None
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
