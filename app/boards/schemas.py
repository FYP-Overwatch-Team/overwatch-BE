from pydantic import BaseModel

from app.core.constants import BoardKind, ColumnColor, ColumnMode, ColumnPrompt, StatusCategory


class Column(BaseModel):
    id: str
    name: str
    status_category: StatusCategory
    #: the board's choice; null for the default its name and category suggest
    color: ColumnColor | None = None


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


class ColumnSettings(BaseModel):
    """Only what's sent changes. Settings are kept by column id, so they survive the tracker's columns changing."""

    #: column ids in the order to show them; columns left out keep their place after these
    column_order: list[str] | None = None
    #: column id -> color, or null to go back to the default
    column_colors: dict[str, ColumnColor | None] | None = None
