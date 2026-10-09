import asyncio
import datetime as dt
import json
import logging
import math
from pathlib import Path

import httpx2
import pytest

from lek24_mcp.directory import parse_pharmacies
from lek24_mcp.geo import Geocoder, haversine_km, normalize_address
from tests.test_client import FakeClock

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("Металлургов 8 А", "Металлургов 8А"),
        ("Крас. раб. 100", "проспект имени газеты Красноярский рабочий 100"),
        ("Крас раб 88 А", "проспект имени газеты Красноярский рабочий 88А"),
        ("Красноярский Рабочий 40", "проспект имени газеты Красноярский рабочий 40"),
        ("им. газ. Пионерская правда 2", "улица Пионерской Правды 2"),
        ("60 лет СССР 56", "проспект 60 лет образования СССР 56"),
        ("пр-т 60 лет Образования СССР 50Г", "проспект 60 лет образования СССР 50Г"),
        ("пр. 60 лет образования СССР 14", "проспект 60 лет образования СССР 14"),
        ("78-й Добровольческой Бригады 10", "улица 78 Добровольческой Бригады 10"),
        ("пр-т Молодёжный 1", "проспект Молодежный 1"),
        ("9 Мая 26/2", "9 Мая 26"),
        ("Устиновича 1а/1", "Устиновича 1а"),
        ("ул.40 Лет Октября 60/2", "40 Лет Октября 60"),
    ],
)
def test_registry_addresses(address: str, expected: str) -> None:
    registry = parse_pharmacies((FIXTURES / "apteki.html").read_text())
    assert address in {pharmacy.address for pharmacy in registry}
    assert normalize_address(address) == expected
    assert normalize_address(expected) == expected


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("пр-кт. Мира 2", "проспект Мира 2"),
        ("проспект Мира 2", "проспект Мира 2"),
        ("пер. Светлогорский 19А", "переулок Светлогорский 19А"),
        ("улица 9 Мая\u200b 83", "9 Мая 83"),
        ("  ул. 9 Мая 83  ", "9 Мая 83"),
        ("60 лет Октября 39а", "60 лет Октября 39а"),
        ("Проспект имени газеты Красноярский рабочий 41", "проспект имени газеты Красноярский рабочий 41"),
    ],
)
def test_normalize_address(address: str, expected: str) -> None:
    assert normalize_address(address) == expected
    assert normalize_address(expected) == expected


def test_haversine() -> None:
    assert haversine_km(56.0471287, 92.8942869, 56.0471287, 92.8942869) == 0
    assert haversine_km(0, 0, 0, 1) == pytest.approx(111.19508, rel=1e-6)
    assert haversine_km(0, 0, 0, 180) == pytest.approx(20015.11444, rel=1e-6)
    assert haversine_km(56.0471287, 92.8942869, 56.0480704, 92.8951554) == pytest.approx(0.118, abs=0.001)


@pytest.mark.parametrize("lat,lon", [(91, 0), (0, -181), (math.nan, 0), (0, math.inf)])
def test_invalid_coordinates(lat: float, lon: float) -> None:
    with pytest.raises(ValueError):
        haversine_km(lat, lon, 0, 0)


async def test_saved_search_and_reverse(tmp_path: Path) -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert "lek24-mcp" in request.headers["User-Agent"]
        assert request.url.params["format"] == "jsonv2"
        name = "nominatim_search.json" if request.url.path == "/search" else "nominatim_reverse.json"
        return httpx2.Response(200, content=(FIXTURES / name).read_bytes())

    clock = FakeClock()
    cache = tmp_path / "geocode.json"
    async with Geocoder(cache_path=cache, clock=clock, transport=httpx2.MockTransport(handler)) as geocoder:
        result = await geocoder.geocode("ул. 9 Мая 83", "Красноярск")
        assert result.status == "ok"
        assert (result.lat, result.lon) == (56.0471287, 92.8942869)
        assert requests[0].url.params["street"] == "9 Мая 83"
        assert requests[0].url.params["city"] == "Красноярск"
        assert requests[0].url.params["limit"] == "1"
        district = await geocoder.reverse(56.0471287, 92.8942869)
        assert district.district == "Советский район"
        assert requests[1].url.params["zoom"] == "14"
        assert clock.sleeps == [1.0]
    async with Geocoder(cache_path=cache, transport=httpx2.MockTransport(handler)) as geocoder:
        assert await geocoder.geocode("9 Мая 83", "красноярск") == result
        assert await geocoder.reverse(56.0471287, 92.8942869) == district
    assert len(requests) == 2
    assert json.loads(cache.read_text())["version"] == 1


