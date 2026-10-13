"""The GitHub token must never reach disk, argv, or an error message."""

import base64
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.integrations import git_cli

TOKEN = "ghs_exampletokenvalue0123456789"
ENCODED_TOKEN = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "README.md").write_text("hello\n")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True, env=env)
    return path


def test_remote_url_carries_no_credentials():
    assert git_cli.remote_url("octocat/hello") == "https://github.com/octocat/hello.git"


def test_token_is_passed_only_as_a_scoped_config_entry():
    env = git_cli.build_environment(git_cli.remote_url("octocat/hello"), TOKEN)

    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == f"Authorization: Basic {ENCODED_TOKEN}"
    # The raw token appears nowhere, in any variable.
    assert not any(TOKEN in value for value in env.values())


def test_environment_is_hardened_against_host_configuration():
    env = git_cli.build_environment(git_cli.remote_url("octocat/hello"))

    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ALLOW_PROTOCOL"] == "https"
    assert "GIT_CONFIG_COUNT" not in env


def test_local_commands_get_no_network_protocol():
    assert git_cli.build_environment(None)["GIT_ALLOW_PROTOCOL"] == ""


def test_scrub_removes_the_token_in_both_forms():
    message = f"fatal: auth failed for {TOKEN} using {ENCODED_TOKEN}"
    scrubbed = git_cli.scrub(message, TOKEN)

    assert TOKEN not in scrubbed
    assert ENCODED_TOKEN not in scrubbed
    assert scrubbed.count(git_cli.REDACTED) == 2


async def test_clone_never_places_the_token_in_arguments(monkeypatch):
    captured: dict = {}

    async def fake_run(args, *, url=None, token=None, cwd=None):
        captured.update(args=list(args), url=url, token=token)
        return ""

    monkeypatch.setattr(git_cli, "run", fake_run)
    await git_cli.clone("octocat/hello", TOKEN, Path("/tmp/checkout"))

    assert TOKEN not in " ".join(captured["args"])
    assert captured["token"] == TOKEN  # supplied through the environment instead
    assert "--" in captured["args"]  # a name starting with "-" cannot become a flag


async def test_fetch_sends_the_token_only_to_the_network_step(monkeypatch):
    calls: list[dict] = []

    async def fake_run(args, *, url=None, token=None, cwd=None):
        calls.append({"args": list(args), "url": url, "token": token})
        return ""

    monkeypatch.setattr(git_cli, "run", fake_run)
    await git_cli.fetch_and_reset(Path("/tmp/checkout"), "octocat/hello", TOKEN, branch="main")

    commands = [call["args"][0] for call in calls]
    assert commands == ["fetch", "reset", "clean"]
    # Only the fetch talks to GitHub; the local steps get neither token nor URL.
    assert calls[0]["token"] == TOKEN and calls[0]["url"] == git_cli.remote_url("octocat/hello")
    assert all(call["token"] is None and call["url"] is None for call in calls[1:])


@needs_git
async def test_clone_writes_no_credentials_into_git_config(tmp_path):
    source = _make_git_repo(tmp_path / "source")
    dest = tmp_path / "checkout"

    await git_cli.run(["clone", "-q", "--", str(source), str(dest)], url=str(source))

    assert not git_cli.has_persisted_credentials(dest)


@needs_git
async def test_ensure_clean_remote_strips_credentials_left_by_older_versions(tmp_path):
    source = _make_git_repo(tmp_path / "source")
    dest = tmp_path / "checkout"
    await git_cli.run(["clone", "-q", "--", str(source), str(dest)], url=str(source))

    # Reproduce what the previous implementation persisted.
    await git_cli.run(
        ["remote", "set-url", "origin", f"https://x-access-token:{TOKEN}@github.com/o/r.git"],
        cwd=dest,
    )
    assert git_cli.has_persisted_credentials(dest)

    assert await git_cli.ensure_clean_remote(dest, "o/r") is True

    config = (dest / ".git" / "config").read_text()
    assert TOKEN not in config
    assert not git_cli.has_persisted_credentials(dest)
    # A second pass has nothing left to do.
    assert await git_cli.ensure_clean_remote(dest, "o/r") is False


@needs_git
async def test_failed_command_raises_without_leaking_the_token(tmp_path):
    with pytest.raises(git_cli.GitCommandError) as excinfo:
        await git_cli.run(["rev-parse", "HEAD"], cwd=tmp_path, token=TOKEN)

    assert TOKEN not in str(excinfo.value)
