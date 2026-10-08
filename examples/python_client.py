"""Minimal MCP client for lek24-mcp using the official SDK (mcp>=2): initialize → list_tools → call_tool.

stdio (spawns the server):   uv run python examples/python_client.py "Исла Моос"
Streamable HTTP:             uv run python examples/python_client.py "Исла Моос" --url http://127.0.0.1:8000/mcp
HTTP with bearer token:      LEK24_HTTP_TOKEN=... uv run python examples/python_client.py "..." --url ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client


async def run(client: Client, query: str) -> None:
    tools = await client.list_tools()
    print("tools:", ", ".join(t.name for t in tools.tools), file=sys.stderr)
    res = await client.call_tool("find_cheapest", {"query": query, "top_k": 5})
    if res.is_error:
        print("error:", res.content, file=sys.stderr)
        raise SystemExit(1)
    data: dict[str, Any] = res.structured_content or {}
    print(json.dumps(data, ensure_ascii=False, indent=2))


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("query")
    p.add_argument("--url", help="Streamable HTTP endpoint; omit to launch the server over stdio")
    a = p.parse_args()

    if a.url:
        token = os.environ.get("LEK24_HTTP_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with (
            httpx2.AsyncClient(headers=headers, timeout=180) as http,
            Client(streamable_http_client(a.url, http_client=http)) as client,
        ):
            await run(client, a.query)
    else:
        params = StdioServerParameters(command=sys.executable, args=["-m", "lek24_mcp.server"])
        async with Client(params, read_timeout_seconds=180) as client:
            await run(client, a.query)


if __name__ == "__main__":
    asyncio.run(main())
