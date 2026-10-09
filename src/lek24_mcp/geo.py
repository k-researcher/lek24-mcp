"""Address normalization, optional Nominatim geocoding and straight-line distances."""

import asyncio
import contextlib
import contextvars
import datetime as dt
import email.utils
import json
import logging
import math
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from lek24_mcp.client import Clock, SystemClock

_GEOCODE_REQUEST = contextvars.ContextVar("lek24_geocode_request", default=False)


class _GeocodeLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _GEOCODE_REQUEST.get()


_GEOCODE_LOG_FILTER = _GeocodeLogFilter()


def normalize_address(address: str) -> str:
    address = re.sub(r"[\u200b-\u200d\ufeff]", "", address).replace("ё", "е").replace("Ё", "Е")
    address = re.sub(r"\b(?:пр-кт|пр-т|проспект|пр)(?:\.|\b)\s*", "проспект ", address, flags=re.I)
    address = re.sub(r"\bпер(?:\.|\b)\s*", "переулок ", address, flags=re.I)
    address = re.sub(r"\b(?:ул\.|улица\b)\s*", "", address, flags=re.I)
    address = re.sub(
        r"(?:проспект\s+)?(?:имени\s+газеты\s+)?(?:Крас\.?\s*раб\.?|Красноярский\s+рабочий)\b\.?",
        "проспект имени газеты Красноярский рабочий",
        address,
        flags=re.I,
    )
    address = re.sub(
        r"(?:им\.?\s*газ\.?\s*)?Пионерск(?:ая\s+правда|ой\s+Правды)",
        "улица Пионерской Правды",
        address,
        flags=re.I,
    )
    address = re.sub(
        r"(?:проспект\s+)?60\s+лет\s+(?:образования\s+)?СССР",
        "проспект 60 лет образования СССР",
        address,
        flags=re.I,
    )
    address = re.sub(
        r"78(?:-й)?\s+Добровольческой\s+бригады", "улица 78 Добровольческой Бригады", address, flags=re.I
    )
    address = re.sub(r"(\d)\s+([А-Яа-яA-Za-z])(?=\s|/|$)", r"\1\2", address)
    address = re.sub(r"(\d+[А-Яа-яA-Za-z]?)/\d+[А-Яа-яA-Za-z]?\b", r"\1", address)
    return " ".join(address.split()).strip(" ,")


def _validate_point(lat: float, lon: float) -> None:
    if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("coordinates must be finite, latitude in [-90, 90], longitude in [-180, 180]")


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    _validate_point(lat1, lon1)
    _validate_point(lat2, lon2)
    latitude_delta = math.radians(lat2 - lat1)
    longitude_delta = math.radians(lon2 - lon1)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(longitude_delta / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, haversine))))


class GeocodeResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    status: Literal["ok", "not_found", "skipped", "unavailable"]
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    district: str | None = None
    fetched_at: dt.datetime | None = None
    warning: str | None = None

    @model_validator(mode="after")
    def validate_result(self) -> "GeocodeResult":
        if self.status == "ok" and (self.lat is None or self.lon is None):
            raise ValueError("successful geocode requires coordinates")
        if self.status != "ok" and (
            self.lat is not None or self.lon is not None or self.district is not None
        ):
            raise ValueError("unsuccessful geocode cannot contain location data")
        if self.fetched_at is not None and self.fetched_at.utcoffset() is None:
            raise ValueError("fetched_at must include a timezone")
        return self


