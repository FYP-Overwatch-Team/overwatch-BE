"""Checking a model's answer against the context it was shown.

The prompt asks the model to stay inside the graph; these checks are what
make that hold. The model returns its answer with inline `[nX]` references,
the references it cited, and the relationships its answer relies on:

- a relationship the context never showed means the answer is refused —
  a claim about how the code connects that the graph can't back is worse
  than no answer;
- a reference the context never contained is dropped, from the text too;
- an image may only point at a chat attachment or be inline data, and links
  keep their text but lose their target: an answer can quote repository
  text, so it must not make the viewer's browser contact anyone else.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.query.context import GroundingContext, Relation, clean

CANNOT_DETERMINE = "This can't be determined from the repository's graph."
#: Where chat attachments are served from; the only place an answer's image may load from.
ATTACHMENT_PATH = "/chat/attachments/"

REF_RE = re.compile(r"\[(n\d+)\]")
# a reference with the space before it, so removing one leaves no gap
_SPACED_REF_RE = re.compile(r"[ \t]*\[(n\d+)\]")
_FENCE_RE = re.compile(r"(```[\s\S]*?(?:```|$))")
_IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\(\s*<?([^)\s>]*)>?(?:\s+[\"'][^\"'\n]*[\"'])?\s*\)")
_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]+)\]\([^)\n]*\)")
_REFERENCE_DEFINITION_RE = re.compile(r"^[ \t]*\[[^\]\n]+\]:[ \t]*\S.*$", re.MULTILINE)
_INLINE_IMAGE_RE = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$")


@dataclass(frozen=True, slots=True)
class Citation:
    ref: str
    node_id: str
    module_id: str
    name: str


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    text: str
    citations: tuple[Citation, ...] = ()
    #: False when the model said it couldn't answer, or the answer was refused.
    grounded: bool = True
    #: The question's @-mentions that exist in the graph.
    mentions: tuple[Citation, ...] = ()
    #: What was thrown away, for logging.
    dropped_refs: tuple[str, ...] = field(default=(), compare=False)
    unsupported: tuple[Relation, ...] = field(default=(), compare=False)


def ground(context: GroundingContext, reply: dict) -> GroundedAnswer:
    """Turn the model's reply into an answer that only says what the graph shows."""
    text = safe_markdown(str(reply.get("answer") or "").strip())
    if not reply.get("answerable", True) or not text:
        return GroundedAnswer(_SPACED_REF_RE.sub("", text).strip() or CANNOT_DETERMINE, grounded=False)

    unsupported = tuple(
        relation for relation in _relations(reply.get("relations")) if not context.supports(relation)
    )
    if unsupported:
        return GroundedAnswer(CANNOT_DETERMINE, grounded=False, unsupported=unsupported)

    cited = list(dict.fromkeys([*REF_RE.findall(text), *map(_bare, reply.get("cited_refs") or [])]))
    known = [ref for ref in cited if ref in context.nodes]
    text = _SPACED_REF_RE.sub(lambda match: match[0] if match[1] in context.nodes else "", text).strip()
    return GroundedAnswer(
        text,
        tuple(Citation(ref, context.nodes[ref], context.modules[ref], context.labels[ref]) for ref in known),
        dropped_refs=tuple(ref for ref in cited if ref not in context.nodes),
    )


def plain_text(text: str, citations: Sequence[Citation]) -> str:
    """An answer with its references spelled out as names.

    References are only meaningful inside the context they came from, so an
    earlier answer goes back to the model as names, never as `[nX]`.
    """
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

    return "".join(part if index % 2 else outside_code(part) for index, part in enumerate(_FENCE_RE.split(text)))


def _relations(raw: object) -> list[Relation]:
    if not isinstance(raw, list):
        return []
    return [
        Relation(_bare(item.get("source")), str(item.get("type") or "").strip().lower(), _bare(item.get("target")))
        for item in raw
        if isinstance(item, dict)
    ]


def _bare(ref: object) -> str:
    """`[n4]` or `n4` -> `n4`."""
    return str(ref or "").strip().strip("[]")

