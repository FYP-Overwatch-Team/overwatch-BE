"""Only changed files are re-extracted, and the decision is made by content."""

from pathlib import Path

from app.knowledge_graph.discovery import DiscoveredFile
from app.knowledge_graph.facts import FileFacts
from app.knowledge_graph.sync_plan import merge_facts, plan_sync


def file(path: str) -> DiscoveredFile:
    return DiscoveredFile(
        path=Path("/repo") / path, relative_path=path, language="python", size_bytes=10,
    )


def facts(path: str, content_hash: str = "hash") -> FileFacts:
    return FileFacts(path=path, language="python", content_hash=content_hash)


def test_unchanged_files_are_not_re_extracted():
    plan = plan_sync(
        [file("a.py"), file("b.py")],
        stored_fingerprints={"a.py": "h1", "b.py": "h2"},
        known_hashes={"a.py": "h1", "b.py": "h2"},
    )

    assert plan.to_extract == ()
    assert set(plan.unchanged) == {"a.py", "b.py"}
    assert plan.is_empty is True


def test_changed_and_new_files_are_extracted():
    plan = plan_sync(
        [file("a.py"), file("new.py")],
        stored_fingerprints={"a.py": "old"},
        known_hashes={"a.py": "new", "new.py": "fresh"},
    )

    assert [f.relative_path for f in plan.to_extract] == ["a.py", "new.py"]
    assert plan.unchanged == ()


def test_files_removed_from_the_checkout_are_deleted():
    plan = plan_sync(
        [file("a.py")],
        stored_fingerprints={"a.py": "h1", "gone.py": "h2"},
        known_hashes={"a.py": "h1"},
    )

    assert plan.deleted == ("gone.py",)
    assert plan.is_empty is False


def test_missing_hashes_are_treated_as_changed():
    # The safe direction: at worst we re-extract a file we did not have to.
    plan = plan_sync([file("a.py")], stored_fingerprints={"a.py": "h1"})

    assert [f.relative_path for f in plan.to_extract] == ["a.py"]


def test_first_index_extracts_everything():
    plan = plan_sync([file("a.py"), file("b.py")], stored_fingerprints={})

    assert len(plan.to_extract) == 2
    assert plan.deleted == ()


def test_summary_reports_each_bucket():
    plan = plan_sync(
        [file("a.py"), file("b.py")],
        stored_fingerprints={"a.py": "h1", "gone.py": "h2"},
        known_hashes={"a.py": "h1", "b.py": "h3"},
    )

    assert plan.summary() == {"extract": 1, "unchanged": 1, "deleted": 1}


def test_merge_lays_fresh_facts_over_stored_ones():
    stored = {"a.py": facts("a.py", "old"), "b.py": facts("b.py"), "gone.py": facts("gone.py")}

    merged = merge_facts(stored, [facts("a.py", "new")], deleted=["gone.py"])

    assert set(merged) == {"a.py", "b.py"}
    assert merged["a.py"].content_hash == "new"
