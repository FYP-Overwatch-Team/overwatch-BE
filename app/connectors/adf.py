"""Atlassian Document Format (ADF), the rich text Jira and Confluence share, to and from Markdown."""

import copy
import re
import uuid
from contextvars import ContextVar
from datetime import datetime

# block nodes whose children are themselves blocks, so they sit on separate lines
ADF_BLOCKS = {"doc", "bulletList", "orderedList", "listItem", "blockquote", "panel", "table", "tableRow", "tableCell", "tableHeader", "expand"}
# what a media node reads as, per call
_media_note: ContextVar[str] = ContextVar("media_note", default="attachment")


def adf_to_text(node: dict | None) -> str | None:
    if not node:
        return None
    if node.get("type") == "text":
        return node.get("text", "")
    if node.get("type") == "hardBreak":
        return "\n"
    parts = [adf_to_text(child) or "" for child in node.get("content", [])]
    return ("\n" if node.get("type") in ADF_BLOCKS else "").join(parts)


def adf_to_markdown(node: dict | None, media_note: str = "attachment") -> str | None:
    """Atlassian rich text as Markdown, for showing a description, comment or page as written.

    Covers what the editors produce day to day; media need the site's auth, so they become a note.
    """
    if not node:
        return None
    token = _media_note.set(media_note)
    try:
        return _md_blocks(node.get("content", [])).strip() or None
    finally:
        _media_note.reset(token)


def _md_blocks(nodes: list[dict], indent: str = "", sep: str = "\n\n") -> str:
    return sep.join(block for block in (_md_block(n, indent) for n in nodes) if block)


def _md_block(node: dict, indent: str) -> str:
    kind, attrs, content = node.get("type"), node.get("attrs") or {}, node.get("content", [])
    if kind == "paragraph":
        return indent + _md_inline(content)
    if kind == "heading":
        return f"{indent}{'#' * min(int(attrs.get('level', 1)), 6)} {_md_inline(content)}"
    if kind in ("bulletList", "orderedList"):
        lines = []
        for number, item in enumerate(content, start=int(attrs.get("order", 1))):
            marker = f"{number}. " if kind == "orderedList" else "- "
            # a list item's blocks stay together, so the list stays tight
            body = _md_blocks(item.get("content", []), indent + "  ", "\n").lstrip()
            lines.append(f"{indent}{marker}{body}")
        return "\n".join(lines)
    if kind == "codeBlock":
        code = "".join(child.get("text", "") for child in content)
        return f"{indent}```{attrs.get('language') or ''}\n{code}\n{indent}```"
    if kind in ("blockquote", "panel"):
        return "\n".join(f"{indent}> {line}" if line else f"{indent}>" for line in _md_blocks(content).split("\n"))
    if kind == "rule":
        return f"{indent}---"
    if kind == "table":
        rows = [[_md_inline_blocks(cell.get("content", [])) for cell in row.get("content", [])] for row in content]
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        lines = [f"| {' | '.join(rows[0])} |", f"|{'---|' * width}", *(f"| {' | '.join(row)} |" for row in rows[1:])]
        return "\n".join(indent + line for line in lines)
    if kind in ("mediaSingle", "mediaGroup", "media"):
        return f"{indent}_[{_media_note.get()}]_"
    if kind in ("expand", "nestedExpand"):
        title = attrs.get("title")
        return (f"{indent}**{title}**\n\n" if title else "") + _md_blocks(content, indent)
    # anything else: keep its text
    return indent + (_md_inline(content) if content else "")


def _md_inline_blocks(nodes: list[dict]) -> str:
    """A table cell's blocks on one line."""
    return " ".join(_md_inline(n.get("content", [])) for n in nodes).replace("|", "\\|")


