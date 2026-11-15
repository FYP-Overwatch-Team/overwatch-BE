"""Keyword search's derived words and Lucene query, built without a database."""

from app.knowledge_graph.search_text import (
    MAX_QUERY_WORDS,
    SEARCH_SCOPE,
    identifier_words,
    lucene_query,
    scope_token,
)


def test_code_names_are_split_into_their_words_and_kept_whole():
    assert identifier_words("SignupForm").split() == ["signupform", "signup", "form"]
    assert identifier_words("open_ask_context").split() == ["open_ask_context", "open", "ask", "context"]
    assert {"http", "client"} <= set(identifier_words("HTTPClient").split())
    assert identifier_words("app/query/context.py").split() == ["app", "query", "context", "py"]


def test_a_repository_scope_is_one_token_and_never_shared_by_a_similar_name():
    token = scope_token("acme/api")

    assert token.isalnum() and token == scope_token("acme/api")
    assert token != scope_token("acme/api-v2")


def test_every_word_must_be_in_the_repository_scope():
    query = lucene_query(["limit"], "acme/api")

    assert query.startswith(f"+{SEARCH_SCOPE}:{scope_token('acme/api')} +(")
    assert "name:limit^4" in query and "search_words:limit*^0.5" in query


def test_lucene_syntax_in_a_word_is_escaped_and_operators_lowercased():
    query = lucene_query(['a:b"(c)*', "AND"], "acme/api")

    assert 'name:a\\:b\\"\\(c\\)\\*^4' in query
    assert "name:and^4" in query and " AND " not in query


def test_a_query_holds_at_most_its_word_limit():
    query = lucene_query([f"word{index}" for index in range(MAX_QUERY_WORDS + 5)], "acme/api")

    assert f"word{MAX_QUERY_WORDS - 1}" in query and f"word{MAX_QUERY_WORDS}" not in query
