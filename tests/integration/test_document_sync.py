"""Confluence spaces mirrored into the docs tree, and edits made here pushed back."""

import json
from datetime import timedelta

import pytest
import respx
from httpx import Response

from app.connectors import doc_sync
from app.core.time import utcnow
from app.connectors.adf import EDITOR_FORMAT
from app.db import mongo, oauth_tokens
from app.documents import service as documents
from app.workers import jobs
from tests.integration.test_auth_flow import auth_headers, login

API = "https://api.atlassian.com/ex/confluence/cloud-1/wiki/api/v2"
V1 = "https://api.atlassian.com/ex/confluence/cloud-1/wiki/rest/api"
SCOPES = ["read:page:confluence", "write:page:confluence", "offline_access"]
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def adf(text: str, *extra: dict) -> dict:
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}, *extra]}


def page(page_id: str, title: str, parent: str | None, version: int = 1, *, parent_type: str = "page", status: str = "current", position: int = 0) -> dict:
    return {
        "id": page_id, "title": title, "status": status, "parentId": parent, "parentType": parent_type if parent else None,
        "position": position, "version": {"number": version, "createdAt": utcnow().isoformat()}, "_links": {"webui": f"/spaces/ENG/pages/{page_id}"},
    }


class FakeConfluence:
    """The parts of Confluence's API the sync uses, over pages kept in memory."""

    def __init__(self) -> None:
        self.pages = {
            "1": page("1", "Home", None, position=0),
            "2": page("2", "Architecture", "1", position=1),
            "3": page("3", "Decisions", "9", parent_type="folder", position=2),
        }
        self.bodies = {"1": adf("Welcome"), "2": adf("Services", {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "file-7", "type": "file"}}]}), "3": adf("Use Mongo")}
        self.folders = {"9": {
            "id": "9", "title": "Records", "status": "current", "parentId": "1", "parentType": "page", "position": 3, "version": {"number": 1},
        }}
        self.body_requests: list[list[str]] = []
        self.puts: list[dict] = []
        self.archived: list = []
        self.deleted: list = []
        self.put_status = 200

    def routes(self, router: respx.Router) -> None:
        router.get(f"{API}/spaces/100").mock(return_value=Response(200, json={"id": "100", "key": "ENG", "name": "Engineering", "_links": {"webui": "/spaces/ENG"}}))
        router.get(f"{API}/spaces/100/pages").mock(side_effect=self._list)
        router.get(url__regex=rf"{API}/folders/\d+").mock(side_effect=self._folder)
        router.get(url__regex=rf"{API}/pages/\d+/attachments").mock(side_effect=self._attachments)
        router.get(f"{API}/pages").mock(side_effect=self._bodies)
        router.get(url__regex=rf"{V1}/content/\d+/child/attachment/att\d+/download").mock(return_value=Response(200, content=PNG))
        router.put(url__regex=rf"{API}/pages/\d+").mock(side_effect=self._put)
        router.post(f"{API}/pages").mock(side_effect=self._create)
        router.post(f"{V1}/content/archive").mock(side_effect=lambda request: self.archived.append(json.loads(request.content)) or Response(202, json={}))
        router.delete(url__regex=rf"{API}/pages/\d+").mock(side_effect=lambda request: self.deleted.append(str(request.url)) or Response(204))

    def _list(self, request) -> Response:
        # two pages of results, to follow Confluence's cursor
        results = sorted(self.pages.values(), key=lambda p: p["id"])
        if "cursor" in request.url.params:
            return Response(200, json={"results": results[2:], "_links": {}})
        return Response(200, json={"results": results[:2], "_links": {"next": "/wiki/api/v2/spaces/100/pages?cursor=c2"}})

    def _folder(self, request) -> Response:
        folder = self.folders.get(request.url.path.split("/")[-1])
        return Response(200, json=folder) if folder else Response(404, json={})

    def _attachments(self, request) -> Response:
        page_id = request.url.path.split("/")[-2]
        if page_id != "2":
            return Response(200, json={"results": []})
        return Response(200, json={"results": [{
            "id": "att5", "fileId": "file-7", "title": "diagram.png", "mediaType": "image/png", "fileSize": len(PNG), "version": {"number": 1},
        }]})

    def _bodies(self, request) -> Response:
        ids = request.url.params["id"].split(",")
        self.body_requests.append(ids)
        return Response(200, json={"results": [
            {**self.pages[i], "body": {"atlas_doc_format": {"value": json.dumps(self.bodies[i])}}} for i in ids
        ], "_links": {}})

    def _put(self, request) -> Response:
        body = json.loads(request.content)
        self.puts.append(body)
        if self.put_status != 200:
            return Response(self.put_status, json={})
        page_id = request.url.path.split("/")[-1]
        self.pages[page_id]["version"]["number"] = body["version"]["number"]
        self.bodies[page_id] = json.loads(body["body"]["value"])
        return Response(200, json={**self.pages[page_id], "version": {"number": body["version"]["number"]}})

    def _create(self, request) -> Response:
        body = json.loads(request.content)
        created = page("50", body["title"], body.get("parentId"))
        self.pages["50"], self.bodies["50"] = created, adf("")
        return Response(200, json=created)


