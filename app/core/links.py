"""Where the app opens things, as paths on the frontend; built here so a link is never taken from the AI."""

from urllib.parse import urlencode

#: where trackers and docs sources are connected
INTEGRATIONS = "/settings/integrations"
#: the board with its New item dialog open
NEW_WORK_ITEM = "/board?new=1"


def work_item_href(board_id: str | None, key: str | None) -> str:
    """A board item, opened on its board."""
    params = {"board": board_id, "item": key}
    return f"/board?{urlencode({name: value for name, value in params.items() if value})}"


def document_href(document_id: str) -> str:
    return f"/documents/{document_id}"


def decision_href(decision_id: str) -> str:
    return f"/decisions/{decision_id}"
