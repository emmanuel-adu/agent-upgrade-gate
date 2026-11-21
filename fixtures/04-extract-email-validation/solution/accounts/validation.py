"""Shared validation for account data."""
import re

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[a-z]{2,}")


class ValidationError(ValueError):
    pass


def normalize_email(email: str) -> str:
    if not isinstance(email, str):
        raise ValidationError(f"invalid email: {email!r}")
    candidate = email.strip().lower()
    if not _EMAIL.fullmatch(candidate):
        raise ValidationError(f"invalid email: {email!r}")
    return candidate
