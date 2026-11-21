import pytest

from shop.pagination import paginate, total_pages

ITEMS = list(range(1, 24))  # 23 items


def test_first_page():
    assert paginate(ITEMS, 1, 10) == list(range(1, 11))


def test_second_page():
    assert paginate(ITEMS, 2, 10) == list(range(11, 21))


def test_last_partial_page():
    assert paginate(ITEMS, 3, 10) == [21, 22, 23]


def test_page_past_end_is_empty():
    assert paginate(ITEMS, 4, 10) == []


def test_total_pages_rounds_up():
    assert total_pages(23, 10) == 3
    assert total_pages(20, 10) == 2
    assert total_pages(0, 10) == 1


def test_invalid_arguments():
    with pytest.raises(ValueError):
        paginate(ITEMS, 0, 10)
    with pytest.raises(ValueError):
        total_pages(5, 0)
