"""Checking a model's answer against the context it was shown.

The prompt asks the model to stay inside the graph; these checks are what
make that hold. The model returns its answer with inline `[nX]` references and the
relationships its answer relies on:

- a relationship the context never showed means the answer is refused —
  a claim about how the code connects that the graph can't back is worse
  than no answer;
- a reference the context never contained is dropped, from the text too;
- a code citation (`[s3:42-58]`) must fall inside lines the context showed;
  it becomes `[c1]`, and its code is copied from the file, never taken from
  the model, so what's shown as the repository's code is the repository's code;
- an image may only point at a chat attachment or be inline data, and links
  keep their text but lose their target: an answer can quote repository
  text, so it must not make the viewer's browser contact anyone else;
- a flow is written as a `STEPS:` list after the summary; each step is kept
  only when its node is one the context showed;
- the reply may end with `FOLLOW_UPS:` (questions to go deeper) or `ASK:` (a
  question back with options); both are cut out of the text, cleaned of
  references and capped, and never two questions back in a row;
- a callout is a quote opening with `[!NOTE]`, `[!TIP]` or `[!WARNING]`; other
  kinds lose the marker and stay a plain quote, and only the first few count.

Each section is one constant marker, one regex and one cap, so a new kind of
block is added the same way.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from app.query.context import GroundingContext, Relation, clean

CANNOT_DETERMINE = "This can't be determined from the repository's graph."
#: Where chat attachments are served from; the only place an answer's image may load from.
ATTACHMENT_PATH = "/chat/attachments/"

#: The most lines one code citation shows.
MAX_CITED_LINES = 60

#: The line that starts a flow's steps, as `STEPS:`.
STEPS_MARKER = "STEPS"
#: Fewer steps than this is no walkthrough; the list stays in the text. More than the most are dropped.
MIN_FLOW_STEPS = 2
MAX_FLOW_STEPS = 12

REF_RE = re.compile(r"\[(n\d+)\]")
# `[s3:42-58]` or `[s3:42]`; a stray `L` before a number is forgiven
_CODE_REF_RE = re.compile(r"[ \t]*\[(s\d+):\s*L?(\d+)(?:\s*[-–]\s*L?(\d+))?\]")
# what models actually write: `[ n4 ]`, `[ s3 : 42 ‑ 58 ]` (any dash), `【s3:42-58】`,
# `[s1:5-13; s1:14-18]` or `[s1:5-13, 14-18]`; made canonical before checking
_LOOSE_REF_RE = re.compile(r"[\[【]\s*(n\d+)\s*[\]】]")
_LOOSE_CODE_GROUP_RE = re.compile(r"[\[【]\s*(s\d+\s*:[^\]】\n]{1,200}?)\s*[\]】]")
_LOOSE_RANGE_RE = re.compile(r"^(?:(s\d+)\s*:)?\s*L?(\d+)(?:\s*[-‐‑‒–—]\s*L?(\d+))?$")
# two or more canonical code citations side by side, and the parts of one such run
_CODE_REF_RUN_RE = re.compile(r"[ \t]*\[s\d+:\d+(?:-\d+)?\](?:[ \t]*\[s\d+:\d+(?:-\d+)?\])+")
_RUN_PART_RE = re.compile(r"\[(s\d+):(\d+)(?:-(\d+))?\]")
# `[c1][c1]` or `[c1] [c1]`: one range cited twice in a row
_REPEATED_CITATION_RE = re.compile(r"(\[c\d+\])(?:\s*\1)+")
_CITATION_RE = re.compile(r"([ \t]*)\[(c\d+)\]")
# a reference with the space before it, so removing one leaves no gap
_SPACED_REF_RE = re.compile(r"[ \t]*\[(n\d+)\]")
_FENCE_RE = re.compile(r"(```[\s\S]*?(?:```|$))")
_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\(\s*<?([^)\s>]*)>?(?:\s+[\"'][^\"'\n]*[\"'])?\s*\)")
_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]+)\]\([^)\n]*\)")
_REFERENCE_DEFINITION_RE = re.compile(r"^[ \t]*\[[^\]\n]+\]:[ \t]*\S.*$", re.MULTILINE)
_INLINE_IMAGE_RE = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$")
# `STEPS:`, or as models dress it up, `**Steps:**` or `### Steps:`
_STEPS_LINE_RE = re.compile(rf"^[ \t*_#]*{STEPS_MARKER}[ \t*_]*:[ \t*_]*$", re.IGNORECASE | re.MULTILINE)
#: Questions to go deeper, after an overview: how many and how long.
FOLLOW_UPS_MARKER = "FOLLOW_UPS"
MAX_FOLLOW_UPS = 4
MAX_FOLLOW_UP_CHARS = 120
#: A question back when the question could mean different things: its options, and their length.
ASK_MARKER = "ASK"
MIN_ASK_OPTIONS = 2
MAX_ASK_OPTIONS = 4
MAX_ASK_CHARS = 200
MAX_OPTION_CHARS = 80
#: Callouts the answer may use, and how many count.
CALLOUT_KINDS = ("NOTE", "TIP", "WARNING")
MAX_CALLOUTS = 2

_FOLLOW_UPS_LINE_RE = re.compile(r"^[ \t*_#]*follow[ _-]?ups[ \t*_]*:[ \t*_]*$", re.IGNORECASE)
# upper case only: "Ask: …" in ordinary prose is not a question back
_ASK_LINE_RE = re.compile(rf"^[ \t*_#]*{ASK_MARKER}[ \t*_]*:[ \t*_]*(.*?)[ \t*_]*$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)$")
_CALLOUT_RE = re.compile(r"^([ \t]*>[ \t]*)\[!(\w+)\][ \t]*$", re.MULTILINE)
_EMPHASIS_RE = re.compile(r"[*_`]+")
_EXACT_STEPS_LINE_RE = re.compile(rf"^{STEPS_MARKER}:[ \t]*\n?", re.MULTILINE)
# a step starts at the margin; an indented numbered line is a list inside a step
_STEP_ITEM_RE = re.compile(r"^ ?\d+[.)][ \t]+(.*)$")
_NESTED_ITEM_RE = re.compile(r"^\s*(?:\d+[.)]|[-*+])[ \t]+")


@dataclass(frozen=True, slots=True)
class Citation:
    ref: str
    node_id: str
    module_id: str
    name: str


@dataclass(frozen=True, slots=True)
class CodeCitation:
    """Lines of the repository an answer points at with `[cN]`, copied from the file at `commit_sha`."""

    ref: str
    path: str
    commit_sha: str
    start_line: int
    end_line: int
    code: str
    language: str | None
    #: the node the code belongs to; None for a range an earlier answer cited
    node_id: str | None


@dataclass(frozen=True, slots=True)
class FlowStep:
    """One step of a flow an answer walks through: the node it happens in, what happens, and the code showing it."""

    #: the step's `nX`, also among the answer's citations
    ref: str
    node_id: str
    #: Markdown; may hold further `[nX]` and `[cN]`
    text: str
    #: a `cN` among the answer's code citations
    citation_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ClarifyingQuestion:
    """A question back, asked only when the question could mean different things."""

    question: str
    options: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    text: str
    citations: tuple[Citation, ...] = ()
    code_citations: tuple[CodeCitation, ...] = ()
    #: The flow the answer walks through, in order; empty when it isn't about one.
    steps: tuple[FlowStep, ...] = ()
    #: Questions the reader might ask next, to go deeper.
    follow_ups: tuple[str, ...] = ()
    #: A question back instead of (part of) an answer.
    ask: ClarifyingQuestion | None = None
    #: False when the model said it couldn't answer, or the answer was refused.
    grounded: bool = True
    #: The question's @-mentions that exist in the graph.
    mentions: tuple[Citation, ...] = ()
    #: What was thrown away, for logging.
    dropped_refs: tuple[str, ...] = field(default=(), compare=False)
    dropped_code_refs: tuple[str, ...] = field(default=(), compare=False)
    unsupported: tuple[Relation, ...] = field(default=(), compare=False)


def ground(
    context: GroundingContext,
    answer: str,
    *,
    answerable: bool = True,
    relations: Sequence[Relation] = (),
    may_ask: bool = True,
) -> GroundedAnswer:
    """Turn the model's reply into an answer that only says what the graph shows.

    `relations` are those the reply says the answer relies on; `may_ask` is False right
    after a question back: another one is written into the text instead.
    """
    text = _outside_code(safe_markdown(answer.strip()), _canonical_refs)
    text, follow_ups, ask = _split_trailing(text)
    if ask and not may_ask:
        text, ask = f"{text}\n\n{ask.question}\n\n" + "\n".join(f"- {option}" for option in ask.options), None
    if ask:
        follow_ups = ()
    text = _outside_code(text, _callouts)
    if not answerable or not text:
        text = _outside_code(_SPACED_REF_RE.sub("", text), lambda part: _CODE_REF_RE.sub("", part))
        return GroundedAnswer(text.strip() or CANNOT_DETERMINE, grounded=False, ask=ask)

    unsupported = tuple(relation for relation in relations if not context.supports(relation))
    if unsupported:
        return GroundedAnswer(CANNOT_DETERMINE, grounded=False, unsupported=unsupported)

    cited = list(dict.fromkeys(REF_RE.findall(text)))
    known = [ref for ref in cited if ref in context.nodes]
    text = _SPACED_REF_RE.sub(lambda match: match[0] if match[1] in context.nodes else "", text)
    text, code_citations, dropped_code = _cite_code(context, text)
    if ask:  # a question back walks through nothing yet
        text, steps = _outside_code(text, lambda part: _EXACT_STEPS_LINE_RE.sub("", part)), ()
    else:
        text, steps = _split_steps(context, text)
    return GroundedAnswer(
        text.strip(),
        tuple(Citation(ref, context.nodes[ref], context.modules[ref], context.labels[ref]) for ref in known),
        code_citations,
        steps,
        follow_ups,
        ask,
        dropped_refs=tuple(ref for ref in cited if ref not in context.nodes),
        dropped_code_refs=dropped_code,
    )


def _cite_code(context: GroundingContext, text: str) -> tuple[str, tuple[CodeCitation, ...], tuple[str, ...]]:
    """`[s3:42-58]` -> `[c1]` when those lines were shown; anything else is dropped from the text."""
    cited: dict[tuple[str, int, int], CodeCitation] = {}
    dropped: list[str] = []

    def cite(match: re.Match) -> str:
        source = context.sources.get(match[1])
        start = int(match[2])
        end = int(match[3] or match[2])
        marker = match[0].lstrip(" \t")
        if source is None or context.commit is None or not source.start_line <= start <= end <= source.end_line:
            dropped.append(marker)
            return ""
        end = min(end, start + MAX_CITED_LINES - 1)
        key = (match[1], start, end)
        if key not in cited:
            offset = start - source.start_line
            cited[key] = CodeCitation(
                f"c{len(cited) + 1}", source.path, context.commit, start, end,
                "\n".join(source.lines[offset:offset + end - start + 1]), source.language, source.node_id,
            )
        return f"{match[0][:len(match[0]) - len(marker)]}[{cited[key].ref}]"

    text = _outside_code(text, lambda part: _REPEATED_CITATION_RE.sub(r"\1", _CODE_REF_RE.sub(cite, part)))
    return text, tuple(cited.values()), tuple(dropped)


def _split_steps(context: GroundingContext, text: str) -> tuple[str, tuple[FlowStep, ...]]:
    """The summary, and the steps of the `STEPS:` list after it; checked text in, so every reference is known.

    A step is the node it opens with and what follows; a line without a node joins the step before it,
    and two steps in a row on one node are one. Text after the list goes back to the summary. With
    fewer than `MIN_FLOW_STEPS` steps the text is left as written.
    """
    start = 0
    for index, part in enumerate(_FENCE_RE.split(text)):
        if not index % 2 and (marker := _STEPS_LINE_RE.search(part)):
            break
        start += len(part)
    else:
        return text, ()
    summary, section = text[:start + marker.start()].rstrip(), text[start + marker.end():]

    lead, items, tail = _step_items(section)
    steps: list[FlowStep] = []
    for item in items:
        node = REF_RE.search(item)
        if node is None:
            if steps:
                steps[-1] = replace(steps[-1], text=f"{steps[-1].text}\n{item}")
            else:
                lead.append(item)
            continue
        previous = steps[-1] if steps and steps[-1].ref == node[1] else None
        body = item[:node.start()].rstrip(" \t") + item[node.end():]
        # a step shows one citation; a second one on the same node stays inline
        code = None if previous and previous.citation_ref else _CITATION_RE.search(body)
        if code:
            body = body[:code.start()] + body[code.end():]
        citation_ref = code[2] if code else None
        if previous:
            steps[-1] = replace(
                previous, text=f"{previous.text} {body.strip()}".strip(), citation_ref=previous.citation_ref or citation_ref,
            )
        else:
            steps.append(FlowStep(node[1], context.nodes[node[1]], body.strip(), citation_ref))

    if len(steps) < MIN_FLOW_STEPS:
        # not a walkthrough after all: the list stays as written, without the marker the prompt asked for
        # (a heading the model chose itself, like "Steps:", is kept)
        return _outside_code(text, lambda part: _EXACT_STEPS_LINE_RE.sub("", part)), ()
    return "\n\n".join(part for part in (summary, *lead, tail) if part), tuple(steps[:MAX_FLOW_STEPS])


def _step_items(section: str) -> tuple[list[str], list[str], str]:
    """The lines before a numbered list, its items (with the lines that continue each), and the text after it."""
    lead: list[str] = []
    items: list[str] = []
    lines = section.split("\n")
    gap = False
    for index, line in enumerate(lines):
        item = _STEP_ITEM_RE.match(line)
        if item:
            items.append(item[1].strip())
            gap = False
        elif not line.strip():
            gap = bool(items)
        elif items and not gap:
            # a list inside a step becomes bullets, so it can't be read as more steps
            items[-1] += f"\n{_NESTED_ITEM_RE.sub('- ', line).strip()}"
        elif items:  # a paragraph after the list: the list is over
            return lead, items, "\n".join(lines[index:]).strip()
        else:
            lead.append(line.strip())
    return lead, items, ""


def _split_trailing(text: str) -> tuple[str, tuple[str, ...], ClarifyingQuestion | None]:
    """The text without its `FOLLOW_UPS:` and `ASK:` sections, and what they held, cleaned and capped.

    A section is its marker line and the list after it; a paragraph that isn't a
    list item ends it and stays in the text. Markers inside fenced code don't count.
    A question back needs at least two usable options, or it stays in the text as written.
    """
    kept: list[str] = []
    follow_ups: list[str] = []
    question, options, asked_lines = None, [], []
    section, fenced = None, False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and _FOLLOW_UPS_LINE_RE.match(line):
            section = FOLLOW_UPS_MARKER
            continue
        if not fenced and (asked := _ASK_LINE_RE.match(line)) and not asked_lines:
            section, question = ASK_MARKER, asked[1] or None
            asked_lines.append(asked[1])
            continue
        if section and not fenced:
            item = _LIST_ITEM_RE.match(line)
            if item:
                (follow_ups if section == FOLLOW_UPS_MARKER else options).append(item[1])
                if section == ASK_MARKER:
                    asked_lines.append(line)
                continue
            if not line.strip():
                continue
            if section == ASK_MARKER and question is None:  # the question on the line after the marker
                question = line
                asked_lines.append(line)
                continue
            section = None
        kept.append(line)

    cleaned_follow_ups = _suggestions(follow_ups, MAX_FOLLOW_UP_CHARS, MAX_FOLLOW_UPS)
    ask = None
    if asked_lines:
        cleaned_question = _suggestions([question or ""], MAX_ASK_CHARS, 1)
        cleaned_options = _suggestions(options, MAX_OPTION_CHARS, MAX_ASK_OPTIONS)
        if cleaned_question and len(cleaned_options) >= MIN_ASK_OPTIONS:
            ask = ClarifyingQuestion(cleaned_question[0], cleaned_options)
        else:  # not a usable question back: it stays as the model wrote it
            kept = ["\n".join(kept).rstrip(), "", *asked_lines]
    return "\n".join(kept).strip(), cleaned_follow_ups, ask


def _suggestions(items: Sequence[str], max_chars: int, limit: int) -> tuple[str, ...]:
    """Short plain lines: references and emphasis taken out, too long ones and repeats dropped."""
    found: dict[str, str] = {}
    for item in items:
        plain = _CITATION_RE.sub("", _CODE_REF_RE.sub("", REF_RE.sub("", item)))
        plain = " ".join(_EMPHASIS_RE.sub("", plain).split())
        if plain and len(plain) <= max_chars:
            found.setdefault(plain.casefold(), plain)
    return tuple(found.values())[:limit]


def _callouts(text: str) -> str:
    """Callout markers made canonical (`[!warning]` -> `[!WARNING]`); unknown kinds and those past the cap dropped."""
    count = 0

    def mark(match: re.Match) -> str:
        nonlocal count
        kind = match[2].upper()
        if kind not in CALLOUT_KINDS or count >= MAX_CALLOUTS:
            return match[1].rstrip()
        count += 1
        return f"{match[1]}[!{kind}]"

    return _CALLOUT_RE.sub(mark, text)


def ask_markdown(ask: ClarifyingQuestion) -> str:
    """A question back written as the model wrote it, for an earlier answer sent to the model again."""
    return f"{ASK_MARKER}: {ask.question}\n" + "\n".join(f"- {option}" for option in ask.options)


def steps_markdown(steps: Sequence[FlowStep]) -> str:
    """Steps written back as the `STEPS:` list they came from, for an earlier answer sent to the model again."""
    lines = (
        f"{number}. [{step.ref}] {step.text}" + (f" [{step.citation_ref}]" if step.citation_ref else "")
        for number, step in enumerate(steps, 1)
    )
    return f"{STEPS_MARKER}:\n" + "\n".join(lines)


def _canonical_refs(text: str) -> str:
    text = _LOOSE_CODE_GROUP_RE.sub(_canonical_code_refs, _LOOSE_REF_RE.sub(r"[\1]", text))
    return _CODE_REF_RUN_RE.sub(_merge_run, text)


def _merge_run(run: re.Match) -> str:
    """Citations written side by side, `[s1:5-12][s1:13-18]`, joined where one source's ranges touch."""
    merged: list[list] = []
    for source, start, end in _RUN_PART_RE.findall(run[0]):
        start, end = int(start), int(end or start)
        last = merged[-1] if merged else None
        if last and last[0] == source and last[1] <= start <= last[2] + 1 and max(end, last[2]) - last[1] < MAX_CITED_LINES:
            last[2] = max(end, last[2])
        else:
            merged.append([source, start, end])
    spacing = run[0][:len(run[0]) - len(run[0].lstrip(" \t"))]
    return spacing + "".join(f"[{s}:{a}-{b}]" if a != b else f"[{s}:{a}]" for s, a, b in merged)


