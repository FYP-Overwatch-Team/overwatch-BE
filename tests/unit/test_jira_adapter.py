import pytest

from app.connectors.jira import JiraAdapter, _fields, adf_to_markdown, adf_to_text, markdown_to_adf, status_category

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


def test_plain_text_keeps_its_lines_in_adf():
    assert adf_to_text(markdown_to_adf("First line\nSecond line\n\nAfter a gap")) == "First line\nSecond line\nAfter a gap"
    assert markdown_to_adf("") is None and markdown_to_adf("  \n") is None and adf_to_text(None) is None


@pytest.mark.parametrize("md", [
    "# Title\n\nSome **bold**, _em_, ~~gone~~, `code` and a [link](https://x.io).",
    "- one\n- two\n  - nested _a_\n  - nested b\n- three",
    "3. third\n4. fourth",
    "```python\nprint('x')\n\nmore\n```",
    "> quoted **line**\n>\n> second",
    "Above\n\n---\n\nBelow",
    "| a | b |\n|---|---|\n| `x` | y \\| z |",
    "line one  \nline two",
    "[**bold link**](https://a.b)",
    "snake_case_name and 2*3*4 stay plain",
])
def test_markdown_round_trips_through_adf(md):
    assert adf_to_markdown(markdown_to_adf(md)) == md


def test_an_editors_markdown_reads_the_same_as_ours():
    # `*em*` and `\\` line ends are other spellings of what adf_to_markdown writes
    assert adf_to_markdown(markdown_to_adf("*em* and **strong**\\\nnext")) == "_em_ and **strong**  \nnext"
    # what the description editor writes: padded tables and escaped underscores
    assert adf_to_markdown(markdown_to_adf("\n| a   | b   |\n| --- | --- |\n| `x` | y   |\n")) == "| a | b |\n|---|---|\n| `x` | y |"
    assert adf_to_markdown(markdown_to_adf("snake\\_case\\_name")) == "snake_case_name"


def test_inline_code_drops_marks_jira_cant_combine_with_it():
    para = markdown_to_adf("**a `b` c**")["content"][0]["content"]
    assert para[1] == {"type": "text", "text": "b", "marks": [{"type": "code"}]}


def test_a_description_is_sent_to_jira_formatted():
    sent = _fields({"description": "Use **this** one"})["description"]
    assert sent["content"][0]["content"][1] == {"type": "text", "text": "this", "marks": [{"type": "strong"}]}


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


def test_rich_text_becomes_markdown():
    from app.connectors.jira import adf_to_markdown

    def text(value, *marks):
        return {"type": "text", "text": value, "marks": [{"type": m} if isinstance(m, str) else m for m in marks]}

    doc = {"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 3}, "content": [text("Acceptance criteria")]},
        {"type": "paragraph", "content": [
            text("Returns "), text("components", "code"), text(", "), text("bold", "strong"), text(" and "),
            text("docs", {"type": "link", "attrs": {"href": "https://example.com"}}),
        ]},
        {"type": "orderedList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [text("first")]},
                {"type": "bulletList", "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": [text("nested")]}]}]}]},
            {"type": "listItem", "content": [{"type": "paragraph", "content": [text("second")]}]},
        ]},
        {"type": "codeBlock", "attrs": {"language": "ts"}, "content": [text("const a = 1;")]},
        {"type": "mediaSingle", "content": [{"type": "media"}]},
        {"type": "paragraph", "content": [{"type": "mention", "attrs": {"text": "@Fusbir"}}, text(" please review")]},
    ]}

    assert adf_to_markdown(doc) == (
        "### Acceptance criteria\n\n"
        "Returns `components`, **bold** and [docs](https://example.com)\n\n"
        "1. first\n  - nested\n2. second\n\n"
        "```ts\nconst a = 1;\n```\n\n"
        "_[attachment in Jira]_\n\n"
        "@Fusbir please review"
    )
    assert adf_to_markdown(None) is None


def test_normalise_reads_the_details_a_ticket_view_shows():
    board = {"fields": {"sprint": "customfield_10020", "story_points": "customfield_10016"}}
    raw = {"id": "1", "key": "DEV-1380", "fields": {
        "summary": "Empty components list", "status": {"id": "3", "name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "reporter": {"accountId": "acc-f", "displayName": "Fusbir Rehman"}, "labels": ["api"], "duedate": "2026-10-20",
        "parent": {"key": "DEV-1298", "fields": {"summary": "Composition fixes"}},
        "customfield_10016": 1.0,
        "subtasks": [{"key": "DEV-1381", "fields": {"summary": "Write test", "status": {"name": "Done", "statusCategory": {"key": "done"}}}}],
        "issuelinks": [{"type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
                        "outwardIssue": {"key": "DEV-1303", "fields": {"summary": "Order responses", "status": {"name": "Done", "statusCategory": {"key": "done"}}}}}],
    }}

    item = JiraAdapter().normalise(CONN, "DEV", raw, board)

    assert item["reporter"] == {"id": "acc-f", "name": "Fusbir Rehman"}
    assert (item["labels"], item["due_date"], item["story_points"]) == (["api"], "2026-10-20", 1.0)
    assert item["parent"] == {"key": "DEV-1298", "title": "Composition fixes"}
    assert item["subtasks"] == [{"key": "DEV-1381", "title": "Write test", "status": "Done", "status_category": "done"}]
    assert item["links"] == [{"type": "relates to", "key": "DEV-1303", "title": "Order responses", "status": "Done", "status_category": "done"}]
    assert item["tracker_sprint"] is None  # no open sprint: the backlog
