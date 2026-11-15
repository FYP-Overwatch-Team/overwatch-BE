"""Routes and requests: the layer that connects a frontend to its backend."""

import hashlib

import pytest

from app.knowledge_graph.build import EdgeType, NodeLabel, build_snapshot
from app.knowledge_graph.discovery import SourceFile, detect_language
from app.knowledge_graph.extract import extract
from app.knowledge_graph.link import link_repository, normalise_path
from app.knowledge_graph.link.http import RouteMatcher
from app.knowledge_graph.project_config import ProjectConfig

REPO = "octocat/hello-world"


def facts_for(sources: dict[str, str]) -> dict:
    facts = {}
    for path, code in sources.items():
        data = code.encode()
        facts[path] = extract(
            SourceFile(
                relative_path=path, language=detect_language(path),
                content_hash=hashlib.sha256(data).hexdigest(), source=data,
                loc=code.count("\n") + 1,
            )
        )
    return facts


def link(sources: dict[str, str]):
    return link_repository(facts_for(sources), ProjectConfig())


# -- Path normalisation ----------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/api/users/{user_id}", "/api/users/*"),   # FastAPI
        ("/api/users/:id", "/api/users/*"),         # Express
        ("/api/users/[id]", "/api/users/*"),        # Next.js
        ("/api/posts/[...slug]", "/api/posts/*"),
        ("https://service.test/api/x?a=1", "/api/x"),
        ("/api/users/", "/api/users"),
        ("/api//users", "/api/users"),
        ("/", "/"),
    ],
)
def test_paths_from_every_framework_normalise_the_same_way(path, expected):
    assert normalise_path(path) == expected


def test_matcher_prefers_an_exact_route_over_a_parameterised_one():
    matcher = RouteMatcher([("GET", "/api/users/{id}"), ("GET", "/api/users/me")])

    assert matcher.match("GET", "/api/users/me").path == "/api/users/me"
    assert matcher.match("GET", "/api/users/42").path == "/api/users/*"


def test_matcher_respects_the_method_and_segment_count():
    matcher = RouteMatcher([("GET", "/api/users/{id}")])

    assert matcher.match("POST", "/api/users/42") is None
    assert matcher.match("GET", "/api/users/42/posts") is None
    assert matcher.match("GET", "/api/users") is None


# -- Extraction ------------------------------------------------------------


def test_fastapi_decorators_declare_routes_with_their_handler():
    facts = facts_for({
        "api/users.py": (
            '@router.get("/api/users/{user_id}")\n'
            "async def get_user(user_id: str):\n    return user_id\n"
        )
    })["api/users.py"]

    route = facts.routes[0]
    assert (route.method, route.path, route.handler) == ("GET", "/api/users/{user_id}", "get_user")
    assert route.framework == "python"


def test_flask_route_decorator_reads_the_method_list():
    facts = facts_for({
        "app.py": '@app.route("/submit", methods=["POST"])\ndef submit():\n    return 1\n'
    })["app.py"]

    assert (facts.routes[0].method, facts.routes[0].path) == ("POST", "/submit")


def test_express_handlers_are_routes_not_client_calls():
    facts = facts_for({
        "server.js": 'app.get("/health", (req, res) => res.json({}));\n'
    })["server.js"]

    assert (facts.routes[0].method, facts.routes[0].path) == ("GET", "/health")
    assert facts.http_calls == ()


def test_next_route_files_declare_routes_from_their_location():
    facts = facts_for({
        "app/api/users/[id]/route.ts": (
            "export async function GET() { return Response.json([]); }\n"
            "export async function DELETE() { return new Response(null); }\n"
        )
    })["app/api/users/[id]/route.ts"]

    assert {(r.method, r.path) for r in facts.routes} == {
        ("GET", "/api/users/[id]"), ("DELETE", "/api/users/[id]"),
    }


def test_route_groups_are_not_part_of_the_url():
    facts = facts_for({
        "app/(dashboard)/api/stats/route.ts": "export async function GET() { return null; }\n"
    })["app/(dashboard)/api/stats/route.ts"]

    assert facts.routes[0].path == "/api/stats"


