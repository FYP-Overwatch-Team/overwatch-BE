"""Where each kind of file lives in the bucket, laid out like a folder tree people can browse:

    workspaces/<workspace_id>/
        docs/<page_id>/images/<attachment_id>.png
        decisions/<decision_id>/attachments/<attachment_id>.pdf

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


def decision_attachment(workspace_id: str, decision_id: str, attachment_id: str, extension: str) -> str:
    """A file shared in a decision's discussion; its real name is kept in the database, not here."""
    return f"{workspace_prefix(workspace_id)}decisions/{decision_id}/attachments/{attachment_id}.{extension}"
