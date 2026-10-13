"""References become edges only when the code says where they point."""

import hashlib

from app.knowledge_graph.discovery import SourceFile, detect_language
from app.knowledge_graph.extract import extract
from app.knowledge_graph.link import EdgeKind, link_repository
from app.knowledge_graph.project_config import ProjectConfig


def facts_for(sources: dict[str, str]) -> dict:
    """Run real extraction over a small in-memory repository."""
    facts = {}
    for path, code in sources.items():
        data = code.encode()
        facts[path] = extract(
            SourceFile(
                relative_path=path,
                language=detect_language(path),
                content_hash=hashlib.sha256(data).hexdigest(),
                source=data,
                loc=code.count("\n") + 1,
            )
        )
    return facts


def link(sources: dict[str, str], config: ProjectConfig | None = None):
    return link_repository(facts_for(sources), config or ProjectConfig())


def call_edges(links):
    return {
        (e.source_file, e.source_symbol, e.target.file, e.target.qualified_name)
        for e in links.edges_of(EdgeKind.CALLS)
    }


def test_calls_to_local_definitions_are_linked():
    links = link({"app.py": "def helper():\n    pass\n\ndef main():\n    helper()\n"})

    assert call_edges(links) == {("app.py", "main", "app.py", "helper")}


def test_calls_through_named_imports_are_linked():
    links = link({
        "services/users.py": "def get_user(user_id):\n    return user_id\n",
        "api/handlers.py": "from services.users import get_user\n\ndef handle():\n    return get_user(1)\n",
    })

    assert call_edges(links) == {
        ("api/handlers.py", "handle", "services/users.py", "get_user"),
    }


def test_calls_through_a_namespace_import_are_linked():
    links = link({
        "lib/format.ts": "export function fmt(value: string) { return value; }\n",
        "app.ts": 'import * as helpers from "./lib/format";\n\nexport function run() { return helpers.fmt("x"); }\n',
    })

    assert call_edges(links) == {("app.ts", "run", "lib/format.ts", "fmt")}


def test_calls_through_a_python_module_import_are_linked():
    links = link({
        "services/users.py": "def get_user(user_id):\n    return user_id\n",
        "main.py": "import services.users\n\ndef run():\n    return services.users.get_user(1)\n",
    })

    assert call_edges(links) == {("main.py", "run", "services/users.py", "get_user")}


def test_self_and_this_resolve_to_the_enclosing_class():
    links = link({
        "svc.py": (
            "class Service:\n"
            "    def helper(self):\n        pass\n\n"
            "    def run(self):\n        self.helper()\n"
        ),
        "svc.ts": (
            "export class Service {\n"
            "  helper() {}\n"
            "  run() { this.helper(); }\n"
            "}\n"
        ),
    })

    assert ("svc.py", "Service.run", "svc.py", "Service.helper") in call_edges(links)
    assert ("svc.ts", "Service.run", "svc.ts", "Service.helper") in call_edges(links)


def test_a_method_call_on_an_untyped_value_is_left_unresolved():
    links = link({"app.py": "def run(payload):\n    return payload.get('id')\n"})

    assert call_edges(links) == set()
    assert links.stats.calls_total == 1
    assert links.stats.calls_resolved == 0
    assert links.stats.calls_to_packages == 0


def test_calls_into_packages_are_counted_not_linked():
    links = link({
        "app.ts": 'import { useState } from "react";\n\nexport function App() { useState(); }\n',
    })

    assert call_edges(links) == set()
    assert links.stats.calls_to_packages == 1
    assert links.stats.calls_accounted == 1.0


def test_a_local_name_never_loses_to_an_identically_named_package_import():
    links = link({
        "app.py": "from redis import Redis\n\ndef Redis_helper():\n    pass\n\ndef run():\n    Redis_helper()\n",
    })

    assert call_edges(links) == {("app.py", "run", "app.py", "Redis_helper")}


def test_jsx_usage_links_components(tmp_path):
    links = link({
        "components/Button.tsx": "export function Button() { return <button />; }\n",
        "app.tsx": 'import { Button } from "@/components/Button";\n\nexport function App() { return <Button />; }\n',
    }, ProjectConfig(ts_paths={"@/*": ("./*",)}))

    renders = {
        (e.source_file, e.source_symbol, e.target.qualified_name)
        for e in links.edges_of(EdgeKind.RENDERS)
    }
    assert renders == {("app.tsx", "App", "Button")}


def test_inheritance_is_linked_for_both_extends_and_implements():
    links = link({
        "base.ts": "export class Base {}\nexport interface Disposable {}\n",
        "svc.ts": (
            'import { Base, Disposable } from "./base";\n'
            "export class Service extends Base implements Disposable {}\n"
        ),
    })

    assert {(e.kind, e.target.qualified_name) for e in links.edges if e.kind in (EdgeKind.EXTENDS, EdgeKind.IMPLEMENTS)} == {
        (EdgeKind.EXTENDS, "Base"), (EdgeKind.IMPLEMENTS, "Disposable"),
    }


def test_repeated_calls_merge_into_one_edge_with_a_count():
    links = link({
        "app.py": "def helper():\n    pass\n\ndef run():\n    helper()\n    helper()\n    helper()\n",
    })

    edge = links.edges_of(EdgeKind.CALLS)[0]
    assert edge.count == 3
    assert edge.lines == (5, 6, 7)


def test_file_level_calls_have_no_source_symbol():
    links = link({
        "lib.py": "def setup():\n    pass\n",
        "main.py": "from lib import setup\n\nsetup()\n",
    })

    edge = links.edges_of(EdgeKind.CALLS)[0]
    assert edge.source_symbol is None


def test_imports_and_packages_are_reported_per_file():
    links = link({
        "lib/utils.ts": "export const x = 1;\n",
        "app.ts": 'import { x } from "./lib/utils";\nimport axios from "axios";\n',
    })

    assert [(i.source_file, i.target_file) for i in links.imports] == [("app.ts", "lib/utils.ts")]
    assert [(p.source_file, p.package) for p in links.packages] == [("app.ts", "axios")]


def test_a_file_importing_itself_produces_no_edge():
    links = link({"app.ts": 'import { x } from "./app";\nexport const x = 1;\n'})

    assert links.imports == ()


def test_linking_is_deterministic():
    sources = {
        "lib.py": "def a():\n    pass\n\ndef b():\n    pass\n",
        "main.py": "from lib import a, b\n\ndef run():\n    a()\n    b()\n",
    }

    assert link(sources) == link(sources)


def test_unresolved_references_are_reported_rather_than_hidden():
    links = link({
        "app.py": (
            "from unknown_pkg import thing\n\n"
            "def run(client):\n"
            "    thing()\n"
            "    client.send()\n"
        ),
    })

    stats = links.stats
    assert stats.calls_total == 2
    assert stats.calls_resolved == 0
    assert stats.calls_to_packages == 1  # thing() came from a package
    assert stats.calls_accounted == 0.5  # client.send() is genuinely unknown