def _md_inline(nodes: list[dict]) -> str:
    parts = []
    for node in nodes:
        kind, attrs = node.get("type"), node.get("attrs") or {}
        if kind == "text":
            parts.append(_md_marks(node.get("text", ""), node.get("marks", [])))
        elif kind == "hardBreak":
            parts.append("  \n")
        elif kind == "mention":
            parts.append(attrs.get("text") or "@someone")
        elif kind == "emoji":
            parts.append(emoji_text(attrs))
        elif kind in ("inlineCard", "blockCard"):
            parts.append(attrs.get("url") or "")
        elif kind == "status":
            parts.append(f"`{attrs.get('text', '')}`")
        elif kind == "date":
            try:
                parts.append(datetime.fromtimestamp(int(attrs.get("timestamp")) / 1000).strftime("%Y-%m-%d"))
            except (TypeError, ValueError):
                pass
        else:
            parts.append(_md_inline(node.get("content", [])))
    return "".join(parts)


_ESCAPED_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def emoji_text(attrs: dict) -> str:
    """An emoji's character: from its `id` (Unicode code points, `1f468-200d-1f469`), else its `text`
    (Confluence templates send it escaped, `\\uD83D\\uDCCA`), else its `:shortName:`."""
    points = (attrs.get("id") or "").split("-")
    if all(re.fullmatch(r"[0-9a-fA-F]{2,6}", point) for point in points):
        values = [int(point, 16) for point in points]
        if all(value <= 0x10FFFF and not 0xD800 <= value <= 0xDFFF for value in values):
            return "".join(map(chr, values))
    text = attrs.get("text") or ""
    if _ESCAPED_RE.search(text):
        units = _ESCAPED_RE.sub(lambda match: chr(int(match.group(1), 16)), text)
        text = units.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return text or attrs.get("shortName") or ""


def _md_marks(text: str, marks: list[dict]) -> str:
    kinds = {mark.get("type"): mark.get("attrs") or {} for mark in marks}
    if "code" in kinds:
        return f"`{text}`"
    if "strong" in kinds:
        text = f"**{text}**"
    if "em" in kinds:
        text = f"_{text}_"
    if "strike" in kinds:
        text = f"~~{text}~~"
    if "link" in kinds and kinds["link"].get("href"):
        text = f"[{text}]({kinds['link']['href']})"
    return text


def markdown_to_adf(md: str | None) -> dict | None:
    """Markdown as Atlassian rich text: the inverse of `adf_to_markdown`, so an edit keeps its formatting.

    Covers what `adf_to_markdown` writes and what an editor produces: headings, lists, code, quotes,
    rules, tables, and bold, italic, strikethrough, code and links inline. A single line break stays
    a line break, so plain text keeps its lines.
    """
    if not md or not md.strip():
        return None
    return {"type": "doc", "version": 1, "content": _adf_blocks(md.replace("\r\n", "\n").split("\n"))}


_FENCE_RE = re.compile(r"^\s*```\s*([\w+#.-]*)\s*$")
_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
_INLINE_RE = re.compile(
    r"`([^`]+)`"
    r"|\[([^\]]+)\]\(([^)\s]+)\)"
    r"|\*\*(.+?)\*\*"
    r"|~~(.+?)~~"
    r"|(?<![\w*])\*(?![\s*])(.+?)(?<!\s)\*(?![\w*])"
    r"|(?<![\w_])_(?![\s_])(.+?)(?<!\s)_(?![\w_])"
    r"|\\([\\`*_\[\]()#+\-.!|~>])"
)


def _starts_block(line: str) -> bool:
    stripped = line.lstrip()
    return bool(
        _FENCE_RE.match(line) or _HEADING_RE.match(line) or _RULE_RE.match(line) or _LIST_RE.match(line) or stripped.startswith(">")
    )


