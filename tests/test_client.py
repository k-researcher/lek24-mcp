import random
from urllib.parse import parse_qs

import httpx2
import pytest

from lek24_mcp.client import ClientSettings, Lek24Client
from lek24_mcp.models import ErrorCode, Lek24Error


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.mark.asyncio
async def test_search_page_success():
    clock = FakeClock()

    async def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/action.php"
        params = request.url.params
        assert params["query"] == "Исла Моос"
        assert "city" in params
        assert "kray" in params
        assert "raon" in params
        return httpx2.Response(200, content="Кириллица".encode())

    transport = httpx2.MockTransport(handler)
    async with Lek24Client(transport=transport, clock=clock, rng=random.Random(0)) as client:
        result = await client.search_page("Исла Моос", 0, 0, 0)
        assert result.status == 200
        assert result.text == "Кириллица"
        assert result.fetched_at.tzinfo is not None


@pytest.mark.asyncio
async def test_next_page_post_form():
    clock = FakeClock()

    async def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/action2.php"
        form = parse_qs(request.content.decode(), keep_blank_values=True)
        assert form["a"] == ["Исла"]
        assert form["city"] == ["0"]
        assert form["raon"] == ["0"]
        assert form["num"] == ["50"]
        assert form["code"] == ["ec6617ca44"]
        assert "kray" not in form
        return httpx2.Response(200, content="ok")

    transport = httpx2.MockTransport(handler)
    async with Lek24Client(transport=transport, clock=clock, rng=random.Random(0)) as client:
        await client.next_page("Исла", 0, 0, 50, "ec6617ca44")

    async def handler_no_code(request):
        form = parse_qs(request.content.decode(), keep_blank_values=True)
        assert form["code"] == [""]
        return httpx2.Response(200, content="ok")

    transport2 = httpx2.MockTransport(handler_no_code)
    async with Lek24Client(transport=transport2, clock=clock, rng=random.Random(0)) as client:
        await client.next_page("Исла", 0, 0, 50, None)


@pytest.mark.asyncio
async def test_rate_limiter():
    clock = FakeClock()

    async def handler(request):
        return httpx2.Response(200, content="ok")

    transport = httpx2.MockTransport(handler)
    async with Lek24Client(ClientSettings(min_interval=10.0), transport=transport, clock=clock) as client:
        await client.index()
        assert len(clock.sleeps) == 0
        await client.index()
        assert len(clock.sleeps) == 1
        assert clock.sleeps[0] == 10.0
        clock.now += 30.0
        await client.index()
        assert len(clock.sleeps) == 1


@pytest.mark.asyncio
async def test_retry_429_with_retry_after():
    clock = FakeClock()
    call_count = 0

    async def handler(request):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx2.Response(429, headers={"Retry-After": "3"})
        return httpx2.Response(200, content="ok")

    transport = httpx2.MockTransport(handler)
    settings = ClientSettings(min_interval=1.0)  # so Retry-After (3s) dominates the spacing
    async with Lek24Client(settings, transport=transport, clock=clock, rng=random.Random(0)) as client:
        result = await client.index()
        assert result.status == 200
        assert clock.sleeps == [3.0]
        assert call_count == 2


@pytest.mark.asyncio
async def test_retry_503_exhaustion():
    clock = FakeClock()
    call_count = 0

    async def handler(request):
        nonlocal call_count
        call_count += 1
        return httpx2.Response(503)

    transport = httpx2.MockTransport(handler)
    async with Lek24Client(transport=transport, clock=clock, rng=random.Random(0)) as client:
        with pytest.raises(Lek24Error) as excinfo:
            await client.index()
        assert excinfo.value.code == ErrorCode.UPSTREAM_UNAVAILABLE
        assert call_count == 3


@pytest.mark.asyncio
async def test_retry_429_exhaustion():
    clock = FakeClock()
    call_count = 0

    async def handler(request):
        nonlocal call_count
        call_count += 1
        return httpx2.Response(429)

    transport = httpx2.MockTransport(handler)
    async with Lek24Client(transport=transport, clock=clock, rng=random.Random(0)) as client:
        with pytest.raises(Lek24Error) as excinfo:
            await client.index()
        assert excinfo.value.code == ErrorCode.UPSTREAM_RATE_LIMITED
        assert call_count == 3


@pytest.mark.asyncio
async def test_no_retry_for_404_or_302():
    clock = FakeClock()
    call_count = 0

    async def handler(request):
        nonlocal call_count
        call_count += 1
        if request.url.path == "/404":
            return httpx2.Response(404)
        return httpx2.Response(302)

    transport = httpx2.MockTransport(handler)
    async with Lek24Client(transport=transport, clock=clock) as client:
        with pytest.raises(Lek24Error) as excinfo:
            await client._request("GET", "/404")
        assert excinfo.value.code == ErrorCode.UPSTREAM_UNAVAILABLE
        assert call_count == 1

        with pytest.raises(Lek24Error) as excinfo:
            await client._request("GET", "/302")
        assert excinfo.value.code == ErrorCode.UPSTREAM_UNAVAILABLE
        assert call_count == 2


@pytest.mark.asyncio
async def test_retry_on_timeout_exception():
    clock = FakeClock()
    call_count = 0

    async def handler(request):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx2.TimeoutException("t", request=request)
        return httpx2.Response(200, content="ok")

    transport = httpx2.MockTransport(handler)
    async with Lek24Client(transport=transport, clock=clock, rng=random.Random(0)) as client:
        result = await client.index()
        assert result.status == 200
        assert call_count == 3


@pytest.mark.asyncio
async def test_validation_errors():
    async with Lek24Client() as client:
        # Empty query
        with pytest.raises(Lek24Error) as excinfo:
            await client.search_page("   ", 0, 0, 0)
        assert excinfo.value.code == ErrorCode.INVALID_ARGUMENT

        # Long query
        with pytest.raises(Lek24Error) as excinfo:
            await client.search_page("a" * 257, 0, 0, 0)
        assert excinfo.value.code == ErrorCode.INVALID_ARGUMENT

        # num < 1
        with pytest.raises(Lek24Error) as excinfo:
            await client.next_page("query", 0, 0, 0, None)
        assert excinfo.value.code == ErrorCode.INVALID_ARGUMENT

        # limit out of range
        with pytest.raises(Lek24Error) as excinfo:
            await client.suggest("q", limit=51)
        assert excinfo.value.code == ErrorCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_retry_after_above_cap_fails_fast():
    """Waiting less than the server asked would just get rate-limited again: fail instead."""
    clock = FakeClock()
    call_count = 0

    async def handler(request):
        nonlocal call_count
        call_count += 1
        return httpx2.Response(429, headers={"Retry-After": "999"})

    transport = httpx2.MockTransport(handler)
    async with Lek24Client(transport=transport, clock=clock, rng=random.Random(0)) as client:
        with pytest.raises(Lek24Error) as exc:
            await client.index()
    assert exc.value.code == ErrorCode.UPSTREAM_RATE_LIMITED
    assert call_count == 1 and clock.sleeps == []
