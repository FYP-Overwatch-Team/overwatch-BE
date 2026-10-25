"""Reading a streamed answer: a short header the model writes first, then the answer itself.

    ANSWERABLE: yes
    RELATIONS: [n1] calls [n2]; [n3] imports [n4]
    ---
    <the answer, in Markdown>

The header comes first so the relationships an answer relies on can be checked
before any of it is shown. Plain text streams on every provider, where a JSON
reply often can't be streamed at all.
"""

import re

from app.query.context import Relation

#: A header longer than this isn't one; everything so far is taken as the answer.
MAX_HEADER_CHARS = 4_000

HEADER_INSTRUCTION = (
    "Reply format: start with exactly these three lines, then the answer.\n"
    "ANSWERABLE: yes (or no, only when nothing in the context is relevant)\n"
    "RELATIONS: every relationship the answer states, as [n1] calls [n2], "
    "separated by semicolons, or none\n"
    "---"
)

_PLAIN_SPACES = str.maketrans({"\u00a0": " ", "\u202f": " ", "\u200b": None})
_HEADER_LINE_RE = re.compile(r"^\W*(answerable|relations)\W*:(.*)$", re.IGNORECASE)
_SEPARATOR_RE = re.compile(r"^\s*-{3,}\s*$")
_RELATION_RE = re.compile(r"\[?(n\d+)\]?\s*(?:--)?\s*([A-Za-z_]+)\s*(?:-->|->)?\s*\[?(n\d+)\]?")


class ReplyReader:
    """Fed the reply as it streams; reads the header line by line, then hands back answer text as it arrives.

    Models drop the `---` now and then, so the header ends at the first line that
    isn't part of it; a reply that doesn't open with it is all answer.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self.header_read = False
        self.answerable = True
        self.relations: list[Relation] = []
        self.answer = ""
        self._header_lines = 0

    def feed(self, text: str) -> str:
        """Take the next piece of the reply; returns the answer text it adds (empty while in the header)."""
        # some models space references with no-break spaces, which would keep a chip from joining its name
        text = text.translate(_PLAIN_SPACES)
        if self.header_read:
            if not self.answer:
                text = text.lstrip("\n")
            self.answer += text
            return text
        self._buffer += text
        while not self.header_read:
            line, newline, rest = self._buffer.partition("\n")
            if not newline:
                # a partial line that can't be the header is the answer starting: pass it on now
                if len(line) > MAX_HEADER_CHARS or (self._header_lines and not _could_be_header(line)):
                    return self._start_answer(line)
                return ""
            if header := _HEADER_LINE_RE.match(line.strip()):
                self._read_header_line(header[1].lower(), header[2])
            elif _SEPARATOR_RE.match(line) and self._header_lines:
                return self._start_answer(rest)
            elif line.strip():
                return self._start_answer(self._buffer)  # the answer's first line
            self._buffer = rest
        return ""

    def finish(self) -> str:
        """The reply ended; returns any answer text not yet handed back.

        Header lines a model put after the answer instead are read too, and taken out of it;
        only those it ends with, so a line like `**Relations:**` inside the answer stays.
        """
        text = ""
        if not self.header_read:
            if header := _HEADER_LINE_RE.match(self._buffer.strip()):
                self._read_header_line(header[1].lower(), header[2])
                self._start_answer("")
            else:
                text = self._start_answer(self._buffer)
        lines = self.answer.rstrip().split("\n")
        while lines and (not lines[-1].strip() or (header := _HEADER_LINE_RE.match(lines[-1].strip()))):
            if lines[-1].strip():
                self._read_header_line(header[1].lower(), header[2])
            lines.pop()
        self.answer = "\n".join(lines).strip()
        return text

    def _read_header_line(self, key: str, value: str) -> None:
        self._header_lines += 1
        if key == "answerable":
            self.answerable = value.strip().lower().split()[:1] not in (["no"], ["false"])
        else:
            self.relations += [
                Relation(source, kind.lower(), target) for source, kind, target in _RELATION_RE.findall(value)
            ]

    def _start_answer(self, text: str) -> str:
        self.header_read = True
        self._buffer = ""
        self.answer = text.lstrip("\n")
        return self.answer


def _could_be_header(partial: str) -> bool:
    """Whether the start of a line may still turn out to be a header line or the separator."""
    start = partial.strip().lower()
    return not start or set(start) <= {"-"} or any(key.startswith(start[:len(key)]) for key in ("answerable:", "relations:"))
