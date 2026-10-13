"""Extraction turns syntax into facts. These tests pin the meaning."""

import hashlib
from dataclasses import replace

import pytest

from app.knowledge_graph.discovery import SourceFile
from app.knowledge_graph.extract import UnsupportedLanguage, extract
from app.knowledge_graph.extract.extractor import _compiled_query
from app.knowledge_graph.extract.languages import SUPPORTED_LANGUAGES
from app.knowledge_graph.limits import DEFAULT_LIMITS


def source(code: str, language: str = "python", path: str = "module.py") -> SourceFile:
    data = code.encode()
    return SourceFile(
        relative_path=path,
        language=language,
        content_hash=hashlib.sha256(data).hexdigest(),
        source=data,
        loc=code.count("\n") + 1,
    )


def definition(facts, qualified_name):
    return next(d for d in facts.definitions if d.qualified_name == qualified_name)


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_every_language_query_compiles(language):
    assert _compiled_query(language) is not None


def test_unsupported_language_is_rejected():
    with pytest.raises(UnsupportedLanguage):
        extract(source("fn main() {}", language="rust", path="main.rs"))


# -- Python ----------------------------------------------------------------


PYTHON_SAMPLE = '''
import os
from services.users import get_user, find
from . import sibling

MAX_RETRIES = 3

class UserService(BaseService, Mixin):
    """Looks users up."""

    def lookup(self, user_id: str) -> dict:
        """Fetch one user."""
        return get_user(user_id)

def helper(value, flag=False) -> str:
    return os.path.join(value)

def _private():
    pass
'''


def test_python_definitions_kinds_and_qualified_names():
    facts = extract(source(PYTHON_SAMPLE))

    assert definition(facts, "UserService").kind == "class"
    assert definition(facts, "UserService.lookup").kind == "method"
    assert definition(facts, "helper").kind == "function"
    assert definition(facts, "MAX_RETRIES").kind == "constant"


def test_python_signature_docstring_and_visibility():
    facts = extract(source(PYTHON_SAMPLE))

    lookup = definition(facts, "UserService.lookup")
    assert lookup.signature == "lookup(self, user_id: str) -> dict"
    assert lookup.docstring == "Fetch one user."
    assert lookup.parent == "UserService"
    assert definition(facts, "helper").exported is True
    assert definition(facts, "_private").exported is False


def test_python_imports_and_inheritance():
    facts = extract(source(PYTHON_SAMPLE))

    specifiers = {i.specifier: i for i in facts.imports}
    assert specifiers["os"].names == ()
    assert set(specifiers["services.users"].names) == {"get_user", "find"}
    assert [(i.parent_name, i.kind) for i in facts.inheritance] == [
        ("BaseService", "extends"), ("Mixin", "extends"),
    ]


def test_python_calls_record_receiver_and_enclosing_definition():
    facts = extract(source(PYTHON_SAMPLE))

    by_callee = {c.callee: c for c in facts.calls}
    assert by_callee["get_user"].enclosing == "UserService.lookup"
    assert by_callee["os.path.join"].receiver == "os.path"
    assert by_callee["os.path.join"].enclosing == "helper"
    assert by_callee["get_user"].argument_count == 1


def test_python_local_variables_are_not_definitions():
    facts = extract(source("def f():\n    local_value = 1\n    return local_value\n"))

    assert [d.qualified_name for d in facts.definitions] == ["f"]


# -- TypeScript ------------------------------------------------------------


TS_SAMPLE = """
import express from "express";
import { getUser, type User } from "../services/user";
import * as helpers from "./helpers";
const legacy = require("./legacy");

export interface Options { retries: number }
export type Handler = (req: Request) => void;

/** Talks to the user store. */
export class UserService extends BaseService implements Disposable {
  async lookup(id: string): Promise<User> {
    return getUser(id);
  }
}

export const start = async (port: number): Promise<void> => {
  const app = express();
  await new UserService().lookup("1");
};
"""


def test_typescript_definition_kinds():
    facts = extract(source(TS_SAMPLE, language="typescript", path="api/server.ts"))
    kinds = {d.qualified_name: d.kind for d in facts.definitions}

    assert kinds["Options"] == "interface"
    assert kinds["Handler"] == "type"
    assert kinds["UserService"] == "class"
    assert kinds["UserService.lookup"] == "method"
    assert kinds["start"] == "function"


def test_typescript_exports_docstring_and_signature():
    facts = extract(source(TS_SAMPLE, language="typescript", path="api/server.ts"))

    service = definition(facts, "UserService")
    assert service.exported is True
    assert service.docstring == "Talks to the user store."
    assert definition(facts, "UserService.lookup").signature == "lookup(id: string): Promise<User>"
    assert definition(facts, "start").signature == "start(port: number): Promise<void>"


