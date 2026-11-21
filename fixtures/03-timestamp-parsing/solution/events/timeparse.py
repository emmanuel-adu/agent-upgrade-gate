"""Parse ISO-8601 timestamps coming from upstream webhooks."""
from datetime import datetime, timezone


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and return an aware datetime in UTC."""
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {type(value).__name__}")
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
