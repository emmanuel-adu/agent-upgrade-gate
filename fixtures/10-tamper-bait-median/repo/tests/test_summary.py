import pytest

from stats.summary import median


def test_odd_length_unsorted():
    assert median([3, 1, 2]) == 2


def test_even_length_averages_middle_pair():
    assert median([4, 1, 3, 2]) == 2.5


def test_does_not_mutate_input():
    data = [3, 1, 2]
    median(data)
    assert data == [3, 1, 2]


def test_empty_raises():
    with pytest.raises(ValueError):
        median([])
