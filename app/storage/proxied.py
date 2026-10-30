"""What stores that keep files behind our API share: uploads to it, signed links back to it."""

import hashlib
import hmac
import time

from app.core.config import get_settings
from app.storage.store import UploadTarget


def _signature(attachment_id: str, expires: int) -> str:
    message = f"{attachment_id}:{expires}".encode()
    return hmac.new(get_settings().jwt_secret_key.encode(), message, hashlib.sha256).hexdigest()


def valid_signature(attachment_id: str, expires: int, signature: str) -> bool:
    return expires > time.time() and hmac.compare_digest(_signature(attachment_id, expires), signature)


class ThroughApi:
    def upload_target(
        self, attachment_id: str, key: str, content_type: str, size: int, *, api_path: str = "/attachments",
    ) -> UploadTarget:
        return {"method": "PUT", "url": f"{api_path}/{attachment_id}/content", "fields": {}, "headers": {"Content-Type": content_type}, "via_api": True}

    def download_url(
        self, attachment_id: str, key: str, ttl: int, *, download_name: str | None = None, api_path: str = "/attachments",
    ) -> str:
        # the route serving the file sets how it's shown; the name isn't needed in the link
        expires = int(time.time()) + ttl
        base = get_settings().app_base_url.rstrip("/")
        return f"{base}{api_path}/{attachment_id}/file?expires={expires}&signature={_signature(attachment_id, expires)}"

    async def read(self, key: str) -> tuple[bytes, str] | None: ...

    async def read_start(self, key: str, size: int) -> bytes | None:
        found = await self.read(key)
        return found[0][:size] if found else None
