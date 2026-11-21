"""Account service layer."""
from .validation import ValidationError, normalize_email

__all__ = ["ValidationError", "create_user", "update_email"]

_users = {}


def create_user(user_id, email):
    _users[user_id] = {"id": user_id, "email": normalize_email(email)}
    return _users[user_id]


def update_email(user_id, email):
    if user_id not in _users:
        raise KeyError(user_id)
    _users[user_id]["email"] = normalize_email(email)
    return _users[user_id]
