"""Keyword search over the graph's folders, files and symbols, ranked by BM25.

Neo4j's full-text index (Lucene) does the ranking. Two things it can't do on its own
are added as properties on every searchable node:

- `search_words`: code names split into words, since Lucene keeps `SignupForm` and
  `open_ask_context` whole. `SignupForm` -> "signupform signup form".
- `search_scope`: one token per repository. Lucene can't filter on another property
  before ranking, so every query requires this token; a hash, so `acme/api` and
  `acme/api-v2` never share one.

Field weights follow BM25F: a word in a name says more than one in a docstring.
"""

import hashlib
import re
from collections.abc import Mapping, Sequence

from app.knowledge_graph.build.model import NodeLabel

TEXT_INDEX = "graph_text"
SEARCH_WORDS = "search_words"
SEARCH_SCOPE = "search_scope"
#: Kept off everything the API returns; they only exist for the index.
SEARCH_PROPERTIES = (SEARCH_WORDS, SEARCH_SCOPE)
SEARCHABLE_LABELS = (NodeLabel.SYMBOL, NodeLabel.FILE, NodeLabel.MODULE)

#: How much a matching word counts in each field.
FIELD_BOOSTS: dict[str, float] = {
    "name": 4,
    SEARCH_WORDS: 3,
    "path": 2,
    "file_path": 2,
    "signature": 1,
    "docstring": 0.5,
}
#: A word that only starts a name ("sign" in `SignupForm`) still finds it, ranked low.
PREFIX_BOOST = 0.5
#: Lucene caps clauses per query; each word adds one per field.
MAX_QUERY_WORDS = 12

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_]+")
# `signupForm` -> signup|Form, `HTTPClient` -> HTTP|Client, `v2api` -> v|2|api
_WORD_BOUNDARY_RE = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])"
)
_LUCENE_SPECIAL_RE = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def identifier_words(*texts: str | None) -> str:
    """Each identifier in `texts` lowercased whole, then split into its words, space separated."""
    words: list[str] = []
    for text in texts:
        for identifier in _IDENTIFIER_RE.findall(text or ""):
            words.append(identifier.lower())
            for part in identifier.split("_"):
                words += [word.lower() for word in _WORD_BOUNDARY_RE.split(part) if word]
    return " ".join(dict.fromkeys(words))


def scope_token(repo_full_name: str) -> str:
    """One Lucene token standing for a repository."""
    return "r" + hashlib.sha256(repo_full_name.encode()).hexdigest()[:24]


def search_properties(repo_full_name: str, properties: Mapping[str, object]) -> dict[str, str]:
    """The derived properties a searchable node is stored with."""
    words = identifier_words(
        *(str(properties.get(key) or "") for key in ("name", "path", "file_path", "qualified_name"))
    )
    return {SEARCH_WORDS: words, SEARCH_SCOPE: scope_token(repo_full_name)}


def lucene_query(words: Sequence[str], repo_full_name: str) -> str:
    """A query for nodes of this repository matching any of `words`, each field weighted.

    `+search_scope:r…  +((name:limit^4 OR search_words:limit^3 OR … OR search_words:limit*^0.5) OR (…))`
    """
    clauses = [_word_clause(_escape(word.lower())) for word in words[:MAX_QUERY_WORDS]]
    return f"+{SEARCH_SCOPE}:{scope_token(repo_full_name)} +({' OR '.join(clauses)})"


def text_score(node: Mapping[str, object], words: Sequence[str]) -> float:
    """What `lucene_query` would favour, without Lucene: the in-memory store ranks with it."""
    fields = {field: set(identifier_words(str(node.get(field) or "")).split()) for field in FIELD_BOOSTS}
    fields[SEARCH_WORDS] = set(search_properties("", node)[SEARCH_WORDS].split())
    score = 0.0
    for word in (word.lower() for word in words[:MAX_QUERY_WORDS]):
        score += sum(boost for field, boost in FIELD_BOOSTS.items() if word in fields[field])
        if any(token.startswith(word) for token in fields[SEARCH_WORDS]):
            score += PREFIX_BOOST
    return score


def _word_clause(word: str) -> str:
    fields = [f"{field}:{word}^{boost}" for field, boost in FIELD_BOOSTS.items()]
    return f"({' OR '.join([*fields, f'{SEARCH_WORDS}:{word}*^{PREFIX_BOOST}'])})"


def _escape(word: str) -> str:
    """Lucene syntax in a word is matched literally, never run as an operator."""
    return _LUCENE_SPECIAL_RE.sub(r"\\\1", word)
