import json

from utils.helpers import fmt


def get_user(user_id):
    return json.dumps({"id": user_id, "name": fmt("user")})
