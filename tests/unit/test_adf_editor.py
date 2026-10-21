from app.connectors.adf import adf_to_editor, adf_to_markdown, editor_to_adf, emoji_text

TEXT = {"type": "text", "text": "hi"}


def doc(*blocks: dict) -> dict:
    return {"type": "doc", "version": 1, "content": list(blocks)}


def test_everyday_blocks_map_both_ways_unchanged():
    adf = doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Title", "marks": [{"type": "strong"}]}]},
        {"type": "paragraph", "marks": [{"type": "alignment", "attrs": {"align": "center"}}], "content": [
            {"type": "text", "text": "a ", "marks": [{"type": "em"}, {"type": "underline"}]},
            {"type": "text", "text": "link", "marks": [{"type": "link", "attrs": {"href": "https://x.dev"}}]},
            {"type": "hardBreak"},
            {"type": "text", "text": "code", "marks": [{"type": "code"}]},
        ]},
        {"type": "bulletList", "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": [TEXT]}]}]},
        {"type": "orderedList", "attrs": {"order": 3}, "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": [TEXT]}]}]},
        {"type": "codeBlock", "attrs": {"language": "python"}, "content": [{"type": "text", "text": "print(1)"}]},
        {"type": "blockquote", "content": [{"type": "paragraph", "content": [TEXT]}]},
        {"type": "rule"},
        {"type": "table", "content": [{"type": "tableRow", "content": [
            {"type": "tableHeader", "attrs": {"colspan": 1, "rowspan": 1, "colwidth": [120]}, "content": [{"type": "paragraph", "content": [TEXT]}]},
            {"type": "tableCell", "attrs": {"colspan": 2, "rowspan": 1}, "content": [{"type": "paragraph"}]},
        ]}]},
    )

    editor = adf_to_editor(adf)

    assert [n["type"] for n in editor["content"]] == [
        "heading", "paragraph", "bulletList", "orderedList", "codeBlock", "blockquote", "horizontalRule", "table",
    ]
    assert editor["content"][1]["attrs"] == {"textAlign": "center"}
    assert editor["content"][1]["content"][0]["marks"] == [{"type": "italic"}, {"type": "underline"}]
    assert editor["content"][3]["attrs"] == {"start": 3}
    assert editor_to_adf(editor) == adf


def test_tasks_nest_inside_their_item_in_the_editor_and_beside_it_in_adf():
    adf = doc({"type": "taskList", "attrs": {"localId": "l1"}, "content": [
        {"type": "taskItem", "attrs": {"localId": "t1", "state": "DONE"}, "content": [TEXT]},
        {"type": "taskList", "attrs": {"localId": "l2"}, "content": [{"type": "taskItem", "attrs": {"localId": "t2", "state": "TODO"}, "content": [TEXT]}]},
    ]})

    editor = adf_to_editor(adf)
    item = editor["content"][0]["content"][0]
    assert item["attrs"] == {"checked": True}
    assert [n["type"] for n in item["content"]] == ["paragraph", "taskList"]

    back = editor_to_adf(editor)["content"][0]
    assert [n["type"] for n in back["content"]] == ["taskItem", "taskList"]
    assert back["content"][0]["attrs"]["state"] == "DONE" and back["content"][1]["content"][0]["attrs"]["state"] == "TODO"


def test_what_the_editor_lacks_is_kept_verbatim_and_written_back():
    decisions = {"type": "decisionList", "attrs": {"localId": "d"}, "content": []}
    macro = {"type": "extension", "attrs": {"extensionKey": "jira-issues", "parameters": {"jql": "x"}}}
    mention = {"type": "mention", "attrs": {"id": "acc-1", "text": "@Sam"}}
    coloured = {"type": "text", "text": "red", "marks": [{"type": "strong"}, {"type": "textColor", "attrs": {"color": "#f00"}}]}
    indented = {"type": "paragraph", "marks": [{"type": "indentation", "attrs": {"level": 1}}], "content": [TEXT]}
    adf = doc(decisions, macro, {"type": "paragraph", "content": [mention, coloured]}, indented)

    editor = adf_to_editor(adf)

    kinds = [(n["type"], n.get("attrs", {}).get("label")) for n in editor["content"]]
    assert kinds == [("sourceBlock", "Decisions"), ("sourceBlock", "Jira issues macro"), ("paragraph", None), ("sourceBlock", "Formatted text")]
    inline = editor["content"][2]["content"]
    assert inline[0] == {"type": "sourceInline", "attrs": {"raw": mention, "label": "@Sam"}}
    assert inline[1]["marks"] == [{"type": "bold"}, {"type": "sourceMark", "attrs": {"marks": [{"type": "textColor", "attrs": {"color": "#f00"}}]}}]
    assert editor_to_adf(editor) == adf


