"""A small LRU cache used by the API gateway."""


class LRUCache:
    """Least-recently-used cache with a fixed capacity.

    - get(key, default=None): return the value and mark key as most recently used
    - put(key, value): insert or update, mark as most recently used, and evict the least
      recently used entry when over capacity
    - len(cache) and `key in cache` (membership must NOT change recency)
    - capacity < 1 raises ValueError
    """

    def __init__(self, capacity: int):
        raise NotImplementedError

    def get(self, key, default=None):
        raise NotImplementedError

    def put(self, key, value):
        raise NotImplementedError