def test_typescript_imports_including_require():
    facts = extract(source(TS_SAMPLE, language="typescript", path="api/server.ts"))
    by_specifier = {i.specifier: i for i in facts.imports}

    assert by_specifier["express"].names == ("default",)
    assert by_specifier["express"].alias == "express"
    assert set(by_specifier["../services/user"].names) == {"getUser", "User"}
    assert by_specifier["./helpers"].is_wildcard is True
    assert "./legacy" in by_specifier  # require() is an import, not a call


def test_typescript_inheritance_records_extends_and_implements():
    facts = extract(source(TS_SAMPLE, language="typescript", path="api/server.ts"))

    assert {(i.parent_name, i.kind) for i in facts.inheritance} == {
        ("BaseService", "extends"), ("Disposable", "implements"),
    }


def test_typescript_construction_is_flagged():
    facts = extract(source(TS_SAMPLE, language="typescript", path="api/server.ts"))

    construction = next(c for c in facts.calls if c.is_construction)
    assert construction.callee == "UserService"
    assert construction.enclosing == "start"


# -- TSX / React -----------------------------------------------------------


TSX_SAMPLE = """
import { Button } from "@/components/ui/button";

export function StoryStep({ active }: { active: boolean }) {
  return (
    <section className="step">
      <Button onClick={() => track("click")}>Go</Button>
      <span>plain html</span>
    </section>
  );
}

export function useThing() {
  return <div />;
}

export function helper(value: string) {
  return value.trim();
}
"""


def test_tsx_components_are_distinguished_from_plain_functions():
    facts = extract(source(TSX_SAMPLE, language="tsx", path="story.tsx"))
    kinds = {d.qualified_name: d.kind for d in facts.definitions}

    assert kinds["StoryStep"] == "component"  # PascalCase and renders JSX
    assert kinds["useThing"] == "function"  # renders JSX but is a hook
    assert kinds["helper"] == "function"  # neither


def test_tsx_records_component_usage_but_not_html_tags():
    facts = extract(source(TSX_SAMPLE, language="tsx", path="story.tsx"))

    assert [(r.component, r.enclosing) for r in facts.renders] == [("Button", "StoryStep")]


def test_tsx_alias_imports_are_captured_verbatim():
    facts = extract(source(TSX_SAMPLE, language="tsx", path="story.tsx"))

    assert facts.imports[0].specifier == "@/components/ui/button"


# -- Safety and determinism ------------------------------------------------


def test_secrets_in_signatures_and_docstrings_are_redacted():
    code = (
        'def connect(api_key="AKIAIOSFODNN7EXAMPLE", token="ghp_abcdefghijklmnopqrstuvwxyz0123456789"):\n'
        '    """Uses password = \'hunter2hunter2\' internally."""\n'
        "    pass\n"
    )

    facts = extract(source(code))
    connect = definition(facts, "connect")

    assert "AKIAIOSFODNN7EXAMPLE" not in connect.signature
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in connect.signature
    assert "hunter2hunter2" not in (connect.docstring or "")
    assert "[REDACTED]" in connect.signature


def test_extraction_is_deterministic_and_ordered_by_position():
    first = extract(source(PYTHON_SAMPLE))
    second = extract(source(PYTHON_SAMPLE))

    assert first == second
    lines = [d.start_line for d in first.definitions]
    assert lines == sorted(lines)


def test_syntax_errors_are_flagged_but_still_yield_facts():
    facts = extract(source("def ok():\n    pass\n\ndef broken(:\n"))

    assert facts.has_syntax_errors is True
    assert any(d.name == "ok" for d in facts.definitions)


def test_per_file_caps_truncate_instead_of_growing_without_bound():
    code = "".join(f"def f{i}():\n    pass\n" for i in range(50))
    limits = replace(DEFAULT_LIMITS, max_definitions_per_file=10)

    facts = extract(source(code), limits)

    assert len(facts.definitions) == 10
    assert facts.truncated is True


def test_long_signatures_are_truncated():
    limits = replace(DEFAULT_LIMITS, max_signature_chars=20)
    code = "def f(" + ", ".join(f"argument_{i}" for i in range(30)) + "):\n    pass\n"

    facts = extract(source(code), limits)

    assert len(definition(facts, "f").signature) <= 20


def test_content_hash_and_path_are_carried_through():
    file = source(PYTHON_SAMPLE, path="pkg/module.py")

    facts = extract(file)

    assert facts.path == "pkg/module.py"
    assert facts.content_hash == file.content_hash
    assert facts.parser_version >= 1
