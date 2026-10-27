from app.decisions.quotes import contains, fingerprint


def test_a_quote_matches_its_source_whatever_the_markdown_spacing_or_case():
    source = "## Export\n\nAdmins export as **CSV** or [PDF](https://x.dev/pdf).\nGuests can't."

    assert contains(source, "admins export as CSV or PDF")
    assert contains(source, "Guests   can't.")
    assert not contains(source, "Guests can.")
    assert not contains(source, "   ")


def test_the_fingerprint_changes_with_the_words_not_the_formatting():
    assert fingerprint("Admins export **CSV**") == fingerprint("admins  export CSV")
    assert fingerprint("Admins export CSV") != fingerprint("Admins export PDF")
