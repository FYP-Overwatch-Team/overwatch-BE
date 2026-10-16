"""How emails go out, behind one interface: the rest of the app never names a provider.

Adding a provider is one adapter module registered in `BACKENDS`, then `EMAIL_BACKEND=<name>`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import structlog

from app.core.config import Settings, get_settings

logger = structlog.get_logger("app.mail")


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    html: str
    text: str


class EmailSender(Protocol):
    async def send(self, email: Email) -> None: ...


def _console(settings: Settings) -> EmailSender:
    from app.mail.console import ConsoleSender

    return ConsoleSender(show_body=settings.app_env == "dev")


def _smtp(settings: Settings) -> EmailSender:
    from app.mail.smtp import SmtpSender

    return SmtpSender(settings)


#: EMAIL_BACKEND name -> how to build its adapter
BACKENDS: dict[str, Callable[[Settings], EmailSender]] = {"console": _console, "smtp": _smtp}

_sender: EmailSender | None = None


def get_email_sender() -> EmailSender:
    global _sender
    if _sender is None:
        settings = get_settings()
        build = BACKENDS.get(settings.email_backend)
        if build is None:
            raise RuntimeError(f"Unknown EMAIL_BACKEND {settings.email_backend!r}; one of {', '.join(BACKENDS)}")
        _sender = build(settings)
    return _sender


def use_email_sender(sender: EmailSender | None) -> None:
    """Swap the sender (tests)."""
    global _sender
    _sender = sender


async def deliver(email: Email) -> None:
    """Send from a background task: a failure is logged, never raised into a finished response."""
    try:
        await get_email_sender().send(email)
    except Exception:
        logger.exception("email_failed", subject=email.subject)
