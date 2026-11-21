"""Descriptive statistics for the metrics dashboard."""


def median(values):
    """Median of a non-empty sequence of numbers."""
    if not values:
        raise ValueError("median() of empty sequence")
    n = len(values)
    return values[n // 2]