class Geocoder:
    """One instance per process; user addresses must use persist=False (no cache reads or writes)."""

    def __init__(
        self,
        *,
        cache_path: Path | None = None,
        enabled: bool | None = None,
        base_url: str = "https://nominatim.openstreetmap.org",
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Clock | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.enabled = enabled if enabled is not None else os.getenv("LEK24_GEOCODER", "").lower() != "off"
        self.cache_path = (
            cache_path
            or Path(os.getenv("LEK24_GEO_CACHE", "~/.cache/lek24-mcp/geocode-v1.json")).expanduser()
        )
        self._provider = base_url.rstrip("/")
        self._clock = clock or SystemClock()
        self._now = now or (lambda: dt.datetime.now(dt.UTC))
        self._lock = asyncio.Lock()
        self._last_start: float | None = None
        self._blocked_until = 0.0
        self._refused = False
        self._cache: dict[str, GeocodeResult] = {}
        self._cache_loaded = False
        logging.getLogger("httpx2").addFilter(_GEOCODE_LOG_FILTER)
        self._client = httpx2.AsyncClient(
            base_url=self._provider,
            timeout=httpx2.Timeout(10.0),
            headers={"User-Agent": "lek24-mcp/0.1 (https://github.com/k-researcher/lek24-mcp)"},
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self) -> "Geocoder":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _key(self, *parts: object) -> str:
        return json.dumps([self._provider, *parts], ensure_ascii=False, separators=(",", ":"))

    def _load_cache(self) -> None:
        if self._cache_loaded:
            return
        self._cache_loaded = True
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                return
            entries = payload.get("entries")
            if not isinstance(entries, dict):
                return
            for key, value in entries.items():
                try:
                    result = GeocodeResult.model_validate(value)
                except ValidationError:
                    continue
                if result.status in {"ok", "not_found"} and result.fetched_at is not None:
                    self._cache[key] = result
        except (OSError, ValueError):
            return

    def _cached(self, key: str) -> GeocodeResult | None:
        self._load_cache()
        result = self._cache.get(key)
        if (
            result is not None
            and result.status == "not_found"
            and (result.fetched_at is None or self._now() - result.fetched_at >= dt.timedelta(days=30))
        ):
            return None
        return result

    def _save(self, key: str, result: GeocodeResult) -> GeocodeResult:
        self._cache[key] = result
        temporary: str | None = None
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.cache_path.parent, delete=False
            ) as stream:
                temporary = stream.name
                json.dump(
                    {
                        "version": 1,
                        "entries": {key: entry.model_dump(mode="json") for key, entry in self._cache.items()},
                    },
                    stream,
                    ensure_ascii=False,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.cache_path)
        except OSError:
            return result.model_copy(update={"warning": "geocode cache could not be saved"})
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    Path(temporary).unlink(missing_ok=True)
        return result

    async def _request(self, path: str, params: dict[str, str]) -> Any:
        if self._refused or self._clock.monotonic() < self._blocked_until:
            raise ValueError("geocoder deferred")
        if self._last_start is not None:
            wait = self._last_start + 1.0 - self._clock.monotonic()
            if wait > 0:
                await self._clock.sleep(wait)
        self._last_start = self._clock.monotonic()
        token = _GEOCODE_REQUEST.set(True)
        try:
            response = await self._client.get(path, params=params)
        finally:
            _GEOCODE_REQUEST.reset(token)
        if response.status_code == 403:
            self._refused = True
        if response.status_code in {429, 503}:
            retry_after = response.headers.get("Retry-After", "60")
            try:
                delay = float(retry_after)
                if not math.isfinite(delay):
                    delay = 60.0
            except ValueError:
                try:
                    delay = (email.utils.parsedate_to_datetime(retry_after) - self._now()).total_seconds()
                except (TypeError, ValueError):
                    delay = 60.0
            self._blocked_until = self._clock.monotonic() + max(1.0, delay)
        if path == "/reverse" and response.status_code == 404:
            payload = response.json()
            if isinstance(payload, dict) and payload.get("error") == "Unable to geocode":
                return payload
        response.raise_for_status()
        return response.json()

    def _result(self, payload: Any) -> GeocodeResult:
        if not isinstance(payload, dict):
            raise ValueError("invalid geocode response")
        lat, lon = float(payload["lat"]), float(payload["lon"])
        _validate_point(lat, lon)
        address = payload.get("address", {})
        if not isinstance(address, dict):
            raise ValueError("invalid geocode address")
        district = address.get("city_district")
        if district is not None and not isinstance(district, str):
            raise ValueError("invalid geocode district")
        return GeocodeResult(status="ok", lat=lat, lon=lon, district=district, fetched_at=self._now())

    async def geocode(self, address: str, city: str, *, persist: bool = True) -> GeocodeResult:
        normalized = normalize_address(address)
        city = " ".join(city.split())
        if not normalized or not city:
            raise ValueError("address and city must not be empty")
        if not self.enabled:
            return GeocodeResult(status="skipped")
        key = self._key("search", normalized.casefold(), city.casefold())
        async with self._lock:
            if persist and (cached := self._cached(key)) is not None:
                return cached
            try:
                result = GeocodeResult(status="not_found", fetched_at=self._now())
                for street in (normalized, "улица " + normalized):
                    payload = await self._request(
                        "/search", {"street": street, "city": city, "format": "jsonv2", "limit": "1"}
                    )
                    if not isinstance(payload, list):
                        raise ValueError("invalid search response")
                    if payload:
                        result = self._result(payload[0])
                        break
                if result.status == "not_found":
                    result = result.model_copy(update={"fetched_at": self._now()})
            except (httpx2.HTTPError, ValueError, KeyError, TypeError, OverflowError):
                return GeocodeResult(status="unavailable", warning="geocoder unavailable or invalid response")
            return self._save(key, result) if persist else result

    async def reverse(self, lat: float, lon: float, *, zoom: int = 14, persist: bool = True) -> GeocodeResult:
        _validate_point(lat, lon)
        if not 0 <= zoom <= 18:
            raise ValueError("zoom must be in [0, 18]")
        if not self.enabled:
            return GeocodeResult(status="skipped")
        key = self._key("reverse", lat, lon, zoom)
        async with self._lock:
            if persist and (cached := self._cached(key)) is not None:
                return cached
            try:
                payload = await self._request(
                    "/reverse", {"lat": str(lat), "lon": str(lon), "format": "jsonv2", "zoom": str(zoom)}
                )
                if isinstance(payload, dict) and payload.get("error") == "Unable to geocode":
                    result = GeocodeResult(status="not_found", fetched_at=self._now())
                else:
                    result = self._result(payload)
            except (httpx2.HTTPError, ValueError, KeyError, TypeError, OverflowError):
                return GeocodeResult(status="unavailable", warning="geocoder unavailable or invalid response")
            return self._save(key, result) if persist else result
