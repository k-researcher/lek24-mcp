import asyncio
import json
import logging
import math
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import Client

from lek24_mcp.client import FetchResult
from lek24_mcp.directory import PHARMACY_COLUMNS
from lek24_mcp.geo import Geocoder, GeocodeResult
from lek24_mcp.models import ErrorCode, Lek24Error
from lek24_mcp.parser import parse_locations
from lek24_mcp.server import build_server
from lek24_mcp.service import SearchService, ServiceSettings
from tests.test_client import FakeClock
from tests.test_regressions import header, row
from tests.test_service import FakeClient, MockClock


def offer_row(index: int, price: str, *, online: bool = False) -> str:
    html = row(index).replace(f"{100 + index}.00", price)
    if online:
        html = html.replace(f"javascript:open_in_map({1000 + index})", "https://apteka.ru/product/test")
    return html


def search_page(rows: list[str], total: int) -> str:
    return (
        f"<span>Найдено: {total}</span><div id='maintable'>{header()}{''.join(rows)}</div>"
        "<script>var num=50; Send['code']='abc'; Send['a']='Товар Тест';</script>"
    )


class NearClient(FakeClient):
    registry_fails = False

    async def pharmacies_page(self) -> FetchResult:
        if self.registry_fails:
            raise Lek24Error(ErrorCode.UPSTREAM_UNAVAILABLE, "registry unavailable")
        html = "<div class='table'><div class='th'>"
        html += "".join(f"<span>{name}</span>" for name in PHARMACY_COLUMNS) + "</div>"
        for index in range(1, 301):
            html += (
                f"<div><span>Аптека {index}</span><span><a href='?apteka={1000 + index}'>"
                f"Тестовая {index}</a></span><span>Советский</span><span>123</span>"
                "<span>08.10.2026 08:00:00</span><span>Красноярск</span></div>"
            )
        return FetchResult(
            url="https://24lek.ru/apteki.php", status=200, text=html + "</div>", fetched_at=self.fetched_at
        )


class NearGeo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self.points: dict[str, GeocodeResult] = {}

    async def geocode(self, address: str, city: str, *, persist: bool = True) -> GeocodeResult:
        self.calls.append((address, city, persist))
        return self.points.get(address, GeocodeResult(status="ok", lat=0, lon=0.005))


def setup_service(*pages: str, geo: NearGeo | None = None) -> tuple[SearchService, NearClient, NearGeo]:
    client = NearClient(responses=deque(pages))
    geocoder = geo or NearGeo()
    service = SearchService(client, geocoder=geocoder, monotonic=MockClock())  # type: ignore[arg-type]
    return service, client, geocoder


async def test_tolerance_tier_and_nearby_have_different_contents() -> None:
    service, _, geo = setup_service(
        search_page([offer_row(1, "100"), offer_row(2, "105"), offer_row(3, "110"), offer_row(4, "150")], 4)
    )
    geo.points["Тестовая 1"] = GeocodeResult(status="ok", lat=0, lon=0.01)
    geo.points["Тестовая 2"] = GeocodeResult(status="ok", lat=0, lon=0.003)
    geo.points["Тестовая 3"] = GeocodeResult(status="ok", lat=0, lon=0.001)
    geo.points["Тестовая 4"] = GeocodeResult(status="ok", lat=0, lon=0.002)
    result = await service.find_cheapest_near(
        "Товар Тест", near_lat=0, near_lon=0, price_tolerance_rub=Decimal("5")
    )
    assert [offer.pharmacy_id for offer in result.cheapest] == [1002, 1001]
    assert [offer.pharmacy_id for offer in result.nearby] == [1003, 1004, 1002, 1001]
    assert result.min_price_rub == Decimal("100")
    assert result.price_tier_complete and result.distance_ranking_complete
    assert result.origin.source == "coords" and result.origin.geocode_status == "ok"
    assert result.search.offers == []
    assert all(persist for _, _, persist in geo.calls)