def _canonical_code_refs(match: re.Match) -> str:
    """One `[s3:42-58]` per range; a range without a source belongs to the one before it."""
    refs, source = [], None
    for part in re.split(r"[;,]", match[1]):
        range_ = _LOOSE_RANGE_RE.match(part.strip())
        if range_ is None:
            return match[0]  # not a citation after all; leave it to be dropped or kept as written
        source = range_[1] or source
        refs.append(f"[{source}:{range_[2]}-{range_[3]}]" if range_[3] else f"[{source}:{range_[2]}]")
    return "".join(refs)


def _outside_code(text: str, change: Callable[[str], str]) -> str:
    """Apply `change` to the text outside fenced code blocks."""
    return "".join(part if index % 2 else change(part) for index, part in enumerate(_FENCE_RE.split(text)))


def plain_text(text: str, citations: Sequence[Citation], code_citations: Sequence[CodeCitation] = ()) -> str:
    """An answer with its references spelled out as names, and its code citations as `(path:L42-58)`.

    References are only meaningful inside the context they came from, so an
    earlier answer goes back to the model as names, never as `[nX]` or `[cN]`.
    """
    where = {c.ref: clean(f"{c.path}:L{c.start_line}-{c.end_line}", 240) for c in code_citations}
    text = _CITATION_RE.sub(lambda match: f"{match[1]}({where[match[2]]})" if match[2] in where else "", text)
    # names go back to the model, so they're escaped like anything else from the repo
    names = {citation.ref: clean(citation.name) for citation in citations}

    def spell(match: re.Match) -> str:
        name = names.get(match[1], "")
        # "login [n1]" already names the node; "see [n1]" doesn't
        if not name or text[:match.start()].endswith(name):
            return ""
        return match[0][: -len(match[1]) - 2] + name

    return _SPACED_REF_RE.sub(spell, text).strip()


def safe_markdown(text: str) -> str:
    """Images only from chat attachments or inline data; links reduced to their text. Code is left alone."""

    def image(match: re.Match) -> str:
        alt, url = match[1], match[2]
        return match[0] if url.startswith(ATTACHMENT_PATH) or _INLINE_IMAGE_RE.match(url) else alt

    def outside_code(part: str) -> str:
        part = _IMAGE_RE.sub(image, part)
        part = _LINK_RE.sub(lambda match: match[1], part)
        return _REFERENCE_DEFINITION_RE.sub("", part)

    return _outside_code(text, outside_code)
