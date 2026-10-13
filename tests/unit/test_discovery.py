"""Discovery decides what a repository may show us. It must stay inside the
checkout and refuse unbounded input."""

from dataclasses import replace
from pathlib import Path

import pytest

from app.knowledge_graph.discovery import (
    DiscoveredFile,
    detect_language,
    discover,
    read_source,
    resolve_within,
)
from app.knowledge_graph.limits import DEFAULT_LIMITS, SkipReason


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    for relative_path, content in files.items():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    repo.mkdir(exist_ok=True)
    return repo


def relative_paths(result) -> list[str]:
    return [file.relative_path for file in result.files]


def test_finds_supported_languages_in_a_stable_order(tmp_path):
    repo = make_repo(tmp_path, {
        "b/second.ts": "export const b = 1;\n",
        "a/first.py": "x = 1\n",
        "app.tsx": "export default function App() { return null; }\n",
        "notes.md": "# not source\n",
    })

    result = discover(repo)

    assert relative_paths(result) == ["a/first.py", "app.tsx", "b/second.ts"]


def test_skips_ignored_directories_and_generated_output(tmp_path):
    repo = make_repo(tmp_path, {
        "src/app.ts": "export const a = 1;\n",
        "node_modules/pkg/index.js": "module.exports = {};\n",
        "dist/bundle.min.js": "var a=1;\n",
        "types/api.d.ts": "export type A = string;\n",
    })

    result = discover(repo)

    assert relative_paths(result) == ["src/app.ts"]
    assert result.skipped[SkipReason.IGNORED_DIRECTORY] >= 1
    assert result.skipped[SkipReason.GENERATED] >= 1


def test_honours_gitignore(tmp_path):
    repo = make_repo(tmp_path, {
        ".gitignore": "secret/\n*.generated.py\n",
        "app.py": "x = 1\n",
        "secret/keys.py": "KEY = 'x'\n",
        "thing.generated.py": "y = 2\n",
    })

    result = discover(repo)

    assert relative_paths(result) == ["app.py"]
    assert result.skipped[SkipReason.GITIGNORED] == 2


def test_never_follows_symlinks_out_of_the_checkout(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("SECRET = 1\n")
    repo = make_repo(tmp_path, {"app.py": "x = 1\n"})
    (repo / "linked.py").symlink_to(outside / "secret.py")
    (repo / "linked_dir").symlink_to(outside, target_is_directory=True)

    result = discover(repo)

    assert relative_paths(result) == ["app.py"]
    assert result.skipped[SkipReason.SYMLINK] == 2


def test_rejects_files_over_the_size_cap(tmp_path):
    repo = make_repo(tmp_path, {"small.py": "x = 1\n", "huge.py": "#" * 1024})
    limits = replace(DEFAULT_LIMITS, max_file_bytes=100)

    result = discover(repo, limits)

    assert relative_paths(result) == ["small.py"]
    assert result.skipped[SkipReason.TOO_LARGE] == 1


def test_stops_at_the_repository_file_cap(tmp_path):
    repo = make_repo(tmp_path, {f"file{i}.py": "x = 1\n" for i in range(10)})
    limits = replace(DEFAULT_LIMITS, max_files=3)

    result = discover(repo, limits)

    assert len(result.files) == 3
    assert result.capped is True
    assert result.skipped[SkipReason.REPO_CAP_REACHED] == 1


def test_stops_at_the_total_size_cap(tmp_path):
    repo = make_repo(tmp_path, {f"file{i}.py": "x = 1\n" * 20 for i in range(10)})
    limits = replace(DEFAULT_LIMITS, max_total_bytes=200)

    result = discover(repo, limits)

    assert result.capped is True
    assert result.total_bytes <= 200


@pytest.mark.parametrize(
    "relative_path,expected",
    [("a.py", "python"), ("a.ts", "typescript"), ("a.tsx", "tsx"),
     ("a.jsx", "javascript"), ("a.mjs", "javascript"), ("a.rs", None)],
)
def test_language_detection(relative_path, expected):
    assert detect_language(relative_path) == expected


def test_read_source_hashes_content(tmp_path):
    repo = make_repo(tmp_path, {"app.py": "x = 1\n"})
    file = discover(repo).files[0]

    source, reason = read_source(file)

    assert reason is None
    assert source.source == b"x = 1\n"
    assert source.loc == 2
    assert len(source.content_hash) == 64  # sha256 hex


def test_read_source_rejects_minified_and_binary(tmp_path):
    repo = make_repo(tmp_path, {"app.py": "x = 1\n"})
    minified = DiscoveredFile(
        path=repo / "app.py", relative_path="bundle.js", language="javascript", size_bytes=1,
    )
    (repo / "app.py").write_bytes(b"var a=1;" * 1000)  # one very long line
    assert read_source(minified)[1] is SkipReason.MINIFIED

    (repo / "app.py").write_bytes(b"\x00\x01binary")
    assert read_source(minified)[1] is SkipReason.BINARY


def test_read_source_reports_unreadable_files(tmp_path):
    missing = DiscoveredFile(
        path=tmp_path / "gone.py", relative_path="gone.py", language="python", size_bytes=0,
    )

    assert read_source(missing) == (None, SkipReason.UNREADABLE)


def test_resolve_within_accepts_repository_paths(tmp_path):
    repo = make_repo(tmp_path, {"src/app.py": "x = 1\n"})

    assert resolve_within(repo, "src/app.py") == repo.resolve() / "src" / "app.py"


@pytest.mark.parametrize(
    "attempt",
    ["", ".", "..", "../outside.py", "src/../../outside.py", "/etc/passwd"],
)
def test_resolve_within_rejects_escapes(tmp_path, attempt):
    repo = make_repo(tmp_path, {"src/app.py": "x = 1\n"})

    assert resolve_within(repo, attempt) is None


def test_resolve_within_rejects_symlinked_paths(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("SECRET = 1\n")
    repo = make_repo(tmp_path, {"app.py": "x = 1\n"})
    (repo / "linked.py").symlink_to(outside / "secret.py")
    (repo / "linked_dir").symlink_to(outside, target_is_directory=True)

    assert resolve_within(repo, "linked.py") is None
    assert resolve_within(repo, "linked_dir/secret.py") is None
