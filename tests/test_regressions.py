"""Regression tests for pagination completeness, parser contract, retries, HTTP security and matching."""

from __future__ import annotations

import datetime as dt
import email.utils
import random
from collections import deque
from typing import Any

import httpx2
import pytest

from lek24_mcp.client import ClientSettings, Lek24Client
from lek24_mcp.models import ErrorCode, Lek24Error, ParserContractChanged
from lek24_mcp.normalize import is_match
from lek24_mcp.parser import parse_fragment, parse_search_page
from lek24_mcp.server import BearerTokenMiddleware, build_http_app, build_server
from lek24_mcp.service import SearchService, ServiceSettings
from tests.test_client import FakeClock
from tests.test_service import FakeClient, MockClock

COLS = ["Наименование товара", "Кол-во", "Цена", "Аптека", "Телефон", "Дата"]


def header(cols: list[str] = COLS) -> str:
    return "<div class='th'>" + "".join(f"<span>{c}</span>" for c in cols) + "</div>"


def row(i: int, extra: str = "") -> str:
    return (
        f"<div id='article{i}'>{extra}<span>ТОВАР ТЕСТ {i}</span><span>1</span><span>{100 + i}.00</span>"
        f'<span><a href="javascript:open_in_map({1000 + i})">Аптека {i}</a></span>'
        f'<span><a href="tel:8-391-000-00-00">8-391-000-00-00</a></span><span>08.10.2026</span></div>'
    )


def page(rows: range, total_text: str, num_rows_js: str | None = None, cols: list[str] = COLS) -> str:
    js = "var num = 50; var num_rows = 0; Send['a'] = \"Товар Тест\"; Send['code'] = \"abc\"; "
    if num_rows_js is not None:
        js += f'num_rows = "{num_rows_js}";'
    body = "".join(row(i) for i in rows)
    return (
        f"<html><body><span>Найдено: {total_text}</span>"
        f"<div class='table action' id='maintable'>{header(cols)}{body}</div>"
        f"<script>{js}</script></body></html>"
    )


def fragment(rows: range) -> str:
    return "".join(row(i) for i in rows)


def service_with(first: str, *fragments: str) -> SearchService:
    client = FakeClient(responses=deque([first, *fragments]))
    return SearchService(client, ServiceSettings(), monotonic=MockClock())  # type: ignore[arg-type]


# 1. overlapping pages must not add up to a "complete" listing
async def test_r1_page_overlap_is_incomplete() -> None:
    svc = service_with(page(range(1, 51), "100", "100"), fragment(range(26, 76)))
    res = await svc.search("Товар Тест", max_pages=5, limit=200)
    assert res.complete is False
    assert "overlap" in (res.incomplete_reason or "")


# 2. more rows than the declared total contradicts completeness
async def test_r2_more_rows_than_total_is_incomplete() -> None:
    svc = service_with(page(range(1, 51), "60", "60"), fragment(range(51, 101)))
    res = await svc.search("Товар Тест", max_pages=5, limit=200)
    assert res.complete is False
    assert "more rows" in (res.incomplete_reason or "")


# 3. the two total sources must agree
def test_r3_total_sources_disagree() -> None:
    with pytest.raises(ParserContractChanged):
        parse_search_page(page(range(1, 51), "50", "100"))


# 4. thousand separators in "Найдено"
@pytest.mark.parametrize("text", ["1 357", "1 357", "1,357", "1357"])
def test_r4_total_with_thousand_separators(text: str) -> None:
    assert parse_search_page(page(range(1, 51), text, "1357")).meta.total == 1357


# 5. an added or reordered column must fail loudly
def test_r5_extra_column_fails() -> None:
    bad = row(1, extra="<span>X</span>")
    html = page(range(0), "1", "1").replace(header(), header() + bad)
    with pytest.raises(ParserContractChanged):
        parse_search_page(html)
    with pytest.raises(ParserContractChanged):
        parse_fragment(bad)


def test_r5_reordered_header_fails() -> None:
    swapped = ["Наименование товара", "Цена", "Кол-во", "Аптека", "Телефон", "Дата"]
    with pytest.raises(ParserContractChanged):
        parse_search_page(page(range(1, 3), "2", "2", cols=swapped))


# 6. Retry-After holds back every request of the client
async def test_r6_retry_after_blocks_other_requests() -> None:
    clock = FakeClock()
    starts: list[tuple[str, float]] = []
    first = True

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal first
        starts.append((request.url.path, clock.now))
        if request.url.path == "/action.php" and first:
            first = False
            return httpx2.Response(429, headers={"Retry-After": "60"})
        return httpx2.Response(200, text="ok")

    async with Lek24Client(
        ClientSettings(min_interval=10.0),
        transport=httpx2.MockTransport(handler),
        clock=clock,
        rng=random.Random(0),
    ) as c:
        await c.search_page("а б в", 0, 0, 0)  # 429 at t=0 -> deferral until t=60 -> retried
        await c.suggest("абв")
    assert starts[0] == ("/action.php", 0.0)
    assert all(t >= 60.0 for _, t in starts[1:]), starts


