"""Repository identity and on-disk location.

`repo_full_name` arrives from request bodies and webhook payloads and is then
used to build filesystem paths, so it is validated here once instead of being
trusted at each call site. Everything that turns a repository name into a path
goes through `checkout_dir`.
"""

import re
from pathlib import Path

from app.core.exceptions import AppError

# GitHub's own rules: owner is 1-39 characters of [A-Za-z0-9-]; repository is
# 1-100 characters of [A-Za-z0-9._-]. Stricter than GitHub in one respect:
# ".." is rejected outright, because these names become path segments.
REPO_FULL_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$"
_REPO_FULL_NAME_RE = re.compile(REPO_FULL_NAME_PATTERN)


class InvalidRepoName(AppError):
    status_code = 400
    error_code = "invalid_repo_name"


def validate_repo_full_name(repo_full_name: str) -> str:
    if not _REPO_FULL_NAME_RE.match(repo_full_name or "") or ".." in repo_full_name:
        raise InvalidRepoName("repository name must be a valid owner/name pair")
    return repo_full_name


def checkout_dir(base_dir: Path, repo_full_name: str) -> Path:
    """Where a repository is checked out: always a direct child of base_dir."""
    validate_repo_full_name(repo_full_name)
    path = (base_dir / repo_full_name.replace("/", "__")).resolve()
    if path.parent != base_dir.resolve():
        raise InvalidRepoName("repository checkout path escapes the repositories directory")
    return path