async def test_equal_prices_on_later_pages_are_loaded_before_distance_ranking() -> None:
    first = search_page([offer_row(index, "100") for index in range(1, 51)], 150)
    second = "".join([offer_row(51, "100"), *[offer_row(index, "101") for index in range(52, 101)]])
    service, client, geo = setup_service(first, second)
    geo.points["Тестовая 51"] = GeocodeResult(status="ok", lat=0, lon=0)
    result = await service.find_cheapest_near("Товар Тест", near_lat=0, near_lon=0, top_k=1)
    assert result.cheapest[0].pharmacy_id == 1051
    assert result.cheapest[0].distance_km == 0
    assert result.search.pages_fetched == 2
    assert [call[0] for call in client.calls].count("next_page") == 1
    assert result.price_tier_complete and not result.search.complete
    assert "only loaded" in result.coverage_note


async def test_more_than_200_matches_do_not_truncate_distance_ranking() -> None:
    pages = [search_page([offer_row(index, "100") for index in range(1, 51)], 250)]
    pages.extend(
        "".join(offer_row(index, "100") for index in range(start, start + 50)) for start in range(51, 251, 50)
    )
    service, _, geo = setup_service(*pages)
    geo.points["Тестовая 250"] = GeocodeResult(status="ok", lat=0, lon=0)
    result = await service.find_cheapest_near("Товар Тест", near_lat=0, near_lon=0, top_k=1)
    assert result.search.rows_fetched == 250 and result.search.matched_count == 250
    assert result.cheapest[0].pharmacy_id == 1250
    assert result.price_tier_complete


async def test_page_limit_at_price_tie_reports_partial_tier() -> None:
    service, _, _ = setup_service(search_page([offer_row(index, "100") for index in range(1, 51)], 100))
    result = await service.find_cheapest_near("Товар Тест", near_lat=0, near_lon=0, max_pages=1)
    assert not result.price_tier_complete and not result.distance_ranking_complete
    assert result.search.unfetched_min_price_rub == Decimal("100")


async def test_tolerance_boundary_also_loads_ties() -> None:
    first = search_page([offer_row(1, "100"), *[offer_row(index, "105") for index in range(2, 51)]], 100)
    second = "".join(offer_row(index, "105") for index in range(51, 101))
    service, _, geo = setup_service(first, second)
    geo.points["Тестовая 100"] = GeocodeResult(status="ok", lat=0, lon=0)
    result = await service.find_cheapest_near(
        "Товар Тест", near_lat=0, near_lon=0, price_tolerance_rub=Decimal("5"), top_k=1
    )
    assert result.cheapest[0].pharmacy_id == 1100
    assert result.search.pages_fetched == 2 and result.price_tier_complete


async def test_online_offers_do_not_set_physical_minimum() -> None:
    service, _, _ = setup_service(search_page([offer_row(1, "1", online=True), offer_row(2, "100")], 2))
    result = await service.find_cheapest_near("Товар Тест", near_lat=0, near_lon=0)
    assert result.min_price_rub == Decimal("100")
    assert all(offer.offer_kind == "physical" for offer in result.cheapest + result.nearby)


async def test_radius_filter_and_geocode_failures_preserve_prices() -> None:
    service, _, geo = setup_service(search_page([offer_row(index, "100") for index in range(1, 5)], 4))
    geo.points["Тестовая 1"] = GeocodeResult(status="not_found")
    geo.points["Тестовая 2"] = GeocodeResult(status="unavailable", warning="unavailable")
    geo.points["Тестовая 3"] = GeocodeResult(status="ok", lat=0, lon=1)
    result = await service.find_cheapest_near("Товар Тест", near_lat=0, near_lon=0)
    assert [offer.pharmacy_id for offer in result.nearby] == [1004]
    assert len(result.cheapest) == 4 and not result.distance_ranking_complete
    by_id = {offer.pharmacy_id: offer for offer in result.cheapest}
    assert by_id[1001].geocode_status == "not_found"
    assert by_id[1002].geocode_status == "skipped"
    assert result.search.warnings


