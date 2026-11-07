"""The answer prompt, read from its Markdown file."""

from app.core.config import get_settings
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


def test_the_prompt_holds_no_secret_settings():
    secrets = [
        value for name, value in get_settings().model_dump().items()
        if any(word in name for word in ("key", "secret", "password", "token", "uri", "url"))
        and isinstance(value, str) and len(value) >= 8
    ]

    assert secrets
    assert not [value for value in secrets if value in ASK_SYSTEM_INSTRUCTION]


def test_lookup_results_are_named_as_data_too():
    assert "lookup" in UNTRUSTED_CONTEXT_NOTICE.lower()
