from __future__ import annotations

import datetime as dt
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

from lek24_mcp.coverage import (
    CoverageSnapshot,
    LicenseSite,
    clean_street,
    load_snapshot,
    match_site,
    read_archive,
    read_retail_sites,
    save_snapshot,
    snapshot_path,
)
from lek24_mcp.matching_hints import add_hints
from lek24_mcp.models import ErrorCode, Lek24Error, Pharmacy

FIXTURES = Path(__file__).parent / "fixtures"
NOW = dt.datetime(2026, 10, 10, tzinfo=dt.UTC)


def pharmacy(name: str = "Аптека Декабрь", address: str = "Устиновича 1г", id: int = 1) -> Pharmacy:
    return Pharmacy(
        id=id,
        name=name,
        address=address,
        district="Советский",
        city="Красноярск",
        phone_raw=None,
        prices_updated_at_raw="",
        prices_updated_at=None,
    )


def site(**overrides: object) -> LicenseSite:
    return LicenseSite.model_validate(
        {
            "license_numbers": ["ЛО-1"],
            "inn": "1234567890",
            "names": ["ООО Декабрь"],
            "address": "г. Красноярск, ул. Устиновича, д. 1г",
            "street": "Устиновича 1г",
            "code_fias": "",
            **overrides,
        }
    )


def snapshot(sites: list[LicenseSite] | None = None) -> CoverageSnapshot:
    return CoverageSnapshot(
        city_id=0,
        region_id=0,
        city_name="Красноярск",
        built_at=NOW,
        licenses_source_url="https://roszdravnadzor.gov.ru/",
        licenses_source_date=None,
        licenses_fetched_at=None,
        directory_source_url="https://24lek.ru/apteki.php",
        directory_fetched_at=NOW,
        sites=sites or [site()],
        pharmacies=[pharmacy()],
        districts=[],
        warnings=[],
    )


def xml(
    city: str = "Красноярск",
    work: str = "розничная торговля лекарственными препаратами",
    extra: str = "",
    namespace: bool = False,
) -> bytes:
    ns = ' xmlns="urn:licenses"' if namespace else ""
    return f"""<licenses_list{ns}><licenses><inn>123</inn><number>ЛО-1</number>
        <address>Юридический адрес Москва</address><brand_name_licensee>Декабрь</brand_name_licensee>
        {extra}<work_address_list><address_place><city>{city}</city>
        <address>Красноярский край, г. Красноярск, ул. Устиновича, д. 1г</address>
        <street>ул. Устиновича, д. 1г</street><works><work>{work}</work></works>
        </address_place></work_address_list></licenses></licenses_list>""".encode()


@pytest.mark.parametrize(
    "city,namespace", [("Красноярск", False), ("г. Красноярск", False), ("", False), ("", True)]
)
def test_city_and_namespace(city: str, namespace: bool) -> None:
    sites = read_retail_sites(io.BytesIO(xml(city=city, namespace=namespace)), "Красноярск")
    assert len(sites) == 1
    assert sites[0].address.startswith("Красноярский край")
    assert sites[0].street == "Устиновича 1г"


@pytest.mark.parametrize(
    "content",
    [
        xml(city="Норильск"),
        xml(work="оптовая торговля"),
        xml(extra="<termination>прекращена</termination>"),
        b"<licenses_list/>",
        b"<wrong/>",
        b"<licenses_list><licenses>",
        b'<!DOCTYPE licenses_list [<!ENTITY x "bad">]><licenses_list>&x;</licenses_list>',
    ],
)
def test_invalid_or_no_city_keeps_snapshot(content: bytes) -> None:
    with pytest.raises(Lek24Error) as caught:
        read_retail_sites(io.BytesIO(content), "Красноярск")
    assert caught.value.code == ErrorCode.PARSER_CONTRACT_CHANGED


def test_real_trimmed_fixture() -> None:
    sites = read_archive(FIXTURES / "retail_licenses.xml", "Красноярск")
    assert len(sites) == 9
    assert any(p.street == "Устиновича 1г" for p in sites)
    assert all(p.license_numbers and p.inn for p in sites)


@pytest.mark.parametrize(
    "value,expected,uncertain",
    [
        ("ул. Кутузова, д. 42 Е", "Кутузова 42Е", False),
        ("Советский район, ул. Устиновича, 1г", "Устиновича 1г", False),
        ("ул. 9 Мая, № 77, помещение № 2 комнаты 1103", "9 Мая 77", False),
        ("ул. Ленина д. 26/2", "Ленина 26", True),
        ("ул. Ленина д. 26, корпус 2", "Ленина 26 корпус 2", True),
    ],
)
def test_clean_street(value: str, expected: str, uncertain: bool) -> None:
    assert clean_street(value) == (expected, uncertain)


