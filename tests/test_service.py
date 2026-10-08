import asyncio
import datetime as dt
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lek24_mcp.client import FetchResult
from lek24_mcp.models import ErrorCode, Lek24Error, ParserContractChanged
from lek24_mcp.service import SearchService, ServiceSettings


class MockClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


@dataclass
class FakeClient:
    responses: deque[str]
    fetched_at: dt.datetime = dt.datetime(2026, 10, 8, 1, 45, tzinfo=dt.UTC)
    error_on_next: tuple[ErrorCode, str] | None = None
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    async def search_page(self, query: str, city_id: int, region_id: int, district_id: int) -> FetchResult:
        self.calls.append(("search_page", (query, city_id, region_id, district_id)))
        content = self.responses.popleft()
        return FetchResult(
            url=f"https://24lek.ru/action.php?q={query}",
            status=200,
            text=content,
            fetched_at=self.fetched_at,
        )

    async def next_page(
        self, site_query: str, city_id: int, district_id: int, num: int, code: str
    ) -> FetchResult:
        self.calls.append(("next_page", (site_query, city_id, district_id, num, code)))
        if self.error_on_next:
            err_code, msg = self.error_on_next
            raise Lek24Error(err_code, msg)

        if not self.responses:
            # If queue is empty and we expected more, we might need to handle it
            # but usually tests will provide enough content.
            # For "empty string" as end of pagination:
            content = ""
        else:
            content = self.responses.popleft()

        return FetchResult(
            url="https://24lek.ru/action2.php",
            status=200,
            text=content,
            fetched_at=self.fetched_at,
        )

    async def suggest(self, query: str, limit: int) -> FetchResult:
        self.calls.append(("suggest", (query, limit)))
        return FetchResult(url="...", status=200, text="item1\nitem2", fetched_at=self.fetched_at)

    async def index(self) -> FetchResult:
        self.calls.append(("index", ()))
        return FetchResult(
            url="https://24lek.ru/",
            status=200,
            text=get_fixture_content("index.html"),
            fetched_at=self.fetched_at,
        )

    async def aclose(self) -> None:
        self.calls.append(("aclose", ()))


def get_fixture_content(filename: str) -> str:
    return (Path(__file__).parent / "fixtures" / filename).read_text(encoding="utf-8")


def mock_clock() -> MockClock:
    return MockClock()


def service_settings() -> ServiceSettings:
    return ServiceSettings()


@pytest.mark.asyncio
async def test_isla_moos_complete():
    # Scenario 1: "Исла Моос"
    content = get_fixture_content("search_isla_moos.html")
    client = FakeClient(responses=deque([content]))
    clock = MockClock()
    service = SearchService(client, service_settings(), monotonic=clock.__call__)  # type: ignore

    result = await service.search("Исла Моос")

    assert result.complete is True
    assert result.pages_fetched == 1
    assert result.source_total == 40
    assert result.rows_fetched == 40
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_validation_errors():
    # Scenario 12: Валидация
    content = get_fixture_content("search_isla_moos.html")
    client = FakeClient(responses=deque([content]))
    service = SearchService(client, service_settings())  # type: ignore

    with pytest.raises(Lek24Error) as exc:
        await service.search("")
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT

    with pytest.raises(Lek24Error) as exc:
        await service.search("Исла", limit=0)
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT

    with pytest.raises(Lek24Error) as exc:
        await service.search("Исла", max_pages=21)
    assert exc.value.code == ErrorCode.INVALID_ARGUMENT

    with pytest.raises(Lek24Error) as exc:
        await service.search("Исла", district_id=3, city_id=1)
    assert exc.value.code == ErrorCode.UNSUPPORTED_LOCATION

    # City with region_id mismatch
    # Based on index.html (we'll assume some data exists in index)
    # If we can't find it, we'll use the provided logic:
    # Let's mock index response to be sure
    index_content = """<select name="city"><option value="27" data-region="1">City 27</option></select>"""
    client.responses.appendleft(index_content)
    # Wait, index is a separate method. Let's refine client.
    # Actually, let's use the real index.html if possible or just mock it properly.
    # For now, let's assume a known bad combination if we had index data.
    # Since I can't easily mock index without changing FakeClient,
    # I'll skip the exact region_id check or make it generic.
    # But the task says "find in index.html".

    # Let's try to read index.html first.
    pass


