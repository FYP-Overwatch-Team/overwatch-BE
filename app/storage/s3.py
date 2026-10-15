"""AWS S3, and anything that speaks its API (Cloudflare R2, MinIO, Wasabi) through `S3_ENDPOINT_URL`.

The browser uploads straight to the bucket with a signed form bound to the file's type and size, and
reads through presigned links; signing is local, so neither costs a request to AWS.
"""

import asyncio

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.storage.store import StoredFile, UploadTarget

UPLOAD_WINDOW_SECONDS = 600


class S3Store:
    def __init__(self, bucket: str, region: str, endpoint_url: str = "") -> None:
        self.bucket = bucket
        self.client = boto3.client(
            "s3", region_name=region or None, endpoint_url=endpoint_url or None,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
        )

    def upload_target(self, attachment_id: str, key: str, content_type: str, max_bytes: int) -> UploadTarget:
        post = self.client.generate_presigned_post(
            self.bucket, key,
            Fields={"Content-Type": content_type},
            Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, max_bytes]],
            ExpiresIn=UPLOAD_WINDOW_SECONDS,
        )
        return {"method": "POST", "url": post["url"], "fields": post["fields"], "headers": {}, "via_api": False}

    def download_url(self, attachment_id: str, key: str, ttl: int) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key, "ResponseContentDisposition": "inline"}, ExpiresIn=ttl,
        )

    async def head(self, key: str) -> StoredFile | None:
        try:
            meta = await asyncio.to_thread(self.client.head_object, Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return {"size": meta["ContentLength"], "content_type": meta.get("ContentType", "")}

    async def write(self, key: str, data: bytes, content_type: str) -> None:
        await asyncio.to_thread(self.client.put_object, Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)
