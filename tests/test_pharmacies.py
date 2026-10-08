"""Pharmacy registry: list_pharmacies filters, staleness, caching, and enrichment of offers."""

from __future__ import annotations

from collections import deque

import pytest

from lek24_mcp.client import FetchResult
from lek24_mcp.models import ErrorCode, Lek24Error
from lek24_mcp.service import SearchService, ServiceSettings
from tests.test_service import FakeClient, MockClock, get_fixture_content


def _svc(*pages: str, settings: ServiceSettings | None = None) -> tuple[SearchService, FakeClient, MockClock]:
    client = FakeClient(responses=deque(get_fixture_content(p) for p in pages))
    clock = MockClock()
    return SearchService(client, settings or ServiceSettings(), monotonic=clock), client, clock  # type: ignore[arg-type]


def _calls(client: FakeClient, name: str) -> int:
    return sum(1 for n, _ in client.calls if n == name)


async def test_krasnoyarsk_registry_and_stale_first() -> None:
    svc, client, _ = _svc()
    res = await svc.list_pharmacies()
    assert res.city_name == "Красноярск" and res.total == 364
    assert _calls(client, "index") == 0  # default city needs no locations lookup
    flags = [p.is_stale for p in res.pharmacies]
    assert 0 < res.stale_count < res.total
    assert flags == [True] * res.stale_count + [False] * (res.total - res.stale_count)
    oldest = max(res.pharmacies, key=lambda p: p.price_list_age_hours or 0)
    assert oldest.name == "Ригла" and oldest.is_stale


async def test_district_and_chain_filters() -> None:
    svc, _, _ = _svc()
    sov = await svc.list_pharmacies(district_id=6)
    assert sov.district_name == "Советский" and sov.total == 108
    assert all(p.district == "Советский" and p.city == "Красноярск" for p in sov.pharmacies)
    zdrav = await svc.list_pharmacies(district_id=6, chain="здравсити")
    assert zdrav.total == 17 and all("Здравсити" in p.name for p in zdrav.pharmacies)


async def test_validation() -> None:
    svc, _, _ = _svc()
    with pytest.raises(Lek24Error) as e:
        await svc.list_pharmacies(stale_days=0)
    assert e.value.code == ErrorCode.INVALID_ARGUMENT
    with pytest.raises(Lek24Error) as e:
        await svc.list_pharmacies(city_id=999)
    assert e.value.code == ErrorCode.UNSUPPORTED_LOCATION


async def test_registry_cached_for_an_hour() -> None:
    svc, client, clock = _svc()
    await svc.list_pharmacies()
    await svc.list_pharmacies(chain="Ригла")
    assert _calls(client, "pharmacies_page") == 1
    clock.t += 3601
    await svc.list_pharmacies()
    assert _calls(client, "pharmacies_page") == 2


async def test_offers_get_district_and_price_list_age() -> None:
    svc, _, _ = _svc("search_isla_moos.html")
    res = await svc.search("Исла Моос", limit=200)
    physical = [o for o in res.offers if o.offer_kind == "physical"]
    online = [o for o in res.offers if o.offer_kind == "online"]
    assert physical and online
    assert all(o.district and o.prices_updated_at and o.price_list_stale is not None for o in physical)
    assert all(o.district is None and o.price_list_stale is None for o in online)
    assert all(o.pharmacy_address and o.pharmacy_address in o.pharmacy_name_address_raw for o in physical)


async def test_stale_offers_are_flagged() -> None:
    svc, _, _ = _svc("search_isla_moos.html", settings=ServiceSettings(stale_days=0.01))
    res = await svc.search("Исла Моос", limit=200)
    assert all(o.price_list_stale for o in res.offers if o.offer_kind == "physical")
    assert any("price list is older" in w for w in res.warnings)


async def test_registry_failure_does_not_break_search() -> None:
    class Broken(FakeClient):
        async def pharmacies_page(self) -> FetchResult:
            raise Lek24Error(ErrorCode.UPSTREAM_UNAVAILABLE, "down")

    client = Broken(responses=deque([get_fixture_content("search_isla_moos.html")]))
    svc = SearchService(client, ServiceSettings(), monotonic=MockClock())  # type: ignore[arg-type]
    res = await svc.search("Исла Моос")
    assert res.offers and all(o.district is None for o in res.offers)
    assert any("registry unavailable" in w for w in res.warnings)
