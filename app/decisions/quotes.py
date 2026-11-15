"""Checking that a quote is really in its source, and noticing when that source changes.

Quotes are compared on their words: case, whitespace and Markdown marks are ignored, so a
sentence copied from the rendered page matches the Markdown it was rendered from.
"""

import hashlib
import re

_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKS = re.compile(r"[*_`~#>|]")
_SPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    text = _LINK.sub(r"\1", text)
    text = _MARKS.sub(" ", text)
    return _SPACE.sub(" ", text).strip().casefold()


def contains(source: str, quote: str) -> bool:
    wanted = normalise(quote)
    return bool(wanted) and wanted in normalise(source)


def fingerprint(source: str) -> str:
    """Changes whenever the source's words do, so a decision can tell its evidence was edited."""
    return hashlib.sha256(normalise(source).encode()).hexdigest()[:16]
