"""The one-off move of a suggestion's "### Sources" list from its background onto the decision."""

from scripts.migrate_decision_sources import sources_for, split_sources

WRITE_UP = (
    "### Background\nGuests asked [S1].\n\n### Pros\n- Simple [S3]\n\n"
    "### Sources\n- [S1] Export PRD\n- [S3] DEV-12 PDF export\n\n"
    "*Pros, cons and the example are the AI's reading of the sources: check them before accepting.*"
)


def test_the_list_and_note_leave_the_background():
    body, listed = split_sources(WRITE_UP)

    assert body == "### Background\nGuests asked [S1].\n\n### Pros\n- Simple [S3]"
    assert listed == [("S1", "Export PRD"), ("S3", "DEV-12 PDF export")]
    # nothing to take from a background without them
    assert split_sources("Just text.") == ("Just text.", [])


def test_listed_sources_link_to_the_evidence_quoting_them_and_evidence_sources_follow():
    decision = {"context_md": WRITE_UP, "evidence": [
        {"kind": "document", "ref_id": "prd", "title": "Export PRD"},
        {"kind": "work_item", "ref_id": "dev-9", "title": "DEV-9 Other"},
    ]}

    body, sources = sources_for(decision)

    assert body.endswith("- Simple [S3]")
    assert sources == [
        {"id": "S1", "kind": "document", "ref_id": "prd", "title": "Export PRD"},
        # cited, but no evidence quotes it: kept unlinked so [S3] still resolves
        {"id": "S3", "kind": None, "ref_id": None, "title": "DEV-12 PDF export"},
        {"id": "S4", "kind": "work_item", "ref_id": "dev-9", "title": "DEV-9 Other"},
    ]


def test_a_manual_decision_numbers_its_evidence():
    decision = {"context_md": None, "evidence": [{"kind": "document", "ref_id": "a", "title": "A"}]}

    assert sources_for(decision) == (None, [{"id": "S1", "kind": "document", "ref_id": "a", "title": "A"}])
