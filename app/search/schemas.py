from pydantic import BaseModel

from app.core.constants import SearchResultKind


class SearchResult(BaseModel):
    kind: SearchResultKind
    id: str
    title: str
    #: where it's mirrored from ("jira", "confluence"...), or "native"
    source: str
    #: a board item's key, and the board it opens on
    key: str | None = None
    board_id: str | None = None
    #: the text around the match, when it was found in the body rather than the key or title
    snippet: str | None = None
