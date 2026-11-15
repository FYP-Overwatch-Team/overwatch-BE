from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import BoardKind, ColumnColor, ColumnMode, ColumnPrompt, StatusCategory


class Column(BaseModel):
    id: str
    name: str
    status_category: StatusCategory
    #: the board's choice; null for the default its name and category suggest
    color: ColumnColor | None = None
    #: the tracker statuses it holds; empty on boards that place items by category
    status_ids: list[str] = []
    #: native boards: the columns a card can move to from here; null for any
    next: list[str] | None = None


class TrackerTransition(BaseModel):
    id: str
    name: str
    #: the statuses it starts from; null for any (a global transition)
    from_: list[str] | None = Field(default=None, alias="from")
    to: str


class TrackerWorkflow(BaseModel):
    """The tracker's workflows, for drag hints; the tracker still decides each move."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    #: status id -> {name, category}
    statuses: dict[str, dict]
    #: workflow id -> its moves
    workflows: dict[str, dict]
    #: issue type id -> workflow id
    by_issue_type: dict[str, str]


class ColumnOffer(BaseModel):
    status: ColumnPrompt
    #: the tracker's column names on offer; empty until a full sync has read them
    columns: list[str]


class Board(BaseModel):
    id: str
    source: str  # NATIVE_SOURCE or the tracker's provider
    target_key: str | None
    name: str
    kind: BoardKind
    column_mode: ColumnMode
    columns: list[Column]
    column_prompt: ColumnOffer
    #: false when the tracker grant can't write back
    editable: bool
    #: the tracker's workflows, once a full sync could read them
    workflow: TrackerWorkflow | None = None


class ColumnSettings(BaseModel):
    """Only what's sent changes. Settings are kept by column id, so they survive the tracker's columns changing."""

    #: column ids in the order to show them; columns left out keep their place after these
    column_order: list[str] | None = None
    #: column id -> color, or null to go back to the default
    column_colors: dict[str, ColumnColor | None] | None = None


ColumnId = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class NativeColumn(BaseModel):
    #: an existing column's id, or a new one the client made up
    id: str = ColumnId
    name: str = Field(min_length=1, max_length=40)
    status_category: StatusCategory
    color: ColumnColor | None = None
    #: the columns a card can move to from here; null for any
    next: list[str] | None = None


class NativeColumns(BaseModel):
    """A native board's columns and workflow, all at once, in the order to show them."""

    columns: list[NativeColumn] = Field(min_length=1, max_length=12)
    #: removed column id -> the column its items move to; the first column when left out
    reassign: dict[str, str] = {}
