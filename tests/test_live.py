"""Live checks against the real 24lek.ru. Opt-in: `uv run pytest -m live`.

Assertions are structural only: prices and stock change, so no historical values are asserted.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

pytestmark = pytest.mark.live


def _params() -> StdioServerParameters:
    src = str(Path(__file__).parents[1] / "src")
    return StdioServerParameters(
        command=sys.executable, args=["-m", "lek24_mcp.server"], env={**os.environ, "PYTHONPATH": src}
    )


async def test_live_search_and_pagination() -> None:
    skip_reason: str | None = None
    async with Client(_params(), read_timeout_seconds=120) as c:
        moos = await c.call_tool("search_offers", {"query": "Исла Моос", "limit": 5})
        assert not moos.is_error, moos.content
        data = moos.structured_content
        assert data is not None and data["source_total"] and data["source_total"] > 0
        assert data["complete"] is True and data["offers"]
        assert all("моос" in o["product_name_raw"].lower() for o in data["offers"])

        broad = await c.call_tool("search_offers", {"query": "Исла", "max_pages": 2, "limit": 5})
        assert not broad.is_error, broad.content
        b = broad.structured_content
        assert b is not None and b["complete"] is False
        reason = b["incomplete_reason"] or ""
        if "refused" in reason:
            # the site sometimes answers action2.php with HTTP 404 for a long time; the result must say so
            assert b["pages_fetched"] == 1 and b["rows_fetched"] > 0
            assert "unfetched_min_price_rub" in b
            skip_reason = f"next pages are refused by the site right now, pagination not verifiable: {reason}"
        else:
            assert b["pages_fetched"] == 2 and b["rows_fetched"] > 50
            assert "max_pages" in reason
    if skip_reason:  # outside the client context: a skip raised inside its task group gets wrapped
        pytest.skip(skip_reason)
