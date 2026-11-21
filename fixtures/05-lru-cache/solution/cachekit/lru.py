"""A small LRU cache used by the API gateway."""
from collections import OrderedDict


class LRUCache:
    """Least-recently-used cache with a fixed capacity.

    - get(key, default=None): return the value and mark key as most recently used
    - put(key, value): insert or update, mark as most recently used, and evict the least
      recently used entry when over capacity
    - len(cache) and `key in cache` (membership must NOT change recency)
    - capacity < 1 raises ValueError
    """

    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._data = OrderedDict()

    def get(self, key, default=None):
        if key not in self._data:
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key, value):
        self._data[key] = value
        self._data.move_to_end(key)
        if len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        return key in self._data
