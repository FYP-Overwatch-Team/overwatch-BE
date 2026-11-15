"""Snapshot regression: exact extraction output for the fixture repositories.

Extraction sits under everything else, so silent drift here (a tree-sitter
grammar bump, an edited query) would quietly change the whole graph. If a
change is intentional, regenerate with

    uv run python -m tests.regression.generate_facts_snapshots

and review the diff.
"""

import json
from pathlib import Path

import pytest

from tests.regression.generate_facts_snapshots import SNAPSHOTS, build_snapshot

pytestmark = pytest.mark.regression

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("fixture,snapshot_file", SNAPSHOTS)
def test_extraction_matches_snapshot(fixture, snapshot_file):
    expected = json.loads((FIXTURES / snapshot_file).read_text(encoding="utf-8"))

    actual = json.loads(json.dumps(build_snapshot(fixture)))  # tuples -> lists

    assert actual == expected, (
        "Extraction drifted from the committed snapshot. If intentional, "
        "regenerate via tests.regression.generate_facts_snapshots and review."
    )


def test_snapshot_covers_every_fixture_file():
    for fixture, snapshot_file in SNAPSHOTS:
        snapshot = json.loads((FIXTURES / snapshot_file).read_text(encoding="utf-8"))
        assert snapshot, f"{snapshot_file} is empty"
        assert all(facts["definitions"] for facts in snapshot.values() if facts["path"] != "index.ts")