def test_matching_conservative() -> None:
    assert match_site(site(), [pharmacy()]).status == "matched"
    assert match_site(site(), [pharmacy(name="Аптека Другая")]).status == "uncertain"
    assert match_site(site(), [pharmacy(), pharmacy(id=2)]).status == "uncertain"
    assert match_site(site(address_uncertain=True), [pharmacy()]).status == "uncertain"
    assert match_site(site(), [pharmacy(address="Ленина 50")]).status == "missing"
    assert match_site(site(address_uncertain=True), []).status == "uncertain"


def test_zip_and_dedup(tmp_path: Path) -> None:
    register = xml().decode()
    record = register[register.index("<licenses>") : register.index("</licenses>") + len("</licenses>")]
    repeated = register.replace("</licenses_list>", record + "</licenses_list>").encode()
    archive = tmp_path / "licenses.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("data.xml", repeated)
    assert len(read_archive(archive, "Красноярск")) == 1
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("one.xml", xml())
        zipped.writestr("two.xml", xml())
    with pytest.raises(Lek24Error):
        read_archive(archive, "Красноярск")


def test_snapshot_roundtrip_and_atomic_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = snapshot_path(0, tmp_path)
    save_snapshot(snapshot(), path)
    assert load_snapshot(0, tmp_path) == snapshot()
    original = path.read_bytes()

    def fail(*args: object) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr("lek24_mcp.coverage.os.replace", fail)
    with pytest.raises(OSError):
        save_snapshot(snapshot([site(inn="changed")]), path)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("content", [None, "{}", '{"version":99}', "broken"])
def test_missing_or_invalid_snapshot(tmp_path: Path, content: str | None) -> None:
    if content:
        snapshot_path(0, tmp_path).write_text(content)
    with pytest.raises(Lek24Error, match="build-coverage") as caught:
        load_snapshot(0, tmp_path)
    assert caught.value.code == ErrorCode.COVERAGE_UNAVAILABLE


@pytest.mark.parametrize(
    "script,timeout",
    [
        ("import sys; sys.exit(1)", 2),
        ("print('invalid')", 2),
        ("import time; time.sleep(10)", 0.05),
        ("print('{\"hints\":{}}')", 2),
        ('print(\'{"hints":{"0":{"choice":"same","confidence":5}}}\')', 2),
    ],
)
async def test_helper_failures_fall_back(script: str, timeout: float) -> None:
    original = [site(candidates=[pharmacy()])]
    result, warnings = await add_hints(original, [sys.executable, "-c", script], timeout=timeout)
    assert result == original and warnings


async def test_missing_helper_and_no_pairs() -> None:
    original = [site(candidates=[pharmacy()])]
    result, warnings = await add_hints(original, ["/nonexistent/helper"])
    assert result == original and warnings
    assert await add_hints([site()], ["/nonexistent/helper"]) == ([site()], [])


async def test_helper_advisory_never_confirms() -> None:
    payload = json.dumps({"hints": {"0": {"choice": "same", "confidence": 0.99}}})
    result, warnings = await add_hints(
        [site(candidates=[pharmacy()])], [sys.executable, "-c", f"print({payload!r})"]
    )
    assert result[0].status == "uncertain"
    assert result[0].hints[0].choice == "same" and warnings


