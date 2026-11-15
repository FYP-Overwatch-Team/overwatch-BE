"""Email and password accounts: sign up, verify the email with a code, sign in, reset a forgotten password.

Nothing here tells a stranger whether an email has an account. Sign-up, "forgot password" and
resend answer the same way either way, backed by a decoy challenge that no code ever matches;
sign-in has one message for every failure, and checks a dummy hash when the email is unknown so
both take as long. "Not verified yet" is only said after the right password.

A code is bound to the challenge its browser holds, not just to the email: a code mailed for
someone else's sign-up attempt can't verify this one.
"""

import secrets
from datetime import timedelta

from fastapi import BackgroundTasks
from pymongo.errors import DuplicateKeyError

from app.accounts import service as accounts
from app.auth import otp, passwords, repository, usernames
from app.core.constants import AuthErrorCode, ChallengePurpose, MergeProof
from app.core.exceptions import AppError, FieldValidationError, UnauthorizedError
from app.core.rate_limit import Limit, allow, cooldown_error, retry_after, take
from app.core.time import utcnow
from app.mail import templates
from app.mail.sender import deliver
from app.workspaces import repository as workspaces
from app.workspaces import service as workspace_service

UNVERIFIED_TTL = timedelta(days=7)
MINUTE, HOUR = timedelta(minutes=1), timedelta(hours=1)
BREACHED = "This password has appeared in a data breach. Choose a different one."
INVALID_CREDENTIALS = "Invalid email or password."
INVALID_CODE = "That code is incorrect or has expired."
RESEND_WAIT = "Wait a moment before requesting another code."

SIGNUPS_PER_IP = Limit("signup:ip", 10, HOUR)
EXISTING_ACCOUNT_NOTICES = Limit("signup:exists", 1, HOUR)
LOGINS_PER_IP = Limit("login:ip", 30, 15 * MINUTE)
LOGINS_PER_EMAIL = Limit("login:email", 10, 15 * MINUTE)
CODE_ENTRIES_PER_IP = Limit("verify:ip", 30, 15 * MINUTE)
RESENDS_PER_IP = Limit("resend:ip", 20, HOUR)
RESETS_PER_IP = Limit("forgot:ip", 20, HOUR)
RESETS_PER_EMAIL = Limit("forgot:email", 5, HOUR)
ADDED_EMAILS_PER_USER = Limit("add_email:user", 5, HOUR)
ADDED_EMAILS_PER_IP = Limit("add_email:ip", 20, HOUR)
# codes sent to one address, so it can't be flooded through someone's account
ADDED_EMAILS_PER_ADDRESS = Limit("add_email:email", 5, HOUR)
UNLINKS_PER_USER = Limit("unlink:user", 10, 15 * MINUTE)


async def _check_new_password(password: str, email: str | None) -> None:
    if problem := passwords.problem(password, email):
        raise FieldValidationError({"password": problem})
    if await passwords.is_breached(password):
        raise FieldValidationError({"password": BREACHED})


def _username_error(reason: str) -> FieldValidationError:
    return FieldValidationError({"username": usernames.MESSAGES[reason]})


def _invalid_code() -> FieldValidationError:
    return FieldValidationError({"code": INVALID_CODE}, INVALID_CODE)


def _check_username(username: str) -> str:
    username = usernames.normalize(username)
    if reason := usernames.problem(username):
        raise _username_error(reason)
    return username


async def _save_account(existing: dict | None, email: str, username: str, fields: dict, unset: tuple[str, ...] = ()) -> dict:
    """Create the account, or overwrite an unverified one for the same email; a username someone else got first is a field error."""
    try:
        user = existing and await repository.update_user(existing["_id"], fields, unset=unset)
        return user or await repository.insert_user({"email": email, **fields})
    except DuplicateKeyError as exc:
        # mongomock reports the wrong key pattern, so the username is checked instead
        if await usernames.is_taken(username):
            raise _username_error(usernames.Reason.TAKEN) from exc
        raise


async def _issue(background: BackgroundTasks, purpose: str, email: str, user: dict | None, extra: dict | None = None) -> dict:
    """A fresh challenge, replacing the user's earlier ones; a decoy when there's no user. Only real ones are mailed."""
    code = otp.new_code()
    now = utcnow()
    doc = {
        "_id": secrets.token_urlsafe(24), "user_id": user["_id"] if user else None, "purpose": purpose,
        "email_key": otp.email_key(email), "code_hash": otp.hash_code(code, purpose) if user else otp.unmatchable_hash(purpose),
        "attempts": 0, "resend_count": 0, "sent_at": now,
        "code_expires_at": now + otp.CODE_TTL, "expires_at": now + otp.CHALLENGE_TTL, **(extra or {}),
    }
    if user:
        await repository.delete_challenges(user["_id"], purpose)
        background.add_task(deliver, _code_email(purpose, email, code))
    await repository.insert_challenge(doc)
    return _issued(doc, email)


