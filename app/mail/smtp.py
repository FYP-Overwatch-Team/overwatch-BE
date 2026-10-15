"""Emails through an SMTP server (Gmail with an App Password): STARTTLS with a verified certificate."""

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app.core.config import Settings
from app.mail.sender import Email

TIMEOUT_SECONDS = 15


class SmtpSender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _send(self, email: Email) -> None:
        settings = self.settings
        message = EmailMessage()
        message["From"] = formataddr((settings.email_from_name, settings.email_from))
        message["To"] = email.to
        message["Subject"] = email.subject
        message["Message-ID"] = make_msgid(domain=settings.email_from.partition("@")[2] or None)
        message.set_content(email.text)
        message.add_alternative(email.html, subtype="html")
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT_SECONDS) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(message)

    async def send(self, email: Email) -> None:
        await asyncio.to_thread(self._send, email)
