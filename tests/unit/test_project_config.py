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


def test_finds_the_repositorys_own_packages_and_nested_tsconfigs(tmp_path):
    write(tmp_path, "package.json", '{ "name": "acme-monorepo" }')
    write(tmp_path, "packages/features/package.json", """
    { "name": "@acme/features",
      "exports": { "./auth/signup": { "types": "./src/signup.tsx", "default": "./dist/signup.js" }, "./*": "./src/*.ts" } }
    """)
    write(tmp_path, "packages/ui/package.json", '{ "name": "@acme/ui", "main": "./dist/index.js", "types": "./src/index.ts" }')
    write(tmp_path, "packages/ui/node_modules/dep/package.json", '{ "name": "dep" }')
    write(tmp_path, "apps/web/tsconfig.json", '{ "compilerOptions": { "paths": { "@/*": ["./*"] } } }')

    config = project_config.load(tmp_path)

    assert set(config.workspace_packages) == {"acme-monorepo", "@acme/features", "@acme/ui"}
    features = config.workspace_packages["@acme/features"]
    assert features.dir == "packages/features"
    assert features.exports == {"./auth/signup": ("./src/signup.tsx", "./dist/signup.js"), "./*": ("./src/*.ts",)}
    assert config.workspace_packages["@acme/ui"].entry == ("./src/index.ts", "./dist/index.js")
    assert [(s.dir, s.base_url, dict(s.paths)) for s in config.ts_scopes] == [("apps/web", None, {"@/*": ("./*",)})]


def test_an_inherited_base_url_stays_relative_to_the_tsconfig_declaring_it(tmp_path):
    write(tmp_path, "tsconfig.json", '{ "compilerOptions": { "baseUrl": ".", "paths": { "@shared/*": ["packages/shared/*"] } } }')
    write(tmp_path, "apps/web/tsconfig.json", '{ "extends": "../../tsconfig.json" }')

    (scope,) = project_config.load(tmp_path).ts_scopes

    assert (scope.dir, scope.base_url, dict(scope.paths)) == ("apps/web", "", {"@shared/*": ("packages/shared/*",)})


def test_inherited_paths_without_a_base_url_stay_relative_to_the_tsconfig_declaring_them(tmp_path):
    write(tmp_path, "tsconfig.base.json", '{ "compilerOptions": { "paths": { "@shared/*": ["./packages/shared/*"] } } }')
    write(tmp_path, "apps/web/tsconfig.json", '{ "extends": "../../tsconfig.base.json" }')

    (scope,) = project_config.load(tmp_path).ts_scopes

    assert (scope.base_url, dict(scope.paths)) == (None, {"@shared/*": ("../../packages/shared/*",)})


def test_a_package_json_without_a_name_or_unreadable_is_skipped(tmp_path):
    write(tmp_path, "a/package.json", '{ "private": true }')
    write(tmp_path, "b/package.json", "not json")

    assert project_config.load(tmp_path).workspace_packages == {}
