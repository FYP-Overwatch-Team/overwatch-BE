"""Snapshot regression: exact parse + graph output for the fixture repos.

Catches silent drift from tree-sitter version bumps or extraction changes.
If a change is intentional, regenerate with:
    uv run python -m tests.regression.generate_snapshots
and review the diff carefully before committing.
"""

import json
from pathlib import Path

import pytest

from app.parsing import ast_service
from app.parsing.graph_builder import graph_snapshot

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


def build(fixture: str, repo_name: str) -> dict:
    parses = ast_service.parse_repo(FIXTURES / fixture)
    return graph_snapshot(repo_name, parses)


@pytest.mark.parametrize("fixture,repo_name,snapshot_file", [
    ("sample_repo_py", "fixture/py", "snapshot_py.json"),
    ("sample_repo_ts", "fixture/ts", "snapshot_ts.json"),
])
def test_parse_output_matches_snapshot(fixture, repo_name, snapshot_file):
    expected = json.loads((FIXTURES / snapshot_file).read_text(encoding="utf-8"))
    actual = build(fixture, repo_name)
    assert actual == expected, (
        "Parse output drifted from the committed snapshot. If intentional, "
        "regenerate via tests.regression.generate_snapshots and review the diff."
    )