def _dedent(lines: list[str]) -> list[str]:
    depth = min((len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0)
    return [line[depth:] for line in lines]


def _adf_blocks(lines: list[str]) -> list[dict]:
    blocks, i = [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif fence := _FENCE_RE.match(line):
            code, i = [], i + 1
            while i < len(lines) and lines[i].strip() != "```":
                code.append(lines[i])
                i += 1
            block: dict = {"type": "codeBlock"}
            if fence.group(1):
                block["attrs"] = {"language": fence.group(1)}
            if code:
                block["content"] = [{"type": "text", "text": "\n".join(code)}]
            blocks.append(block)
            i += 1
        elif heading := _HEADING_RE.match(line):
            blocks.append({"type": "heading", "attrs": {"level": len(heading.group(1))}, "content": _adf_inline(heading.group(2))})
            i += 1
        elif _RULE_RE.match(line):
            blocks.append({"type": "rule"})
            i += 1
        elif line.lstrip().startswith(">"):
            quoted = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                rest = lines[i].lstrip()[1:]
                quoted.append(rest[1:] if rest.startswith(" ") else rest)
                i += 1
            blocks.append({"type": "blockquote", "content": _adf_blocks(quoted) or [{"type": "paragraph"}]})
        elif "|" in line and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
            rows = [line]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            blocks.append(_adf_table(rows))
        elif _LIST_RE.match(line):
            block, i = _adf_list(lines, i)
            blocks.append(block)
        else:
            paragraph = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
                paragraph.append(lines[i])
                i += 1
            blocks.append({"type": "paragraph", "content": _adf_lines(paragraph)})
    return blocks


def _adf_list(lines: list[str], i: int) -> tuple[dict, int]:
    first = _LIST_RE.match(lines[i])
    base, ordered = len(first.group(1)), first.group(2)[0].isdigit()
    items: list[dict] = []
    while i < len(lines):
        item = _LIST_RE.match(lines[i])
        if not item or len(item.group(1)) != base or item.group(2)[0].isdigit() != ordered:
            break
        children, i = [], i + 1
        while i < len(lines):
            line = lines[i]
            if line.strip() and len(line) - len(line.lstrip()) <= base:
                break
            if not line.strip():
                # a blank line ends the list unless more of it follows
                following = next((later for later in lines[i + 1:] if later.strip()), "")
                if len(following) - len(following.lstrip()) <= base and not _LIST_RE.match(following):
                    break
            children.append(line)
            i += 1
        content = _adf_blocks([item.group(3), *_dedent(children)]) or [{"type": "paragraph"}]
        items.append({"type": "listItem", "content": content})
        while i < len(lines) and not lines[i].strip() and _LIST_RE.match(next((later for later in lines[i:] if later.strip()), "")):
            i += 1
    block: dict = {"type": "orderedList" if ordered else "bulletList", "content": items}
    start = int(re.match(r"\d+", first.group(2)).group()) if ordered else 1
    if start != 1:
        block["attrs"] = {"order": start}
    return block, i


def _adf_table(rows: list[str]) -> dict:
    def cells(row: str) -> list[str]:
        row = row.strip()
        row = row[1:] if row.startswith("|") else row
        row = row[:-1] if row.endswith("|") and not row.endswith("\\|") else row
        return [cell.strip().replace("\\|", "|") for cell in _CELL_SPLIT_RE.split(row)]

    def cell(kind: str, text: str) -> dict:
        return {"type": kind, "content": [{"type": "paragraph", "content": _adf_inline(text)} if text else {"type": "paragraph"}]}

    header, *body = [cells(row) for row in rows]
    return {
        "type": "table",
        "content": [
            {"type": "tableRow", "content": [cell("tableHeader", text) for text in header]},
            *({"type": "tableRow", "content": [cell("tableCell", text) for text in row]} for row in body),
        ],
    }


def _adf_lines(lines: list[str]) -> list[dict]:
    """A paragraph's lines, joined by hard breaks (Markdown's `  ` or `\\` line ends included)."""
    content: list[dict] = []
    for index, line in enumerate(lines):
        if index:
            content.append({"type": "hardBreak"})
        text = line.strip()
        content.extend(_adf_inline(text[:-1].rstrip() if text.endswith("\\") else text))
    return content


def _adf_inline(text: str, marks: tuple[dict, ...] = ()) -> list[dict]:
    nodes: list[dict] = []
    position = 0
    for match in _INLINE_RE.finditer(text):
        if match.start() > position:
            nodes.append(_adf_text(text[position:match.start()], marks))
        code, label, href, strong, strike, star_em, underscore_em, escaped = match.groups()
        if code is not None:
            # Jira's code mark only combines with a link
            nodes.append(_adf_text(code, (*(mark for mark in marks if mark["type"] == "link"), {"type": "code"})))
        elif label is not None:
            nodes.extend(_adf_inline(label, (*marks, {"type": "link", "attrs": {"href": href}})))
        elif strong is not None:
            nodes.extend(_adf_inline(strong, (*marks, {"type": "strong"})))
        elif strike is not None:
            nodes.extend(_adf_inline(strike, (*marks, {"type": "strike"})))
        elif (star_em or underscore_em) is not None:
            nodes.extend(_adf_inline(star_em or underscore_em, (*marks, {"type": "em"})))
        else:
            nodes.append(_adf_text(escaped, marks))
        position = match.end()
    if position < len(text):
        nodes.append(_adf_text(text[position:], marks))
    # escapes split a run of text; Jira rejects empty text nodes
    merged: list[dict] = []
    for node in nodes:
        if not node["text"]:
            continue
        if merged and merged[-1].get("marks") == node.get("marks"):
            merged[-1] = {**merged[-1], "text": merged[-1]["text"] + node["text"]}
        else:
            merged.append(node)
    return merged


def _adf_text(text: str, marks: tuple[dict, ...]) -> dict:
    node: dict = {"type": "text", "text": text}
    if marks:
        node["marks"] = list(marks)
    return node


# ADF and our editor (Tiptap) are both ProseMirror documents, so most nodes map one to one.
# Anything the editor has no node for travels as a `sourceBlock` / `sourceInline` / `sourceMark`
# holding the original ADF, shown read-only and written back exactly as it came.
SOURCE_BLOCK, SOURCE_INLINE, SOURCE_MARK = "sourceBlock", "sourceInline", "sourceMark"
# raise when `adf_to_editor` turns ADF into something new, so mirrored pages are converted again
EDITOR_FORMAT = 3
# an image's width in the editor is a percent of the page column; Confluence may give pixels on its fixed-width body
IMAGE_WIDTH_MIN, IMAGE_WIDTH_MAX = 10, 100
CONFLUENCE_BODY_PX = 760
# image layouts that honour a width; wide and full-width images span the page whatever it says
_SIZED_LAYOUTS = {"center", "align-start", "align-end", "wrap-left", "wrap-right"}
_SAME_BLOCKS = {"paragraph", "heading", "bulletList", "orderedList", "listItem", "blockquote", "codeBlock", "table", "tableRow", "tableHeader", "tableCell"}
# blocks the editor has under its own shape (see `_block`); ADF's nested expand is an expand there
_EDITOR_BLOCKS = _SAME_BLOCKS | {"panel", "expand", "nestedExpand", "layoutSection", "layoutColumn"}
# inline atoms whose ADF attrs the editor keeps as they are
_SAME_ATOMS = {"emoji", "status", "date"}
_CARDS = {"inlineCard", "blockCard", "embedCard"}
# block marks the editor keeps beside alignment, written back as they came (a wide layout)
_KEPT_MARKS = {"layoutSection": "breakout", "expand": "breakout", "nestedExpand": "breakout"}
_TABLE_ATTRS = ("isNumberColumnEnabled", "layout", "width", "displayMode", "localId")
_CELL_ATTRS = ("colspan", "rowspan", "colwidth", "background")
# what an ADF panel may hold; anything else goes after it
_PANEL_BLOCKS = {
    "paragraph", "heading", "bulletList", "orderedList", "blockCard", "mediaGroup", "mediaSingle", "codeBlock",
    "taskList", "rule", "decisionList", "extension",
}
_MAX_COLUMNS = 3
_MARKS = {"strong": "bold", "em": "italic", "strike": "strike", "code": "code", "underline": "underline", "link": "link"}
_MARKS_BACK = {editor: adf for adf, editor in _MARKS.items()}
_ALIGN = {"center": "center", "end": "right"}
_ALIGN_BACK = {editor: adf for adf, editor in _ALIGN.items()}
_LABELS = {
    "decisionList": "Decisions", "blockCard": "Linked list", "mediaGroup": "Files", "mediaSingle": "Image",
    "extension": "Macro", "bodiedExtension": "Macro", "syncBlock": "Synced block", "mention": "Mention", "emoji": "Emoji",
    "inlineCard": "Link", "status": "Status", "date": "Date", "mediaInline": "File", "placeholder": "Placeholder",
    "inlineExtension": "Macro",
}


def _label(node: dict) -> str:
    kind, attrs = node.get("type"), node.get("attrs") or {}
    if kind in ("extension", "bodiedExtension", "inlineExtension"):
        return f"{(attrs.get('extensionKey') or 'macro').replace('-', ' ').capitalize()} macro"
    if kind == "mention":
        return attrs.get("text") or "@someone"
    if kind == "emoji":
        return emoji_text(attrs) or "emoji"
    if kind == "status":
        return attrs.get("text") or "Status"
    if kind in _EDITOR_BLOCKS:
        # a block the editor has, with formatting it hasn't (indentation)
        return "Formatted text"
    return _LABELS.get(kind, (kind or "Content").capitalize())


def _present(attrs: dict, keys: tuple[str, ...]) -> dict:
    return {key: attrs[key] for key in keys if attrs.get(key) is not None}


def adf_to_editor(adf: dict | None, images: dict[str, str] | None = None) -> dict:
    """A page's ADF as editor JSON; `images` maps a media file id to our copy's attachment id."""
    content = _blocks((adf or {}).get("content") or [], images or {})
    return {"type": "doc", "content": content or [{"type": "paragraph"}]}


def _blocks(nodes: list[dict], images: dict[str, str]) -> list[dict]:
    out: list[dict] = []
    for node in nodes:
        out.extend(_block(node, images))
    return out


def _block(node: dict, images: dict[str, str]) -> list[dict]:
    kind, attrs = node.get("type"), node.get("attrs") or {}
    marks = node.get("marks") or []
    if kind == "mediaSingle":
        media = next((child for child in node.get("content") or [] if child.get("type") == "media"), None)
        attachment_id = media and images.get((media.get("attrs") or {}).get("id", ""))
        if attachment_id:
            alt = (media.get("attrs") or {}).get("alt")
            image = {"src": None, "alt": alt, "attachmentId": attachment_id, "raw": node}
            if width := _image_width(attrs):
                image["width"] = width
            return [{"type": "image", "attrs": image}]
    if kind == "taskList":
        return [_task_list(node, images)]
    if kind == "rule":
        return [{"type": "horizontalRule"}]
    # a list of Jira issues is a live query, not a link
    if kind in ("blockCard", "embedCard") and attrs.get("url") and not attrs.get("datasource") and not marks:
        return [_card(node)]
    kept = _KEPT_MARKS.get(kind)
    if kind not in _EDITOR_BLOCKS or any(mark.get("type") not in ("alignment", kept) for mark in marks):
        return [{"type": SOURCE_BLOCK, "attrs": {"raw": node, "label": _label(node)}}]

    out: dict = {"type": "expand" if kind == "nestedExpand" else kind}
    if kind == "heading":
        out["attrs"] = {"level": attrs.get("level", 1)}
    elif kind == "orderedList":
        out["attrs"] = {"start": attrs.get("order", 1)}
    elif kind == "codeBlock":
        out["attrs"] = {"language": attrs.get("language")}
    elif kind == "table" and (table := _present(attrs, _TABLE_ATTRS)):
        out["attrs"] = table
    elif kind in ("tableHeader", "tableCell"):
        out["attrs"] = {"colspan": attrs.get("colspan", 1), "rowspan": attrs.get("rowspan", 1), "colwidth": attrs.get("colwidth")}
        if attrs.get("background"):
            out["attrs"]["background"] = attrs["background"]
    elif kind == "panel":
        out["attrs"] = dict(attrs)
    elif kind in ("expand", "nestedExpand"):
        out["attrs"] = {"title": attrs.get("title") or "", "nested": kind == "nestedExpand"}
    elif kind == "layoutColumn":
        out["attrs"] = {"width": attrs.get("width")}
    if kept and (breakout := [mark for mark in marks if mark.get("type") == kept]):
        out.setdefault("attrs", {})["sourceMarks"] = breakout
    align = next((_ALIGN.get((m.get("attrs") or {}).get("align")) for m in marks if m.get("type") == "alignment"), None)
    if align:
        out.setdefault("attrs", {})["textAlign"] = align
    children = node.get("content") or []
    if kind in ("paragraph", "heading"):
        inline = _inlines(children)
    elif kind == "codeBlock":
        text = "".join(child.get("text", "") for child in children)
        inline = [{"type": "text", "text": text}] if text else []
    else:
        inline = _blocks(children, images)
        if kind not in ("table", "tableRow", "bulletList", "orderedList", "layoutSection") and not inline:
            inline = [{"type": "paragraph"}]
    if inline:
        out["content"] = inline
    return [out]


def _card(node: dict) -> dict:
    """A link shown as a chip, a card or an embed; the original rides along so it goes back as it came."""
    kind, attrs = node["type"], node.get("attrs") or {}
    out = {"url": attrs["url"], "raw": node}
    if kind == "embedCard":
        out |= {"layout": attrs.get("layout"), "width": attrs.get("width"), "originalHeight": attrs.get("originalHeight")}
    return {"type": kind, "attrs": out}


def _task_list(node: dict, images: dict[str, str]) -> dict:
    """ADF nests a task list beside the item it belongs to; the editor puts it inside that item."""
    items: list[dict] = []
    for child in node.get("content") or []:
        if child.get("type") == "taskItem":
            paragraph = {"type": "paragraph", **({"content": inline} if (inline := _inlines(child.get("content") or [])) else {})}
            items.append({"type": "taskItem", "attrs": {"checked": (child.get("attrs") or {}).get("state") == "DONE"}, "content": [paragraph]})
        elif child.get("type") == "taskList" and items:
            items[-1]["content"].append(_task_list(child, images))
    return {"type": "taskList", "content": items or [{"type": "taskItem", "attrs": {"checked": False}, "content": [{"type": "paragraph"}]}]}


def _inlines(nodes: list[dict]) -> list[dict]:
    out: list[dict] = []
    for node in nodes:
        kind, attrs = node.get("type"), node.get("attrs") or {}
        if kind == "text":
            if not node.get("text"):
                continue
            known = [_mark(m) for m in node.get("marks") or [] if m.get("type") in _MARKS]
            other = [m for m in node.get("marks") or [] if m.get("type") not in _MARKS]
            marks = known + ([{"type": SOURCE_MARK, "attrs": {"marks": other}}] if other else [])
            out.append({"type": "text", "text": node["text"], **({"marks": marks} if marks else {})})
        elif kind == "hardBreak":
            out.append({"type": "hardBreak"})
        elif kind in _SAME_ATOMS and not node.get("marks"):
            out.append({"type": kind, "attrs": dict(attrs)})
        elif kind == "inlineCard" and attrs.get("url") and not node.get("marks"):
            out.append(_card(node))
        else:
            out.append({"type": SOURCE_INLINE, "attrs": {"raw": node, "label": _label(node)}})
    return out


def _mark(mark: dict) -> dict:
    kind = _MARKS[mark["type"]]
    if kind == "link":
        return {"type": "link", "attrs": {"href": (mark.get("attrs") or {}).get("href")}}
    return {"type": kind}


def editor_to_adf(doc: dict) -> dict:
    """Editor JSON back to ADF: the inverse of `adf_to_editor`, preserved nodes exactly as they came."""
    return {"type": "doc", "version": 1, "content": _adf_from_blocks(doc.get("content") or [])}


def _adf_from_blocks(nodes: list[dict]) -> list[dict]:
    out: list[dict] = []
    for node in nodes:
        out.extend(_adf_from_block(node))
    return out


def _unwrap(blocks: list[dict], kinds: set[str]) -> list[dict]:
    """Blocks of a kind ADF doesn't allow where they are, replaced by what they hold."""
    out: list[dict] = []
    for block in blocks:
        if block.get("type") not in kinds:
            out.append(block)
        elif block["type"] == "layoutSection":
            out.extend(_unwrap([inner for column in block.get("content") or [] for inner in column.get("content") or []], kinds))
        else:
            out.extend(_unwrap(block.get("content") or [], kinds))
    return out


def _image_width(attrs: dict) -> int | None:
    """A `mediaSingle`'s width as a percent of the column; None when it has none."""
    width = attrs.get("width")
    if isinstance(width, bool) or not isinstance(width, int | float) or width <= 0:
        return None
    if attrs.get("widthType") == "pixel":
        width = width / CONFLUENCE_BODY_PX * 100
    return max(IMAGE_WIDTH_MIN, min(IMAGE_WIDTH_MAX, round(width)))


def _media(single: dict) -> dict | None:
    return next((child for child in single.get("content") or [] if child.get("type") == "media"), None)


def _adf_image(attrs: dict) -> dict:
    """A source image as it goes back: exactly as it came, unless its width or alt text changed here."""
    raw = attrs["raw"]
    media = _media(raw)
    width, alt = attrs.get("width"), attrs.get("alt") or None
    old_alt = ((media or {}).get("attrs") or {}).get("alt") or None
    if width == _image_width(raw.get("attrs") or {}) and alt == old_alt:
        return raw
    single = copy.deepcopy(raw)
    single_attrs = single.setdefault("attrs", {})
    if width:
        single_attrs.update(width=width, widthType="percentage")
        if single_attrs.get("layout") not in _SIZED_LAYOUTS:
            single_attrs["layout"] = "center"
    else:
        single_attrs.pop("width", None)
        single_attrs.pop("widthType", None)
    if media_copy := _media(single):
        media_attrs = media_copy.setdefault("attrs", {})
        if alt:
            media_attrs["alt"] = alt
        else:
            media_attrs.pop("alt", None)
    return single


def _adf_from_block(node: dict) -> list[dict]:
    kind, attrs = node.get("type"), node.get("attrs") or {}
    children = node.get("content") or []
    if kind == SOURCE_BLOCK:
        return [attrs["raw"]] if attrs.get("raw") else []
    if kind == "image":
        # only images that came from the source go back; new uploads stay here until upload is supported
        return [_adf_image(attrs)] if attrs.get("raw") else []
    if kind == "horizontalRule":
        return [{"type": "rule"}]
    if kind == "taskList":
        return [_adf_task_list(node)]
    if kind in ("blockCard", "embedCard"):
        return [card] if (card := _adf_card(node)) else []
    if kind == "layoutSection":
        return [_adf_layout(node)] if children else []
    if kind not in _EDITOR_BLOCKS:
        return []

    out: dict = {"type": "nestedExpand" if kind == "expand" and attrs.get("nested") else kind}
    if kind == "heading":
        out["attrs"] = {"level": attrs.get("level", 1)}
    elif kind == "orderedList" and attrs.get("start", 1) != 1:
        out["attrs"] = {"order": attrs["start"]}
    elif kind == "codeBlock" and attrs.get("language"):
        out["attrs"] = {"language": attrs["language"]}
    elif kind == "table" and (table := _present(attrs, _TABLE_ATTRS)):
        out["attrs"] = table
    elif kind in ("tableHeader", "tableCell"):
        out["attrs"] = {"colspan": attrs.get("colspan", 1), "rowspan": attrs.get("rowspan", 1), **_present(attrs, _CELL_ATTRS[2:])}
    elif kind == "panel":
        out["attrs"] = {"panelType": "info", **{key: value for key, value in attrs.items() if value is not None}}
    elif kind == "expand":
        out["attrs"] = {"title": attrs.get("title") or ""}
    if attrs.get("sourceMarks"):
        out["marks"] = attrs["sourceMarks"]
    if attrs.get("textAlign") in _ALIGN_BACK:
        out["marks"] = [{"type": "alignment", "attrs": {"align": _ALIGN_BACK[attrs["textAlign"]]}}]
    if kind in ("paragraph", "heading"):
        content = _adf_from_inlines(children)
    elif kind == "codeBlock":
        text = "".join(child.get("text", "") for child in children)
        content = [{"type": "text", "text": text}] if text else []
    elif kind == "expand":
        content = _unwrap(_adf_from_blocks(children), {"expand", "nestedExpand", "layoutSection"}) or [{"type": "paragraph"}]
    else:
        content = _adf_from_blocks(children)
    if kind == "panel":
        allowed = [block for block in content if block.get("type") in _PANEL_BLOCKS]
        out["content"] = allowed or [{"type": "paragraph"}]
        return [out, *(block for block in content if block.get("type") not in _PANEL_BLOCKS)]
    if content or kind not in ("paragraph", "heading", "codeBlock"):
        out["content"] = content
    return [out]


def _adf_layout(node: dict) -> dict:
    """Columns as Confluence takes them: at most three, widths adding up to 100, none nested."""
    columns = [column for column in node.get("content") or [] if column.get("type") == "layoutColumn"]
    if len(columns) > _MAX_COLUMNS:
        last = {**columns[_MAX_COLUMNS - 1], "content": [inner for column in columns[_MAX_COLUMNS - 1:] for inner in column.get("content") or []]}
        columns = [*columns[:_MAX_COLUMNS - 1], last]
    widths = [(column.get("attrs") or {}).get("width") or 0 for column in columns]
    if not all(width > 0 for width in widths):
        widths = [1] * len(columns)
    total = sum(widths)
    section: dict = {"type": "layoutSection", "content": [
        {
            "type": "layoutColumn",
            "attrs": {"width": round(width / total * 100, 2)},
            "content": _unwrap(_adf_from_blocks(column.get("content") or []), {"layoutSection"}) or [{"type": "paragraph"}],
        }
        for column, width in zip(columns, widths)
    ]}
    if (node.get("attrs") or {}).get("sourceMarks"):
        section["marks"] = node["attrs"]["sourceMarks"]
    return section


def _adf_card(node: dict) -> dict | None:
    """A link as it came when its address is unchanged; otherwise a new one for that address."""
    kind, attrs = node.get("type"), node.get("attrs") or {}
    raw = attrs.get("raw")
    if raw and (raw.get("attrs") or {}).get("url") == attrs.get("url"):
        return raw
    if not attrs.get("url"):
        return None
    card: dict = {"url": attrs["url"]}
    if kind == "embedCard":
        card = {"url": attrs["url"], "layout": attrs.get("layout") or "center", **_present(attrs, ("width", "originalHeight"))}
    return {"type": kind, "attrs": card}


def _adf_task_list(node: dict) -> dict:
    content: list[dict] = []
    for item in node.get("content") or []:
        first, *rest = item.get("content") or [{"type": "paragraph"}]
        content.append({
            "type": "taskItem",
            "attrs": {"localId": uuid.uuid4().hex, "state": "DONE" if (item.get("attrs") or {}).get("checked") else "TODO"},
            "content": _adf_from_inlines(first.get("content") or []),
        })
        content.extend(_adf_task_list(child) for child in rest if child.get("type") == "taskList")
    return {"type": "taskList", "attrs": {"localId": uuid.uuid4().hex}, "content": content}


def _adf_from_inlines(nodes: list[dict]) -> list[dict]:
    out: list[dict] = []
    for node in nodes:
        kind, attrs = node.get("type"), node.get("attrs") or {}
        if kind == "text" and node.get("text"):
            marks: list[dict] = []
            for mark in node.get("marks") or []:
                if mark.get("type") == SOURCE_MARK:
                    marks.extend((mark.get("attrs") or {}).get("marks") or [])
                elif mark.get("type") == "link":
                    marks.append({"type": "link", "attrs": {"href": (mark.get("attrs") or {}).get("href")}})
                elif mark.get("type") in _MARKS_BACK:
                    marks.append({"type": _MARKS_BACK[mark["type"]]})
            out.append({"type": "text", "text": node["text"], **({"marks": marks} if marks else {})})
        elif kind == "hardBreak":
            out.append({"type": "hardBreak"})
        elif kind in _SAME_ATOMS:
            out.append({"type": kind, "attrs": {key: value for key, value in attrs.items() if value is not None}})
        elif kind == "inlineCard" and (card := _adf_card(node)):
            out.append(card)
        elif kind == SOURCE_INLINE and attrs.get("raw"):
            out.append(attrs["raw"])
    return out
