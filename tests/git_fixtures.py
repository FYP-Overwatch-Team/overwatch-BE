"""Real git checkouts for tests that read code at a commit."""

import os
import subprocess
from pathlib import Path


def commit_all(root: Path, message: str = "test") -> str:
    """Commit everything under `root` (a repo is created on first use) and return the commit's SHA."""
    # nothing inherited: a GIT_DIR from a hook would otherwise point these commands at the real repo
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env |= {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false"]

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *identity, *args], cwd=root, env=env, check=True, capture_output=True, text=True,
        ).stdout.strip()

    if not (root / ".git").exists():
        git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "--allow-empty", "-m", message)
    return git("rev-parse", "HEAD")
