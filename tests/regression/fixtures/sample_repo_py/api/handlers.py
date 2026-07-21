import os
from services.users import get_user
from utils.helpers import fmt


class ApiHandler:
    pass


def handle_request(payload):
    return fmt(get_user(payload.get("id", os.getpid())))
