import shutil
from pathlib import Path

from app.db import mongo
from app.parsing.graph_builder import node_id
from app.workers import jobs
from tests.integration.test_auth_flow import login

FIXTURES = Path(__file__).parent.parent / "regression" / "fixtures"
REPO = "octocat/hello-world"


async def authed(client) -> dict:
    access = await login(client)
    return {"Authorization": f"Bearer {access}"}


async def seed_connected_repo(user_id: str, parse_status: str = "pending") -> None:
    await mongo.repos().insert_one({
        "user_id": user_id,
        "repo_full_name": REPO,
        "default_branch": "main",
        "webhook_status": "created",
        "parse_status": parse_status,
        "parse_error": None,
        "connected_at": 1,
        "updated_at": 1,
    })


def stage_workdir(monkeypatch, tmp_path, fixture: str = "sample_repo_py") -> None:
    monkeypatch.setenv("REPOS_DIR", str(tmp_path))
    from app.core.config import get_settings
    get_settings.cache_clear()
    shutil.copytree(FIXTURES / fixture, tmp_path / REPO.replace("/", "__"))


async def test_initial_parse_job_builds_graph(client, graph_repo, monkeypatch, tmp_path):
    headers = await authed(client)
    user = await mongo.users().find_one({})
    await seed_connected_repo(user["_id"])
    stage_workdir(monkeypatch, tmp_path)

    await jobs.run_initial_parse(user["_id"], REPO)

    doc = await mongo.repos().find_one({"repo_full_name": REPO})
    assert doc["parse_status"] == "done"

    resp = await client.get(f"/graph?repo_full_name={REPO}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["parse_status"] == "done"
    names = [n["name"] for n in body["nodes"]]
    assert "api" in names and "services" in names and "utils" in names
    assert any(e["type"] == "DEPENDS_ON" for e in body["edges"])


async def test_parse_lock_skips_overlapping_run(client, graph_repo, monkeypatch, tmp_path):
    await authed(client)
    user = await mongo.users().find_one({})
    await seed_connected_repo(user["_id"], parse_status="in_progress")
    stage_workdir(monkeypatch, tmp_path)

    await jobs.run_initial_parse(user["_id"], REPO)

    doc = await mongo.repos().find_one({"repo_full_name": REPO})
    assert doc["parse_status"] == "in_progress"  # untouched: the running parse owns it
    assert await graph_repo.fetch_graph(REPO) == {"nodes": [], "edges": []}


async def test_parse_failure_recorded(client, monkeypatch, tmp_path):
    await authed(client)
    user = await mongo.users().find_one({})
    await seed_connected_repo(user["_id"])
    monkeypatch.setenv("REPOS_DIR", str(tmp_path / "missing"))
    from app.core.config import get_settings
    get_settings.cache_clear()

    await jobs.run_initial_parse(user["_id"], REPO)

    doc = await mongo.repos().find_one({"repo_full_name": REPO})
    assert doc["parse_status"] == "failed"
    assert doc["parse_error"]


async def test_graph_endpoint_404_for_unconnected_repo(client):
    headers = await authed(client)
    resp = await client.get("/graph?repo_full_name=nobody/nothing", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "repo_not_connected"


async def test_reparse_removes_stale_nodes(client, graph_repo):
    """Re-parses MERGE on stable ids: changed content updates, removed dirs disappear."""
    graph_v1 = {
        "nodes": [
            {"id": node_id(REPO, "api"), "name": "api", "kind": "module", "file_count": 1, "definition_count": 0},
            {"id": node_id(REPO, "legacy"), "name": "legacy", "kind": "module", "file_count": 1, "definition_count": 0},
        ],
        "edges": [],
    }
    await graph_repo.upsert_graph(REPO, graph_v1)

    graph_v2 = {
        "nodes": [
            {"id": node_id(REPO, "api"), "name": "api", "kind": "module", "file_count": 3, "definition_count": 2},
        ],
        "edges": [],
    }
    await graph_repo.upsert_graph(REPO, graph_v2)

    result = await graph_repo.fetch_graph(REPO)
    assert [n["id"] for n in result["nodes"]] == [node_id(REPO, "api")]
    assert result["nodes"][0]["file_count"] == 3
