"""Spotting credentials in text before it's stored where other people and AI tools will read it.

Only shapes that are almost never anything else: a miss is better than refusing ordinary prose.
"""

import re

_PATTERNS = {
    "an AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "a GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})"),
    "an API key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "a Slack token": re.compile(r"\bxox[abeprs]-[A-Za-z0-9-]{10,}"),
    "a private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "a signed token (JWT)": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "a Bearings token": re.compile(r"\bbwp_[A-Za-z0-9_-]{20,}"),
}


def find_secret(text: str) -> str | None:
    """What kind of credential `text` holds ("a GitHub token"), never the credential itself; None when it holds none."""
    return next((name for name, pattern in _PATTERNS.items() if pattern.search(text)), None)
