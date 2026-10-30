"""Where uploaded files live, behind one interface: the rest of the app never names a provider.

Adding a provider is one adapter module (see `s3.py`, or `r2.py` for one built on it) registered in `BACKENDS`, then
`STORAGE_BACKEND=<name>`. Records keep each file's `key`, never a URL, so moving providers is copying
the objects and flipping the setting.
"""

from collections.abc import Callable
from typing import Protocol, TypedDict, runtime_checkable
from urllib.parse import quote

from app.core.config import Settings, get_settings


class UploadTarget(TypedDict):
    """How the browser sends a file; it follows this as given, whatever the provider."""

    method: str  # POST (a signed form), PUT (a signed URL, or our API)
    url: str
    fields: dict[str, str]  # form fields to send before the file; empty for a PUT
    headers: dict[str, str]  # headers a PUT must carry (e.g. Azure's x-ms-blob-type)
    via_api: bool  # true when the URL is our API, which needs the user's login


def disposition(download_name: str | None) -> str:
    """`inline` to show a file, or `attachment` with its name to save it (any characters, RFC 6266)."""
    if download_name is None:
        return "inline"
    return f"attachment; filename*=UTF-8''{quote(download_name, safe='')}"


class StoredFile(TypedDict):
    size: int
    content_type: str


class ObjectStore(Protocol):
    def upload_target(
        self, attachment_id: str, key: str, content_type: str, size: int, *, api_path: str = "/attachments",
    ) -> UploadTarget:
        """A one-time way to upload `key`, bound to its type and exact size where the provider allows.
        `api_path` is where our API takes the file, for stores that keep files behind it."""

    def download_url(
        self, attachment_id: str, key: str, ttl: int, *, download_name: str | None = None, api_path: str = "/attachments",
    ) -> str:
        """A link that reads `key` for `ttl` seconds, without the user's login (images can't send it).
        With `download_name`, the browser saves the file under that name instead of opening it."""

    async def head(self, key: str) -> StoredFile | None:
        """What was actually stored at `key`; None when nothing is there."""

    async def read_start(self, key: str, size: int) -> bytes | None:
        """The first `size` bytes stored at `key`, to tell what the file really is; None when nothing is there."""

    async def write(self, key: str, data: bytes, content_type: str) -> None:
        """Store a file the server has itself (an image copied from a docs source, or a proxied upload)."""

    async def delete(self, key: str) -> None: ...


@runtime_checkable
class ProxiedStore(ObjectStore, Protocol):
    """A store whose files go through our API (dev, tests): it takes the upload and serves the file."""

    async def read(self, key: str) -> tuple[bytes, str] | None: ...


def _local(settings: Settings) -> ObjectStore:
    from app.storage.local import LocalStore

    return LocalStore(settings.uploads_dir)


def _s3(settings: Settings) -> ObjectStore:
    from app.storage.s3 import S3Store

    return S3Store(settings.storage_bucket, settings.storage_region, settings.storage_endpoint_url)


def _r2(settings: Settings) -> ObjectStore:
    from app.storage.r2 import R2Store

    if not settings.storage_endpoint_url:
        raise RuntimeError("STORAGE_BACKEND=r2 needs STORAGE_ENDPOINT_URL (https://<account_id>.r2.cloudflarestorage.com)")
    return R2Store(settings.storage_bucket, settings.storage_region, settings.storage_endpoint_url)


#: STORAGE_BACKEND name -> how to build its adapter; imported lazily so unused SDKs never load
BACKENDS: dict[str, Callable[[Settings], ObjectStore]] = {"local": _local, "s3": _s3, "r2": _r2}

_store: ObjectStore | None = None


def get_object_store() -> ObjectStore:
    global _store
    if _store is None:
        settings = get_settings()
        build = BACKENDS.get(settings.storage_backend)
        if build is None:
            raise RuntimeError(f"Unknown STORAGE_BACKEND {settings.storage_backend!r}; one of {', '.join(BACKENDS)}")
        _store = build(settings)
    return _store


def use_object_store(store: ObjectStore | None) -> None:
    """Swap the store (tests)."""
    global _store
    _store = store
