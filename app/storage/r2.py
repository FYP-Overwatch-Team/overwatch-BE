"""Cloudflare R2, through its S3-compatible API."""

from app.storage.s3 import UPLOAD_WINDOW_SECONDS, S3Store
from app.storage.store import UploadTarget


class R2Store(S3Store):
    """Cloudflare R2: no form uploads and no checksum-algorithm headers, so uploads are a signed PUT instead.

    The signature covers the type and length, so R2 refuses a different file; `/complete` checks again anyway.
    """

    checksums = "when_required"

    def __init__(self, bucket: str, region: str, endpoint_url: str) -> None:
        super().__init__(bucket, region or "auto", endpoint_url)

    def upload_target(
        self, attachment_id: str, key: str, content_type: str, size: int, *, api_path: str = "/attachments",
    ) -> UploadTarget:
        url = self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type, "ContentLength": size},
            ExpiresIn=UPLOAD_WINDOW_SECONDS,
        )
        # the browser sets Content-Length itself, from the file it sends
        return {"method": "PUT", "url": url, "fields": {}, "headers": {"Content-Type": content_type}, "via_api": False}
