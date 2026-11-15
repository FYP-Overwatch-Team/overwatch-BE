"""Which search leads for a question, and how many starting points each one gives.

Two searches find where the code for a question lives:

- keyword search (BM25 over names, paths, signatures and docstrings) is right when the
  question names code: `open_ask_context`, SignupForm, app/auth/otp.py;
- meaning search (embeddings) is right when it describes behaviour in plain words:
  "where do we stop people asking too many questions".

A question is routed to the search that fits it. The other one still contributes,
with a smaller share. The shares were measured with `scripts/eval_chat.py retrieval`.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class Route(StrEnum):
    KEYWORD = "keyword"
    MEANING = "meaning"


@dataclass(frozen=True, slots=True)
class RouteBudget:
    """Starting points (followed through the graph) and code snippets taken from each search."""

    keyword_seeds: int
    meaning_seeds: int
    keyword_snippets: int
    meaning_snippets: int

    @property
    def seeds(self) -> int:
        return self.keyword_seeds + self.meaning_seeds

    @property
    def snippets(self) -> int:
        return self.keyword_snippets + self.meaning_snippets


BUDGETS: dict[Route, RouteBudget] = {
    Route.KEYWORD: RouteBudget(keyword_seeds=4, meaning_seeds=1, keyword_snippets=2, meaning_snippets=1),
    # the best of the splits tried on plain questions (scripts/eval_chat.py retrieval)
    Route.MEANING: RouteBudget(keyword_seeds=1, meaning_seeds=4, keyword_snippets=1, meaning_snippets=3),
}

#: Shapes that only code names have; one in the question routes it to keyword search.
CODE_NAME_PATTERNS = (
    re.compile(r"\b[a-z0-9]+(?:_[a-z0-9]+)+\b"),  # snake_case
    re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b"),  # CONSTANT_CASE
    re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]+)+\b"),  # camelCase
    re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b"),  # PascalCase
    re.compile(r"`[^`\n]+`"),  # `quoted code`
    re.compile(r"@\w+"),  # @mention
    re.compile(r"\w/\w"),  # a/path
    re.compile(r"\b[\w-]+\.[A-Za-z]{2,5}\b"),  # file.py (two letters at least, so "e.g." isn't one)
)

def route_of(question: str, mentioned_node_ids: Sequence[str] = ()) -> Route:
    """Keyword search leads when the question names code (or @-mentions it), meaning search otherwise."""
    if mentioned_node_ids or any(pattern.search(question) for pattern in CODE_NAME_PATTERNS):
        return Route.KEYWORD
    return Route.MEANING


#: Words for how the whole codebase is laid out, rather than one part of it.
OVERVIEW_PATTERN = re.compile(
    r"\b(?:structur\w*|organi[sz]\w*|architecture|layout|laid out|overview|folders?)\b", re.IGNORECASE,
)


def is_overview(question: str, mentioned_node_ids: Sequence[str] = ()) -> bool:
    """A question about the codebase's overall shape, naming no code: its folder tree answers it best."""
    return route_of(question, mentioned_node_ids) == Route.MEANING and bool(OVERVIEW_PATTERN.search(question))


def merge_hits(route: Route, keyword: Sequence[dict], meaning: Sequence[dict], keyword_share: int, meaning_share: int) -> list[dict]:
    """Both searches' hits in one order: the leading search's share, the other's share, then the rest.

    A node found by both is kept once, where it first appears. When one search finds less than its
    share, the rest of the other's hits fill the gap.
    """
    if route == Route.KEYWORD:
        ordered = [*keyword[:keyword_share], *meaning[:meaning_share], *keyword[keyword_share:], *meaning[meaning_share:]]
    else:
        ordered = [*meaning[:meaning_share], *keyword[:keyword_share], *meaning[meaning_share:], *keyword[keyword_share:]]
    first_seen: dict[str, dict] = {}
    for row in ordered:
        first_seen.setdefault(row["id"], row)
    return list(first_seen.values())
