"""The docs tree: folders and pages, ordering, breadcrumbs and conflict-safe saves."""

from app.db import mongo
from tests.integration.test_auth_flow import auth_headers, login
from tests.integration.test_workspaces import in_workspace, team, user


def text_doc(text: str) -> dict:
    return {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


async def create(client, headers: dict, **body) -> dict:
    resp = await client.post("/documents", json={"kind": "page", **body}, headers=headers)
    assert resp.status_code == 201, resp.json()
    return resp.json()["data"]


async def titles(client, headers: dict, parent_id: str | None = None) -> list[str]:
    params = {"parent_id": parent_id} if parent_id else {}
    return [n["title"] for n in (await client.get("/documents", params=params, headers=headers)).json()["data"]]


async def test_folders_and_pages_nest_with_breadcrumbs(client):
    headers = auth_headers(await login(client))
    eng = await create(client, headers, kind="folder", title="Engineering")
    arch = await create(client, headers, kind="folder", title="Architecture", parent_id=eng["id"])
    page = await create(client, headers, title="  Review  ", parent_id=arch["id"], content=text_doc("Hi"), content_md="Hi")

    got = (await client.get(f"/documents/{page['id']}", headers=headers)).json()["data"]
    root = (await client.get("/documents", headers=headers)).json()

    assert (got["title"], got["content_md"], got["version"], got["editable"]) == ("Review", "Hi", 0, True)
    assert got["author"]["name"] == "Octo Cat"
    assert [c["title"] for c in got["breadcrumbs"]] == ["Engineering", "Architecture"]
    assert [(n["title"], n["has_children"]) for n in root["data"]] == [("Engineering", True)]
    assert "content" not in root["data"][0] and root["meta"]["total"] == 1
    assert await titles(client, headers, arch["id"]) == ["Review"]


async def test_new_nodes_go_last_unless_placed_between_siblings(client):
    headers = auth_headers(await login(client))
    a = await create(client, headers, title="A")
    c = await create(client, headers, title="C")
    await create(client, headers, title="B", before_id=a["id"], after_id=c["id"])
    await create(client, headers, title="Start", after_id=a["id"])

    assert await titles(client, headers) == ["Start", "A", "B", "C"]


async def test_ranks_are_spread_out_again_when_midpoints_run_out(client):
    headers = auth_headers(await login(client))
    a = await create(client, headers, title="A")
    b = await create(client, headers, title="B")
    await mongo.documents().update_one({"_id": b["id"]}, {"$set": {"rank": a["rank"] + 1e-7}})

    await create(client, headers, title="Mid", before_id=a["id"], after_id=b["id"])

    assert await titles(client, headers) == ["A", "Mid", "B"]


async def test_a_stale_save_is_refused_and_changes_nothing(client):
    headers = auth_headers(await login(client))
    page = await create(client, headers, title="Plan")

    first = await client.patch(f"/documents/{page['id']}", json={"content": text_doc("v1"), "content_md": "v1", "base_version": 0}, headers=headers)
    stale = await client.patch(f"/documents/{page['id']}", json={"content": text_doc("mine"), "content_md": "mine", "base_version": 0}, headers=headers)
    renamed = await client.patch(f"/documents/{page['id']}", json={"title": "Roadmap"}, headers=headers)

    assert first.json()["data"]["version"] == 1
    assert (stale.status_code, stale.json()["meta"]["error_code"]) == (409, "doc_conflict")
    assert (renamed.json()["data"]["content_md"], renamed.json()["data"]["title"], renamed.json()["data"]["version"]) == ("v1", "Roadmap", 1)


async def test_content_rules(client):
    headers = auth_headers(await login(client))
    folder = await create(client, headers, kind="folder", title="F")
    page = await create(client, headers, title="P")

    no_version = await client.patch(f"/documents/{page['id']}", json={"content": text_doc("x")}, headers=headers)
    into_folder = await client.patch(f"/documents/{folder['id']}", json={"content": text_doc("x"), "base_version": 0}, headers=headers)
    huge = await client.patch(f"/documents/{page['id']}", json={"content": text_doc("x" * 1_000_001), "base_version": 0}, headers=headers)
    long_title = await client.post("/documents", json={"kind": "page", "title": "x" * 256}, headers=headers)

    assert no_version.status_code == 422
    assert into_folder.json()["meta"]["error_code"] == "folder_has_no_content"
    assert (huge.status_code, long_title.status_code) == (422, 422)


async def test_nesting_is_capped(client):
    headers = auth_headers(await login(client))
    parent_id = None
    for depth in range(10):
        parent_id = (await create(client, headers, kind="folder", title=f"L{depth}", parent_id=parent_id))["id"]

    too_deep = await client.post("/documents", json={"kind": "page", "parent_id": parent_id}, headers=headers)

    assert (too_deep.status_code, too_deep.json()["meta"]["error_code"]) == (409, "max_depth")


async def test_documents_belong_to_their_workspace(client):
    alice, bob = await user(client, "alice"), await user(client, "bob")
    acme = await team(client, alice)
    page = await create(client, in_workspace(alice, acme), title="Secret")
    bobs_own = (await client.get("/workspaces", headers=bob)).json()["data"][0]["id"]
    as_bob = in_workspace(bob, bobs_own)

    read = await client.get(f"/documents/{page['id']}", headers=as_bob)
    write = await client.patch(f"/documents/{page['id']}", json={"title": "x"}, headers=as_bob)
    child = await client.post("/documents", json={"kind": "page", "parent_id": page["id"]}, headers=as_bob)
    outsider = await client.get("/documents", headers=in_workspace(bob, acme))

    assert [r.json()["meta"]["error_code"] for r in (read, write, child)] == ["document_not_found"] * 3
    assert outsider.status_code == 404


async def archive(client, headers: dict, node_id: str, **body):
    return await client.post(f"/documents/{node_id}/archive", json=body, headers=headers)


async def crumbs(client, headers: dict, node_id: str) -> list[str]:
    return [c["title"] for c in (await client.get(f"/documents/{node_id}", headers=headers)).json()["data"]["breadcrumbs"]]


async def test_archiving_a_folder_archives_what_is_inside_and_restore_brings_it_all_back(client):
    headers = auth_headers(await login(client))
    eng = await create(client, headers, kind="folder", title="Eng")
    specs = await create(client, headers, kind="folder", title="Specs", parent_id=eng["id"])
    page = await create(client, headers, title="API", parent_id=specs["id"])

    assert (await archive(client, headers, specs["id"])).status_code == 200
    hidden = await client.get(f"/documents/{page['id']}", headers=headers)
    eng_row = (await client.get("/documents", headers=headers)).json()["data"][0]
    listed = (await client.get("/documents/archived", headers=headers)).json()["data"]

    assert hidden.json()["meta"]["error_code"] == "document_not_found"
    assert (eng_row["title"], eng_row["has_children"]) == ("Eng", False)
    assert [(a["title"], a["archived_with"]) for a in listed] == [("Specs", 1)]

    restored = await client.post(f"/documents/{specs['id']}/restore", headers=headers)
    assert restored.json()["data"]["breadcrumbs"] == [{"id": eng["id"], "title": "Eng"}]
    assert await crumbs(client, headers, page["id"]) == ["Eng", "Specs"]
    assert (await client.get("/documents", headers=headers)).json()["data"][0]["has_children"] is True
    assert (await client.get("/documents/archived", headers=headers)).json()["data"] == []


async def test_archiving_can_move_what_is_inside_to_another_folder_first(client):
    headers = auth_headers(await login(client))
    old = await create(client, headers, kind="folder", title="Old")
    sub = await create(client, headers, kind="folder", title="Sub", parent_id=old["id"])
    deep = await create(client, headers, title="Deep", parent_id=sub["id"])
    await create(client, headers, title="Notes", parent_id=old["id"])
    keep = await create(client, headers, kind="folder", title="Keep")
    await create(client, headers, title="Existing", parent_id=keep["id"])

    resp = await archive(client, headers, old["id"], children="move", move_to=keep["id"])

    assert resp.status_code == 200
    assert await titles(client, headers) == ["Keep"]
    assert await titles(client, headers, keep["id"]) == ["Existing", "Sub", "Notes"]
    assert await crumbs(client, headers, deep["id"]) == ["Keep", "Sub"]
    assert [(a["title"], a["archived_with"]) for a in (await client.get("/documents/archived", headers=headers)).json()["data"]] == [("Old", 0)]


async def test_archiving_can_move_what_is_inside_to_the_top_level(client):
    headers = auth_headers(await login(client))
    top = await create(client, headers, kind="folder", title="Top")
    mid = await create(client, headers, kind="folder", title="Mid", parent_id=top["id"])
    leaf = await create(client, headers, kind="folder", title="Leaf", parent_id=mid["id"])
    page = await create(client, headers, title="Page", parent_id=leaf["id"])

    await archive(client, headers, mid["id"], children="move", move_to=None)

    assert await titles(client, headers) == ["Top", "Leaf"]
    assert await crumbs(client, headers, page["id"]) == ["Leaf"]
    assert (await client.get("/documents", headers=headers)).json()["data"][0]["has_children"] is False


async def test_moving_into_the_archived_folder_or_too_deep_is_refused(client):
    headers = auth_headers(await login(client))
    folder = await create(client, headers, kind="folder", title="F")
    inner = await create(client, headers, kind="folder", title="Inner", parent_id=folder["id"])
    await create(client, headers, title="Under inner", parent_id=inner["id"])
    chain = None
    for depth in range(9):
        chain = (await create(client, headers, kind="folder", title=f"L{depth}", parent_id=chain))["id"]

    into_self = await archive(client, headers, folder["id"], children="move", move_to=folder["id"])
    into_inner = await archive(client, headers, folder["id"], children="move", move_to=inner["id"])
    # Inner would sit 10 deep, the page under it 11
    too_deep = await archive(client, headers, folder["id"], children="move", move_to=chain)

    assert [r.json()["meta"]["error_code"] for r in (into_self, into_inner, too_deep)] == ["move_into_archived", "move_into_archived", "max_depth"]
    assert await crumbs(client, headers, inner["id"]) == ["F"]


async def test_a_restored_archive_whose_folder_is_gone_returns_to_the_top_level(client):
    headers = auth_headers(await login(client))
    parent = await create(client, headers, kind="folder", title="Parent")
    child = await create(client, headers, kind="folder", title="Child", parent_id=parent["id"])
    page = await create(client, headers, title="Page", parent_id=child["id"])
    await archive(client, headers, child["id"])
    await archive(client, headers, parent["id"])

    restored = (await client.post(f"/documents/{child['id']}/restore", headers=headers)).json()["data"]

    assert (restored["parent_id"], restored["breadcrumbs"]) == (None, [])
    assert await crumbs(client, headers, page["id"]) == ["Child"]
    assert await titles(client, headers) == ["Child"]
