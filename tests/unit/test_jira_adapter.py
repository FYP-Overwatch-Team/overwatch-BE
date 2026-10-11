import pytest

from app.connectors.jira import JiraAdapter, adf_to_text, status_category, text_to_adf

CONN = {"external_account": {"id": "cloud-1", "url": "https://acme.atlassian.net"}}


@pytest.mark.parametrize(("status", "expected"), [
    ({"name": "Whatever", "statusCategory": {"key": "new"}}, "todo"),
    ({"name": "QA", "statusCategory": {"key": "indeterminate"}}, "in_progress"),
    ({"name": "Shipped", "statusCategory": {"key": "done"}}, "done"),
    ({"name": "Closed"}, "done"),
    ({"name": "Backlog"}, "todo"),
    ({"name": "In Review"}, "in_progress"),
    ({}, "in_progress"),
])
def test_status_category(status, expected):
    assert status_category(status) == expected


def test_description_round_trips_through_adf():
    text = "First line\n\nThird line"
    assert adf_to_text(text_to_adf(text)) == text
    assert text_to_adf("") is None and adf_to_text(None) is None


def test_lists_and_breaks_become_lines():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "a"}, {"type": "hardBreak"}, {"type": "text", "text": "b"}]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]},
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "two"}]}]},
        ]},
    ]}
    assert adf_to_text(doc) == "a\nb\none\ntwo"


def test_normalise_a_sparse_issue():
    item = JiraAdapter().normalise(CONN, "OVR", {"id": "7", "key": "OVR-7", "fields": {}})
    assert (item["title"], item["status_category"], item["assignee"], item["description"]) == ("", "in_progress", None, None)
    assert item["external_url"] == "https://acme.atlassian.net/browse/OVR-7"
    assert item["rank"] > 0  # no created date: placed at the end
