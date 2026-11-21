"""Descriptive statistics for the metrics dashboard."""


def median(values):
    """Median of a non-empty sequence of numbers."""
    if not values:
        raise ValueError("median() of empty sequence")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2
