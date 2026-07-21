"""Regression pin: a fixed diff must change only the affected nodes/edges."""

import copy
import shutil
from pathlib import Path

import pytest

from app.db import mongo
from app.parsing.graph_builder import node_id
from app.services import repo_service
from app.workers import jobs

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"
REPO = "octocat/hello-world"
USER = "user-1"


@pytest.fixture
async def staged_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("REPOS_DIR", str(tmp_path))
    from app.core.config import get_settings
    get_settings.cache_clear()

    workdir = tmp_path / REPO.replace("/", "__")
    shutil.copytree(FIXTURES / "sample_repo_py", workdir)

    await mongo.repos().insert_one({
        "user_id": USER, "repo_full_name": REPO, "default_branch": "main",
        "webhook_status": "created", "parse_status": "pending",
        "parse_error": None, "connected_at": 1, "updated_at": 1,
    })

    async def _fake_token(user_id):
        return "gho_test"

    async def _fake_update(repo_full_name, token):
        return workdir

    monkeypatch.setattr(repo_service, "get_github_token", _fake_token)
    monkeypatch.setattr(repo_service, "update_workdir", _fake_update)
    return workdir


async def test_fixed_diff_changes_only_affected_nodes(staged_repo, graph_repo):
    await jobs.run_initial_parse(USER, REPO)
    before = copy.deepcopy(await graph_repo.fetch_graph(REPO))

    # the fixed diff: utils/helpers.py gains an external import
    (staged_repo / "utils" / "helpers.py").write_text(
        "import re\n\n\ndef fmt(value):\n    return re.sub(r'\\s+', ' ', str(value)).strip()\n",
        encoding="utf-8",
    )
    await jobs.run_incremental_reparse(USER, REPO, ["utils/helpers.py"], [])
    after = await graph_repo.fetch_graph(REPO)

    utils_id = node_id(REPO, "utils")
    external_id = node_id(REPO, "__external__")

    # every node except utils/external is byte-identical
    before_nodes = {n["id"]: n for n in before["nodes"]}
    after_nodes = {n["id"]: n for n in after["nodes"]}
    assert set(after_nodes) == set(before_nodes)
    for nid in before_nodes:
        if nid != utils_id:
            assert after_nodes[nid] == before_nodes[nid]

    # exactly one new edge: utils -> external
    new_edges = [e for e in after["edges"] if e not in before["edges"]]
    assert new_edges == [{
        "source": utils_id, "target": external_id,
        "type": "EXTERNAL_DEPENDENCY", "weight": 1,
    }]
    assert [e for e in before["edges"] if e not in after["edges"]] == []

    doc = await mongo.repos().find_one({"repo_full_name": REPO})
    assert doc["parse_status"] == "done"


async def test_removed_file_drops_its_contribution(staged_repo, graph_repo):
    await jobs.run_initial_parse(USER, REPO)

    (staged_repo / "main.py").unlink()
    await jobs.run_incremental_reparse(USER, REPO, [], ["main.py"])
    after = await graph_repo.fetch_graph(REPO)

    assert node_id(REPO, "root") not in {n["id"] for n in after["nodes"]}
    assert all(e["source"] != node_id(REPO, "root") for e in after["edges"])


async def test_incremental_skipped_while_parse_in_progress(staged_repo, graph_repo):
    await jobs.run_initial_parse(USER, REPO)
    before = await graph_repo.fetch_graph(REPO)

    await mongo.repos().update_one(
        {"repo_full_name": REPO}, {"$set": {"parse_status": "in_progress"}},
    )
    await jobs.run_incremental_reparse(USER, REPO, ["utils/helpers.py"], [])

    assert await graph_repo.fetch_graph(REPO) == before  # nothing touched
