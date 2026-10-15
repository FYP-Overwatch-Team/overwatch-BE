"""Storage adapters: S3 signs uploads and links locally; the backend is picked by name."""

import base64
import json

from botocore.stub import Stubber

import pytest

from app.core.config import get_settings
from app.storage.local import LocalStore
from app.storage.memory import InMemoryStore
from app.storage.s3 import S3Store
from app.storage.store import ProxiedStore, get_object_store, use_object_store


def store(monkeypatch) -> S3Store:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    return S3Store("docs-bucket", "eu-west-1")


def test_an_upload_is_bound_to_its_type_and_size(monkeypatch):
    target = store(monkeypatch).upload_target("a1", "ws/w/docs/n/a1", "image/png", 1024)
    policy = json.loads(base64.b64decode(target["fields"]["policy"]))

    assert (target["method"], target["via_api"], target["fields"]["key"], target["headers"]) == ("POST", False, "ws/w/docs/n/a1", {})
    assert {"Content-Type": "image/png"} in policy["conditions"]
    assert ["content-length-range", 1, 1024] in policy["conditions"]


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
