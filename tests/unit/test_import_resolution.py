"""Import specifiers become files, packages, or nothing — never a guess."""

import pytest

from app.knowledge_graph.link.imports import TargetKind, npm_package_name, resolve_import
from app.knowledge_graph.link.module_index import ModuleIndex
from app.knowledge_graph.project_config import ProjectConfig

JS_FILES = [
    "app/page.tsx",
    "features/landing/index.tsx",
    "features/landing/components/Story.tsx",
    "lib/utils.ts",
    "lib/http/index.ts",
    "src/components/Button.tsx",
]
PY_FILES = [
    "main.py",
    "api/handlers.py",
    "api/__init__.py",
    "services/users.py",
    "src/pkg/thing.py",
]


def resolve(importer: str, specifier: str, files=None, config=None):
    return resolve_import(
        importer,
        specifier,
        ModuleIndex(files if files is not None else JS_FILES + PY_FILES),
        config or ProjectConfig(),
    )


# -- JavaScript / TypeScript ----------------------------------------------


@pytest.mark.parametrize(
    "importer,specifier,expected",
    [
        ("app/page.tsx", "../lib/utils", "lib/utils.ts"),
        ("features/landing/index.tsx", "./components/Story", "features/landing/components/Story.tsx"),
        ("app/page.tsx", "../features/landing", "features/landing/index.tsx"),  # directory index
        ("app/page.tsx", "../lib/http", "lib/http/index.ts"),
        ("app/page.tsx", "../lib/utils.ts", "lib/utils.ts"),  # explicit extension
    ],
)
def test_relative_imports_resolve_to_files(importer, specifier, expected):
    target = resolve(importer, specifier)

    assert target.kind is TargetKind.FILE
    assert target.value == expected


def test_alias_imports_resolve_through_tsconfig_paths():
    # The gap this fixes: today "@/lib/utils" is misfiled as a package.
    config = ProjectConfig(ts_base_url="", ts_paths={"@/*": ("./*",)})

    target = resolve("app/page.tsx", "@/lib/utils", config=config)

    assert target.kind is TargetKind.FILE
    assert target.value == "lib/utils.ts"


def test_alias_resolution_honours_base_url():
    config = ProjectConfig(ts_base_url="src", ts_paths={"~/*": ("./components/*",)})

    target = resolve("app/page.tsx", "~/Button", config=config)

    assert target.value == "src/components/Button.tsx"


def test_the_longest_matching_alias_wins():
    config = ProjectConfig(
        ts_paths={"@/*": ("./src/*",), "@/components/*": ("./src/components/*",)},
    )

    target = resolve("app/page.tsx", "@/components/Button", config=config)

    assert target.value == "src/components/Button.tsx"


def test_alias_falls_through_to_package_when_no_file_matches():
    config = ProjectConfig(ts_paths={"@/*": ("./*",)})

    target = resolve("app/page.tsx", "@/does/not/exist", config=config)

    assert target.kind is TargetKind.PACKAGE


def test_base_url_allows_non_relative_internal_imports():
    config = ProjectConfig(ts_base_url="src")

    target = resolve("app/page.tsx", "components/Button", config=config)

    assert target.value == "src/components/Button.tsx"


@pytest.mark.parametrize(
    "specifier,package",
    [
        ("react", "react"),
        ("lodash/merge", "lodash"),
        ("@xyflow/react", "@xyflow/react"),
        ("@radix-ui/react-dialog/dist/index", "@radix-ui/react-dialog"),
        ("node:fs", "fs"),
    ],
)
def test_package_specifiers_reduce_to_a_distribution_name(specifier, package):
    target = resolve("app/page.tsx", specifier)

    assert target.kind is TargetKind.PACKAGE
    assert target.value == package
    assert npm_package_name(specifier) == package


def test_a_relative_import_of_a_missing_file_is_unresolved_not_invented():
    target = resolve("app/page.tsx", "./nope")

    assert target.kind is TargetKind.UNRESOLVED


@pytest.mark.parametrize(
    "specifier",
    ["../../../../etc/passwd", "./../../outside", "/etc/passwd", "../.."],
)
def test_specifiers_cannot_escape_the_repository(specifier):
    target = resolve("app/page.tsx", specifier)

    assert target.kind is not TargetKind.FILE


# -- Python ----------------------------------------------------------------


def test_absolute_python_imports_resolve_against_roots():
    target = resolve("main.py", "services.users")

    assert target.kind is TargetKind.FILE
    assert target.value == "services/users.py"


def test_package_imports_resolve_to_init_files():
    target = resolve("main.py", "api")

    assert target.value == "api/__init__.py"


def test_src_layout_is_honoured():
    config = ProjectConfig(python_roots=("", "src"))

    target = resolve("main.py", "pkg.thing", config=config)

    assert target.value == "src/pkg/thing.py"


@pytest.mark.parametrize(
    "importer,specifier,expected",
    [
        ("api/handlers.py", ".", "api/__init__.py"),
        ("api/handlers.py", "..services.users", "services/users.py"),
        ("api/handlers.py", "...services.users", None),  # climbs past the root
    ],
)
def test_relative_python_imports(importer, specifier, expected):
    target = resolve(importer, specifier)

    if expected is None:
        assert target.kind is TargetKind.UNRESOLVED
    else:
        assert target.value == expected


def test_third_party_python_imports_become_packages():
    target = resolve("main.py", "google.genai")

    assert target.kind is TargetKind.PACKAGE
    assert target.value == "google"


def test_empty_specifier_is_unresolved():
    assert resolve("main.py", "   ").kind is TargetKind.UNRESOLVED
