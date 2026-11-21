"""Parse ISO-8601 timestamps coming from upstream webhooks."""
from datetime import datetime, timezone


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp and return an aware datetime in UTC."""
    dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    return dt.replace(tzinfo=timezone.utc)
