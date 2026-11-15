from app.core.logging import redact


def test_sensitive_keys_redacted_recursively():
    event = {
        "msg": "github call",
        "access_token": "gho_secret",
        "nested": {"refresh_token": "abc", "safe": "keep-me"},
        "items": [{"client_secret": "shh"}, "plain"],
    }
    result = redact(event)
    assert result["access_token"] == "[REDACTED]"
    assert result["nested"]["refresh_token"] == "[REDACTED]"
    assert result["nested"]["safe"] == "keep-me"
    assert result["items"][0]["client_secret"] == "[REDACTED]"
    assert result["items"][1] == "plain"


def test_bearer_tokens_redacted_in_strings():
    assert redact("Authorization: Bearer gho_abc123") == "Authorization: Bearer [REDACTED]"


def test_non_sensitive_values_untouched():
    assert redact({"path": "/health", "status": 200}) == {"path": "/health", "status": 200}
