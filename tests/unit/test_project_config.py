"""tsconfig aliases are the fix for the biggest gap in today's graph, and the
files they come from are untrusted repository input."""

from pathlib import Path

from app.knowledge_graph import project_config


def write(root: Path, relative_path: str, content: str) -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_reads_the_alias_table_that_today_is_ignored(tmp_path):
    write(tmp_path, "tsconfig.json", """
    {
      "compilerOptions": {
        "baseUrl": ".",
        "paths": { "@/*": ["./*"], "~lib/*": ["src/lib/*", "vendor/lib/*"] }
      }
    }
    """)

    config = project_config.load(tmp_path)

    assert config.ts_base_url == ""
    assert config.ts_paths["@/*"] == ("./*",)
    assert config.ts_paths["~lib/*"] == ("src/lib/*", "vendor/lib/*")


def test_tolerates_comments_and_trailing_commas(tmp_path):
    write(tmp_path, "tsconfig.json", """
    {
      // Next.js writes this file with comments
      "compilerOptions": {
        /* block comment */
        "baseUrl": "./src",
        "paths": { "@/*": ["./*"], },
      },
    }
    """)

    config = project_config.load(tmp_path)

    assert config.ts_base_url == "src"
    assert config.ts_paths == {"@/*": ("./*",)}


def test_does_not_strip_comment_markers_inside_strings(tmp_path):
    write(tmp_path, "tsconfig.json", '{"compilerOptions": {"paths": {"@//*": ["./*"]}}}')

    config = project_config.load(tmp_path)

    assert config.ts_paths == {"@//*": ("./*",)}


def test_merges_an_extends_chain_with_the_child_winning(tmp_path):
    write(tmp_path, "base/tsconfig.base.json", """
    {"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./base/*"], "@shared/*": ["./shared/*"]}}}
    """)
    write(tmp_path, "tsconfig.json", """
    {"extends": "./base/tsconfig.base.json",
     "compilerOptions": {"paths": {"@/*": ["./src/*"]}}}
    """)

    config = project_config.load(tmp_path)

    assert config.ts_paths["@/*"] == ("./src/*",)  # child overrides
    assert "@shared/*" not in config.ts_paths  # TS replaces `paths` wholesale


def test_ignores_an_extends_pointing_outside_the_repository(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.json").write_text('{"compilerOptions": {"paths": {"@/*": ["/etc/*"]}}}')
    repo = tmp_path / "repo"
    write(repo, "tsconfig.json", '{"extends": "../outside/evil.json", "compilerOptions": {}}')

    config = project_config.load(repo)

    assert config.ts_paths == {}


def test_ignores_package_style_extends(tmp_path):
    write(tmp_path, "tsconfig.json", '{"extends": "@tsconfig/node20/tsconfig.json", "compilerOptions": {"paths": {"@/*": ["./src/*"]}}}')

    config = project_config.load(tmp_path)

    assert config.ts_paths == {"@/*": ("./src/*",)}


def test_survives_a_malformed_config(tmp_path):
    write(tmp_path, "tsconfig.json", "{ this is not json ")

    config = project_config.load(tmp_path)

    assert config.ts_paths == {}
    assert config.ts_base_url == ""


def test_falls_back_to_jsconfig(tmp_path):
    write(tmp_path, "jsconfig.json", '{"compilerOptions": {"paths": {"@/*": ["./*"]}}}')

    assert project_config.load(tmp_path).ts_paths == {"@/*": ("./*",)}


def test_python_roots_default_to_the_repository_root(tmp_path):
    assert project_config.load(tmp_path).python_roots == ("",)


def test_python_roots_include_a_src_layout(tmp_path):
    (tmp_path / "src").mkdir()

    assert project_config.load(tmp_path).python_roots == ("", "src")


def test_python_roots_read_pyproject_package_directories(tmp_path):
    write(tmp_path, "pyproject.toml", """
    [tool.poetry]
    name = "thing"
    [[tool.poetry.packages]]
    include = "thing"
    from = "lib"
    """)

    assert project_config.load(tmp_path).python_roots == ("", "lib")


def test_oversized_config_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(project_config, "MAX_CONFIG_BYTES", 10)
    write(tmp_path, "tsconfig.json", '{"compilerOptions": {"paths": {"@/*": ["./*"]}}}')

    assert project_config.load(tmp_path).ts_paths == {}