async def test_fallback_and_negative_cache_expiry(tmp_path: Path) -> None:
    streets: list[str] = []
    timestamp = dt.datetime(2026, 10, 9, tzinfo=dt.UTC)

    def handler(request: httpx2.Request) -> httpx2.Response:
        streets.append(request.url.params["street"])
        return httpx2.Response(200, json=[])

    cache = tmp_path / "geo.json"
    async with Geocoder(
        cache_path=cache, clock=FakeClock(), now=lambda: timestamp, transport=httpx2.MockTransport(handler)
    ) as geocoder:
        assert (await geocoder.geocode("9 Мая 83", "Красноярск")).status == "not_found"
        timestamp += dt.timedelta(days=29)
        assert (await geocoder.geocode("9 Мая 83", "Красноярск")).status == "not_found"
        assert len(streets) == 2
        timestamp += dt.timedelta(days=1)
        await geocoder.geocode("9 Мая 83", "Красноярск")
        assert streets == ["9 Мая 83", "улица 9 Мая 83"] * 2


async def test_fallback_success(tmp_path: Path) -> None:
    requests = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx2.Response(200, json=[])
        assert request.url.params["street"] == "улица 9 Мая 83"
        return httpx2.Response(200, content=(FIXTURES / "nominatim_search.json").read_bytes())

    async with Geocoder(
        cache_path=tmp_path / "geo.json", clock=FakeClock(), transport=httpx2.MockTransport(handler)
    ) as geocoder:
        assert (await geocoder.geocode("9 Мая 83", "Красноярск")).status == "ok"
    assert requests == 2


async def test_private_address_bypasses_cache(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    requests = 0
    cache = tmp_path / "geo.json"
    cache.write_text("existing content")

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        return httpx2.Response(200, content=(FIXTURES / "nominatim_search.json").read_bytes())

    async with Geocoder(
        cache_path=cache, clock=FakeClock(), transport=httpx2.MockTransport(handler)
    ) as geocoder:
        await geocoder.geocode("9 Мая 83", "Красноярск", persist=False)
        await geocoder.geocode("9 Мая 83", "Красноярск", persist=False)
        await geocoder.reverse(56.0471287, 92.8942869, persist=False)
        assert geocoder._cache == {}
    assert requests == 3
    assert cache.read_text() == "existing content"
    assert "street=" not in caplog.text
    assert "9 Мая" not in caplog.text
    assert "56.0471287" not in caplog.text


async def test_disabled_and_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEK24_GEOCODER", "off")
    monkeypatch.setenv("LEK24_GEO_CACHE", str(tmp_path / "geo.json"))

    def handler(request: httpx2.Request) -> httpx2.Response:
        pytest.fail("disabled geocoder must not request upstream")

    async with Geocoder(transport=httpx2.MockTransport(handler)) as geocoder:
        assert geocoder.cache_path == tmp_path / "geo.json"
        assert (await geocoder.geocode("9 Мая 83", "Красноярск")).status == "skipped"
        assert (await geocoder.reverse(56, 92)).status == "skipped"
    assert not (tmp_path / "geo.json").exists()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [None],
        [{"lat": "nan", "lon": "92"}],
        [{"lat": 91, "lon": 0}],
        [{"lat": 56, "lon": 92, "address": None}],
    ],
)
async def test_malformed_response_not_cached(tmp_path: Path, payload: object) -> None:
    async with Geocoder(
        cache_path=tmp_path / "geo.json",
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json=payload)),
    ) as geocoder:
        assert (await geocoder.geocode("9 Мая 83", "Красноярск")).status == "unavailable"
    assert not (tmp_path / "geo.json").exists()


