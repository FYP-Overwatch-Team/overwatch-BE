"""Emails written to the log instead of sent, for dev."""

import structlog

from app.mail.sender import Email

logger = structlog.get_logger("app.mail")


class ConsoleSender:
    def __init__(self, show_body: bool) -> None:
        self.show_body = show_body

    async def send(self, email: Email) -> None:
        # the body carries one-time codes, so only a dev machine prints it
        logger.info("email", subject=email.subject, body=email.text if self.show_body else None)