async def test_missing_registry_does_not_break_price_search() -> None:
    service, client, geo = setup_service(search_page([offer_row(1, "100")], 1))
    client.registry_fails = True
    result = await service.find_cheapest_near("Товар Тест", near_lat=0, near_lon=0)
    assert result.cheapest[0].distance_km is None
    assert result.cheapest[0].geocode_status == "skipped"
    assert geo.calls == []


async def test_origin_failure_skips_pharmacy_geocoding() -> None:
    service, _, geo = setup_service(search_page([offer_row(1, "100")], 1))
    geo.points["private origin"] = GeocodeResult(status="not_found")
    result = await service.find_cheapest_near("Товар Тест", near="private origin")
    assert result.origin.source == "geocoded" and result.origin.lat is None
    assert result.cheapest[0].price_rub == Decimal("100") and result.nearby == []
    assert geo.calls == [("private origin", "Красноярск", False)]


async def test_private_origin_not_in_cache_logs_or_response(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.params["street"])
        return httpx2.Response(200, json=[{"lat": "0", "lon": "0"}])

    client = NearClient(
        responses=deque([search_page([offer_row(1, "100")], 1), search_page([offer_row(1, "100")], 1)])
    )
    cache = tmp_path / "geo.json"
    async with Geocoder(
        cache_path=cache, clock=FakeClock(), transport=httpx2.MockTransport(handler)
    ) as geocoder:
        service = SearchService(client, geocoder=geocoder)  # type: ignore[arg-type]
        first = await service.find_cheapest_near("Товар Тест", near="секретный адрес 987")
        await service.find_cheapest_near("Товар Тест", near="секретный адрес 987")
    assert requests.count("секретный адрес 987") == 2
    assert "секретный" not in cache.read_text()
    assert "секретный" not in caplog.text
    assert "секретный" not in json.dumps(first.model_dump(mode="json"), ensure_ascii=False)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"near_lat": 0},
        {"near_lon": 0},
        {"near": "  "},
        {"near": "address", "near_lat": 0, "near_lon": 0},
        {"near_lat": math.nan, "near_lon": 0},
        {"near_lat": 91, "near_lon": 0},
        {"near_lat": 0, "near_lon": 0, "price_tolerance_rub": Decimal("-1")},
        {"near_lat": 0, "near_lon": 0, "price_tolerance_rub": Decimal("NaN")},
        {"near_lat": 0, "near_lon": 0, "nearby_radius_km": 0},
        {"near_lat": 0, "near_lon": 0, "nearby_radius_km": math.inf},
        {"near_lat": 0, "near_lon": 0, "top_k": 0},
    ],
)
async def test_invalid_arguments_before_network(kwargs: dict[str, Any]) -> None:
    service, client, geo = setup_service()
    with pytest.raises(Lek24Error) as error:
        await service.find_cheapest_near("Товар Тест", **kwargs)
    assert error.value.code == ErrorCode.INVALID_ARGUMENT
    assert client.calls == geo.calls == []


async def test_mcp_tool_schema_and_price_fallback() -> None:
    service, _, _ = setup_service(search_page([offer_row(1, "100")], 1))
    async with Client(build_server(service)) as client:
        tools = await client.list_tools()
        tool = next(tool for tool in tools.tools if tool.name == "find_cheapest_near")
        assert "OpenStreetMap Nominatim" in tool.input_schema["properties"]["near"]["description"]
        result = await client.call_tool(
            "find_cheapest_near", {"query": "Товар Тест", "near_lat": 0, "near_lon": 0}
        )
        assert not result.is_error
        assert result.structured_content is not None
        assert Decimal(result.structured_content["min_price_rub"]) == Decimal("100")
        invalid = await client.call_tool("find_cheapest_near", {"query": "Товар Тест"})
        assert invalid.is_error


async def test_geocode_deadline_returns_partial_distances() -> None:
    service, _, geo = setup_service(search_page([offer_row(1, "100")], 1))
    service._s = ServiceSettings(geocode_deadline_seconds=0)
    result = await service.find_cheapest_near("Товар Тест", near_lat=0, near_lon=0)
    assert result.cheapest[0].geocode_status == "skipped"
    assert result.search.warnings and geo.calls == []


