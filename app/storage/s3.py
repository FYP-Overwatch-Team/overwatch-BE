"""AWS S3, and the base for stores that speak its API through `STORAGE_ENDPOINT_URL` (R2 in `r2.py`, MinIO, Wasabi).

The browser uploads straight to the bucket with a signed request bound to the file's type and exact size,
and reads through presigned links; signing is local, so neither costs a request to the provider.
"""

import asyncio

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.storage.store import StoredFile, UploadTarget, disposition

UPLOAD_WINDOW_SECONDS = 600


class S3Store:
    #: when the SDK adds integrity checksums to requests; S3-compatible stores may not accept them
    checksums = "when_supported"

    def __init__(self, bucket: str, region: str, endpoint_url: str = "") -> None:
        self.bucket = bucket
        self.client = boto3.client(
            "s3", region_name=region or None, endpoint_url=endpoint_url or None,
            config=Config(
                signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"},
                request_checksum_calculation=self.checksums, response_checksum_validation=self.checksums,
            ),
        )

    def upload_target(
        self, attachment_id: str, key: str, content_type: str, size: int, *, api_path: str = "/attachments",
    ) -> UploadTarget:
        """A signed form: S3 checks the policy's type and size before it stores anything."""
        post = self.client.generate_presigned_post(
            self.bucket, key,
            Fields={"Content-Type": content_type},
            Conditions=[{"Content-Type": content_type}, ["content-length-range", size, size]],
            ExpiresIn=UPLOAD_WINDOW_SECONDS,
        )
        return {"method": "POST", "url": post["url"], "fields": post["fields"], "headers": {}, "via_api": False}

    def download_url(
        self, attachment_id: str, key: str, ttl: int, *, download_name: str | None = None, api_path: str = "/attachments",
    ) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key, "ResponseContentDisposition": disposition(download_name)},
            ExpiresIn=ttl,
        )

    async def head(self, key: str) -> StoredFile | None:
        try:
            meta = await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return {"size": meta["ContentLength"], "content_type": meta.get("ContentType", "")}

    async def read_start(self, key: str, size: int) -> bytes | None:
        try:
            found = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key, Range=f"bytes=0-{size - 1}")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return await asyncio.to_thread(found["Body"].read)

    async def write(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(self.client.put_object, Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)

