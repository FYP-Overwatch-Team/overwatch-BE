"""Regenerate the knowledge-graph fact snapshots after an intentional change.

    uv run python -m tests.regression.generate_facts_snapshots

Review the diff before committing: these snapshots are what catch silent
drift from a grammar bump or a query edit.
"""

import json
from pathlib import Path

from app.knowledge_graph.discovery import discover, read_source
from app.knowledge_graph.extract import extract

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOTS = [("sample_repo_py", "facts_py.json"), ("sample_repo_ts", "facts_ts.json")]


def build_snapshot(fixture: str) -> dict:
    facts = {}
    for file in discover(FIXTURES / fixture).files:
        source, reason = read_source(file)
        assert source is not None, f"{file.relative_path} unreadable: {reason}"
        facts[file.relative_path] = extract(source).to_dict()
    return facts


def main() -> None:
    for fixture, filename in SNAPSHOTS:
        path = FIXTURES / filename
        path.write_text(
            json.dumps(build_snapshot(fixture), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
