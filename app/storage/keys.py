"""Where each kind of file lives in the bucket, laid out like a folder tree people can browse:

    workspaces/<workspace_id>/
        docs/<page_id>/images/<attachment_id>.png

Everything a workspace owns sits under its folder, so new kinds of files (chat uploads, exports) slot in
beside `docs/`, and a workspace's files can be listed or removed by prefix. Records store the full key,
so changing this layout only affects files written after the change.
"""

#: the images pages may show, by content type, with the extension their keys end in
IMAGE_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}


def workspace_prefix(workspace_id: str) -> str:
    return f"workspaces/{workspace_id}/"


def doc_image(workspace_id: str, page_id: str, attachment_id: str, content_type: str) -> str:
    return f"{workspace_prefix(workspace_id)}docs/{page_id}/images/{attachment_id}.{IMAGE_EXTENSIONS[content_type]}"
