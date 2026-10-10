"""Shared cookie policy for the auth and OAuth-state cookies.

Production runs the frontend and the API on different domains, so any cookie
the browser must send on a cross-site request — the refresh token on the
/auth/refresh XHR, the Jira state cookie set from a cross-site POST — has to be
SameSite=None. Without it the browser silently drops the cookie and the user is
logged out the moment their 15-minute access token expires.

SameSite=None requires Secure. Dev keeps Lax, where the frontend and API are
both on localhost (ports do not affect SameSite) and Secure is off.
"""

from app.core.config import get_settings


def cookie_secure() -> bool:
    return get_settings().app_env != "dev"


def cookie_samesite() -> str:
    return "none" if cookie_secure() else "lax"
