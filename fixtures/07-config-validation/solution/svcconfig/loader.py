"""Service configuration loading."""

_ALLOWED = {"host", "port", "retries", "timeout_s"}


class ConfigError(ValueError):
    """Raised for invalid configuration. The message must name the offending field."""


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def load_config(raw: dict) -> dict:
    """Validate and normalise a raw config mapping."""
    unknown = sorted(set(raw) - _ALLOWED)
    if unknown:
        raise ConfigError(f"unknown field(s): {', '.join(unknown)}")
    host = raw.get("host")
    if not isinstance(host, str) or not host:
        raise ConfigError("host must be a non-empty string")
    port = raw.get("port")
    if not _is_int(port) or not 1 <= port <= 65535:
        raise ConfigError("port must be an int between 1 and 65535")
    retries = raw.get("retries", 3)
    if not _is_int(retries) or retries < 0:
        raise ConfigError("retries must be an int >= 0")
    timeout = raw.get("timeout_s", 10.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("timeout_s must be a number > 0")
    return {"host": host, "port": port, "retries": retries, "timeout_s": float(timeout)}
