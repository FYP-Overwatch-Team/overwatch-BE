import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.mail import templates


def test_the_product_defaults_to_overwatch():
    assert Settings().product == "Overwatch"


def test_an_unknown_product_is_refused(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "acme")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(("noun", "expected"), [("Overwatch", "an Overwatch"), ("Bearings", "a Bearings")])
def test_the_article_follows_the_product_name(noun, expected):
    assert templates._a(noun) == expected
