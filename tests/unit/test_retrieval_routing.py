"""Routing a question to keyword or meaning search, and merging what both found."""

import pytest

from app.query.retrieval import BUDGETS, Route, is_overview, merge_hits, route_of


@pytest.mark.parametrize("question", [
    "where is rank_matches used",
    "what does SignupForm render",
    "how is openAskContext called",
    "how is SCHEMA_STATEMENTS applied",
    "what does `take` do",
    "explain app/usage/allowance.py",
    "what's in config.py",
    "what does @limits do",
])
def test_a_question_naming_code_goes_to_keyword_search(question):
    assert route_of(question) == Route.KEYWORD


@pytest.mark.parametrize("question", [
    "where do we stop people asking too many questions",
    "how does a user sign up, e.g. with email?",
    "What happens when someone disconnects a repository?",
])
def test_a_plain_question_goes_to_meaning_search(question):
    assert route_of(question) == Route.MEANING


def test_a_mention_goes_to_keyword_search():
    assert route_of("how does this work", ["acme/api:app.py#run"]) == Route.KEYWORD


@pytest.mark.parametrize("question", [
    "how is the code structured in this repo?",
    "how is this project organised",
    "give me an overview of the architecture",
    "what folders are there",
])
def test_a_question_about_the_whole_layout_is_an_overview(question):
    assert is_overview(question)


@pytest.mark.parametrize(("question", "mentioned"), [
    ("how is the SignupForm structured", ()),  # names code
    ("how is this structured", ("acme/api:app.py#run",)),  # about what's mentioned
    ("how does a user sign up", ()),
])
def test_a_question_about_one_part_is_not_an_overview(question, mentioned):
    assert not is_overview(question, mentioned)


def rows(*ids: str) -> list[dict]:
    return [{"id": node_id} for node_id in ids]


def test_the_leading_search_comes_first_then_the_other_then_the_rest_once_each():
    keyword, meaning = rows("k1", "both", "k3"), rows("m1", "both", "m3")

    assert ids(merge_hits(Route.MEANING, keyword, meaning, keyword_share=1, meaning_share=2)) == ["m1", "both", "k1", "m3", "k3"]
    assert ids(merge_hits(Route.KEYWORD, keyword, meaning, keyword_share=1, meaning_share=2)) == ["k1", "m1", "both", "k3", "m3"]


def test_when_one_search_finds_nothing_the_other_fills_its_share():
    assert ids(merge_hits(Route.MEANING, rows("k1", "k2", "k3"), [], keyword_share=1, meaning_share=2)[:3]) == ["k1", "k2", "k3"]


def test_each_route_gives_its_own_search_the_larger_share():
    assert BUDGETS[Route.KEYWORD].keyword_seeds > BUDGETS[Route.KEYWORD].meaning_seeds
    assert BUDGETS[Route.MEANING].meaning_seeds > BUDGETS[Route.MEANING].keyword_seeds


def ids(found: list[dict]) -> list[str]:
    return [row["id"] for row in found]