@pytest.fixture
def confluence() -> FakeConfluence:
    return FakeConfluence()


async def connect(workspace_id: str, scopes: list[str] = SCOPES) -> None:
    await oauth_tokens.save(workspace_id, "atlassian", {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    await mongo.connections().insert_one({
        "_id": "conn-c", "workspace_id": workspace_id, "provider": "confluence", "auth_backend": "native_oauth", "status": "active",
        "scopes": scopes, "external_account": {"id": "cloud-1", "name": "acme", "url": "https://acme.atlassian.net"},
    })
    await mongo.sync_targets().insert_one({
        "workspace_id": workspace_id, "provider": "confluence", "key": "100", "name": "Engineering", "status": "active",
        "sync_status": "pending", "last_synced_at": None, "last_full_sync_at": None, "connected_at": utcnow(),
    })


async def sync(confluence: FakeConfluence, workspace_id: str, run=None) -> None:
    with respx.mock(assert_all_called=False) as router:
        confluence.routes(router)
        await (run or jobs.run_connector_sync)(workspace_id, "confluence", "100")
    target = await mongo.sync_targets().find_one({"key": "100"})
    assert target["sync_status"] == "done", target.get("sync_error")


async def node(external_id: str) -> dict:
    return await mongo.documents().find_one({"source": "confluence", "external_id": external_id})


@pytest.fixture
async def synced(client, confluence) -> dict:
    headers = auth_headers(await login(client))
    await connect(headers["X-Workspace-Id"])
    await sync(confluence, headers["X-Workspace-Id"])
    return headers


async def test_a_space_becomes_a_folder_with_its_page_tree(client, confluence, synced):
    tree = (await client.get("/documents", headers=synced)).json()["data"]
    space = tree[0]
    home, arch, decisions, records = await node("1"), await node("2"), await node("3"), await node("9")

    assert (space["title"], space["kind"], space["source"], space["editable"]) == ("Engineering", "folder", "confluence", False)
    assert home["parent_id"] == space["id"] and arch["parent_id"] == home["_id"]
    # a page in a Confluence folder sits in that folder here
    assert (records["kind"], records["parent_id"]) == ("folder", home["_id"])
    assert decisions["ancestor_ids"] == [space["id"], home["_id"], records["_id"]]
    assert (home["child_count"], records["child_count"]) == (2, 1)

    got = (await client.get(f"/documents/{arch['_id']}", headers=synced)).json()["data"]
    assert got["content_md"].startswith("Services") and got["external_url"] == "https://acme.atlassian.net/wiki/spaces/ENG/pages/2"
    assert (got["editable"], got["push_state"]) == (True, "none")
    # the image was copied, so it shows from our storage
    image = got["content"]["content"][1]
    assert image["type"] == "image" and image["attrs"]["attachmentId"] in got["attachment_urls"]


async def test_later_syncs_fetch_only_pages_that_changed(client, confluence, synced):
    await mongo.sync_targets().update_one({"key": "100"}, {"$set": {"last_full_sync_at": utcnow()}})
    confluence.pages["3"]["version"]["number"] = 2
    confluence.bodies["3"] = adf("Use Postgres")
    confluence.body_requests.clear()

    await sync(confluence, synced["X-Workspace-Id"])

    assert confluence.body_requests == [["3"]]
    assert (await node("3"))["content_md"] == "Use Postgres"


async def test_pages_converted_by_an_older_editor_format_are_converted_again(client, confluence, synced):
    await mongo.documents().update_one({"source": "confluence", "external_id": "2"}, {"$set": {"editor_format": 1}})
    confluence.body_requests.clear()

    await sync(confluence, synced["X-Workspace-Id"])

    assert confluence.body_requests == [["2"]]
    assert (await node("2"))["editor_format"] == EDITOR_FORMAT


async def test_a_moved_page_takes_its_subtree_and_gone_pages_are_archived_together(client, confluence, synced):
    confluence.folders.pop("9")
    confluence.pages["2"]["parentId"] = None  # to the space's top level
    confluence.pages["3"]["status"] = "trashed"
    await mongo.sync_targets().update_one({"key": "100"}, {"$set": {"last_full_sync_at": None}})

    await sync(confluence, synced["X-Workspace-Id"])

    space = (await client.get("/documents", headers=synced)).json()["data"][0]
    arch = await node("2")
    assert arch["ancestor_ids"] == [space["id"]] and arch["parent_id"] == space["id"]
    assert (await node("9"))["archived_by_sync"] and (await node("3"))["archived_at"] is not None
    # the folder that went is one archive, with the page that was in it
    assert (await node("3"))["archive_root_id"] == (await node("9"))["_id"]
    assert (await node("1"))["child_count"] == 0


async def test_removing_a_space_archives_it_and_adding_it_back_restores_it(client, confluence, synced, record_jobs):
    space_id = (await client.get("/documents", headers=synced)).json()["data"][0]["id"]

    await client.delete("/connections/confluence/targets/100", headers=synced)
    assert (await client.get("/documents", headers=synced)).json()["data"] == []
    archived = (await client.get("/documents/archived", headers=synced)).json()["data"]
    assert [(a["id"], a["archived_with"], a["source"]) for a in archived] == [(space_id, 4, "confluence")]

    await client.post("/connections/confluence/targets", json={"key": "100"}, headers=synced)
    assert record_jobs[-1] == ("sync", synced["X-Workspace-Id"], "confluence", "100")
    await sync(confluence, synced["X-Workspace-Id"], doc_sync.run_document_sync)
    assert [n["id"] for n in (await client.get("/documents", headers=synced)).json()["data"]] == [space_id]
    assert (await node("3"))["archived_at"] is None


async def save(client, headers: dict, node_id: str, text: str) -> dict:
    current = (await client.get(f"/documents/{node_id}", headers=headers)).json()["data"]
    resp = await client.patch(f"/documents/{node_id}", json={
        "content": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]},
        "content_md": text, "base_version": current["version"],
    }, headers=headers)
    assert resp.status_code == 200, resp.json()
    return resp.json()["data"]


