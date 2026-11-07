"""Strip credential-shaped text out of anything we keep from source code.

We store signatures and docstrings so the graph can answer questions about
them, and we later send some of that text to an LLM. Real repositories contain
hardcoded keys, and a default argument like `def connect(key="AKIA…")` would
otherwise be copied into the database and into a third-party prompt.

This is defence in depth, not a secret scanner: it removes the shapes that are
unmistakably credentials and leaves everything else alone.
"""

import re
from collections.abc import Sequence

PLACEHOLDER = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Provider-issued tokens with distinctive prefixes.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9-_]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
    # JSON web tokens.
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    # PEM private keys.
    re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"),
    # key = "…" / password: '…' — the assignment, keeping the field name.
    re.compile(
        r"""(?i)\b(api[_-]?key|secret|token|password|passwd|credential)s?\b(\s*[:=]\s*)"""
        r"""(['"])[^'"\n]{8,}\3""",
    ),
)

_KEY_BEGIN_RE = re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----")
_KEY_END_RE = re.compile(r"-----END[A-Z ]*PRIVATE KEY-----")

# Index of the group that should survive in the assignment pattern above.
_ASSIGNMENT_PATTERN = _PATTERNS[-1]


def redact(text: str) -> str:
    """Replace credential-shaped substrings with a placeholder."""
    if not text:
        return text
    for pattern in _PATTERNS:
        if pattern is _ASSIGNMENT_PATTERN:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{PLACEHOLDER}", text)
        else:
            text = pattern.sub(PLACEHOLDER, text)
    return text


def redact_lines(lines: Sequence[str]) -> list[str]:
    """`redact` line by line, keeping the count: every line of a private key block is replaced whole."""
    redacted, in_key = [], False
    for line in lines:
        starts = bool(_KEY_BEGIN_RE.search(line))
        redacted.append(PLACEHOLDER if starts or in_key else redact(line))
        if starts:
            in_key = True
        if _KEY_END_RE.search(line):
            in_key = False
    return redacted


def redact_optional(text: str | None) -> str | None:
    return None if text is None else redact(text)