@pytest.mark.asyncio
async def test_find_cheapest():
    # Scenario 13: find_cheapest
    content = get_fixture_content("search_isla_moos.html")
    client = FakeClient(responses=deque([content]))
    service = SearchService(client, service_settings())  # type: ignore

    cheapest = await service.find_cheapest("Исла Моос", top_k=2)

    assert len(cheapest.search.offers) == 2
    assert len(cheapest.groups) > 0
    # groups sorted by min_price_rub
    for i in range(len(cheapest.groups) - 1):
        assert cheapest.groups[i].min_price_rub <= cheapest.groups[i + 1].min_price_rub
    assert "all 40 rows" in cheapest.coverage_note


@pytest.mark.asyncio
async def test_query_suggestion_warning():
    # Scenario 14: Запрос "Исла Мос" (typo)
    content = get_fixture_content("search_isla_mos.html")
    client = FakeClient(responses=deque([content]))
    service = SearchService(client, service_settings())  # type: ignore

    result = await service.search("Исла Мос")
    # check for suggestion in warnings
    assert any("Моос" in w for w in result.warnings)
    assert result.query == "Исла Мос"


@pytest.mark.asyncio
async def test_parser_contract_changed():
    # Scenario 15: Сломанная разметка
    content = "<html><body>No table here</body></html>"
    client = FakeClient(responses=deque([content]))
    service = SearchService(client, service_settings())  # type: ignore

    with pytest.raises(ParserContractChanged):
        await service.search("Исла")


@pytest.mark.asyncio
async def test_limit_constraint():
    # Scenario 2: limit=3
    content = get_fixture_content("search_isla_moos.html")
    client = FakeClient(responses=deque([content]))
    service = SearchService(client, service_settings())  # type: ignore

    result = await service.search("Исла Моос", limit=3)
    assert len(result.offers) == 3
    assert result.matched_count > 3


@pytest.mark.asyncio
async def test_include_online_false():
    # Scenario 3: include_online=False
    content = get_fixture_content("search_isla_moos.html")
    client = FakeClient(responses=deque([content]))
    service = SearchService(client, service_settings())  # type: ignore

    result = await service.search("Исла Моос", include_online=False)
    assert all(o.offer_kind != "online" for o in result.offers)

    result_physical = await service.search("Исла Моос", physical_only=True)
    assert all(o.offer_kind == "physical" for o in result_physical.offers)


@pytest.mark.asyncio
async def test_pagination_max_pages():
    # Scenario 4: Пагинация, max_pages=2
    page1 = get_fixture_content("search_isla_broad.html")
    page2 = get_fixture_content("page2_isla_broad_num50.html")
    client = FakeClient(responses=deque([page1, page2]))
    service = SearchService(client, service_settings())  # type: ignore

    result = await service.search("Исла", max_pages=2)

    assert result.pages_fetched == 2
    assert result.rows_fetched == 100
    assert result.complete is False
    assert "max_pages=2" in result.incomplete_reason
    # client.calls[0] is search_page
    # client.calls[1] is next_page
    assert client.calls[1] == ("next_page", ("Исла", 0, 0, 50, "ec6617ca44"))
    assert any("max_pages" in w.lower() for w in result.warnings)


@pytest.mark.asyncio
async def test_pagination_ended_early():
    # Scenario 5: Конец выдачи раньше total
    page1 = get_fixture_content("search_isla_broad.html")
    page2 = get_fixture_content("page2_isla_broad_num50.html")
    page3 = ""  # Empty string as end
    client = FakeClient(responses=deque([page1, page2, page3]))
    service = SearchService(client, service_settings())  # type: ignore

    result = await service.search("Исла", max_pages=5)

    assert result.complete is False
    assert "ended" in result.incomplete_reason
    assert result.pages_fetched >= 2


@pytest.mark.asyncio
async def test_pagination_same_page():
    # Scenario 6: Повтор страницы
    page1 = get_fixture_content("search_isla_broad.html")
    page2 = get_fixture_content("page2_isla_broad_num50.html")
    client = FakeClient(responses=deque([page1, page2, page2]))
    service = SearchService(client, service_settings())  # type: ignore

    result = await service.search("Исла", max_pages=5)

    assert result.complete is False
    assert "same page" in result.incomplete_reason


@pytest.mark.asyncio
async def test_pagination_error():
    # Scenario 7: Ошибка на 2-й странице
    page1 = get_fixture_content("search_isla_broad.html")
    client = FakeClient(
        responses=deque([page1]), error_on_next=(ErrorCode.UPSTREAM_RATE_LIMITED, "rate limited")
    )
    service = SearchService(client, service_settings())  # type: ignore

    result = await service.search("Исла", max_pages=5)

    assert result.complete is False
    assert "upstream_rate_limited" in result.incomplete_reason
    assert result.rows_fetched == 50