async def test_refused_pagination_keeps_tier_partial() -> None:
    service, client, _ = setup_service(search_page([offer_row(index, "100") for index in range(1, 51)], 100))
    client.error_on_next = (ErrorCode.UPSTREAM_REFUSED, "refused")
    result = await service.find_cheapest_near("Товар Тест", near_lat=0, near_lon=0)
    assert result.min_price_rub == Decimal("100") and result.cheapest
    assert not result.price_tier_complete
    assert "upstream_refused" in (result.search.incomplete_reason or "")


async def test_unordered_listing_does_not_claim_complete_tier() -> None:
    service, _, _ = setup_service(search_page([offer_row(1, "100"), offer_row(2, "50")], 100))
    result = await service.find_cheapest_near("Товар Тест", near_lat=0, near_lon=0, max_pages=1)
    assert result.min_price_rub == Decimal("50")
    assert result.search.unfetched_min_price_rub is None
    assert not result.price_tier_complete


async def test_near_search_cache_does_not_truncate_plain_search() -> None:
    first = search_page([offer_row(index, str(100 + index)) for index in range(1, 51)], 100)
    second = "".join(offer_row(index, str(100 + index)) for index in range(51, 101))
    service, _, _ = setup_service(first, first, second)
    near_result = await service.find_cheapest_near("Товар Тест", near_lat=0, near_lon=0)
    assert near_result.price_tier_complete and near_result.search.pages_fetched == 1
    full = await service.search("Товар Тест", max_pages=2)
    assert full.rows_fetched == 100 and full.complete and not full.cached


async def test_city_name_for_geocoding_comes_from_selected_location() -> None:
    service, _, geo = setup_service(search_page([offer_row(1, "100")], 1))
    locations = parse_locations((Path(__file__).parent / "fixtures" / "index.html").read_text())
    city = next(city for city in locations.cities if city.name == "Абакан")
    await service.find_cheapest_near(
        "Товар Тест", near="private origin", city_id=city.id, region_id=city.region_id or 0
    )
    assert all(city_name == "Абакан" for _, city_name, _ in geo.calls)


async def test_actual_timeout_preserves_prices() -> None:
    class SlowGeo:
        async def geocode(self, address: str, city: str, *, persist: bool = True) -> GeocodeResult:
            await asyncio.sleep(1)
            return GeocodeResult(status="not_found")

    client = NearClient(responses=deque([search_page([offer_row(1, "100")], 1)]))
    service = SearchService(client, ServiceSettings(geocode_deadline_seconds=0.001), geocoder=SlowGeo())  # type: ignore[arg-type]
    result = await service.find_cheapest_near("Товар Тест", near_lat=0, near_lon=0)
    assert result.cheapest[0].price_rub == Decimal("100")
    assert result.cheapest[0].distance_km is None
    assert result.search.warnings


async def test_geocoder_exception_preserves_prices() -> None:
    class BrokenGeo:
        async def geocode(self, address: str, city: str, *, persist: bool = True) -> GeocodeResult:
            raise httpx2.ReadTimeout("unavailable")

    client = NearClient(responses=deque([search_page([offer_row(1, "100")], 1)]))
    service = SearchService(client, geocoder=BrokenGeo())  # type: ignore[arg-type]
    result = await service.find_cheapest_near("Товар Тест", near_lat=0, near_lon=0)
    assert result.cheapest[0].geocode_status == "skipped"
    assert result.search.warnings


async def test_unparseable_price_does_not_claim_complete_tier() -> None:
    service, _, _ = setup_service(search_page([offer_row(1, "100"), offer_row(2, "неизвестно")], 2))
    result = await service.find_cheapest_near("Товар Тест", near_lat=0, near_lon=0)
    assert result.min_price_rub == Decimal("100") and result.search.complete
    assert not result.price_tier_complete and not result.distance_ranking_complete
    assert any("unparseable price" in warning for warning in result.search.warnings)
