"""Emails kept in a list, for tests."""

from app.mail.sender import Email


class Outbox:
    def __init__(self) -> None:
        self.sent: list[Email] = []

    async def send(self, email: Email) -> None:
        self.sent.append(email)

    def to(self, address: str) -> list[Email]:
        return [email for email in self.sent if email.to == address]
