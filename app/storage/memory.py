"""Files in memory, for tests."""

from app.storage.proxied import ThroughApi
from app.storage.store import StoredFile


class InMemoryStore(ThroughApi):
    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, str]] = {}

    async def write(self, key: str, data: bytes, content_type: str) -> None:
        self.files[key] = (data, content_type)

    async def read(self, key: str) -> tuple[bytes, str] | None:
        return self.files.get(key)

    async def head(self, key: str) -> StoredFile | None:
        found = self.files.get(key)
        return found and {"size": len(found[0]), "content_type": found[1]}

    async def delete(self, key: str) -> None:
        self.files.pop(key, None)