CODE_EMAILS = {
    ChallengePurpose.VERIFY_EMAIL: templates.verification_email,
    ChallengePurpose.RESET_PASSWORD: templates.password_reset_email,
    ChallengePurpose.ADD_EMAIL: templates.add_address_email,
}


def _code_email(purpose: str, email: str, code: str):
    return CODE_EMAILS[purpose](email, code, int(otp.CODE_TTL.total_seconds() // 60))


def _issued(challenge: dict, email: str) -> dict:
    return {"challenge_id": challenge["_id"], "email": email, "resend_available_in": retry_after(challenge["sent_at"], otp.RESEND_COOLDOWN)}


async def signup(background: BackgroundTasks, ip: str, name: str, username: str, email: str, password: str) -> dict:
    await take(SIGNUPS_PER_IP, ip)
    username = _check_username(username)
    await _check_new_password(password, email)
    existing = await repository.find_user_by_email(email)
    if existing and existing.get("email_verified_at"):
        # same answer as a new sign-up; the owner hears about it, at most hourly
        if existing.get("username") != username and await usernames.is_taken(username):
            raise _username_error(usernames.Reason.TAKEN)
        if await allow(EXISTING_ACCOUNT_NOTICES, otp.email_key(email)):
            background.add_task(deliver, templates.account_exists_email(email))
        return await _issue(background, ChallengePurpose.VERIFY_EMAIL, email, None)

    # signing up again before verifying: the latest details win, and the clean-up clock restarts
    fields = {
        "name": name, "username": username, "password_hash": await passwords.hash_password(password),
        "email_verified_at": None, "unverified_expires_at": utcnow() + UNVERIFIED_TTL,
    }
    user = await _save_account(existing, email, username, fields)
    return await _issue(background, ChallengePurpose.VERIFY_EMAIL, email, user)


async def login(background: BackgroundTasks, ip: str, email: str, password: str) -> dict:
    """The signed-in user; raises `email_not_verified` (with a challenge) for a right password on an unverified account."""
    await take(LOGINS_PER_IP, ip)
    await take(LOGINS_PER_EMAIL, otp.email_key(email))
    user = await repository.find_user_by_email(email)
    if not await passwords.verify_password((user or {}).get("password_hash"), password):
        raise UnauthorizedError(INVALID_CREDENTIALS, error_code=AuthErrorCode.INVALID_CREDENTIALS)
    assert user is not None
    if user.get("merge_id"):
        raise accounts.merging_error()
    if not user.get("email_verified_at"):
        await repository.update_user(user["_id"], {"unverified_expires_at": utcnow() + UNVERIFIED_TTL})
        challenge = await repository.latest_challenge(user["_id"], ChallengePurpose.VERIFY_EMAIL)
        # a code sent moments ago is still on its way; don't send another
        if challenge and retry_after(challenge["sent_at"], otp.RESEND_COOLDOWN):
            issued = _issued(challenge, email)
        else:
            # an error response skips background tasks, so this code is sent before answering
            pending = BackgroundTasks()
            issued = await _issue(pending, ChallengePurpose.VERIFY_EMAIL, email, user)
            await pending()
        raise AppError("Verify your email to continue.", error_code=AuthErrorCode.EMAIL_NOT_VERIFIED, status_code=403, meta=issued)
    if passwords.needs_rehash(user["password_hash"]):
        await repository.update_user(user["_id"], {"password_hash": await passwords.hash_password(password)})
    return user


async def _redeem(challenge_id: str, purpose: str, code: str) -> str:
    """Spend one attempt on a code; the challenge's user once it matches (a decoy never does)."""
    challenge = await repository.take_attempt(challenge_id, purpose, otp.MAX_ATTEMPTS)
    if challenge is None:
        spent = await repository.find_challenge(challenge_id, purpose)
        if spent and spent["attempts"] >= otp.MAX_ATTEMPTS:
            raise AppError("Too many incorrect codes. Request a new one.", error_code=AuthErrorCode.OTP_ATTEMPTS_EXCEEDED, status_code=429)
        raise _invalid_code()
    if not otp.matches(code, purpose, challenge["code_hash"]) or not challenge["user_id"]:
        raise _invalid_code()
    if not await repository.consume_challenge(challenge_id, challenge["code_hash"]):
        raise _invalid_code()
    return challenge["user_id"]


async def _verified(user_id: str, fields: dict | None = None) -> dict:
    user = await repository.update_user(
        user_id, {"email_verified_at": utcnow(), **(fields or {})}, unset=("unverified_expires_at",),
    )
    if user is None:
        raise _invalid_code()
    await workspaces.ensure_personal(user)
    return user


async def verify_email(ip: str, challenge_id: str, code: str) -> dict:
    await take(CODE_ENTRIES_PER_IP, ip)
    user_id = await _redeem(challenge_id, ChallengePurpose.VERIFY_EMAIL, code)
    user = await repository.get_user(user_id)
    if user and user.get("email_verified_at"):
        return user
    return await _verified(user_id)


async def resend(background: BackgroundTasks, ip: str, challenge_id: str) -> dict:
    await take(RESENDS_PER_IP, ip)
    challenge = await repository.find_challenge(challenge_id)
    if challenge is None:
        raise AppError("This code has expired. Start again.", error_code=AuthErrorCode.INVALID_CODE)
    if wait := retry_after(challenge["sent_at"], otp.RESEND_COOLDOWN):
        raise cooldown_error(RESEND_WAIT, AuthErrorCode.OTP_RESEND_COOLDOWN, wait)
    if challenge["resend_count"] >= otp.MAX_RESENDS:
        raise AppError("You've requested too many codes. Start again later.", error_code=AuthErrorCode.OTP_RESEND_LIMIT, status_code=429)

    now = utcnow()
    cooldown = int(otp.RESEND_COOLDOWN.total_seconds())
    purpose = challenge["purpose"]
    user = challenge["user_id"] and await repository.get_user(challenge["user_id"])
    code = otp.new_code()
    renewed = await repository.renew_challenge(
        challenge_id, otp.hash_code(code, purpose) if user else otp.unmatchable_hash(purpose), now - otp.RESEND_COOLDOWN, otp.MAX_RESENDS,
        {"sent_at": now, "code_expires_at": now + otp.CODE_TTL, "expires_at": now + otp.CHALLENGE_TTL},
    )
    if not renewed:
        raise cooldown_error(RESEND_WAIT, AuthErrorCode.OTP_RESEND_COOLDOWN, cooldown)
    if user:
        # an address being added is the challenge's, not yet the user's
        background.add_task(deliver, _code_email(purpose, challenge.get("email") or user["email"], code))
    return {"resend_available_in": cooldown}


async def forgot_password(background: BackgroundTasks, ip: str, email: str) -> dict:
    await take(RESETS_PER_IP, ip)
    await take(RESETS_PER_EMAIL, otp.email_key(email))
    user = await repository.find_user_by_email(email)
    return await _issue(background, ChallengePurpose.RESET_PASSWORD, email, user if user and user.get("password_hash") else None)


async def reset_password(ip: str, challenge_id: str, code: str, password: str) -> None:
    """Set a new password and sign out every session; the code also proves the email, so it counts as verified."""
    await take(CODE_ENTRIES_PER_IP, ip)
    challenge = await repository.find_challenge(challenge_id, ChallengePurpose.RESET_PASSWORD)
    owner = await repository.get_user(challenge["user_id"]) if challenge and challenge["user_id"] else None
    await _check_new_password(password, owner["email"] if owner else None)
    user_id = await _redeem(challenge_id, ChallengePurpose.RESET_PASSWORD, code)
    if owner is None:
        raise _invalid_code()
    fields = {"password_hash": await passwords.hash_password(password), "password_changed_at": utcnow()}
    if not owner.get("email_verified_at"):
        await _verified(user_id, fields)
    else:
        await repository.update_user(user_id, fields)
    await repository.delete_challenges(user_id)
    await repository.revoke_user_sessions(user_id)


async def signup_with_invite(ip: str, token: str, name: str, username: str, password: str) -> tuple[dict, dict]:
    """Create an account from an email invite and join its workspace. The link proves the inbox, so no code is needed.

    The email is the invite's, never the browser's.
    """
    await take(SIGNUPS_PER_IP, ip)
    invite, workspace = await workspace_service.open_email_invite(token)
    email = invite["email"]
    username = _check_username(username)
    await _check_new_password(password, email)
    existing = await repository.find_user_by_email(email)
    if existing and existing.get("email_verified_at"):
        raise AppError("An account with this email already exists. Sign in to accept the invite.", error_code=AuthErrorCode.ACCOUNT_EXISTS, status_code=409)
    fields = {"name": name, "username": username, "password_hash": await passwords.hash_password(password), "email_verified_at": utcnow()}
    user = await _save_account(existing, email, username, fields, unset=("unverified_expires_at",))
    await repository.delete_challenges(user["_id"])
    await workspaces.ensure_personal(user)
    joined = await workspace_service.accept_invite(token, user)
    return user, joined


async def set_username(user: dict, username: str) -> dict:
    """Choose a username once; accounts from before usernames, or whose GitHub login was taken, have none."""
    username = usernames.normalize(username)
    if reason := await usernames.availability(username):
        raise _username_error(reason)
    try:
        if not await repository.set_username_if_unset(user["_id"], username):
            raise AppError("you already have a username", error_code=AuthErrorCode.USERNAME_ALREADY_SET, status_code=409)
    except DuplicateKeyError as exc:
        raise _username_error(usernames.Reason.TAKEN) from exc
    return {**user, "username": username}


async def start_add_email(background: BackgroundTasks, ip: str, user: dict, email: str, password: str) -> dict:
    """Email a code to an address being added to the signed-in account, with the password it will sign in with.

    The same answer whether or not the address has an account; only its inbox learns that.
    """
    await take(ADDED_EMAILS_PER_USER, user["_id"])
    await take(ADDED_EMAILS_PER_IP, ip)
    await take(ADDED_EMAILS_PER_ADDRESS, otp.email_key(email))
    if user.get("email_verified_at"):
        raise AppError("Your account already has an email address.", error_code=AuthErrorCode.EMAIL_ALREADY_SET, status_code=409)
    await _check_new_password(password, email)
    # kept on the challenge until the code proves the address: an unverified address on the account itself would
    # set off the clean-up of unverified accounts
    extra = {"email": email, "password_hash": await passwords.hash_password(password)}
    return await _issue(background, ChallengePurpose.ADD_EMAIL, email, user, extra)


async def finish_add_email(ip: str, user: dict, challenge_id: str, code: str) -> tuple[str, dict]:
    """Add the proven address, or, when another verified account holds it, propose merging that account in.

    Returns ("added", the updated user) or ("merge_required", the merge preview).
    """
    await take(CODE_ENTRIES_PER_IP, ip)
    challenge = await repository.find_challenge(challenge_id, ChallengePurpose.ADD_EMAIL)
    if challenge is None or challenge["user_id"] != user["_id"]:
        raise _invalid_code()
    await _redeem(challenge_id, ChallengePurpose.ADD_EMAIL, code)
    email = challenge["email"]
    holder = await repository.find_user_by_email(email)
    if holder and holder["_id"] != user["_id"]:
        if holder.get("email_verified_at"):
            # the code proved this inbox, which already controls that account (it could reset its password)
            return "merge_required", await accounts.propose(user, holder, MergeProof.EMAIL_OTP, email)
        # never verified, so it owns nothing yet: the address goes to the account that proved it
        await repository.delete_challenges(holder["_id"])
        await repository.delete_user(holder["_id"])
    fields = {"email": email, "email_verified_at": utcnow()}
    if not user.get("password_hash"):
        fields |= {"password_hash": challenge["password_hash"], "password_changed_at": utcnow()}
    updated = await repository.update_user(user["_id"], fields)
    return "added", updated or user


async def _confirm_password(user: dict, password: str) -> None:
    await take(UNLINKS_PER_USER, user["_id"])
    if not await passwords.verify_password(user.get("password_hash"), password):
        raise UnauthorizedError("That password is incorrect.", error_code=AuthErrorCode.INVALID_CREDENTIALS)


def _last_method() -> AppError:
    return AppError(
        "This is the only way you sign in. Add another before removing it.", error_code=AuthErrorCode.LAST_LOGIN_METHOD,
        status_code=409,
    )


async def unlink_github(user: dict, password: str) -> dict:
    """Stop signing in with GitHub; the account must keep its email and password, which the password proves."""
    if not (user.get("email_verified_at") and user.get("password_hash")):
        raise _last_method()
    await _confirm_password(user, password)
    await repository.unlink_github(user["_id"])
    return await repository.get_user(user["_id"]) or user


async def unlink_email(user: dict, password: str) -> dict:
    """Stop signing in with email and password; GitHub stays as the way in."""
    if not user.get("github_id"):
        raise _last_method()
    await _confirm_password(user, password)
    updated = await repository.update_user(
        user["_id"], {}, unset=("email", "email_verified_at", "password_hash", "password_changed_at"),
    )
    return updated or user
