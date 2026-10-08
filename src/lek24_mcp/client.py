"""Async HTTP adapter for 24lek.ru: fixed host, one request at a time, spaced starts, bounded retries."""

import asyncio
import datetime as dt
import email.utils
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2

from lek24_mcp.models import ErrorCode, Lek24Error

logger = logging.getLogger(__name__)


class Clock(Protocol):
    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class RateLimiter:
    """At most one request in flight; starts are `min_interval` apart and never before a deferral ends."""

    def __init__(self, min_interval: float, clock: Clock, max_wait: float | None = None) -> None:
        self._min_interval = min_interval
        self._max_wait = max_wait
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_start: float | None = None
        self._blocked_until = 0.0

    def defer(self, seconds: float) -> None:
        """Hold back every later request (from any caller) for `seconds` from now, e.g. Retry-After."""
        self._blocked_until = max(self._blocked_until, self._clock.monotonic() + seconds)

    async def __aenter__(self) -> None:
        await self._lock.acquire()
        try:
            earliest = self._blocked_until
            if self._last_start is not None:
                earliest = max(earliest, self._last_start + self._min_interval)
            wait = earliest - self._clock.monotonic()
            if self._max_wait is not None and wait > self._max_wait:
                # fail fast instead of parking an MCP call for many minutes
                raise Lek24Error(
                    ErrorCode.UPSTREAM_RATE_LIMITED, f"upstream asked to pause; retry in {wait:.0f}s"
                )
            if wait > 0:
                await self._clock.sleep(wait)
            self._last_start = self._clock.monotonic()
        except BaseException:
            self._lock.release()
            raise

    async def __aexit__(self, *exc: object) -> None:
        self._lock.release()


_RETRY_STATUSES = {429, 503}


@dataclass(frozen=True)
class ClientSettings:
    base_url: str = "https://24lek.ru"
    timeout: float = 20.0
    min_interval: float = 10.0
    max_retries: int = 2
    retry_after_cap: float = 60.0
    user_agent: str = "lek24-mcp/0.1 (read-only offer search)"
    max_body_bytes: int = 5_000_000


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    text: str
    fetched_at: dt.datetime


class Lek24Client:
    def __init__(
        self,
        settings: ClientSettings | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings or ClientSettings()
        self.clock = clock or SystemClock()
        self.rng = rng or random.Random()
        self._limiter = RateLimiter(
            self.settings.min_interval,
            self.clock,
            max_wait=self.settings.retry_after_cap + self.settings.min_interval,
        )
        self._client = httpx2.AsyncClient(
            base_url=self.settings.base_url,
            timeout=self.settings.timeout,
            headers={"User-Agent": self.settings.user_agent},
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "Lek24Client":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> FetchResult:
        attempts = self.settings.max_retries + 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            retry_after: float | None = None
            try:
                async with self._limiter:
                    response = await self._client.request(method, path, **kwargs)
                logger.debug("%s %s -> %s", method, path, response.status_code)
            except (httpx2.TimeoutException, httpx2.TransportError) as e:
                if last:
                    raise Lek24Error(ErrorCode.UPSTREAM_UNAVAILABLE, f"{type(e).__name__} at {path}") from e
                logger.warning("%s %s failed (%s); retry %d", method, path, type(e).__name__, attempt + 1)
            else:
                status = response.status_code
                if status == 200:
                    if len(response.content) > self.settings.max_body_bytes:
                        raise Lek24Error(ErrorCode.UPSTREAM_UNAVAILABLE, "response too large")
                    return FetchResult(
                        url=str(response.request.url),
                        status=status,
                        text=response.content.decode("utf-8", errors="replace"),
                        fetched_at=dt.datetime.now(dt.UTC),
                    )
                if status not in _RETRY_STATUSES:
                    raise Lek24Error(
                        ErrorCode.UPSTREAM_UNAVAILABLE, f"HTTP {status} at {path}", status=status
                    )
                retry_after = self._get_retry_after(response)
                if retry_after is not None:
                    # honour the server's pause for every later request, even when we give up now
                    self._limiter.defer(retry_after)
                code = ErrorCode.UPSTREAM_RATE_LIMITED if status == 429 else ErrorCode.UPSTREAM_UNAVAILABLE
                if last:
                    raise Lek24Error(code, f"HTTP {status} at {path} after {attempts} attempts")
                if retry_after is not None and retry_after > self.settings.retry_after_cap:
                    # retrying earlier than the server asked would be impolite and likely fail again
                    raise Lek24Error(code, f"HTTP {status} at {path}; server asks to wait {retry_after:.0f}s")
                logger.warning("%s %s -> %s; retry %d", method, path, status, attempt + 1)
            if retry_after is None:
                # no server hint: back off for every caller of this client, not just this request
                self._limiter.defer(2**attempt + self.rng.uniform(0, 1))
        raise AssertionError("unreachable")

    @staticmethod
    def _get_retry_after(response: httpx2.Response) -> float | None:
        """Seconds to wait from Retry-After (delta-seconds or HTTP-date); None if absent/invalid."""
        header = (response.headers.get("Retry-After") or "").strip()
        if header.isdigit():
            return float(header)
        try:
            when = email.utils.parsedate_to_datetime(header)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            return None
        return max(0.0, (when - dt.datetime.now(dt.UTC)).total_seconds())

    async def search_page(self, query: str, city_id: int, region_id: int, district_id: int) -> FetchResult:
        q = query.strip()
        if not q or len(q) > 256:
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, "invalid query")
        return await self._request(
            "GET",
            "/action.php",
            params={"city": city_id, "kray": region_id, "query": q, "raon": district_id},
        )

    async def next_page(
        self, query: str, city_id: int, district_id: int, num: int, code: str | None
    ) -> FetchResult:
        q = query.strip()
        if not q or len(q) > 256:
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, "invalid query")
        if num < 1:
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, "num < 1")
        try:
            return await self._request(
                "POST",
                "/action2.php",
                data={
                    "a": q,
                    "city": str(city_id),
                    "raon": str(district_id),
                    "num": str(num),
                    "code": code or "",
                },
            )
        except Lek24Error as e:
            # the site answers 404 to every next-page request, even page 2 of a fresh search, once it has
            # served a client enough pages (observed 2026-10-08 after ~11 pages; still blocked 13 min later)
            if e.status in (403, 404):
                raise Lek24Error(
                    ErrorCode.UPSTREAM_REFUSED,
                    f"site refused further pages (HTTP {e.status})",
                    status=e.status,
                ) from e
            raise

    async def suggest(self, q: str, limit: int = 10) -> FetchResult:
        sq = q.strip()
        if not (1 <= limit <= 50):
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, "limit out of range")
        if not sq or len(sq) > 256:
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, "invalid query")
        return await self._request(
            "GET",
            "/data.php",
            params={"mode": "sql", "q": sq, "limit": limit},
        )

    async def pharmacies_page(self) -> FetchResult:
        """Registry of every pharmacy connected to the site."""
        return await self._request("GET", "/apteki.php")

    async def index(self) -> FetchResult:
        return await self._request("GET", "/")