async def test_offline_tool_lists_and_district(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections import deque

    from lek24_mcp.models import District
    from lek24_mcp.service import SearchService
    from tests.test_service import FakeClient

    monkeypatch.setenv("LEK24_COVERAGE_DIR", str(tmp_path))
    data = snapshot(
        [
            site(status="missing", district="Советский район"),
            site(status="uncertain", district=None),
            site(status="matched", district="Центральный район"),
        ]
    )
    data = data.model_copy(update={"districts": [District(id=7, name="Советский")]})
    save_snapshot(data, snapshot_path(0))
    client = FakeClient(deque())
    service = SearchService(client)  # type: ignore[arg-type]
    result = await service.coverage_gaps()
    assert len(result.missing) == len(result.uncertain) == result.matched_count == 1
    assert result.stale and not result.directory_refreshed and result.prices_checked_at == NOW
    filtered = await service.coverage_gaps(district_id=7)
    assert len(filtered.missing) == 1 and filtered.uncertain == []
    assert filtered.unlocated_count == 1
    assert client.calls == []
    with pytest.raises(Lek24Error):
        await service.coverage_gaps(district_id=1000)
    with pytest.raises(Lek24Error, match="build-coverage"):
        await service.coverage_gaps(city_id=9999)
    assert client.calls == []


async def test_refresh_failure_uses_stored_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections import deque

    from lek24_mcp.service import SearchService
    from tests.test_service import FakeClient

    class RefusingClient(FakeClient):
        async def pharmacies_page(self) -> None:
            raise Lek24Error(ErrorCode.UPSTREAM_UNAVAILABLE, "failed")

    monkeypatch.setenv("LEK24_COVERAGE_DIR", str(tmp_path))
    save_snapshot(snapshot(), snapshot_path(0))
    service = SearchService(RefusingClient(deque()))  # type: ignore[arg-type]
    result = await service.coverage_gaps(refresh=True)
    assert not result.directory_refreshed and result.stale
    assert any("stored directory used" in w for w in result.warnings)


async def test_build_and_reference_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse
    from collections import deque

    from lek24_mcp.coverage_cli import build
    from lek24_mcp.service import SearchService
    from tests.test_service import FakeClient

    monkeypatch.setenv("LEK24_COVERAGE_DIR", str(tmp_path))
    monkeypatch.setenv("LEK24_GEO_CACHE", str(tmp_path / "geo.json"))
    monkeypatch.setenv("LEK24_GEOCODER", "off")
    service = SearchService(FakeClient(deque()))  # type: ignore[arg-type]
    args = argparse.Namespace(
        city_id=0,
        region_id=0,
        licenses=str(FIXTURES / "retail_licenses.xml"),
        matching_helper="/missing/helper",
        geocode=False,
    )
    path = await build(args, service)
    data = load_snapshot(0, tmp_path)
    assert len(data.sites) == 9 and data.licenses_fetched_at is None
    assert path.exists() and (tmp_path / "references-v1.json").exists()
    offline_client = FakeClient(deque())
    offline = SearchService(offline_client)  # type: ignore[arg-type]
    assert (await offline.list_locations()).cities
    assert (await offline.list_pharmacies()).pharmacies
    assert offline_client.calls == []
    assert (await offline.coverage_gaps()).missing or (await offline.coverage_gaps()).uncertain


async def test_cache_only_geocoder_never_network(tmp_path: Path) -> None:
    import httpx2

    from lek24_mcp.geo import Geocoder

    def no_network(request: httpx2.Request) -> httpx2.Response:
        pytest.fail("cache-only geocoding made a network request")

    geo = Geocoder(
        cache_path=tmp_path / "geo.json", cache_only=True, transport=httpx2.MockTransport(no_network)
    )
    assert (await geo.geocode("Устиновича 1г", "Красноярск")).status == "skipped"
    assert (await geo.reverse(56, 92)).status == "skipped"
    await geo.aclose()


async def test_matching_adapter_response_validation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib.util

    import httpx2

    spec = importlib.util.spec_from_file_location(
        "matching_adapter", FIXTURES.parent.parent / "examples" / "typesafe_matching.py"
    )
    assert spec and spec.loader
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    real_client = httpx2.Client
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        body = json.loads(request.content)
        assert "pair with id 0" in body["questions"]["0"]["instructions"]
        return httpx2.Response(
            200, json={"answers": {"0": {"type": "choice", "choice": "unknown", "confidence": 0.7}}}
        )

    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")
    monkeypatch.setattr(
        adapter.httpx2, "Client", lambda **kwargs: real_client(transport=httpx2.MockTransport(respond))
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"pairs": [{"id": "0"}]})))
    assert adapter.main() == 0
    output = capsys.readouterr()
    assert "test-key" not in output.out + output.err
    assert json.loads(output.out)["hints"]["0"]["choice"] == "unknown"
    assert len(requests) == 1


async def test_matching_adapter_no_key_and_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib.util

    import httpx2

    spec = importlib.util.spec_from_file_location(
        "matching_adapter", FIXTURES.parent.parent / "examples" / "typesafe_matching.py"
    )
    assert spec and spec.loader
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)

    def missing_key() -> str:
        raise ValueError("no credentials")

    monkeypatch.setattr(adapter, "api_key", missing_key)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"pairs": [{"id": "0"}]})))
    assert adapter.main() == 1
    assert capsys.readouterr().out == ""
    real_client = httpx2.Client

    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("secret must not leak")

    monkeypatch.setattr(adapter, "api_key", lambda: "test-key")
    monkeypatch.setattr(
        adapter.httpx2, "Client", lambda **kwargs: real_client(transport=httpx2.MockTransport(timeout))
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"pairs": [{"id": "0"}]})))
    assert adapter.main() == 1
    output = capsys.readouterr()
    assert output.out == "" and "secret" not in output.err


async def test_offline_pagination_and_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from collections import deque

    from mcp import Client

    from lek24_mcp.server import build_server
    from lek24_mcp.service import SearchService
    from tests.test_service import FakeClient

    monkeypatch.setenv("LEK24_COVERAGE_DIR", str(tmp_path))
    save_snapshot(
        snapshot([site(status="missing", address=f"Address {i:02}") for i in range(30)]), snapshot_path(0)
    )
    upstream = FakeClient(deque())
    service = SearchService(upstream)  # type: ignore[arg-type]
    result = await service.coverage_gaps(limit=10)
    assert result.missing_count == 30 and len(result.missing) == 10 and result.next_offset == 10
    page = await service.coverage_gaps(limit=10, offset=20)
    assert page.next_offset is None and page.missing[0].address == "Address 20"
    async with Client(build_server(service)) as client:
        response = await client.call_tool("coverage_gaps", {"limit": 5})
        assert not response.is_error and response.structured_content
        assert len(response.structured_content["missing"]) == 5
        assert response.structured_content["next_offset"] == 5
    assert upstream.calls == []