async def push(confluence: FakeConfluence) -> None:
    await mongo.documents().update_many({"push_state": "pending"}, {"$set": {"push_due_at": utcnow() - timedelta(seconds=1)}})
    with respx.mock as router:
        confluence.routes(router)
        await doc_sync.push_due()


async def test_edits_settle_then_go_back_as_one_version(client, confluence, synced):
    page_id = (await node("3"))["_id"]
    await save(client, synced, page_id, "Use Mongo, sharded")
    saved = await save(client, synced, page_id, "Use Mongo, sharded by workspace")
    assert saved["push_state"] == "pending"

    # not yet: it waits until nobody has typed for a while
    with respx.mock(assert_all_called=False) as router:
        confluence.routes(router)
        await doc_sync.push_due()
    assert confluence.puts == []

    await push(confluence)

    assert len(confluence.puts) == 1
    sent = confluence.puts[0]
    assert (sent["version"]["number"], sent["title"]) == (2, "Decisions")
    assert sent["version"]["message"] == "Edited by Octo Cat via Overwatch"
    assert json.loads(sent["body"]["value"])["content"][0]["content"][0]["text"] == "Use Mongo, sharded by workspace"
    pushed = await node("3")
    assert (pushed["push_state"], pushed["external_version"]) == ("none", 2)


