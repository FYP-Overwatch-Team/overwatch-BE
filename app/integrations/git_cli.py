"""Hardened wrapper around the git command line.

Everything that shells out to git goes through here, so the security
properties below hold for the whole application rather than per call site.

* **The token never reaches disk.** The remote is stored without credentials
  and the token is supplied per invocation through ``GIT_CONFIG_*``
  environment entries. git applies those to that process only and never
  writes them to ``.git/config`` — unlike a credential-bearing remote URL,
  which it persists (that is how CVE-shaped token leaks happen in CI images).
* **The token never appears in argv**, so it cannot be read out of the
  process list. ``/proc/<pid>/environ`` is readable only by the owner, argv
  is readable by every local user.
* **Host git configuration is ignored** (``GIT_CONFIG_NOSYSTEM`` and
  ``GIT_CONFIG_GLOBAL``), so no credential helper on the machine can capture
  our token and no local setting can redirect a clone somewhere else.
* **Only the protocol we intend to use is allowed**, and terminal prompts are
  disabled, so a hostile remote cannot talk git into another transport or
  leave a command hanging on a password prompt.
* **Every invocation is bounded by a timeout** and its whole process group is
  killed if it overruns.
* **Failure output is scrubbed** before it is raised or logged.

Requires git >= 2.31 for ``GIT_CONFIG_COUNT``. On anything older the auth
header is simply not applied, so private clones fail to authenticate: the
failure mode is a clear error, never a silent fallback that leaks the token.
"""

import asyncio
import base64
import os
import signal
from collections.abc import Sequence
from pathlib import Path

import structlog

from app.core.config import get_settings

logger = structlog.get_logger("app.git")

GITHUB_HOST_URL = "https://github.com/"

# Marker for the credential style older checkouts were cloned with, and which
# `ensure_clean_remote` scrubs out.
CREDENTIAL_MARKER = "x-access-token:"

REDACTED = "[REDACTED]"


class GitCommandError(RuntimeError):
    """A git invocation failed. The message is already scrubbed of secrets."""

    def __init__(self, command: str, returncode: int, detail: str = ""):
        self.command = command
        self.returncode = returncode
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(f"git {command} failed with exit code {returncode}{suffix}")


def remote_url(repo_full_name: str) -> str:
    """The canonical, credential-free clone URL for a GitHub repository."""
    return f"{GITHUB_HOST_URL}{repo_full_name}.git"


def _auth_environment(token: str) -> dict[str, str]:
    """Config entries that authenticate this invocation, and only this one.

    Scoped to the GitHub host, so the header is not sent anywhere else if a
    response redirects off-site.
    """
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{GITHUB_HOST_URL}.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def build_environment(url: str | None, token: str | None = None) -> dict[str, str]:
    """The complete environment for a git child process.

    Deliberately minimal: git inherits nothing from this process except PATH
    and HOME. `url` is the remote this command may contact; its scheme is the
    only protocol git is allowed to use. Pass None for purely local commands,
    which then get no network protocols at all.
    """
    scheme = ""
    if url:
        scheme = url.split("://", 1)[0] if "://" in url else "file"

    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": scheme,
    }
    if token:
        environment |= _auth_environment(token)
    return environment


def scrub(text: str, token: str | None) -> str:
    """Remove a token and its base64 form from text destined for logs or errors."""
    if not token:
        return text
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return text.replace(token, REDACTED).replace(encoded, REDACTED)


def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """Kill git and the helpers it spawned (git-remote-https holds the socket)."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


async def run(
    args: Sequence[str],
    *,
    url: str | None = None,
    token: str | None = None,
    cwd: Path | None = None,
) -> str:
    """Run one git command and return its stdout.

    Raises GitCommandError on non-zero exit or timeout, with scrubbed detail.
    """
    timeout = get_settings().git_command_timeout_seconds
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        env=build_environment(url, token),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Its own process group, so a timeout can take the helpers down too.
        start_new_session=True,
    )

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        _kill_process_group(process)
        await process.wait()
        logger.warning("git_command_timeout", command=args[0], timeout_seconds=timeout)
        raise GitCommandError(args[0], -1, f"timed out after {timeout}s") from None

    if process.returncode != 0:
        detail = scrub(stderr.decode("utf-8", errors="replace").strip(), token)
        raise GitCommandError(args[0], process.returncode, detail[-500:])

    return stdout.decode("utf-8", errors="replace")


async def clone(repo_full_name: str, token: str, dest: Path, *, depth: int = 1) -> None:
    """Shallow-clone the default branch into `dest`, which must not exist."""
    url = remote_url(repo_full_name)
    # "--" stops a crafted name from being read as an option.
    await run(
        ["clone", "--depth", str(depth), "--single-branch", "--no-tags", "--", url, str(dest)],
        url=url,
        token=token,
    )


async def fetch_and_reset(dest: Path, repo_full_name: str, token: str, *, branch: str | None = None) -> None:
    """Bring an existing checkout to the remote's current tip, discarding local state."""
    url = remote_url(repo_full_name)
    await run(
        ["fetch", "--depth", "1", "--no-tags", "origin", branch or "HEAD"],
        url=url,
        token=token,
        cwd=dest,
    )
    await run(["reset", "--hard", "FETCH_HEAD"], cwd=dest)
    # Leave no untracked or ignored leftovers: later stages hash the working
    # tree to decide what changed, so it has to reflect the commit exactly.
    await run(["clean", "-ffdx"], cwd=dest)


async def current_commit(dest: Path) -> str:
    """The checked-out commit SHA, used to version the graph."""
    return (await run(["rev-parse", "HEAD"], cwd=dest)).strip()


def has_persisted_credentials(dest: Path) -> bool:
    config = dest / ".git" / "config"
    if not config.is_file():
        return False
    text = config.read_text(encoding="utf-8", errors="replace")
    return CREDENTIAL_MARKER in text or "@github.com" in text


async def ensure_clean_remote(dest: Path, repo_full_name: str) -> bool:
    """Strip credentials that an earlier version persisted in `.git/config`.

    Returns True when something was removed. Raises if anything
    credential-shaped survives the rewrite, so the caller can discard the
    checkout instead of trusting it.
    """
    if not has_persisted_credentials(dest):
        return False

    await run(["remote", "set-url", "origin", "--", remote_url(repo_full_name)], cwd=dest)

    if has_persisted_credentials(dest):
        raise GitCommandError("remote set-url", 1, "credentials still present in .git/config")

    logger.warning("git_persisted_credentials_scrubbed", repo=repo_full_name)
    return True