@pytest.mark.parametrize("status", [403, 429, 503])
async def test_refusal_and_retry_after(tmp_path: Path, status: int) -> None:
    requests = 0
    clock = FakeClock()

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        return httpx2.Response(status, headers={"Retry-After": "120"})

    async with Geocoder(
        cache_path=tmp_path / "geo.json", clock=clock, transport=httpx2.MockTransport(handler)
    ) as geocoder:
        assert (await geocoder.geocode("9 Мая 83", "Красноярск")).status == "unavailable"
        assert (await geocoder.reverse(56, 92)).status == "unavailable"
        assert requests == 1
        clock.now += 120
        await geocoder.reverse(56, 92)
        assert requests == (1 if status == 403 else 2)


async def test_timeout_and_corrupt_cache(tmp_path: Path) -> None:
    cache = tmp_path / "geo.json"
    cache.write_text("{broken")

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timeout", request=request)

    async with Geocoder(cache_path=cache, transport=httpx2.MockTransport(handler)) as geocoder:
        assert (await geocoder.geocode("9 Мая 83", "Красноярск")).status == "unavailable"
    assert cache.read_text() == "{broken"


async def test_concurrent_requests_are_spaced(tmp_path: Path) -> None:
    clock = FakeClock()
    starts: list[float] = []
    active = 0

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal active
        active += 1
        assert active == 1
        starts.append(clock.now)
        await asyncio.sleep(0)
        active -= 1
        return httpx2.Response(200, content=(FIXTURES / "nominatim_search.json").read_bytes())

    async with Geocoder(
        cache_path=tmp_path / "geo.json", clock=clock, transport=httpx2.MockTransport(handler)
    ) as geocoder:
        await asyncio.gather(*(geocoder.geocode(f"9 Мая {number}", "Красноярск") for number in range(3)))
    assert starts == [0, 1, 2]


async def test_cache_write_failure_retains_result(tmp_path: Path) -> None:
    parent = tmp_path / "file"
    parent.write_text("not a directory")
    async with Geocoder(
        cache_path=parent / "geo.json",
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, content=(FIXTURES / "nominatim_search.json").read_bytes())
        ),
    ) as geocoder:
        result = await geocoder.geocode("9 Мая 83", "Красноярск")
    assert result.status == "ok"
    assert result.warning == "geocode cache could not be saved"


async def test_atomic_write_failure_preserves_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "geo.json"
    cache.write_text('{"version":1,"entries":{}}')
    original = cache.read_bytes()

    def refuse_replace(source: str, destination: Path) -> None:
        raise OSError("replace unavailable")

    monkeypatch.setattr("lek24_mcp.geo.os.replace", refuse_replace)
    async with Geocoder(
        cache_path=cache,
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, content=(FIXTURES / "nominatim_search.json").read_bytes())
        ),
    ) as geocoder:
        assert (await geocoder.geocode("9 Мая 83", "Красноярск")).status == "ok"
    assert cache.read_bytes() == original
    assert list(tmp_path.iterdir()) == [cache]


@pytest.mark.parametrize("status", [200, 404])
async def test_reverse_not_found(tmp_path: Path, status: int) -> None:
    async with Geocoder(
        cache_path=tmp_path / "geo.json",
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(status, json={"error": "Unable to geocode"})
        ),
    ) as geocoder:
        result = await geocoder.reverse(0, 0)
        assert result.status == "not_found"
        assert await geocoder.reverse(0, 0) == result


async def test_geocoder_input_validation(tmp_path: Path) -> None:
    async with Geocoder(cache_path=tmp_path / "geo.json", enabled=False) as geocoder:
        with pytest.raises(ValueError):
            await geocoder.geocode(" ", "Красноярск")
        with pytest.raises(ValueError):
            await geocoder.geocode("9 Мая 83", " ")
        with pytest.raises(ValueError):
            await geocoder.reverse(math.inf, 92)
        with pytest.raises(ValueError):
            await geocoder.reverse(56, 92, zoom=19)


async def test_concurrent_same_address_single_request(tmp_path: Path) -> None:
    requests = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        return httpx2.Response(200, content=(FIXTURES / "nominatim_search.json").read_bytes())

    async with Geocoder(
        cache_path=tmp_path / "geo.json", transport=httpx2.MockTransport(handler)
    ) as geocoder:
        results = await asyncio.gather(*(geocoder.geocode("9 Мая 83", "Красноярск") for _ in range(5)))
    assert requests == 1
    assert all(result == results[0] for result in results)
