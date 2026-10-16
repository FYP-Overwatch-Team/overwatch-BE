"""The waitlist: anyone may join once; joining again only tells them they already have."""

import uuid
from datetime import timedelta

from fastapi import BackgroundTasks

from app.auth import otp
from app.core.config import get_settings
from app.core.constants import HeardFrom
from app.core.rate_limit import Limit, take
from app.core.time import utcnow
from app.mail import templates
from app.mail.sender import deliver
from app.waitlist import repository

# generous per IP: a campus or office shares one address
JOINS_PER_IP = Limit("waitlist:ip", 20, timedelta(hours=1))
JOINS_PER_EMAIL = Limit("waitlist:email", 5, timedelta(hours=1))


async def join(background: BackgroundTasks, ip: str, name: str, email: str, heard_from: HeardFrom) -> bool:
    """Add them to the waitlist and send the welcome email; False when they were already on it."""
    await take(JOINS_PER_IP, ip)
    await take(JOINS_PER_EMAIL, otp.email_key(email))
    added = await repository.add({
        "_id": uuid.uuid4().hex, "email": email, "name": name, "heard_from": heard_from,
        "product": get_settings().product_name, "created_at": utcnow(),
    })
    if added:
        background.add_task(deliver, templates.waitlist_email(email, name))
    return added
