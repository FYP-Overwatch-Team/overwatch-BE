"""Files in a decision's discussion: uploaded, checked, sent with a message, downloaded, and cleaned up."""

import uuid
from datetime import timedelta
from urllib.parse import urlsplit

from app.core.time import utcnow
from app.db import mongo
from app.decision_comments import attachments
from tests.integration.test_decision_comments import send, workspace_of_three

PDF = b"%PDF-1.7\n" + b"x" * 64


async def upload(client, headers: dict, decision_id: str, filename: str = "plan.pdf", data: bytes = PDF):
    started = await client.post(f"/decisions/{decision_id}/attachments", json={"filename": filename, "size": len(data)}, headers=headers)
    assert started.status_code == 201, started.json()
    pending = started.json()["data"]
    await client.put(pending["upload"]["url"], content=data, headers=headers)
    return pending["id"], await client.post(f"/decision-attachments/{pending['id']}/complete", headers=headers)


async def send_with(client, headers: dict, decision_id: str, attachment_ids: list[str], body: str = ""):
    return await client.post(
        f"/decisions/{decision_id}/comments",
        json={"client_id": uuid.uuid4().hex, "body": body, "attachment_ids": attachment_ids}, headers=headers,
    )


async def test_a_file_is_sent_with_a_message_and_downloads_under_its_name(client, object_store):
    owner, member, _, decision_id = await workspace_of_three(client)
    attachment_id, done = await upload(client, owner, decision_id, filename="../Q3 plan ✓.PDF")

    sent = await send_with(client, owner, decision_id, [attachment_id])
    link = await client.get(f"/decision-attachments/{attachment_id}/download", headers=member)
    parts = urlsplit(link.json()["data"]["url"])
    served = await client.get(f"{parts.path}?{parts.query}")

    assert done.json()["data"] == {"id": attachment_id, "filename": "Q3 plan ✓.PDF", "content_type": "application/pdf", "kind": "pdf", "size": len(PDF)}
    assert (sent.status_code, sent.json()["data"]["body"], sent.json()["data"]["attachments"][0]["id"]) == (201, "", attachment_id)
    key = (await mongo.decision_attachments().find_one({"_id": attachment_id}))["key"]
    assert key == f"workspaces/{owner['X-Workspace-Id']}/decisions/{decision_id}/attachments/{attachment_id}.pdf"
    assert served.content == PDF
    assert served.headers["content-disposition"] == "attachment; filename*=UTF-8''Q3%20plan%20%E2%9C%93.PDF"
    assert (served.headers["x-content-type-options"], served.headers["content-security-policy"]) == ("nosniff", "sandbox")


async def test_only_allowed_files_of_the_kind_their_name_says_are_kept(client, object_store):
    owner, _, _, decision_id = await workspace_of_three(client)

    script = await client.post(f"/decisions/{decision_id}/attachments", json={"filename": "x.svg", "size": 10}, headers=owner)
    huge = await client.post(f"/decisions/{decision_id}/attachments", json={"filename": "x.pdf", "size": attachments.MAX_BYTES + 1}, headers=owner)
    disguised_id, disguised = await upload(client, owner, decision_id, filename="notes.pdf", data=b"MZ\x90\x00 an executable")
    text_id, text = await upload(client, owner, decision_id, filename="notes.md", data="# Plan ✓\n".encode())

    assert (script.status_code, huge.status_code) == (415, 413)
    assert (disguised.status_code, disguised.json()["meta"]["error_code"]) == (415, "unsupported_file_type")
    assert await mongo.decision_attachments().count_documents({"_id": disguised_id}) == 0 and object_store.files.keys() == {
        (await mongo.decision_attachments().find_one({"_id": text_id}))["key"],
    }
    assert (text.status_code, text.json()["data"]["kind"]) == (200, "text")


async def test_a_file_is_sent_once_by_its_uploader_and_unsent_ones_stay_private(client, object_store):
    owner, member, _, decision_id = await workspace_of_three(client)
    attachment_id, _ = await upload(client, owner, decision_id)

    private = await client.get(f"/decision-attachments/{attachment_id}/download", headers=member)
    by_other = await send_with(client, member, decision_id, [attachment_id])
    first = await send_with(client, owner, decision_id, [attachment_id], body="once")
    again = await send_with(client, owner, decision_id, [attachment_id], body="twice")

    assert private.status_code == 404
    assert (by_other.status_code, by_other.json()["meta"]["error_code"]) == (422, "attachment_unavailable")
    assert (first.status_code, again.status_code) == (201, 422)
    # the failed sends left nothing behind
    assert await mongo.decision_comments().count_documents({}) == 1


async def test_deleting_a_message_or_its_decision_removes_the_files(client, object_store):
    owner, _, _, decision_id = await workspace_of_three(client)
    first_id, _ = await upload(client, owner, decision_id)
    second_id, _ = await upload(client, owner, decision_id)
    message = (await send_with(client, owner, decision_id, [first_id])).json()["data"]

    await client.delete(f"/decisions/{decision_id}/comments/{message['id']}", headers=owner)
    assert await mongo.decision_attachments().count_documents({"_id": first_id}) == 0
    await mongo.decisions().update_one({"_id": decision_id}, {"$set": {"origin": "suggested", "status": "proposed"}})
    await client.post(f"/decisions/{decision_id}/dismiss", headers=owner)

    assert await mongo.decision_attachments().count_documents({"_id": second_id}) == 0
    assert object_store.files == {}


async def test_the_sweep_removes_uploads_never_sent(client, object_store):
    owner, _, _, decision_id = await workspace_of_three(client)
    unsent_id, _ = await upload(client, owner, decision_id)
    kept_id, _ = await upload(client, owner, decision_id)
    await send(client, owner, decision_id, "no files")
    await mongo.decision_attachments().update_one({"_id": unsent_id}, {"$set": {"expires_at": utcnow() - timedelta(minutes=1)}})

    assert await attachments.sweep() == 1
    assert [doc["_id"] async for doc in mongo.decision_attachments().find({})] == [kept_id]
    assert len(object_store.files) == 1