async def test_an_edit_at_both_ends_keeps_both_for_the_user_to_choose(client, confluence, synced):
    page_id = (await node("3"))["_id"]
    await save(client, synced, page_id, "Mine")
    confluence.put_status = 409
    confluence.pages["3"]["version"]["number"] = 5
    confluence.bodies["3"] = adf("Theirs")

    await push(confluence)

    got = (await client.get(f"/documents/{page_id}", headers=synced)).json()["data"]
    assert (got["push_state"], got["content_md"], got["editable"]) == ("conflict", "Theirs", False)
    assert got["conflict"]["content_md"] == "Mine"
    blocked = await client.patch(f"/documents/{page_id}", json={"title": "x"}, headers=synced)
    assert blocked.json()["meta"]["error_code"] == "read_only"

    resolved = await client.post(f"/documents/{page_id}/resolve-conflict", json={"action": "overwrite", "base_version": got["version"]}, headers=synced)
    assert (resolved.json()["data"]["push_state"], resolved.json()["data"]["content_md"], resolved.json()["data"]["conflict"]) == ("pending", "Mine", None)

    confluence.put_status = 200
    await push(confluence)
    assert confluence.puts[-1]["version"]["number"] == 6


async def test_keeping_the_sources_version_drops_the_local_copy(client, confluence, synced):
    page_id = (await node("3"))["_id"]
    await save(client, synced, page_id, "Mine")
    confluence.pages["3"]["version"]["number"] = 5
    confluence.bodies["3"] = adf("Theirs")
    await mongo.sync_targets().update_one({"key": "100"}, {"$set": {"last_full_sync_at": utcnow()}})

    # a sync leaves a page with unsent edits to the push, which finds the conflict
    await sync(confluence, synced["X-Workspace-Id"])
    assert (await node("3"))["push_state"] == "pending"
    confluence.put_status = 409
    await push(confluence)
    got = (await client.get(f"/documents/{page_id}", headers=synced)).json()["data"]
    assert (got["push_state"], got["content_md"]) == ("conflict", "Theirs")

    resolved = await client.post(f"/documents/{page_id}/resolve-conflict", json={"action": "keep_source", "base_version": got["version"]}, headers=synced)
    assert (resolved.json()["data"]["push_state"], resolved.json()["data"]["content_md"], resolved.json()["data"]["editable"]) == ("none", "Theirs", True)


async def test_a_push_landing_during_a_sync_is_not_a_conflict(client, confluence, synced):
    page_id = (await node("3"))["_id"]
    await save(client, synced, page_id, "Mine")
    # the push's write reached Confluence (now v2), but it hasn't recorded that yet
    await mongo.documents().update_one({"_id": page_id}, {"$set": {"push_state": "pushing", "push_started_at": utcnow()}})
    confluence.pages["3"]["version"]["number"] = 2
    await mongo.sync_targets().update_one({"key": "100"}, {"$set": {"last_full_sync_at": utcnow()}})

    await sync(confluence, synced["X-Workspace-Id"])

    stored = await node("3")
    assert (stored["push_state"], stored["content_md"]) == ("pushing", "Mine")


