"""One-off migration to stored decision sources: each decision gets its numbered `sources`.

Dry run by default; pass --apply to write. Reads MONGO_URI / MONGO_DB_NAME from .env.
Safe to run again: only decisions without `sources` are touched.

- AI suggestions wrote their sources into the background as a "### Sources" list of
  "- [S1] Title" lines, followed by a note on the AI's reading. The list becomes `sources`
  (matched to the evidence quoting the same title, so it links to the item or page), and the
  list and the note leave the background. A cited source no evidence quotes stays, unlinked.
- every other decision numbers its evidence's sources in order, S1 first

Run it right after deploying the backend that stores sources; until then a decision reads as
having none, and its evidence still shows.
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import mongo  # noqa: E402
from app.decisions.service import with_sources  # noqa: E402

SOURCES_HEADING = "\n### Sources\n"
SOURCE_LINE = re.compile(r"^- \[(S\d+)\] (.+)$")
AI_NOTE = re.compile(r"\n*\*Pros, cons and the example are the AI's reading of the sources: check them before accepting\.\*\s*$")


def split_sources(context_md: str) -> tuple[str, list[tuple[str, str]]]:
    """The background without its "### Sources" list and AI note, and the list's `(id, title)` pairs."""
    text = AI_NOTE.sub("", context_md)
    at = text.rfind(SOURCES_HEADING)
    if at < 0:
        return text.rstrip(), []
    listed = [SOURCE_LINE.match(line.strip()) for line in text[at + len(SOURCES_HEADING):].splitlines() if line.strip()]
    return text[:at].rstrip(), [(match.group(1), match.group(2)) for match in listed if match]


def sources_for(decision: dict) -> tuple[str | None, list[dict]]:
    """A decision's background as it should now read, and its numbered sources."""
    context_md = decision.get("context_md")
    body, listed = split_sources(context_md) if context_md else (context_md, [])
    quoted = {e["title"]: e for e in decision["evidence"]}
    cited = [
        {"id": sid, "kind": quoted[title]["kind"] if title in quoted else None,
         "ref_id": quoted[title]["ref_id"] if title in quoted else None, "title": title}
        for sid, title in listed
    ]
    return body or None, with_sources(cited, decision["evidence"])


async def migrate(apply: bool) -> list[str]:
    report, rewritten, numbered = [], 0, 0
    async for decision in mongo.decisions().find({"sources": {"$exists": False}}):
        context_md, sources = sources_for(decision)
        rewritten += context_md != decision.get("context_md")
        numbered += 1
        if apply:
            await mongo.decisions().update_one(
                {"_id": decision["_id"], "sources": {"$exists": False}},
                {"$set": {"context_md": context_md, "sources": sources}},
            )
    report.append(f"{numbered} decisions numbered their sources")
    report.append(f"{rewritten} backgrounds lost their Sources list and AI note")
    return report


async def main(apply: bool) -> None:
    mongo.connect()
    try:
        for line in await migrate(apply):
            print(("" if apply else "[dry run] ") + line)
    finally:
        mongo.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    asyncio.run(main(parser.parse_args().apply))
