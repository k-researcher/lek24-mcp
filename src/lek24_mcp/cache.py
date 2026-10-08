import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable


class TTLCache[K: Hashable, V]:
    """
    A Least Recently Used (LRU) cache with Time-To-Live (TTL) expiration.

    Note: This class is NOT thread-safe. It is designed for use within a single
    asyncio event loop.
    """

    def __init__(
        self, ttl_seconds: float, max_entries: int, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")

        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        # Stores key -> (value, stored_at)
        self._cache: OrderedDict[K, tuple[V, float]] = OrderedDict()

    def get(self, key: K) -> tuple[V, float] | None:
        """Returns (value, stored_at) if key exists and is not expired, else None."""
        if key not in self._cache:
            return None

        value, stored_at = self._cache[key]
        if self._clock() - stored_at >= self._ttl:
            del self._cache[key]
            return None

        # LRU: move to end (most recent)
        self._cache.move_to_end(key)
        return value, stored_at

    def set(self, key: K, value: V) -> None:
        """Sets a value for the given key and updates its freshness."""
        now = self._clock()

        if key in self._cache:
            del self._cache[key]

        self._cache[key] = (value, now)
        self._cache.move_to_end(key)

        if len(self._cache) > self._max_entries:
            self._evict()

    def _evict(self) -> None:
        """Evicts expired entries first, then oldest LRU entries."""
        now = self._clock()

        # 1. Try to remove expired entries
        keys_to_remove = []
        for k, (_, stored_at) in self._cache.items():
            if now - stored_at >= self._ttl:
                keys_to_remove.append(k)

        for k in keys_to_remove:
            del self._cache[k]

        # 2. If still over capacity, remove LRU (from the beginning)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)

    def invalidate(self, key: K) -> None:
        """Removes key from cache if it exists."""
        self._cache.pop(key, None)

    def clear(self) -> None:
        """Clears all entries from the cache."""
        self._cache.clear()

    def __len__(self) -> int:
        """Returns the number of entries in the cache."""
        return len(self._cache)


class SingleFlight[K: Hashable, V]:
    """
    Implements single-flight pattern to collapse multiple concurrent requests
    for the same key into a single execution.
    """

    def __init__(self) -> None:
        # key -> Task[V]
        self._in_flight: dict[K, asyncio.Task[V]] = {}

    async def run(self, key: K, fn: Callable[[], Awaitable[V]]) -> V:
        """Executes fn if no task is running for key, else waits for existing task."""
        task = self._in_flight.get(key)
        if task is None:
            task = asyncio.create_task(self._execute(key, fn))
            self._in_flight[key] = task
        # shield: cancelling one waiter (including the first) must not cancel the shared task
        return await asyncio.shield(task)

    async def _execute(self, key: K, fn: Callable[[], Awaitable[V]]) -> V:
        """Internal wrapper to ensure key is removed from in_flight."""
        try:
            return await fn()
        finally:
            self._in_flight.pop(key, None)

    def in_flight(self, key: K) -> bool:
        """Returns True if there is an active task for the given key."""
        return key in self._in_flight
