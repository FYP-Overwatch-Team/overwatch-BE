"""Reading the streamed reply format: header first, then the answer."""

import pytest

from app.query.context import Relation
from app.query.reply import MAX_HEADER_CHARS, ReplyReader

REPLY = "ANSWERABLE: yes\nRELATIONS: [n1] calls [n2]; n3 --imports--> n4\n---\nlogin [n1] calls verify [n2].\n\n---\n\nMore."


def read(text: str, size: int) -> tuple[ReplyReader, str]:
    reader = ReplyReader()
    shown = "".join(reader.feed(text[i:i + size]) for i in range(0, len(text), size)) + reader.finish()
    return reader, shown


@pytest.mark.parametrize("size", [1, 3, 7, 1000])
def test_the_header_is_read_whatever_the_chunks_and_never_shown(size):
    reader, shown = read(REPLY, size)

    assert reader.answerable
    assert reader.relations == [Relation("n1", "calls", "n2"), Relation("n3", "imports", "n4")]
    assert shown == reader.answer == "login [n1] calls verify [n2].\n\n---\n\nMore."


def test_relations_are_known_before_any_answer_is_handed_on():
    reader = ReplyReader()

    assert reader.feed("ANSWERABLE: yes\nRELATIONS: [n1] calls [n2]\n") == ""
    assert reader.feed("---\nlogin") == "login"
    assert reader.relations == [Relation("n1", "calls", "n2")]


def test_an_unanswerable_reply_with_no_relations():
    reader, shown = read("ANSWERABLE: no\nRELATIONS: none\n---\nNothing about billing here.", 5)

    assert not reader.answerable
    assert reader.relations == []
    assert shown == "Nothing about billing here."


def test_a_reply_without_a_header_is_all_answer_rules_included():
    reader, shown = read("It lives in api.\n\n---\n\nAnd more.", 4)

    assert reader.answerable
    assert shown == "It lives in api.\n\n---\n\nAnd more."


def test_a_header_line_that_never_ends_is_taken_as_answer():
    text = "ANSWERABLE yes " + "x" * (MAX_HEADER_CHARS + 10)

    reader, shown = read(text, 100)

    assert shown == text


def test_the_separator_is_optional():
    reader, shown = read("ANSWERABLE: yes\nRELATIONS: none\n\nThe three files all export it.\n\n- more", 3)

    assert reader.relations == []
    assert shown == "The three files all export it.\n\n- more"


def test_answer_text_streams_before_its_first_line_ends():
    reader = ReplyReader()
    reader.feed("ANSWERABLE: yes\nRELATIONS: none\n")

    assert reader.feed("The thr") == "The thr"


def test_header_lines_written_after_the_answer_are_read_and_taken_out():
    reader, _ = read("It lives in api [n1].\n\nRELATIONS: [n1] calls [n2]\n\nANSWERABLE: yes", 5)

    assert reader.answer == "It lives in api [n1]."
    assert reader.relations == [Relation("n1", "calls", "n2")]


def test_a_reply_that_is_only_a_header_has_an_empty_answer():
    reader, shown = read("ANSWERABLE: no\nRELATIONS: none", 4)

    assert not reader.answerable
    assert shown == ""


def test_no_break_spaces_become_plain_spaces():
    _, shown = read("ANSWERABLE: yes\nRELATIONS: none\n---\nlogin\u202f[n1]\u00a0calls\u200b it.", 4)

    assert shown == "login [n1] calls it."


def test_a_flows_steps_are_answer_text_not_header():
    reader, shown = read("ANSWERABLE: yes\nRELATIONS: none\n---\nHere.\n\nSTEPS:\n1. [n1] a", 4)

    assert shown == reader.answer == "Here.\n\nSTEPS:\n1. [n1] a"


def test_a_header_like_line_inside_the_answer_stays_in_it():
    reader, _ = read("ANSWERABLE: yes\nRELATIONS: none\n---\n**Relations:** api calls services.\n\nThat's all.", 4)

    assert reader.relations == []
    assert reader.answer == "**Relations:** api calls services.\n\nThat's all."
