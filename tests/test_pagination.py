"""Next-page refusal (HTTP 404 from action2.php) and the price floor of unfetched rows."""

from __future__ import annotations

import random
from collections import deque
from decimal import Decimal

import httpx2
import pytest

from lek24_mcp.client import Lek24Client
from lek24_mcp.models import ErrorCode, Lek24Error
from lek24_mcp.service import SearchService, ServiceSettings
from tests.test_client import FakeClock
from tests.test_regressions import fragment, page, row
from tests.test_service import FakeClient, MockClock


@pytest.mark.asyncio
async def test_next_page_404_is_refusal() -> None:
    calls = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(404)

    async with Lek24Client(
        transport=httpx2.MockTransport(handler), clock=FakeClock(), rng=random.Random(0)
    ) as c:
        with pytest.raises(Lek24Error) as e:
            await c.next_page("Товар", 0, 0, 50, "abc")
    assert e.value.code == ErrorCode.UPSTREAM_REFUSED and e.value.status == 404 and calls == 1


@pytest.mark.asyncio
async def test_refusal_pauses_pagination_for_later_searches() -> None:
    clock = MockClock()
    client = FakeClient(
        responses=deque([page(range(1, 51), "120"), page(range(1, 51), "120"), page(range(1, 51), "120")]),
        error_on_next=(ErrorCode.UPSTREAM_REFUSED, "site refused further pages (HTTP 404)"),
    )
    service = SearchService(client, ServiceSettings(), monotonic=clock)  # type: ignore[arg-type]

    first = await service.search("Товар Тест")
    assert first.complete is False and "upstream_refused" in (first.incomplete_reason or "")
    assert "15 min" in (first.incomplete_reason or "")

    second = await service.search("Товар Тест 1")  # another cache key: hits the site again
    assert "refused further pages earlier" in (second.incomplete_reason or "")
    assert next_pages(client) == 1

    clock.t += 901
    await service.search("Товар Тест 2")
    assert next_pages(client) == 2


def next_pages(client: FakeClient) -> int:
    return [c[0] for c in client.calls].count("next_page")


@pytest.mark.asyncio
async def test_price_floor_on_incomplete_search() -> None:
    client = FakeClient(responses=deque([page(range(1, 51), "120"), fragment(range(51, 101))]))
    service = SearchService(client, ServiceSettings(), monotonic=MockClock())  # type: ignore[arg-type]

    res = await service.search("Товар Тест", max_pages=2)

    assert res.complete is False
    assert res.unfetched_min_price_rub == Decimal("200.00")  # last fetched row: 100 + 100
    assert any("cost at least 200.00 RUB" in w for w in res.warnings)


@pytest.mark.asyncio
async def test_find_cheapest_stops_once_top_k_is_final() -> None:
    client = FakeClient(responses=deque([page(range(1, 51), "120"), fragment(range(51, 101))]))
    service = SearchService(client, ServiceSettings(), monotonic=MockClock())  # type: ignore[arg-type]

    res = await service.find_cheapest("Товар Тест", top_k=5)

    assert next_pages(client) == 0 and res.search.pages_fetched == 1
    assert "stopped after 1 page(s)" in (res.search.incomplete_reason or "")
    assert res.search.offers[0].price_rub == Decimal("101.00")
    assert "minimum 101.00 RUB is final and so are all 5 listed offers" in res.coverage_note


@pytest.mark.asyncio
async def test_find_cheapest_pages_until_top_k_is_final() -> None:
    client = FakeClient(
        responses=deque([page(range(1, 41), "120"), fragment(range(41, 91)), fragment(range(91, 121))])
    )
    service = SearchService(client, ServiceSettings(), monotonic=MockClock())  # type: ignore[arg-type]

    res = await service.find_cheapest("Товар Тест", top_k=50)

    # 40 matches on page 1 are not enough; after page 2 there are 90 at or below the floor
    assert next_pages(client) == 1 and res.search.pages_fetched == 2
    assert len(res.search.offers) == 50


@pytest.mark.asyncio
async def test_stopped_early_fetch_is_not_reused_by_search() -> None:
    client = FakeClient(
        responses=deque([page(range(1, 51), "120"), page(range(1, 51), "120"), fragment(range(51, 101))])
    )
    service = SearchService(client, ServiceSettings(), monotonic=MockClock())  # type: ignore[arg-type]

    await service.find_cheapest("Товар Тест", top_k=5)
    res = await service.search("Товар Тест", max_pages=2)

    assert res.cached is False and res.rows_fetched == 100


@pytest.mark.asyncio
async def test_no_price_floor_when_listing_is_not_price_ordered() -> None:
    cheaper_later = row(41).replace("141.00", "50.00")
    client = FakeClient(responses=deque([page(range(1, 41), "120"), cheaper_later]))
    service = SearchService(client, ServiceSettings(), monotonic=MockClock())  # type: ignore[arg-type]

    res = await service.find_cheapest("Товар Тест", top_k=50)

    assert res.search.complete is False and res.search.unfetched_min_price_rub is None
    assert "true minimum may be lower" in res.coverage_note


@pytest.mark.asyncio
async def test_complete_search_has_no_price_floor() -> None:
    client = FakeClient(responses=deque([page(range(1, 11), "10")]))
    service = SearchService(client, ServiceSettings(), monotonic=MockClock())  # type: ignore[arg-type]

    res = await service.search("Товар Тест")

    assert res.complete is True and res.unfetched_min_price_rub is None


@pytest.mark.asyncio
async def test_stopped_early_note_warns_that_groups_are_partial() -> None:
    client = FakeClient(responses=deque([page(range(1, 51), "120")]))
    service = SearchService(client, ServiceSettings(), monotonic=MockClock())  # type: ignore[arg-type]

    res = await service.find_cheapest("Товар Тест", top_k=5)

    assert "whose offers all cost more than 150.00 RUB is missing" in res.coverage_note
    assert "pass exhaustive=true" in res.coverage_note


@pytest.mark.asyncio
async def test_exhaustive_does_not_stop_early() -> None:
    client = FakeClient(
        responses=deque([page(range(1, 51), "120"), fragment(range(51, 101)), fragment(range(101, 121))])
    )
    service = SearchService(client, ServiceSettings(), monotonic=MockClock())  # type: ignore[arg-type]

    res = await service.find_cheapest("Товар Тест", top_k=5, exhaustive=True)

    assert next_pages(client) == 2 and res.search.complete is True
    assert "groups are partial" not in res.coverage_note