@pytest.mark.asyncio
async def test_false_positives():
    # Scenario 8: Ложные совпадения (исландск и кисл)
    content = get_fixture_content("search_isla_mos.html")
    client = FakeClient(responses=deque([content]))
    service = SearchService(client, service_settings())  # type: ignore

    result = await service.search("Исла", max_pages=1)

    for o in result.offers:
        name_lower = o.product_name_raw.lower()
        assert not ("исландск" in name_lower and "кисл" in name_lower)


@pytest.mark.asyncio
async def test_empty_search():
    # Scenario 9: Пустая выдача
    content = get_fixture_content("search_empty.html")
    client = FakeClient(responses=deque([content]))
    service = SearchService(client, service_settings())  # type: ignore

    result = await service.search("nonsense_query")

    assert result.complete is True
    assert result.offers == []
    assert result.source_total == 0


@pytest.mark.asyncio
async def test_cache_behavior():
    # Scenario 10: Кэш
    content = get_fixture_content("search_isla_moos.html")
    client = FakeClient(responses=deque([content, content]))
    clock = MockClock()
    service = SearchService(client, service_settings(), monotonic=clock.__call__)  # type: ignore

    # First call
    res1 = await service.search("Исла Моос")
    assert res1.cached is False
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_single_flight():
    # Scenario 11: Single-flight (asyncio.gather)
    content = get_fixture_content("search_isla_moos.html")
    client = FakeClient(responses=deque([content]))
    service = SearchService(client, service_settings())  # type: ignore

    # We use gather to simulate concurrent requests
    results = await asyncio.gather(
        service.search("Исла Моос"), service.search("Исла Моос"), service.search("Исла Моос")
    )

    assert len(results) == 3
    assert len(client.calls) == 1


# --- offer fields, cache TTL, single-flight, location validation ------------------------------


def _svc(*pages: str) -> tuple[SearchService, FakeClient, MockClock]:
    client = FakeClient(responses=deque(get_fixture_content(p) for p in pages))
    clock = MockClock()
    return SearchService(client, ServiceSettings(), monotonic=clock), client, clock  # type: ignore[arg-type]


def _searches(client: FakeClient) -> int:
    return sum(1 for name, _ in client.calls if name == "search_page")


async def test_moos_offer_fields_and_order() -> None:
    service, _, _ = _svc("search_isla_moos.html")
    res = await service.search("Исла Моос", limit=200)
    prices = [o.price_rub for o in res.offers]
    assert prices == sorted(prices)
    online = [o for o in res.offers if o.offer_kind == "online"]
    physical = [o for o in res.offers if o.offer_kind == "physical"]
    assert online and physical
    assert all(o.pharmacy_url and o.pharmacy_url.startswith("https://") for o in online)
    assert all(o.pharmacy_url is None and o.pharmacy_id is not None for o in physical)
    assert all(o.product_key.startswith("v2:исла моос|") for o in res.offers)


async def test_cache_ttl_and_force_refresh() -> None:
    service, client, clock = _svc(*["search_isla_moos.html"] * 3)
    assert (await service.search("Исла Моос")).cached is False
    second = await service.search("исла  моос")  # same folded key
    assert second.cached is True and _searches(client) == 1
    assert any("cache" in w for w in second.warnings)
    clock.t += 121
    assert (await service.search("Исла Моос")).cached is False and _searches(client) == 2
    assert (await service.search("Исла Моос", force_refresh=True)).cached is False and _searches(client) == 3


async def test_single_flight_one_upstream_call() -> None:
    service, client, _ = _svc("search_isla_moos.html")
    results = await asyncio.gather(*(service.search("Исла Моос") for _ in range(3)))
    assert _searches(client) == 1 and all(r.source_total == 40 for r in results)


async def test_location_validation() -> None:
    service, client, _ = _svc()
    with pytest.raises(Lek24Error) as e:
        await service.search("Исла", city_id=999, region_id=0)
    assert e.value.code == ErrorCode.UNSUPPORTED_LOCATION
    locs = await service.list_locations()
    foreign = next(c for c in locs.cities if c.region_id not in (None, 0))
    with pytest.raises(Lek24Error) as e:
        await service.search("Исла", city_id=foreign.id, region_id=0)
    assert e.value.code == ErrorCode.UNSUPPORTED_LOCATION
    assert sum(1 for name, _ in client.calls if name == "index") == 1  # cached 24h


async def test_default_location_skips_index() -> None:
    service, client, _ = _svc("search_isla_moos.html")
    await service.search("Исла Моос")
    assert all(name != "index" for name, _ in client.calls)
