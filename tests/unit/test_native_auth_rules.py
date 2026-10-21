"""Password and username rules, one-time codes and the emails that carry them."""

from datetime import UTC, datetime

import pytest

from app.auth import otp, passwords, usernames
from app.mail import templates


@pytest.mark.parametrize(("password", "ok"), [
    ("1234567", False), ("12345678", True), ("        ", True), ("пароль-длинный", True), ("x" * 129, False),
])
def test_passwords_are_judged_by_length_alone(password, ok):
    assert (passwords.problem(password, None) is None) is ok


def test_passwords_may_not_contain_the_email_name():
    assert passwords.problem("my-ada-password", "ada@example.com")
    assert passwords.problem("my-ad-password", "ad@example.com") is None


async def test_hashes_are_argon2id_and_never_match_without_a_hash():
    stored = await passwords.hash_password("ﬁne password")  # NFKC: "ﬁ" is "fi"
    assert stored.startswith("$argon2id$")
    assert await passwords.verify_password(stored, "fine password")
    assert not await passwords.verify_password(stored, "wrong password")
    assert not await passwords.verify_password(None, "overwatch-no-such-account")


def test_codes_are_six_digits_and_hashed_per_purpose():
    code = otp.new_code()
    assert len(code) == 6 and code.isdigit()
    assert otp.matches(code, "verify_email", otp.hash_code(code, "verify_email"))
    assert not otp.matches(code, "reset_password", otp.hash_code(code, "verify_email"))


@pytest.mark.parametrize(("username", "reason"), [
    ("ada", None), ("a_b-c9", None), ("ab", "invalid"), ("-ada", "invalid"), ("ada_", "invalid"),
    ("a__b", "invalid"), ("Ada", "invalid"), ("a" * 31, "invalid"), ("settings", "reserved"),
])
def test_username_rules(username, reason):
    assert usernames.problem(username) == reason


def test_github_logins_become_usernames_when_they_fit():
    assert usernames.default_from("OctoCat") == "octocat"
    assert usernames.default_from("ab") is None
    assert usernames.default_from("admin") is None


def test_emails_escape_what_people_type_and_have_a_text_part():
    email = templates.workspace_invite_email(
        "g@example.com", "<b>eve</b>", 'Acme\r\nBcc: x@evil.test <img src=x onerror=alert(1)>', "member",
        "https://app.test/invite#tok", datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert "\n" not in email.subject and "\r" not in email.subject
    assert "<img" not in email.html and "&lt;img" in email.html and "<b>eve</b>" not in email.html
    assert "https://app.test/invite#tok" in email.text and "January 2, 2026" in email.text


def test_code_emails_keep_the_code_out_of_the_subject():
    email = templates.verification_email("a@example.com", "123456", 10)
    assert "123456" not in email.subject and "123456" in email.html and "123456" in email.text
    assert "http" not in email.html.replace("http-equiv", "")


def test_people_are_shown_by_name_then_username_then_github_login():
    github_only = {"name": None, "username": None, "github_login": "octocat"}
    assert usernames.display_name(github_only) == "octocat"
    assert usernames.display_name({"name": "Ada", "username": "ada"}) == "Ada"
    assert usernames.display_name(None, "someone") == "someone"
    assert usernames.handle({"name": "Ada", "username": "ada"}) == "ada"
    assert usernames.handle(github_only) == "octocat"


def test_a_decoy_challenge_hash_matches_no_code():
    decoy = otp.unmatchable_hash("verify_email")
    assert not any(otp.matches(str(code).zfill(6), "verify_email", decoy) for code in range(0, 10**6, 9973))
