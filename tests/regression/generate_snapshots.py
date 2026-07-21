"""Regenerate the parse snapshots after an intentional extraction change."""

import json
from pathlib import Path

from app.parsing import ast_service
from app.parsing.graph_builder import graph_snapshot

FIXTURES = Path(__file__).parent / "fixtures"


def main() -> None:
    for fixture, repo_name, out in [
        ("sample_repo_py", "fixture/py", "snapshot_py.json"),
        ("sample_repo_ts", "fixture/ts", "snapshot_ts.json"),
    ]:
        parses = ast_service.parse_repo(FIXTURES / fixture)
        snapshot = graph_snapshot(repo_name, parses)
        path = FIXTURES / out
        path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
