"""Images in docs pages: start an upload, send the file, complete it, and the page links to it."""

from urllib.parse import urlsplit

from tests.integration.test_auth_flow import auth_headers, login
from tests.integration.test_documents_api import create
from tests.integration.test_workspaces import in_workspace, team, user

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def upload(client, headers: dict, page_id: str, data: bytes = PNG, content_type: str = "image/png"):
    started = await client.post(f"/documents/{page_id}/attachments", json={"filename": "shot.png", "content_type": content_type, "size": len(data)}, headers=headers)
    assert started.status_code == 201, started.json()
    attachment = started.json()["data"]
    sent = await client.put(attachment["upload"]["url"], content=data, headers=headers)
    return attachment, sent


def image_doc(attachment_id: str) -> dict:
    return {"type": "doc", "content": [{"type": "image", "attrs": {"attachmentId": attachment_id, "src": "blob:x"}}]}


async def test_an_uploaded_image_is_linked_from_its_page(client):
    headers = auth_headers(await login(client))
    page = await create(client, headers, title="Shots")
    attachment, sent = await upload(client, headers, page["id"])
    done = await client.post(f"/attachments/{attachment['id']}/complete", headers=headers)
    await client.patch(f"/documents/{page['id']}", json={"content": image_doc(attachment["id"]), "content_md": "", "base_version": 0}, headers=headers)

    url = (await client.get(f"/documents/{page['id']}", headers=headers)).json()["data"]["attachment_urls"][attachment["id"]]
    parts = urlsplit(url)
    served = await client.get(f"{parts.path}?{parts.query}")

    assert (sent.status_code, attachment["upload"]["via_api"]) == (200, True)
    assert done.json()["data"]["url"].startswith("http")
    assert (served.status_code, served.content, served.headers["content-type"]) == (200, PNG, "image/png")


async def test_only_small_real_images_are_taken(client):
    headers = auth_headers(await login(client))
    page = await create(client, headers, title="P")
    svg = await client.post(f"/documents/{page['id']}/attachments", json={"filename": "x.svg", "content_type": "image/svg+xml", "size": 10}, headers=headers)
    huge = await client.post(f"/documents/{page['id']}/attachments", json={"filename": "x.png", "content_type": "image/png", "size": 11 * 1024 * 1024}, headers=headers)
    _, disguised = await upload(client, headers, page["id"], data=b"<html>not an image</html>")
    folder = await create(client, headers, kind="folder", title="F")
    into_folder = await client.post(f"/documents/{folder['id']}/attachments", json={"filename": "x.png", "content_type": "image/png", "size": 10}, headers=headers)

    assert [r.status_code for r in (svg, huge, disguised)] == [415, 413, 415]
    assert into_folder.json()["meta"]["error_code"] == "read_only"


async def test_a_link_needs_a_valid_unexpired_signature(client):
    headers = auth_headers(await login(client))
    page = await create(client, headers, title="P")
    attachment, _ = await upload(client, headers, page["id"])
    url = (await client.post(f"/attachments/{attachment['id']}/complete", headers=headers)).json()["data"]["url"]
    parts = urlsplit(url)

    tampered = await client.get(f"{parts.path}?{parts.query.replace('signature=', 'signature=0')}")
    expired = await client.get(f"{parts.path}?expires=1&signature=abc")

    assert (tampered.status_code, expired.status_code) == (404, 404)


async def test_completing_before_the_file_arrives_is_refused(client):
    headers = auth_headers(await login(client))
    page = await create(client, headers, title="P")
    started = (await client.post(f"/documents/{page['id']}/attachments", json={"filename": "a.png", "content_type": "image/png", "size": 10}, headers=headers)).json()["data"]

    early = await client.post(f"/attachments/{started['id']}/complete", headers=headers)

    assert early.json()["meta"]["error_code"] == "upload_missing"


async def test_uploads_belong_to_their_workspace(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    page = await create(client, in_workspace(alice, acme), title="P")
    attachment, _ = await upload(client, in_workspace(alice, acme), page["id"])
    bobs_own = (await client.get("/workspaces", headers=bob)).json()["data"][0]["id"]

    stolen = await client.post(f"/attachments/{attachment['id']}/complete", headers=in_workspace(bob, bobs_own))

    assert stolen.json()["meta"]["error_code"] == "attachment_not_found"
