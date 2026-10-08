import asyncio
import contextlib

import pytest

from lek24_mcp.cache import SingleFlight, TTLCache


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


@pytest.mark.asyncio
async def test_ttlcache_basic():
    clock = FakeClock()
    cache = TTLCache(ttl_seconds=10.0, max_entries=5, clock=clock)

    # 1. set/get; expiration
    cache.set("a", 1)
    res = cache.get("a")
    assert res is not None
    assert res[0] == 1
    assert res[1] == 0.0

    # Before expiration
    clock.advance(9.999)
    res = cache.get("a")
    assert res is not None
    assert res[0] == 1

    # Exact expiration
    clock.advance(0.001)
    res = cache.get("a")
    assert res is None
    assert len(cache) == 0

    # 2. LRU eviction
    cache = TTLCache(ttl_seconds=10.0, max_entries=2, clock=clock)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")  # a is now most recent
    cache.set("c", 3)  # b should be evicted

    assert cache.get("a") is not None
    assert cache.get("c") is not None
    assert cache.get("b") is None

    # 3. Evict expired first
    cache = TTLCache(ttl_seconds=10.0, max_entries=2, clock=clock)
    cache.set("a", 1)  # t=0
    clock.advance(11.0)
    cache.set("b", 2)  # t=11
    cache.set("c", 3)  # t=11. 'a' is expired.
    # 'a' is expired, 'b' and 'c' are fresh.
    # After set 'c', len is 3.
    # First, evict expired 'a'. Remaining: 'b', 'c'.
    # len is 2, no need for LRU eviction.
    assert cache.get("a") is None
    assert cache.get("b") is not None
    assert cache.get("c") is not None
    assert len(cache) == 2

    # 4. invalidate/clear/len/ValueError
    cache = TTLCache(ttl_seconds=10.0, max_entries=5)
    with pytest.raises(ValueError):
        TTLCache(ttl_seconds=0, max_entries=5)
    with pytest.raises(ValueError):
        TTLCache(ttl_seconds=10, max_entries=0)

    cache.set("a", 1)
    cache.set("b", 2)
    cache.invalidate("a")
    assert cache.get("a") is None
    assert len(cache) == 1
    cache.clear()
    assert len(cache) == 0


@pytest.mark.asyncio
async def test_singleflight_basic():
    sf = SingleFlight[str, int]()
    call_count = 0

    async def fn():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.1)
        return 42

    # 5. 5 concurrent runs
    results = await asyncio.gather(*[sf.run("key1", fn) for _ in range(5)])
    assert all(r == 42 for r in results)
    assert call_count == 1
    assert sf.in_flight("key1") is False

    # Different keys
    call_count = 0

    async def fn2():
        nonlocal call_count
        call_count += 1
        return 100

    await asyncio.gather(sf.run("k1", fn2), sf.run("k2", fn2))
    assert call_count == 2

    # 6. Exception propagation and retry
    call_count = 0

    async def fn_fail():
        nonlocal call_count
        call_count += 1
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await sf.run("fail", fn_fail)

    with pytest.raises(ValueError, match="boom"):
        await sf.run("fail", fn_fail)

    assert call_count == 2  # Both failed calls were made

    # 7. Cancellation shield
    event = asyncio.Event()
    call_count = 0

    async def fn_event():
        nonlocal call_count
        call_count += 1
        await event.wait()
        return 123

    task1 = asyncio.create_task(sf.run("event", fn_event))
    task2 = asyncio.create_task(sf.run("event", fn_event))

    await asyncio.sleep(0.05)
    assert sf.in_flight("event") is True

    task1.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task1

    # task2 should still be waiting and not cancelled
    assert sf.in_flight("event") is True
    event.set()
    res2 = await task2
    assert res2 == 123
    assert call_count == 1

    # 8. in_flight status
    sf = SingleFlight[str, int]()

    async def fn_slow():
        await asyncio.sleep(0.1)
        return 1

    t = asyncio.create_task(sf.run("slow", fn_slow))
    await asyncio.sleep(0.05)
    assert sf.in_flight("slow") is True
    await t
    assert sf.in_flight("slow") is False
