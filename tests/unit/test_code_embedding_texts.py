"""What of a repository is embedded by meaning, and the text each vector is made from."""

import pytest

from app.embeddings import code
from app.knowledge_graph.build.ids import definition_ids, symbol_id
from app.knowledge_graph.facts import Definition, FileFacts, RouteDef

REPO = "octocat/hello-world"


def definition(name: str, kind: str = "function", start: int = 1, end: int = 10, **fields) -> Definition:
    return Definition(name=name, kind=kind, qualified_name=fields.pop("qualified_name", name), start_line=start, end_line=end, **fields)


def facts(path: str, *definitions: Definition, routes: tuple[RouteDef, ...] = ()) -> FileFacts:
    return FileFacts(path=path, language="python", content_hash=f"hash-{path}", definitions=definitions, routes=routes)


@pytest.mark.parametrize("path", [
    "tests/test_api.py", "app/tests/helpers.py", "api/users_test.py", "conftest.py", "web/__tests__/form.tsx",
    "web/form.test.tsx", "web/form.spec.ts", "web/button.stories.tsx", "web/__mocks__/api.ts", "spec/models.rb",
])
def test_tests_are_recognised_by_folder_or_name(path):
    assert code.is_test_path(path)


@pytest.mark.parametrize("path", ["app/testing.py", "app/contest.py", "web/latest.tsx", "docs/testing.md"])
def test_ordinary_files_are_not_tests(path):
    assert not code.is_test_path(path)


def test_files_come_first_then_routes_shapes_exported_and_the_rest_by_size():
    found = code.candidates(REPO, {
        "api/users.py": facts(
            "api/users.py",
            definition("helper", start=1, end=5),
            definition("long_helper", start=10, end=40),
            definition("list_users", start=50, end=52),
            definition("User", kind="class", start=60, end=61),
            definition("public", start=70, end=75, exported=True),
            routes=(RouteDef("GET", "/users", "fastapi", handler="list_users"),),
        ),
        "web/page.tsx": facts("web/page.tsx"),
    }, include_symbols=True)

    assert [candidate.definition.name if candidate.definition else candidate.path for candidate in found] == [
        "api/users.py", "web/page.tsx", "list_users", "User", "public", "long_helper", "helper",
    ]
    assert found[0].ref_id == f"{REPO}:api/users.py"
    assert found[2].ref_id == symbol_id(REPO, "api/users.py", "list_users")


def test_tests_constants_and_tiny_functions_get_no_vector():
    found = code.candidates(REPO, {
        "tests/test_users.py": facts("tests/test_users.py", definition("test_lists", start=1, end=30)),
        "api/users.py": facts(
            "api/users.py",
            definition("TIMEOUT", kind="constant", start=1, end=1),
            definition("tiny", start=3, end=5),
            definition("Settings", kind="type", start=7, end=7),
        ),
    }, include_symbols=True)

    assert [(candidate.path, candidate.definition and candidate.definition.name) for candidate in found] == [
        ("api/users.py", None), ("api/users.py", "Settings"),
    ]


def test_only_files_when_the_graph_left_out_its_functions():
    found = code.candidates(REPO, {"api/users.py": facts("api/users.py", definition("long", start=1, end=50))}, include_symbols=False)

    assert [candidate.definition for candidate in found] == [None]


def test_definition_ids_number_repeated_names_like_the_graph_does():
    first, second = definition("helper"), definition("helper", start=20, end=30)

    ids = definition_ids(REPO, "api/a.py", [first, second])

    assert ids == [(first, symbol_id(REPO, "api/a.py", "helper")), (second, symbol_id(REPO, "api/a.py", "helper", 2))]


def test_a_definitions_text_is_its_place_name_signature_docstring_and_first_lines():
    lines = [f"line {n}" for n in range(1, 101)]
    found = definition(
        "refresh", kind="method", start=3, end=90, qualified_name="Tokens.refresh",
        signature="refresh(self, token)", docstring="Swap an old token for a new one.",
    )

    text = code.symbol_text("auth/tokens.py", found, lines)

    assert text.splitlines()[:5] == [
        "auth/tokens.py", "method Tokens.refresh", "refresh(self, token)", "Swap an old token for a new one.", "line 3",
    ]
    assert "line 42" in text and "line 43" not in text
    assert len(code.symbol_text("a.py", definition("big", start=1, end=40), ["x" * 500] * 40)) == code.MAX_CODE_CHARS


def test_credentials_in_code_are_redacted_before_embedding():
    text = code.symbol_text("a.py", definition("connect", start=1, end=2), ['def connect():', '    key = "AKIAIOSFODNN7EXAMPLE"'])

    assert "AKIAIOSFODNN7EXAMPLE" not in text


def test_a_files_text_names_what_it_defines_including_constants():
    file = facts(
        "auth/settings.py",
        definition("PASSWORD_RESET_TIMEOUT", kind="constant", start=1, end=1),
        definition("Settings", kind="class", start=3, end=9, signature="class Settings(BaseModel)"),
        definition("load", kind="method", start=5, end=9, qualified_name="Settings.load", parent="Settings"),
    )

    text = code.file_text("auth/settings.py", file, "PASSWORD_RESET_TIMEOUT = 900\n")

    assert text.splitlines()[:4] == [
        "auth/settings.py", "python", "constant PASSWORD_RESET_TIMEOUT", "class Settings: class Settings(BaseModel)",
    ]
    assert "Settings.load" not in text
    assert text.endswith("PASSWORD_RESET_TIMEOUT = 900\n")
