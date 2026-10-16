import importlib.util
from pathlib import Path

from app.db import mongo, oauth_tokens

spec = importlib.util.spec_from_file_location(
    "migrate_atlassian_credentials", Path(__file__).resolve().parents[2] / "scripts" / "migrate_atlassian_credentials.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


async def test_a_dry_run_changes_nothing():
    await oauth_tokens.save("ws1", "jira", {"access_token": "a"})

    assert await migration.migrate(apply=False) == ["jira tokens to rename: 1", "already moved: 0"]
    assert await oauth_tokens.get("ws1", "jira")


async def test_jira_tokens_become_the_shared_atlassian_token_once():
    await oauth_tokens.save("ws1", "jira", {"access_token": "a"})
    await oauth_tokens.save("u1", "github", {"access_token": "g"})

    await migration.migrate(apply=True)
    again = await migration.migrate(apply=True)

    assert again == ["jira tokens to rename: 0", "already moved: 0"]
    assert await oauth_tokens.get("ws1", "atlassian") and not await oauth_tokens.get("ws1", "jira")
    assert await mongo.oauth_tokens().count_documents({"provider": "github"}) == 1