def test_copied_images_become_editor_images_and_go_back_as_they_came():
    media = {"type": "mediaSingle", "attrs": {"layout": "center"}, "content": [{"type": "media", "attrs": {"id": "file-1", "type": "file", "alt": "chart"}}]}
    unknown = {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "file-2", "type": "file"}}]}

    editor = adf_to_editor(doc(media, unknown), {"file-1": "att-1"})

    assert editor["content"][0] == {"type": "image", "attrs": {"src": None, "alt": "chart", "attachmentId": "att-1", "raw": media}}
    assert editor["content"][1]["type"] == "sourceBlock"
    # an image uploaded here has no source copy yet, so it isn't sent
    uploaded = {"type": "image", "attrs": {"src": "https://x/a.png", "attachmentId": "att-9"}}
    assert editor_to_adf({**editor, "content": [*editor["content"], uploaded]}) == doc(media, unknown)


def image_block(**single_attrs) -> dict:
    return {"type": "mediaSingle", "attrs": {"layout": "center", **single_attrs}, "content": [{"type": "media", "attrs": {"id": "file-1", "type": "file", "alt": "chart"}}]}


def test_an_image_brings_its_width_as_a_percent_of_the_column():
    def width(block):
        return adf_to_editor(doc(block), {"file-1": "att-1"})["content"][0]["attrs"].get("width")

    assert width(image_block(width=50, widthType="percentage")) == 50
    assert width(image_block(width=66.6)) == 67
    assert width(image_block(width=380, widthType="pixel")) == 50
    assert width(image_block(width=5)) == 10
    assert width(image_block()) is None


def test_an_untouched_image_goes_back_exactly_as_it_came():
    block = image_block(width=380, widthType="pixel")
    editor = adf_to_editor(doc(block), {"file-1": "att-1"})

    assert editor_to_adf(editor) == doc(block)


def test_a_resized_or_described_image_goes_back_changed():
    block = image_block(layout="wide")
    editor = adf_to_editor(doc(block), {"file-1": "att-1"})
    image = editor["content"][0]

    image["attrs"].update(width=40, alt="Code graph")
    sent = editor_to_adf(editor)["content"][0]
    assert sent["attrs"] == {"layout": "center", "width": 40, "widthType": "percentage"}
    assert sent["content"][0]["attrs"]["alt"] == "Code graph"
    assert block["attrs"] == {"layout": "wide"}, "the original stays untouched"

    image["attrs"].update(width=None, alt=None)
    sent = editor_to_adf(editor)["content"][0]
    assert "width" not in sent["attrs"] and "alt" not in sent["content"][0]["attrs"]


def test_an_empty_page_is_one_empty_paragraph():
    assert adf_to_editor(None) == {"type": "doc", "content": [{"type": "paragraph"}]}
    assert adf_to_editor(doc()) == {"type": "doc", "content": [{"type": "paragraph"}]}


def test_emoji_read_as_their_character_whatever_atlassian_sends():
    assert emoji_text({"id": "1f4ca", "shortName": ":bar_chart:", "text": "\\uD83D\\uDCCA"}) == "\U0001f4ca"
    assert emoji_text({"id": "1f468-200d-1f469-200d-1f467"}) == "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert emoji_text({"id": "", "text": "\\uD83E\\uDD14"}) == "\U0001f914"
    assert emoji_text({"id": "atlassian-blue_star", "shortName": ":blue_star:"}) == ":blue_star:"
    assert emoji_text({"id": "a3f9e2c1-0d4b-4c1e-9f7a-1b2c3d4e5f60", "shortName": ":party:", "text": ":party:"}) == ":party:"


def test_emoji_status_and_date_are_editor_nodes_and_go_back_unchanged():
    emoji = {"type": "emoji", "attrs": {"shortName": ":thinking:", "id": "1f914", "text": "\\uD83E\\uDD14"}}
    status = {"type": "status", "attrs": {"text": "DONE", "color": "green", "localId": "s1"}}
    date = {"type": "date", "attrs": {"timestamp": "1700000000000"}}
    adf = doc({"type": "heading", "attrs": {"level": 2}, "content": [emoji, {"type": "text", "text": " Assumptions"}, status, date]})

    editor = adf_to_editor(adf)

    assert [n["type"] for n in editor["content"][0]["content"]] == ["emoji", "text", "status", "date"]
    assert editor_to_adf(editor) == adf
    assert adf_to_markdown(adf).startswith("## \U0001f914 Assumptions`DONE`")


