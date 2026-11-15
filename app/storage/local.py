"""Files in a folder (dev), the content type beside each one."""

import asyncio
from pathlib import Path

from app.storage.proxied import ThroughApi
from app.storage.store import StoredFile


class LocalStore(ThroughApi):
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError("key escapes the uploads folder")
        return path

    async def write(self, key: str, data: bytes, content_type: str) -> None:
        def _write() -> None:
            path = self._path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.with_suffix(".type").write_text(content_type)

        await asyncio.to_thread(_write)

    async def read(self, key: str) -> tuple[bytes, str] | None:
        def _read() -> tuple[bytes, str] | None:
            path = self._path(key)
            if not path.exists():
                return None
            return path.read_bytes(), path.with_suffix(".type").read_text()

        return await asyncio.to_thread(_read)

    async def head(self, key: str) -> StoredFile | None:
        def _head() -> StoredFile | None:
            path = self._path(key)
            if not path.exists():
                return None
            return {"size": path.stat().st_size, "content_type": path.with_suffix(".type").read_text()}

        return await asyncio.to_thread(_head)

    async def delete(self, key: str) -> None:
        def _delete() -> None:
            for path in (self._path(key), self._path(key).with_suffix(".type")):
                path.unlink(missing_ok=True)

        await asyncio.to_thread(_delete)
