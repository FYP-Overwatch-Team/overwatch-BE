from pathlib import Path

from app.parsing import ast_service

FIXTURES = Path(__file__).parent.parent / "regression" / "fixtures"


def by_path(parses):
    return {p.path: p for p in parses}


def test_python_fixture_extraction():
    parsed = by_path(ast_service.parse_repo(FIXTURES / "sample_repo_py"))
    assert set(parsed) == {"main.py", "api/handlers.py", "services/users.py", "utils/helpers.py"}

    handlers = parsed["api/handlers.py"]
    assert handlers.language == "python"
    assert handlers.imports == ["os", "services.users", "utils.helpers"]
    assert {d["name"] for d in handlers.definitions} == {"ApiHandler", "handle_request"}
    assert {d["kind"] for d in handlers.definitions} == {"class", "function"}


def test_typescript_fixture_extraction():
    parsed = by_path(ast_service.parse_repo(FIXTURES / "sample_repo_ts"))
    assert set(parsed) == {"index.ts", "api/server.ts", "services/user.ts", "utils/format.ts"}

    server = parsed["api/server.ts"]
    assert server.language == "typescript"
    assert server.imports == ["express", "../services/user"]
    assert server.definitions == [{"name": "startServer", "kind": "function"}]

    user = parsed["services/user.ts"]
    assert {d["name"] for d in user.definitions} == {"UserService", "getUser"}

    fmt = parsed["utils/format.ts"]
    assert fmt.definitions == [{"name": "fmt", "kind": "function"}]


def test_require_style_imports_detected(tmp_path):
    (tmp_path / "app.js").write_text('const express = require("express");\nconst helper = require("./helper");\n')
    (tmp_path / "helper.js").write_text("module.exports = () => 1;\n")
    parsed = by_path(ast_service.parse_repo(tmp_path))
    assert parsed["app.js"].imports == ["express", "./helper"]


def test_ignore_dirs_skipped(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1;\n")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "b.py").write_text("y = 2\n")

    files = [p.path for p in ast_service.parse_repo(tmp_path)]
    assert files == ["src/a.py"]


def test_gitignore_respected(tmp_path):
    (tmp_path / ".gitignore").write_text("generated/\nsecret.py\n")
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "gen.py").write_text("g = 1\n")
    (tmp_path / "secret.py").write_text("s = 1\n")
    (tmp_path / "keep.py").write_text("k = 1\n")

    files = [p.path for p in ast_service.parse_repo(tmp_path)]
    assert files == ["keep.py"]


def test_unknown_extensions_ignored(tmp_path):
    (tmp_path / "readme.md").write_text("# hi\n")
    (tmp_path / "data.json").write_text("{}\n")
    assert ast_service.parse_repo(tmp_path) == []
