"""Exact mentions: tracker keys a text names and docs pages it links."""

from app.decisions import mentions


def test_keys_are_found_and_lookalikes_arent():
    text = "Blocked by DEV-12 and [OPS-7](https://x.atlassian.net/browse/OPS-7); not dev-3, UTF-8, A-1 or X-ray."

    assert mentions.keys(text) == {"DEV-12", "OPS-7", "UTF-8"}


def test_page_links_are_found_by_our_id_and_confluences():
    native = "0f" * 16
    text = (
        f"See [the PRD](/documents/{native}) and "
        "https://acme.atlassian.net/wiki/spaces/ENG/pages/98414/Export+rules, not /pages/12 elsewhere."
    )

    assert mentions.page_ids(text) == {native, "98414"}