# 7a. HTTP-date Retry-After is honoured
async def test_r7a_retry_after_http_date() -> None:
    clock = FakeClock()
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            when = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)
            return httpx2.Response(
                429, headers={"Retry-After": email.utils.format_datetime(when, usegmt=True)}
            )
        return httpx2.Response(200, text="ok")

    async with Lek24Client(
        ClientSettings(min_interval=1.0),
        transport=httpx2.MockTransport(handler),
        clock=clock,
        rng=random.Random(0),
    ) as c:
        assert (await c.index()).status == 200
    assert len(clock.sleeps) == 1 and 25.0 <= clock.sleeps[0] <= 31.0


# 7b. a wait beyond the cap fails fast instead of retrying early
async def test_r7b_retry_after_beyond_cap() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(429, headers={"Retry-After": "999"})

    async with Lek24Client(
        transport=httpx2.MockTransport(handler), clock=FakeClock(), rng=random.Random(0)
    ) as c:
        with pytest.raises(Lek24Error) as e:
            await c.index()
    assert e.value.code == ErrorCode.UPSTREAM_RATE_LIMITED and calls == 1


# 8. non-loopback bind without a Host allowlist is refused
def test_r8_public_bind_requires_allowed_hosts() -> None:
    mcp = build_server(SearchService(FakeClient(responses=deque())))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_http_app(
            mcp, host="0.0.0.0", path="/mcp", allowed_hosts=[], allowed_origins=[], max_sessions=4, token=None
        )


# 9. bearer scheme is case-insensitive, token is not
async def _status(mw: BearerTokenMiddleware, path: str, auth: bytes | None) -> int:
    sent: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    headers = [(b"authorization", auth)] if auth is not None else []
    await mw({"type": "http", "path": path, "headers": headers}, None, send)
    return int(sent[0]["status"])


async def test_r9_bearer_scheme_case_insensitive() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})

    mw = BearerTokenMiddleware(app, "s3cret", "/mcp")
    assert await _status(mw, "/mcp", b"bearer s3cret") == 200
    assert await _status(mw, "/mcp", b"BEARER s3cret") == 200
    assert await _status(mw, "/mcp", b"Bearer S3CRET") == 401
    assert await _status(mw, "/mcp", b"Bearer wrong") == 401
    assert await _status(mw, "/mcp", None) == 401
    assert await _status(mw, "/health", None) == 200


# 10. numbers in the query bind to dosage / pack size
@pytest.mark.parametrize(
    ("query", "name", "expected"),
    [
        ("Парацетамол 500 мг", "ПАРАЦЕТАМОЛ ТАБЛ 200МГ №500", False),
        ("Товар 10 мг №20", "ТОВАР 20МГ №10", False),
        ("Исла Моос №30", "ИСЛА МООС N30 ПАСТИЛ МАССОЙ 1000МГ", True),
        ("Исла Моос 1000 мг", "ИСЛА МООС N30 ПАСТИЛ МАССОЙ 1000МГ", True),
    ],
)
@pytest.mark.parametrize("mode", ["tokens", "strict"])
def test_r10_numbers_bind_to_attributes(query: str, name: str, expected: bool, mode: Any) -> None:
    assert is_match(query, name, mode) is expected


# --- duplicates across pages, pauses on give-up, multiple dosages ------------------------------


async def test_r1b_legit_duplicate_keeps_new_rows() -> None:
    # row 50 legitimately repeats on page 2; the other 49 rows are new and must not be dropped
    svc = service_with(page(range(1, 51), "100", "100"), fragment(range(50, 100)))
    res = await svc.search("Товар Тест", max_pages=5, limit=200)
    assert res.rows_fetched == 100 and res.complete is False and "overlap" in (res.incomplete_reason or "")
    assert any(o.product_name_raw == "ТОВАР ТЕСТ 99" for o in res.offers)


async def test_r6b_pause_applies_even_when_giving_up() -> None:
    clock = FakeClock()
    starts: list[float] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        starts.append(clock.now)
        if len(starts) == 1:
            return httpx2.Response(429, headers={"Retry-After": "30"})
        return httpx2.Response(200, text="ok")

    settings = ClientSettings(min_interval=10.0, max_retries=0)
    async with Lek24Client(settings, transport=httpx2.MockTransport(handler), clock=clock) as c:
        with pytest.raises(Lek24Error):
            await c.index()
        await c.index()
    assert starts == [0.0, 30.0]


async def test_r6c_long_pause_fails_fast_for_later_calls() -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(429, headers={"Retry-After": "999"})

    clock = FakeClock()
    async with Lek24Client(transport=httpx2.MockTransport(handler), clock=clock, rng=random.Random(0)) as c:
        with pytest.raises(Lek24Error):
            await c.index()
        with pytest.raises(Lek24Error) as e:
            await c.index()  # must not sleep ~999 s nor hit the server
    assert e.value.code == ErrorCode.UPSTREAM_RATE_LIMITED and calls == 1 and clock.sleeps == []


@pytest.mark.parametrize(
    ("query", "name", "expected"),
    [
        ("Товар 10 мг 20 мг", "ТОВАР 10МГ №20", False),
        ("Товар 20 мл", "ТОВАР 10МГ 20МЛ", True),
    ],
)
def test_r10b_every_query_dosage_must_be_present(query: str, name: str, expected: bool) -> None:
    assert is_match(query, name, "tokens") is expected
