import pytest

from cachekit.lru import LRUCache


def test_evicts_least_recently_used():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    c.put("c", 3)
    assert "a" not in c and c.get("b") == 2 and c.get("c") == 3


def test_get_refreshes_recency():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    assert c.get("a") == 1
    c.put("c", 3)
    assert "b" not in c and "a" in c


def test_put_updates_existing_without_growing():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("a", 10)
    assert len(c) == 1 and c.get("a") == 10


def test_contains_does_not_refresh():
    c = LRUCache(2)
    c.put("a", 1)
    c.put("b", 2)
    assert "a" in c
    c.put("c", 3)
    assert "a" not in c


def test_default_and_capacity_validation():
    assert LRUCache(1).get("missing", "d") == "d"
    with pytest.raises(ValueError):
        LRUCache(0)
