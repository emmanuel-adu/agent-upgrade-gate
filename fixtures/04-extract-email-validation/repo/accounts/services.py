"""Account service layer."""
import re

_users = {}


class ValidationError(ValueError):
    pass


def create_user(user_id, email):
    email = email.strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email):
        raise ValidationError(f"invalid email: {email!r}")
    _users[user_id] = {"id": user_id, "email": email.lower()}
    return _users[user_id]


def update_email(user_id, email):
    if user_id not in _users:
        raise KeyError(user_id)
    if "@" not in email:
        raise ValidationError(f"invalid email: {email!r}")
    _users[user_id]["email"] = email
    return _users[user_id]
