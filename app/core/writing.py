"""How every AI-written text people read should sound, shared by the prompts that write it."""

from pathlib import Path

#: Markdown bullets, filled into a prompt's `$writing_style`
WRITING_STYLE = (Path(__file__).parent / "writing.md").read_text(encoding="utf-8").strip()