async def test_a_page_deleted_while_its_space_was_removed_stays_archived(client, confluence, synced, record_jobs):
    await client.delete("/connections/confluence/targets/100", headers=synced)
    confluence.pages.pop("2")
    await client.post("/connections/confluence/targets", json={"key": "100"}, headers=synced)

    await sync(confluence, synced["X-Workspace-Id"], doc_sync.run_document_sync)

    gone, back = await node("2"), await node("3")
    assert gone["archived_at"] is not None and gone["archived_root"] is True
    assert back["archived_at"] is None
    archived = (await client.get("/documents/archived", headers=synced)).json()["data"]
    assert [a["title"] for a in archived] == ["Architecture"]


async def test_docs_spaces_are_not_boards(client, confluence, synced):
    sources = [board["source"] for board in (await client.get("/boards", headers=synced)).json()["data"]]
    assert sources == ["native"]


async def test_without_write_access_mirrored_pages_are_read_only(client, confluence):
    headers = auth_headers(await login(client))
    await connect(headers["X-Workspace-Id"], scopes=["read:page:confluence"])
    await sync(confluence, headers["X-Workspace-Id"])
    page_id = (await node("3"))["_id"]

    got = (await client.get(f"/documents/{page_id}", headers=headers)).json()["data"]
    resp = await client.patch(f"/documents/{page_id}", json={"title": "x"}, headers=headers)

    assert got["editable"] is False
    assert resp.json()["meta"]["error_code"] == "read_only"


async def test_new_pages_under_a_mirrored_page_are_created_at_the_source(client, confluence, synced):
    parent = await node("2")
    with respx.mock as router:
        confluence.routes(router)
        resp = await client.post("/documents", json={"kind": "page", "title": "ADR 7", "parent_id": parent["_id"]}, headers=synced)
    created = resp.json()["data"]

    assert (resp.status_code, created["source"], created["editable"]) == (201, "confluence", True)
    assert (await node("50"))["parent_id"] == parent["_id"]
    folder = await client.post("/documents", json={"kind": "folder", "title": "x", "parent_id": parent["_id"]}, headers=synced)
    assert folder.json()["meta"]["error_code"] == "parent_is_mirrored"


async def test_archiving_archives_at_the_source_and_deleting_deletes_there(client, confluence, synced):
    arch = await node("2")
    with respx.mock as router:
        confluence.routes(router)
        archived = await client.post(f"/documents/{arch['_id']}/archive", json={}, headers=synced)
    assert archived.status_code == 200
    assert confluence.archived == [{"pages": [{"id": 2}]}]

    restore = await client.post(f"/documents/{arch['_id']}/restore", headers=synced)
    assert restore.json()["meta"]["error_code"] == "restore_at_source"

    with respx.mock as router:
        confluence.routes(router)
        deleted = await client.delete(f"/documents/{arch['_id']}", headers=synced)
    assert deleted.status_code == 200
    # to the trash, then out of it
    assert [url.split("?")[-1] if "?" in url else "" for url in confluence.deleted] == ["", "purge=true"]
    assert await node("2") is None
    assert await mongo.doc_attachments().count_documents({"node_id": arch["_id"]}) == 0


async def test_old_archives_are_purged_here_only(client, confluence, synced):
    await client.delete("/connections/confluence/targets/100", headers=synced)
    native = (await client.post("/documents", json={"kind": "page", "title": "Notes"}, headers=synced)).json()["data"]
    await client.post(f"/documents/{native['id']}/archive", json={}, headers=synced)
    await mongo.documents().update_many({"source": "confluence"}, {"$set": {"archived_at": utcnow() - timedelta(days=31)}})

    with respx.mock:  # nothing is deleted at the source
        purged = await documents.purge_expired(timedelta(days=30))

    assert purged == 1
    assert await mongo.documents().count_documents({"source": "confluence"}) == 0
    assert await mongo.doc_attachments().count_documents({}) == 0
    assert [a["title"] for a in (await client.get("/documents/archived", headers=synced)).json()["data"]] == ["Notes"]
