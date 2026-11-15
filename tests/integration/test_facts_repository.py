"""The facts store is a per-repository cache; both implementations must agree."""

import pytest

from app.knowledge_graph.facts import PARSER_VERSION, Definition, FileFacts
from app.services.facts_repository import InMemoryFactsStore, MongoFactsStore

REPO = "octocat/hello-world"
OTHER_REPO = "someone-else/private"


def facts(path: str, content_hash: str = "hash-1", parser_version: int = PARSER_VERSION) -> FileFacts:
    return FileFacts(
        path=path,
        language="python",
        content_hash=content_hash,
        loc=12,
        definitions=(
            Definition(
                name="handler", kind="function", qualified_name="handler",
                start_line=1, end_line=4, signature="handler(request)",
            ),
        ),
        parser_version=parser_version,
    )


@pytest.fixture(params=["mongo", "memory"])
def store(request):
    """Run every test against both implementations."""
    return MongoFactsStore() if request.param == "mongo" else InMemoryFactsStore()


async def test_saves_and_loads_facts(store):
    await store.save_many(REPO, [facts("api/handlers.py")])

    loaded = await store.load_all(REPO)

    assert set(loaded) == {"api/handlers.py"}
    assert loaded["api/handlers.py"].definitions[0].qualified_name == "handler"
    assert loaded["api/handlers.py"].loc == 12


async def test_saving_the_same_path_twice_replaces_it(store):
    await store.save_many(REPO, [facts("a.py", "hash-1")])
    await store.save_many(REPO, [facts("a.py", "hash-2")])

    loaded = await store.load_all(REPO)

    assert len(loaded) == 1
    assert loaded["a.py"].content_hash == "hash-2"


async def test_fingerprints_expose_only_path_and_hash(store):
    await store.save_many(REPO, [facts("a.py", "hash-a"), facts("b.py", "hash-b")])

    assert await store.fingerprints(REPO) == {"a.py": "hash-a", "b.py": "hash-b"}


async def test_facts_from_an_older_parser_are_not_reused(store):
    await store.save_many(REPO, [facts("a.py", parser_version=PARSER_VERSION - 1)])

    # Omitted from fingerprints, so the sync plan re-extracts the file.
    assert await store.fingerprints(REPO) == {}


async def test_repositories_are_isolated(store):
    await store.save_many(REPO, [facts("a.py", "mine")])
    await store.save_many(OTHER_REPO, [facts("a.py", "theirs")])

    assert (await store.load_all(REPO))["a.py"].content_hash == "mine"
    assert (await store.fingerprints(OTHER_REPO)) == {"a.py": "theirs"}


async def test_delete_paths_removes_only_the_named_files(store):
    await store.save_many(REPO, [facts("a.py"), facts("b.py")])

    await store.delete_paths(REPO, ["a.py"])

    assert set(await store.load_all(REPO)) == {"b.py"}


async def test_replace_all_drops_anything_missing(store):
    await store.save_many(REPO, [facts("a.py"), facts("stale.py")])

    await store.replace_all(REPO, [facts("a.py"), facts("c.py")])

    assert set(await store.load_all(REPO)) == {"a.py", "c.py"}


async def test_replace_all_leaves_other_repositories_alone(store):
    await store.save_many(OTHER_REPO, [facts("keep.py")])

    await store.replace_all(REPO, [facts("a.py")])

    assert set(await store.load_all(OTHER_REPO)) == {"keep.py"}


async def test_delete_repo_removes_everything_for_that_repository(store):
    await store.save_many(REPO, [facts("a.py")])
    await store.save_many(OTHER_REPO, [facts("a.py")])

    await store.delete_repo(REPO)

    assert await store.load_all(REPO) == {}
    assert set(await store.load_all(OTHER_REPO)) == {"a.py"}


async def test_empty_writes_are_harmless(store):
    await store.save_many(REPO, [])
    await store.delete_paths(REPO, [])

    assert await store.load_all(REPO) == {}
