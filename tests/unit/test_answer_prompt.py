"""The answer prompt, read from its Markdown file."""

from app.query.grounding import ASK_MARKER, FOLLOW_UPS_MARKER, STEPS_MARKER
from app.query.reply import HEADER_INSTRUCTION
from app.query.service import ASK_SYSTEM_INSTRUCTION, UNTRUSTED_CONTEXT_NOTICE


def test_every_placeholder_is_filled():
    assert "$" not in ASK_SYSTEM_INSTRUCTION
    assert f"`{STEPS_MARKER}:`" in ASK_SYSTEM_INSTRUCTION
    assert HEADER_INSTRUCTION in ASK_SYSTEM_INSTRUCTION
    assert UNTRUSTED_CONTEXT_NOTICE.strip() in ASK_SYSTEM_INSTRUCTION


def test_the_answer_blocks_are_described_with_their_markers():
    assert f"`{FOLLOW_UPS_MARKER}:`" in ASK_SYSTEM_INSTRUCTION
    assert f"`{ASK_MARKER}: `" in ASK_SYSTEM_INSTRUCTION
    assert "> [!WARNING]" in ASK_SYSTEM_INSTRUCTION