def test_columns_are_editable_and_go_back_as_confluence_takes_them():
    column = lambda width, *blocks: {"type": "layoutColumn", "attrs": {"width": width}, "content": list(blocks)}
    para = {"type": "paragraph", "content": [TEXT]}
    adf = doc({"type": "layoutSection", "marks": [{"type": "breakout", "attrs": {"mode": "wide"}}], "content": [column(33.33, para), column(66.67, para)]})

    editor = adf_to_editor(adf)

    section = editor["content"][0]
    assert (section["type"], [c["attrs"]["width"] for c in section["content"]]) == ("layoutSection", [33.33, 66.67])
    assert editor_to_adf(editor) == adf
    # four columns from the editor, with no widths and one nested inside: three, even, flat
    nested = {"type": "layoutSection", "content": [column(None, para), column(None, para)]}
    edited = {"type": "doc", "content": [{"type": "layoutSection", "content": [
        column(None, nested), column(None, para), column(None, para), column(None, para),
    ]}]}
    back = editor_to_adf(edited)["content"][0]["content"]
    assert [c["attrs"]["width"] for c in back] == [33.33, 33.33, 33.33]
    assert [b["type"] for b in back[0]["content"]] == ["paragraph", "paragraph"]
    assert len(back[2]["content"]) == 2


def test_links_cards_and_embeds_keep_their_data_until_the_address_changes():
    inline = {"type": "inlineCard", "attrs": {"url": "https://loom.com/share/a", "data": {"name": "Demo"}}}
    card = {"type": "blockCard", "attrs": {"url": "https://x.dev/doc"}}
    embed = {"type": "embedCard", "attrs": {"url": "https://www.loom.com/share/b", "layout": "wide", "originalHeight": 360}}
    issues = {"type": "blockCard", "attrs": {"datasource": {"id": "jira"}, "url": "https://x.atlassian.net/issues"}}
    adf = doc({"type": "paragraph", "content": [inline]}, card, embed, issues)

    editor = adf_to_editor(adf)

    assert editor["content"][0]["content"][0] == {"type": "inlineCard", "attrs": {"url": inline["attrs"]["url"], "raw": inline}}
    assert [n["type"] for n in editor["content"][1:]] == ["blockCard", "embedCard", "sourceBlock"]
    assert editor["content"][2]["attrs"]["originalHeight"] == 360
    assert editor_to_adf(editor) == adf
    editor["content"][1]["attrs"]["url"] = "https://x.dev/other"
    added = {"type": "embedCard", "attrs": {"url": "https://youtu.be/v", "layout": None, "width": None, "originalHeight": None, "raw": None}}
    back = editor_to_adf({**editor, "content": [*editor["content"], added]})["content"]
    assert back[1] == {"type": "blockCard", "attrs": {"url": "https://x.dev/other"}}
    assert back[-1] == {"type": "embedCard", "attrs": {"url": "https://youtu.be/v", "layout": "center"}}


def test_panels_expands_and_table_formatting_survive_the_round_trip():
    para = {"type": "paragraph", "content": [TEXT]}
    panel = {"type": "panel", "attrs": {"panelType": "custom", "panelIcon": ":dart:", "panelIconId": "1f3af", "panelColor": "#E6FCFF"}, "content": [para]}
    expand = {"type": "expand", "attrs": {"title": "Details"}, "marks": [{"type": "breakout", "attrs": {"mode": "wide"}}], "content": [para]}
    table = {"type": "table", "attrs": {"layout": "full-width", "isNumberColumnEnabled": True, "localId": "t"}, "content": [
        {"type": "tableRow", "content": [{"type": "tableCell", "attrs": {"colspan": 1, "rowspan": 1, "background": "#deebff"}, "content": [
            {"type": "nestedExpand", "attrs": {"title": "More"}, "content": [para]},
        ]}]},
    ]}
    adf = doc(panel, expand, table)

    editor = adf_to_editor(adf)

    assert [n["type"] for n in editor["content"]] == ["panel", "expand", "table"]
    assert editor["content"][2]["content"][0]["content"][0]["content"][0]["attrs"] == {"title": "More", "nested": True}
    assert editor_to_adf(editor) == adf
    # what a panel can't hold moves out after it
    edited = {"type": "doc", "content": [{"type": "panel", "attrs": {"panelType": "note"}, "content": [table]}]}
    assert [n["type"] for n in editor_to_adf(edited)["content"]] == ["panel", "table"]
