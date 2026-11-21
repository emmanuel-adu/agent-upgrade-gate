"""Pagination helpers for the catalogue API."""


def total_pages(n_items: int, per_page: int) -> int:
    """Number of pages needed to show n_items, per_page at a time (at least 1)."""
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    return max(1, n_items // per_page)


def paginate(items: list, page: int, per_page: int) -> list:
    """Return the items on a 1-indexed page."""
    if page < 1:
        raise ValueError("page must be >= 1")
    start = page * per_page
    return items[start : start + per_page]
