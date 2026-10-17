"""Storage adapters: S3 and R2 sign uploads and links locally; the backend is picked by name."""

import base64
import json
from urllib.parse import parse_qs, urlsplit

from botocore.stub import Stubber

import pytest

from app.core.config import Settings, get_settings
from app.storage.local import LocalStore
from app.storage.memory import InMemoryStore
from app.storage.r2 import R2Store
from app.storage.s3 import S3Store
from app.storage.store import ProxiedStore, get_object_store, use_object_store

R2_ENDPOINT = "https://account.r2.cloudflarestorage.com"


def store(monkeypatch) -> S3Store:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    return S3Store("docs-bucket", "eu-west-1")


def r2(monkeypatch) -> R2Store:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    return R2Store("docs-bucket", "", R2_ENDPOINT)


def test_an_upload_is_bound_to_its_type_and_exact_size(monkeypatch):
    target = store(monkeypatch).upload_target("a1", "ws/w/docs/n/a1", "image/png", 1024)
    policy = json.loads(base64.b64decode(target["fields"]["policy"]))

    assert (target["method"], target["via_api"], target["fields"]["key"], target["headers"]) == ("POST", False, "ws/w/docs/n/a1", {})
    assert {"Content-Type": "image/png"} in policy["conditions"]
    assert ["content-length-range", 1024, 1024] in policy["conditions"]


def test_an_r2_upload_is_a_put_signed_for_its_type_and_length(monkeypatch):
    target = r2(monkeypatch).upload_target("a1", "workspaces/w/docs/n/images/a1.png", "image/png", 1024)
    url = urlsplit(target["url"])
    query = parse_qs(url.query)

    assert (target["method"], target["via_api"], target["fields"], target["headers"]) == ("PUT", False, {}, {"Content-Type": "image/png"})
    assert (url.netloc, url.path) == ("account.r2.cloudflarestorage.com", "/docs-bucket/workspaces/w/docs/n/images/a1.png")
    assert {"content-length", "content-type", "host"} <= set(query["X-Amz-SignedHeaders"][0].split(";"))
    # R2 rejects the checksum-algorithm headers newer SDKs add by default
    assert "checksum" not in target["url"].lower()


def test_r2_uses_its_auto_region_and_needs_an_endpoint(monkeypatch):
    assert r2(monkeypatch).client.meta.region_name == "auto"

    use_object_store(None)
    monkeypatch.setenv("STORAGE_BACKEND", "r2")
    monkeypatch.setenv("STORAGE_ENDPOINT_URL", "")  # a local .env may set one
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="STORAGE_ENDPOINT_URL"):
        get_object_store()

    use_object_store(None)
    monkeypatch.setenv("STORAGE_ENDPOINT_URL", R2_ENDPOINT)
    get_settings.cache_clear()
    assert isinstance(get_object_store(), R2Store)
    use_object_store(None)


def test_storage_settings_still_read_the_old_s3_names(monkeypatch):
    for name in ("STORAGE_BUCKET", "STORAGE_REGION", "STORAGE_ENDPOINT_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("S3_BUCKET", "old-bucket")
    monkeypatch.setenv("S3_REGION", "eu-west-1")
    monkeypatch.setenv("S3_ENDPOINT_URL", "")
    assert (Settings().storage_bucket, Settings().storage_region) == ("old-bucket", "eu-west-1")

    monkeypatch.setenv("STORAGE_BUCKET", "new-bucket")
    assert Settings().storage_bucket == "new-bucket"



def test_a_download_link_is_signed_and_expires(monkeypatch):
    url = store(monkeypatch).download_url("a1", "ws/w/docs/n/a1", 600)

    assert "docs-bucket" in url and "X-Amz-Signature=" in url and "X-Amz-Expires=600" in url


async def test_a_missing_file_reads_as_none(monkeypatch):
    s3 = store(monkeypatch)
    with Stubber(s3.client) as stub:
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        stub.add_response("head_object", {"ContentLength": 7, "ContentType": "image/png"})
        assert await s3.head("missing") is None
        assert await s3.head("there") == {"size": 7, "content_type": "image/png"}


def test_the_backend_is_picked_by_name(monkeypatch, tmp_path):
    use_object_store(None)
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    get_settings.cache_clear()
    assert isinstance(get_object_store(), LocalStore)

    use_object_store(None)
    monkeypatch.setenv("STORAGE_BACKEND", "dropbox")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="Unknown STORAGE_BACKEND 'dropbox'"):
        get_object_store()


def test_only_stores_behind_our_api_take_uploads_through_it(monkeypatch, tmp_path):
    assert isinstance(InMemoryStore(), ProxiedStore) and isinstance(LocalStore(str(tmp_path)), ProxiedStore)
    assert not isinstance(store(monkeypatch), ProxiedStore)


async def test_the_local_store_keeps_files_inside_its_folder(tmp_path):
    local = LocalStore(str(tmp_path))
    await local.write("ws/w/a.png", b"png", "image/png")

    assert await local.head("ws/w/a.png") == {"size": 3, "content_type": "image/png"}
    with pytest.raises(ValueError):
        await local.read("../outside")
