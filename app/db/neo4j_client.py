from collections.abc import Sequence
from typing import Any, Protocol

from neo4j import AsyncGraphDatabase

from app.core.config import get_settings


class GraphStore(Protocol):
    """Injectable interface over Neo4j so services and tests never touch the driver directly."""

    async def run(self, query: str, **params: Any) -> list[dict]: ...

    async def run_write(self, statements: Sequence[tuple[str, dict]]) -> None:
        """Run several statements in one write transaction, all or nothing."""

    async def close(self) -> None: ...


class Neo4jStore:
    def __init__(self, uri: str | None = None, user: str | None = None, password: str | None = None):
        settings = get_settings()
        self._driver = AsyncGraphDatabase.driver(
            uri or settings.neo4j_uri,
            auth=(user or settings.neo4j_user, password or settings.neo4j_password),
        )

    async def run(self, query: str, **params: Any) -> list[dict]:
        async with self._driver.session() as session:
            result = await session.run(query, **params)
            return [dict(record) async for record in result]

    async def run_write(self, statements: Sequence[tuple[str, dict]]) -> None:
        """One explicit write transaction for a batch of statements.

        Graph writes come in related groups — nodes then the edges between
        them — and a half-applied group would leave dangling references until
        the next sync. Committing a group together avoids that.
        """
        async with self._driver.session() as session:

            async def work(tx):
                for query, params in statements:
                    await tx.run(query, **params)

            await session.execute_write(work)

    async def close(self) -> None:
        await self._driver.close()


_store: GraphStore | None = None


def get_graph_store() -> GraphStore:
    global _store
    if _store is None:
        _store = Neo4jStore()
    return _store


def use_graph_store(store: GraphStore) -> None:
    """Inject a store (tests use an in-memory fake here)."""
    global _store
    _store = store


async def close_graph_store() -> None:
    global _store
    if _store is not None:
        await _store.close()
    _store = None
