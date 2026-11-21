"""Service configuration loading."""


class ConfigError(ValueError):
    """Raised for invalid configuration. The message must name the offending field."""


def load_config(raw: dict) -> dict:
    """Validate and normalise a raw config mapping.

    TODO: currently returns the input unchanged.
    """
    return raw
