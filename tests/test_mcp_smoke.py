"""MCP-level smoke tests: in-process, stdio and Streamable HTTP expose the same tools.

No test here touches 24lek.ru: in-process calls use a fake upstream client; stdio/HTTP calls only
list tools or trigger argument validation, which fails before any network access.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from lek24_mcp.client import FetchResult
from lek24_mcp.server import build_server
from lek24_mcp.service import SearchService

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED_TOOLS = {"list_locations", "suggest_products", "search_offers", "find_cheapest"}
FETCHED = dt.datetime(2026, 10, 8, 1, 45, tzinfo=dt.UTC)


class _FakeUpstream:
    async def search_page(self, query: str, city_id: int, region_id: int, district_id: int) -> FetchResult:
        text = (FIXTURES / "search_isla_moos.html").read_text(encoding="utf-8")
        return FetchResult(
            url="https://24lek.ru/action.php?query=x", status=200, text=text, fetched_at=FETCHED
        )

    async def next_page(self, *args: Any) -> FetchResult:
        raise AssertionError("40-row result must not paginate")

    async def suggest(self, q: str, limit: int = 10) -> FetchResult:
        text = (FIXTURES / "suggest_isla.txt").read_text(encoding="utf-8")
        return FetchResult(url="https://24lek.ru/data.php", status=200, text=text, fetched_at=FETCHED)

    async def index(self) -> FetchResult:
        text = (FIXTURES / "index.html").read_text(encoding="utf-8")
        return FetchResult(url="https://24lek.ru/", status=200, text=text, fetched_at=FETCHED)

    async def aclose(self) -> None:
        pass


def _server() -> Any:
    return build_server(SearchService(_FakeUpstream()))  # type: ignore[arg-type]


def _schemas(tools: Any) -> dict[str, Any]:
    return {t.name: (t.input_schema, t.output_schema, t.annotations) for t in tools.tools}


def _env() -> dict[str, str]:
    src = str(Path(__file__).parents[1] / "src")
    return {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}


async def test_in_process_tools_and_call() -> None:
    async with Client(_server()) as c:
        tools = await c.list_tools()
        assert {t.name for t in tools.tools} == EXPECTED_TOOLS
        assert all(t.annotations and t.annotations.read_only_hint for t in tools.tools)

        res = await c.call_tool("search_offers", {"query": "Исла Моос", "limit": 3})
        assert not res.is_error
        data = res.structured_content
        assert data is not None
        assert data["complete"] is True and data["source_total"] == 40
        assert len(data["offers"]) == 3
        prices = [float(o["price_rub"]) for o in data["offers"]]
        assert prices == sorted(prices)

        cheapest = await c.call_tool("find_cheapest", {"query": "Исла Моос", "top_k": 2})
        assert not cheapest.is_error and cheapest.structured_content is not None
        assert len(cheapest.structured_content["search"]["offers"]) == 2

        bad = await c.call_tool("search_offers", {"query": "Исла", "district_id": 3, "city_id": 1})
        assert bad.is_error
        assert "unsupported_location" in str(bad.content)


async def test_stdio_matches_in_process() -> None:
    async with Client(_server()) as c:
        expected = _schemas(await c.list_tools())
    params = StdioServerParameters(command=sys.executable, args=["-m", "lek24_mcp.server"], env=_env())
    async with Client(params) as c:
        assert _schemas(await c.list_tools()) == expected
        bad = await c.call_tool("search_offers", {"query": ""})  # rejected by schema, no network
        assert bad.is_error


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def http_server() -> Any:
    port = _free_port()
    env = {**_env(), "LEK24_HTTP_TOKEN": "s3cret"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "lek24_mcp.server", "--transport", "http", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx2.get(base + "/health", timeout=1).status_code == 200:
                break
        except httpx2.TransportError:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError(
            "HTTP server did not start: " + (proc.stderr.read() if proc.stderr else b"").decode()
        )
    yield base
    proc.terminate()
    proc.wait(10)


async def test_http_matches_in_process(http_server: str) -> None:
    async with Client(_server()) as c:
        expected = _schemas(await c.list_tools())

    health = httpx2.get(http_server + "/health")
    assert health.json() == {"status": "ok", "service": "lek24-mcp"}
    assert httpx2.post(http_server + "/mcp", json={}).status_code == 401  # bearer required
    assert (
        httpx2.post(http_server + "/mcp", json={}, headers={"Authorization": "Bearer nope"}).status_code
        == 401
    )
    evil = httpx2.post(
        http_server + "/mcp",
        json={},
        headers={"Authorization": "Bearer s3cret", "Origin": "https://evil.example"},
    )
    assert evil.status_code in (400, 403)  # Origin validation (DNS rebinding protection)

    async with (
        httpx2.AsyncClient(headers={"Authorization": "Bearer s3cret"}) as http,
        Client(streamable_http_client(http_server + "/mcp", http_client=http)) as c,
    ):
        assert _schemas(await c.list_tools()) == expected