def test_client_calls_are_recorded_with_their_caller():
    facts = facts_for({
        "web/api.ts": (
            'import axios from "axios";\n'
            "export async function loadUsers() {\n"
            '  await fetch("/api/users");\n'
            '  await axios.post("/api/users", {});\n'
            "}\n"
        )
    })["web/api.ts"]

    assert {(c.method, c.path, c.caller) for c in facts.http_calls} == {
        ("GET", "/api/users", "loadUsers"), ("POST", "/api/users", "loadUsers"),
    }


def test_a_computed_url_is_skipped_rather_than_guessed():
    facts = facts_for({
        "web/api.ts": (
            "export async function load(id: string) {\n"
            "  await fetch(`/api/users/${id}`);\n"
            "  await fetch(buildUrl());\n"
            "}\n"
        )
    })["web/api.ts"]

    assert facts.http_calls == ()


def test_python_client_calls_are_recognised():
    facts = facts_for({
        "jobs/sync.py": (
            "import requests\n\n"
            "def sync():\n"
            '    requests.get("https://api.example.com/v1/items")\n'
        )
    })["jobs/sync.py"]

    assert (facts.http_calls[0].method, facts.http_calls[0].path) == (
        "GET", "https://api.example.com/v1/items",
    )


# -- Linking ---------------------------------------------------------------


FULLSTACK = {
    "api/users.py": (
        '@router.get("/api/users/{user_id}")\n'
        "async def get_user(user_id: str):\n    return user_id\n\n"
        '@router.post("/api/users")\n'
        "async def create_user():\n    return 1\n"
    ),
    "web/client.ts": (
        "export async function loadUser() {\n"
        '  return fetch("/api/users/42");\n'
        "}\n"
        "export async function callElsewhere() {\n"
        '  return fetch("/api/not-ours");\n'
        "}\n"
    ),
}


def test_a_frontend_call_links_to_the_backend_route_that_serves_it():
    links = link(FULLSTACK)

    assert [(r.method, r.path, r.source_file, r.source_symbol) for r in links.requests] == [
        ("GET", "/api/users/*", "web/client.ts", "loadUser"),
    ]


def test_unmatched_requests_are_counted_not_attached_to_a_guess():
    stats = link(FULLSTACK).stats

    assert stats.routes_total == 2
    assert stats.http_calls_total == 2
    assert stats.http_calls_matched == 1  # the other route is not ours


# -- Graph -----------------------------------------------------------------


def test_routes_become_nodes_with_handler_and_caller_edges():
    facts = facts_for(FULLSTACK)
    snapshot = build_snapshot(REPO, "sha-1", facts, link_repository(facts, ProjectConfig()))

    route_ids = {node.id for node in snapshot.nodes if node.label is NodeLabel.ROUTE}
    assert f"{REPO}:route:GET /api/users/*" in route_ids

    handles = {(e.source, e.target) for e in snapshot.edges if e.type is EdgeType.HANDLES}
    requests = {(e.source, e.target) for e in snapshot.edges if e.type is EdgeType.REQUESTS}
    assert (f"{REPO}:api/users.py#get_user", f"{REPO}:route:GET /api/users/*") in handles
    assert (f"{REPO}:web/client.ts#loadUser", f"{REPO}:route:GET /api/users/*") in requests


def test_a_route_declared_twice_is_one_node():
    facts = facts_for({
        "api/a.py": '@router.get("/api/x")\ndef a(): return 1\n',
        "api/b.py": '@app.get("/api/x")\ndef b(): return 2\n',
    })
    snapshot = build_snapshot(REPO, "sha-1", facts, link_repository(facts, ProjectConfig()))

    routes = [node for node in snapshot.nodes if node.label is NodeLabel.ROUTE]
    assert len(routes) == 1
    assert len([e for e in snapshot.edges if e.type is EdgeType.HANDLES]) == 2
