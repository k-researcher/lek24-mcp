"""Search orchestration: pagination, caching, validation, filtering, dedup, ranking.

The service never invents data: every Offer is built from one upstream row, and every limit or
inconsistency that could hide offers is reported through `complete=False` and `warnings`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import itertools
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

import httpx2

from lek24_mcp import normalize as nz
from lek24_mcp.cache import SingleFlight, TTLCache
from lek24_mcp.client import Lek24Client
from lek24_mcp.coverage import CoverageGaps, district_key, load_references, load_snapshot
from lek24_mcp.directory import parse_pharmacies
from lek24_mcp.geo import GeocodeResult, haversine_km, normalize_address
from lek24_mcp.models import (
    CheapestNearResult,
    CheapestResult,
    ErrorCode,
    Lek24Error,
    Locations,
    MatchMode,
    NearbyOffer,
    NearbyOrigin,
    Offer,
    ParserContractChanged,
    Pharmacy,
    PharmacyList,
    PharmacyStatus,
    ProductGroup,
    RawRow,
    SearchResult,
)
from lek24_mcp.parser import parse_fragment, parse_locations, parse_search_page, parse_suggestions

log = logging.getLogger(__name__)

DEFAULT_CITY_ID = 0  # Красноярск
DEFAULT_REGION_ID = 0  # Красноярский край
KRASNOYARSK_CITY_ID = 0


@dataclass(frozen=True)
class ServiceSettings:
    max_pages_default: int = 5
    max_pages_cap: int = 20
    limit_cap: int = 200
    offers_ttl: float = 120.0
    locations_ttl: float = 86_400.0
    cache_entries: int = 256
    deadline_seconds: float = 120.0
    refused_pause_seconds: float = 900.0
    # price-list timestamps in the registry move every hour, so a day-long cache would lie
    pharmacies_ttl: float = 3_600.0
    stale_days: float = 3.0
    geocode_deadline_seconds: float = 60.0


class GeocodeProvider(Protocol):
    async def geocode(self, address: str, city: str, *, persist: bool = True) -> GeocodeResult: ...


@dataclass(frozen=True)
class _Registry:
    source_url: str
    fetched_at: dt.datetime
    pharmacies: list[Pharmacy]
    by_id: dict[int, Pharmacy]
    offline: bool = False


@dataclass
class _RawSearch:
    """Upstream rows for one (query, location, max_pages) before any filtering."""

    source_url: str
    fetched_at: dt.datetime
    site_updated_at_raw: str | None
    source_total: int | None
    rows: list[RawRow]
    pages_fetched: int
    complete: bool
    incomplete_reason: str | None
    unfetched_min_price: Decimal | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StopRule:
    """Stop paginating once the `top_k` cheapest matches can no longer change (find_cheapest)."""

    top_k: int
    match_mode: MatchMode
    physical_only: bool
    price_tolerance: Decimal | None = None

    def reached(self, rows: list[RawRow], query: str, floor: Decimal) -> bool:
        offers, bad_rows, _ = _build_offers(rows, query, self.match_mode)
        if self.physical_only:
            offers = [o for o in offers if o.offer_kind == "physical"]
        if self.price_tolerance is not None:
            return (
                not bad_rows
                and bool(offers)
                and floor > min(o.price_rub for o in offers) + self.price_tolerance
            )
        # unfetched rows cost >= floor, so matches at or below it are final (ties change no price)
        return sum(1 for o in offers if o.price_rub <= floor) >= self.top_k


_RawKey = tuple[str, int, int, int, int, StopRule | None]


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class SearchService:
    def __init__(
        self,
        client: Lek24Client,
        settings: ServiceSettings | None = None,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], dt.datetime] = _utcnow,
        geocoder: GeocodeProvider | None = None,
    ) -> None:
        self._client = client
        self._s = settings or ServiceSettings()
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._geo = geocoder
        self._raw_cache: TTLCache[_RawKey, _RawSearch] = TTLCache(
            self._s.offers_ttl, self._s.cache_entries, clock=monotonic
        )
        self._loc_cache: TTLCache[str, Locations] = TTLCache(self._s.locations_ttl, 1, clock=monotonic)
        self._raw_flight: SingleFlight[_RawKey, _RawSearch] = SingleFlight()
        self._loc_flight: SingleFlight[str, Locations] = SingleFlight()
        self._reg_cache: TTLCache[str, _Registry] = TTLCache(self._s.pharmacies_ttl, 1, clock=monotonic)
        self._reg_flight: SingleFlight[str, _Registry] = SingleFlight()
        # set when the site refuses next-page requests; it keeps refusing for a long time
        self._pages_refused_until: float | None = None

    # ------------------------------------------------------------------ locations / suggest

    async def list_locations(self, force_refresh: bool = False) -> Locations:
        if not force_refresh and (hit := self._loc_cache.get("all")) is not None:
            return hit[0]

        if not force_refresh and (stored := load_references()) is not None:
            return stored.locations

        async def fetch() -> Locations:
            res = await self._client.index()
            locs = parse_locations(res.text)
            self._loc_cache.set("all", locs)
            return locs

        return await self._loc_flight.run("all", fetch)

    # ------------------------------------------------------------------ pharmacy registry

    async def _registry(self, force_refresh: bool = False) -> _Registry:
        if not force_refresh and (hit := self._reg_cache.get("all")) is not None:
            return hit[0]

        if not force_refresh and (stored := load_references()) is not None:
            return _Registry(
                stored.source_url,
                stored.fetched_at,
                stored.pharmacies,
                {p.id: p for p in stored.pharmacies},
                True,
            )

        async def fetch() -> _Registry:
            res = await self._client.pharmacies_page()
            items = parse_pharmacies(res.text)
            reg = _Registry(res.url, res.fetched_at, items, {p.id: p for p in items})
            self._reg_cache.set("all", reg)
            return reg

        return await self._reg_flight.run("all", fetch)

    async def _location_names(self, city_id: int, district_id: int) -> tuple[str, str | None, list[str]]:
        """City and district names as the registry spells them, plus ambiguity warnings."""
        if city_id == DEFAULT_CITY_ID and district_id == 0:
            return "Красноярск", None, []
        locs = await self.list_locations()
        city = next(c for c in locs.cities if c.id == city_id)  # validated before
        warnings: list[str] = []
        if sum(1 for c in locs.cities if c.name == city.name) > 1:
            warnings.append(
                f"several cities are named {city.name!r}; the registry has no region column, "
                "so pharmacies of all of them are listed"
            )
        district = (
            next((d.name for d in locs.districts if d.id == district_id), None) if district_id else None
        )
        return city.name, district, warnings

    async def coverage_gaps(
        self, city_id: int = 0, district_id: int = 0, refresh: bool = False, limit: int = 25, offset: int = 0
    ) -> CoverageGaps:
        if city_id < 0 or district_id < 0:
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, "Location ids must be nonnegative")
        if not 1 <= limit <= 100 or offset < 0:
            raise Lek24Error(
                ErrorCode.INVALID_ARGUMENT, "limit must be in [1, 100]; offset must be nonnegative"
            )
        snapshot = load_snapshot(city_id)
        district = (
            next((d.name for d in snapshot.districts if d.id == district_id), None) if district_id else None
        )
        if district_id and (city_id != 0 or district is None):
            raise Lek24Error(ErrorCode.UNSUPPORTED_LOCATION, "Unknown district for this snapshot city")
        warnings = list(snapshot.warnings)
        sites = [
            site
            for site in snapshot.sites
            if district is None or district_key(site.district) == district_key(district)
        ]
        checked_at = snapshot.directory_fetched_at
        current_time = dt.datetime.now(dt.UTC)
        stored = [
            p
            for p in snapshot.pharmacies
            if district is None or district_key(p.district) == district_key(district)
        ]
        stale = [_status(p, current_time, self._s.stale_days) for p in stored]
        stale = [p for p in stale if p.is_stale]
        refreshed = False
        if refresh:
            try:
                current = await self.list_pharmacies(
                    city_id, snapshot.region_id, district_id, force_refresh=True
                )
                checked_at = current.fetched_at
                stale = [p for p in current.pharmacies if p.is_stale]
                warnings.extend(current.warnings)
                refreshed = True
            except Lek24Error as exc:
                warnings.append(f"Directory refresh unavailable ({exc.code.value}); stored directory used.")
        if not refreshed:
            warnings.append("Offline snapshot used; price-list timestamps have not been refreshed.")
        unlocated = sum(site.district is None for site in snapshot.sites)
        if district and unlocated:
            warnings.append(f"{unlocated} places without district excluded from district-filtered lists.")
        missing = sorted((site for site in sites if site.status == "missing"), key=lambda s: s.address)
        uncertain = sorted((site for site in sites if site.status == "uncertain"), key=lambda s: s.address)
        stale.sort(key=lambda p: (p.name, p.address, p.id))
        end = offset + limit
        return CoverageGaps(
            city_id=city_id,
            district_id=district_id,
            city_name=snapshot.city_name,
            district_name=district,
            snapshot_built_at=snapshot.built_at,
            licenses_source_url=snapshot.licenses_source_url,
            licenses_source_date=snapshot.licenses_source_date,
            licenses_fetched_at=snapshot.licenses_fetched_at,
            directory_source_url=snapshot.directory_source_url,
            directory_fetched_at=snapshot.directory_fetched_at,
            prices_checked_at=checked_at,
            directory_refreshed=refreshed,
            stale_check_available=True,
            missing=missing[offset:end],
            stale=stale[offset:end],
            uncertain=uncertain[offset:end],
            missing_count=len(missing),
            stale_count=len(stale),
            uncertain_count=len(uncertain),
            next_offset=end if max(len(missing), len(stale), len(uncertain)) > end else None,
            matched_count=sum(site.status == "matched" for site in sites),
            unlocated_count=unlocated,
            warnings=list(dict.fromkeys(warnings)),
        )

    async def list_pharmacies(
        self,
        city_id: int = DEFAULT_CITY_ID,
        region_id: int = DEFAULT_REGION_ID,
        district_id: int = 0,
        chain: str | None = None,
        stale_days: float | None = None,
        force_refresh: bool = False,
    ) -> PharmacyList:
        stale = self._s.stale_days if stale_days is None else stale_days
        if not 0 < stale <= 365:
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, "stale_days must be in (0, 365]")
        await self._validate_location(city_id, region_id, district_id)
        city_name, district_name, warnings = await self._location_names(city_id, district_id)
        reg = await self._registry(force_refresh)
        if reg.offline:
            warnings.append("Stored pharmacy directory used; timestamps have not been refreshed.")
        chain_f = nz.fold(chain) if chain and chain.strip() else None
        picked = [
            _status(p, _utcnow() if reg.offline else reg.fetched_at, stale)
            for p in reg.pharmacies
            if p.city == city_name
            and (district_name is None or p.district == district_name)
            and (chain_f is None or chain_f in nz.fold(p.name))
        ]
        picked.sort(key=lambda p: (not p.is_stale, nz.fold(p.name), nz.fold(p.address)))
        if not picked:
            warnings.append("no connected pharmacy matches these filters")
        return PharmacyList(
            city_id=city_id,
            region_id=region_id,
            district_id=district_id,
            city_name=city_name,
            district_name=district_name,
            chain=chain.strip() if chain and chain.strip() else None,
            stale_days=stale,
            source_url=reg.source_url,
            fetched_at=reg.fetched_at,
            total=len(picked),
            stale_count=sum(p.is_stale for p in picked),
            warnings=warnings,
            pharmacies=picked,
        )

    async def _enrich(self, offers: list[Offer], warnings: list[str]) -> list[Offer]:
        """Add district and price-list age from the registry to physical offers; never fails the search."""
        if not any(o.pharmacy_id is not None for o in offers):
            return offers
        try:
            reg = await self._registry()
        except Lek24Error as e:
            warnings.append(
                f"pharmacy registry unavailable ({e.code.value}); district and price-list age not shown"
            )
            return offers
        out: list[Offer] = []
        stale_n = 0
        for o in offers:
            p = reg.by_id.get(o.pharmacy_id) if o.pharmacy_id is not None else None
            if p is None:
                out.append(o)
                continue
            st = _status(p, _utcnow() if reg.offline else reg.fetched_at, self._s.stale_days)
            stale_n += st.is_stale
            out.append(
                o.model_copy(
                    update={
                        "pharmacy_name": p.name,
                        "pharmacy_address": p.address,
                        "district": p.district,
                        "prices_updated_at": p.prices_updated_at,
                        "price_list_stale": st.is_stale,
                    }
                )
            )
        if stale_n:
            warnings.append(
                f"{stale_n} offer(s) come from pharmacies whose price list is older than "
                f"{self._s.stale_days:g} days; check them directly"
            )
        return out

    async def suggest(self, query: str) -> list[str]:
        q = nz.clean_text(query)
        if len(q) < 3:
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, "query must have at least 3 characters")
        res = await self._client.suggest(q, limit=15)
        return parse_suggestions(res.text)

    async def _validate_location(self, city_id: int, region_id: int, district_id: int) -> None:
        if district_id != 0 and city_id != KRASNOYARSK_CITY_ID:
            raise Lek24Error(
                ErrorCode.UNSUPPORTED_LOCATION, "district_id is only supported for Krasnoyarsk (city_id=0)"
            )
        if (city_id, region_id) == (DEFAULT_CITY_ID, DEFAULT_REGION_ID) and district_id == 0:
            return  # the site's own default form state; no lookup needed
        locs = await self.list_locations()
        city = next((c for c in locs.cities if c.id == city_id), None)
        if city is None:
            raise Lek24Error(ErrorCode.UNSUPPORTED_LOCATION, f"unknown city_id={city_id}; see list_locations")
        if city.region_id is not None and city.region_id != region_id:
            raise Lek24Error(
                ErrorCode.UNSUPPORTED_LOCATION,
                f"city_id={city_id} ({city.name}) belongs to region_id={city.region_id}, not {region_id}",
            )
        if district_id != 0 and all(d.id != district_id for d in locs.districts):
            raise Lek24Error(ErrorCode.UNSUPPORTED_LOCATION, f"unknown district_id={district_id}")

    # ------------------------------------------------------------------ raw fetch + pagination

    async def _fetch_raw(
        self,
        query: str,
        city_id: int,
        region_id: int,
        district_id: int,
        max_pages: int,
        stop: StopRule | None = None,
    ) -> _RawSearch:
        started = self._monotonic()
        first = await self._client.search_page(query, city_id, region_id, district_id)
        page = parse_search_page(first.text)
        meta = page.meta
        raw = _RawSearch(
            source_url=first.url,
            fetched_at=first.fetched_at,
            site_updated_at_raw=meta.site_updated_at_raw,
            source_total=meta.total,
            rows=list(page.rows),
            pages_fetched=1,
            complete=False,
            incomplete_reason=None,
        )
        if page.no_results:
            raw.complete = True
            return raw

        total = meta.total
        page_size = meta.page_size or len(page.rows) or 50
        num = page_size
        prev_sig = _signature(page.rows)
        seen = {_identity(r) for r in page.rows}
        overlap = 0
        site_query = meta.query_echo or query

        while total is None or len(raw.rows) < total:
            # every next page counts against the site's per-client limit, so do not fetch unneeded ones
            floor = _price_floor(raw.rows) if stop is not None else None
            if stop is not None and floor is not None and stop.reached(raw.rows, query, floor):
                if stop.price_tolerance is not None:
                    raw.incomplete_reason = (
                        f"stopped after {raw.pages_fetched} page(s): the minimum + "
                        f"{stop.price_tolerance} RUB price tier is loaded; "
                        f"later pages cost at least {floor} RUB"
                    )
                else:
                    raw.incomplete_reason = (
                        f"stopped after {raw.pages_fetched} page(s): the {stop.top_k} cheapest matches "
                        f"are final, later pages cost at least {floor} RUB"
                    )
                break
            if raw.pages_fetched >= max_pages:
                raw.incomplete_reason = f"max_pages={max_pages} reached"
                break
            if self._monotonic() - started >= self._s.deadline_seconds:
                raw.incomplete_reason = f"deadline {self._s.deadline_seconds:.0f}s reached"
                break
            if self._pages_refused_until is not None:
                left = self._pages_refused_until - self._monotonic()
                if left > 0:
                    raw.incomplete_reason = (
                        f"site refused further pages earlier; next pages not requested for {left:.0f}s more"
                    )
                    break
                self._pages_refused_until = None
            try:
                nxt = await self._client.next_page(site_query, city_id, district_id, num, meta.code)
                frag = parse_fragment(nxt.text)
            except Lek24Error as e:
                raw.incomplete_reason = f"page {raw.pages_fetched + 1} failed: {e.code.value}: {e.message}"
                if e.code == ErrorCode.UPSTREAM_REFUSED:
                    self._pages_refused_until = self._monotonic() + self._s.refused_pause_seconds
                    raw.incomplete_reason += (
                        f"; next pages not requested for {self._s.refused_pause_seconds / 60:.0f} min"
                    )
                break
            if frag.is_end:
                if total is not None and len(raw.rows) < total:
                    raw.incomplete_reason = f"site ended pagination at {len(raw.rows)} of {total} rows"
                break
            sig = _signature(frag.rows)
            if sig == prev_sig:
                raw.incomplete_reason = "upstream returned the same page twice"
                break
            # rows identical to earlier pages: a shifted listing or a legitimate duplicate row --
            # indistinguishable without upstream ids, so keep everything but never claim completeness
            overlap += sum(1 for r in frag.rows if _identity(r) in seen)
            prev_sig = sig
            seen.update(_identity(r) for r in frag.rows)
            raw.rows.extend(frag.rows)
            raw.pages_fetched += 1
            num += page_size

        if overlap and raw.incomplete_reason is None:
            raw.incomplete_reason = (
                f"pages overlap ({overlap} row(s) repeat earlier pages); listing may have shifted"
            )
        if total is None:
            raw.warnings.append("site did not report a total; completeness cannot be confirmed")
            raw.incomplete_reason = raw.incomplete_reason or "total unknown"
        elif len(raw.rows) > total and raw.incomplete_reason is None:
            raw.incomplete_reason = f"fetched more rows ({len(raw.rows)}) than the site reported ({total})"
        raw.complete = raw.incomplete_reason is None
        if not raw.complete:
            raw.unfetched_min_price = _price_floor(raw.rows)
        return raw

    async def _get_raw(
        self,
        query: str,
        city_id: int,
        region_id: int,
        district_id: int,
        max_pages: int,
        force_refresh: bool,
        stop: StopRule | None,
    ) -> tuple[_RawSearch, bool]:
        # a stopped-early fetch is not a full listing, so it never serves plain searches
        key: _RawKey = (nz.fold(query), city_id, region_id, district_id, max_pages, stop)
        if not force_refresh and (hit := self._raw_cache.get(key)) is not None:
            return hit[0], True

        async def fetch() -> _RawSearch:
            raw = await self._fetch_raw(query, city_id, region_id, district_id, max_pages, stop)
            self._raw_cache.set(key, raw)
            return raw

        return await self._raw_flight.run(key, fetch), False

    # ------------------------------------------------------------------ public search

    async def search(
        self,
        query: str,
        city_id: int = DEFAULT_CITY_ID,
        region_id: int = DEFAULT_REGION_ID,
        district_id: int = 0,
        limit: int = 20,
        max_pages: int | None = None,
        match_mode: MatchMode = "tokens",
        include_online: bool = True,
        physical_only: bool = False,
        force_refresh: bool = False,
        stop: StopRule | None = None,
        _all_offers: bool = False,
    ) -> SearchResult:
        q = nz.clean_text(query)
        if not q or len(q) > 256:
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, "query must be 1..256 characters")
        if not 1 <= limit <= self._s.limit_cap:
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, f"limit must be 1..{self._s.limit_cap}")
        pages = self._s.max_pages_default if max_pages is None else max_pages
        if not 1 <= pages <= self._s.max_pages_cap:
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, f"max_pages must be 1..{self._s.max_pages_cap}")
        await self._validate_location(city_id, region_id, district_id)

        raw, cached = await self._get_raw(q, city_id, region_id, district_id, pages, force_refresh, stop)
        warnings = [*nz.query_warnings(q), *raw.warnings]
        offers, bad_rows, dupes = _build_offers(raw.rows, q, match_mode)
        if raw.rows and bad_rows == len(raw.rows):
            raise ParserContractChanged("no row had a parseable price")
        if bad_rows:
            warnings.append(f"{bad_rows} row(s) skipped: unparseable price")
        if dupes:
            warnings.append(f"{dupes} exact duplicate row(s) merged")
        if physical_only:
            offers = [o for o in offers if o.offer_kind == "physical"]
        elif not include_online:
            offers = [o for o in offers if o.offer_kind != "online"]
        offers.sort(key=lambda o: (o.price_rub, -o.relevance, o.pharmacy_name_address_raw))
        offers = await self._enrich(offers, warnings)
        if not raw.complete:
            if raw.unfetched_min_price is not None:
                tail = "the site lists offers by ascending price, so unfetched rows cost at least "
                tail += f"{raw.unfetched_min_price} RUB"
            else:
                tail = "cheaper offers may exist on later pages"
            warnings.append(f"results are incomplete ({raw.incomplete_reason}); {tail}")
        if cached:
            warnings.append(
                f"served from cache; data fetched at {raw.fetched_at.isoformat(timespec='seconds')}"
            )

        return SearchResult(
            query=q,
            city_id=city_id,
            region_id=region_id,
            district_id=district_id,
            source_url=raw.source_url,
            fetched_at=raw.fetched_at,
            site_updated_at_raw=raw.site_updated_at_raw,
            source_total=raw.source_total,
            rows_fetched=len(raw.rows),
            matched_count=len(offers),
            complete=raw.complete,
            incomplete_reason=raw.incomplete_reason,
            unfetched_min_price_rub=raw.unfetched_min_price,
            pages_fetched=raw.pages_fetched,
            cached=cached,
            warnings=warnings,
            offers=offers if _all_offers else offers[:limit],
        )

    async def find_cheapest(
        self,
        query: str,
        city_id: int = DEFAULT_CITY_ID,
        region_id: int = DEFAULT_REGION_ID,
        district_id: int = 0,
        top_k: int = 5,
        max_pages: int | None = None,
        match_mode: MatchMode = "tokens",
        physical_only: bool = False,
        force_refresh: bool = False,
        exhaustive: bool = False,
    ) -> CheapestResult:
        if not 1 <= top_k <= 50:
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, "top_k must be 1..50")
        full = await self.search(
            query,
            city_id,
            region_id,
            district_id,
            limit=self._s.limit_cap,
            max_pages=max_pages,
            match_mode=match_mode,
            physical_only=physical_only,
            force_refresh=force_refresh,
            stop=None if exhaustive else StopRule(top_k, match_mode, physical_only),
        )
        groups = _group(full.offers)
        floor = full.unfetched_min_price_rub
        if full.complete:
            note = f"ranked across all {full.rows_fetched} rows the site returned for this query"
        elif floor is not None and full.offers and full.offers[0].price_rub <= floor:
            note = (
                f"ranked across {full.rows_fetched} of {full.source_total or 'unknown'} rows "
                f"({full.incomplete_reason}); the site lists offers by ascending price and unfetched rows "
                f"cost at least {floor} RUB, so the minimum {full.offers[0].price_rub} RUB is final"
            )
            top = full.offers[:top_k]
            if all(o.price_rub <= floor for o in top):
                note += f" and so are all {len(top)} listed offers"
        else:
            note = (
                f"ranked across {full.rows_fetched} of {full.source_total or 'unknown'} rows only "
                f"({full.incomplete_reason}); the true minimum may be lower"
            )
            if floor is not None:
                note += f" (unfetched rows cost at least {floor} RUB)"
        if not full.complete:
            # a variant priced entirely above the fetched rows has no group at all; do not read that as absent
            above = f"cost more than {floor} RUB" if floor is not None else "sit on unfetched pages"
            hint = "pass exhaustive=true or query it by name" if not exhaustive else "query it by name"
            note += (
                f"; groups are partial: a product variant whose offers all {above} is missing from them "
                f"({hint})"
            )
        if full.matched_count > self._s.limit_cap:
            note += f"; ranking used the {self._s.limit_cap} cheapest of {full.matched_count} matches"
        return CheapestResult(
            search=full.model_copy(update={"offers": full.offers[:top_k]}),
            coverage_note=note,
            groups=groups,
        )

    async def find_cheapest_near(
        self,
        query: str,
        *,
        near_lat: float | None = None,
        near_lon: float | None = None,
        near: str | None = None,
        city_id: int = DEFAULT_CITY_ID,
        region_id: int = DEFAULT_REGION_ID,
        district_id: int = 0,
        price_tolerance_rub: Decimal = Decimal("0"),
        nearby_radius_km: float = 1.5,
        top_k: int = 5,
        max_pages: int | None = None,
        match_mode: MatchMode = "tokens",
        force_refresh: bool = False,
    ) -> CheapestNearResult:
        has_coords = near_lat is not None or near_lon is not None
        if has_coords == (near is not None) or (has_coords and (near_lat is None or near_lon is None)):
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, "provide either near_lat + near_lon or near")
        if near is not None and not 1 <= len(near.strip()) <= 512:
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, "near must be 1..512 characters")
        if near_lat is not None and near_lon is not None:
            try:
                haversine_km(near_lat, near_lon, near_lat, near_lon)
            except ValueError as exc:
                raise Lek24Error(ErrorCode.INVALID_ARGUMENT, "invalid origin coordinates") from exc
        if not price_tolerance_rub.is_finite() or price_tolerance_rub < 0:
            raise Lek24Error(
                ErrorCode.INVALID_ARGUMENT, "price_tolerance_rub must be finite and non-negative"
            )
        if not math.isfinite(nearby_radius_km) or nearby_radius_km <= 0:
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, "nearby_radius_km must be finite and positive")
        if not 1 <= top_k <= 50:
            raise Lek24Error(ErrorCode.INVALID_ARGUMENT, "top_k must be 1..50")

        full = await self.search(
            query,
            city_id,
            region_id,
            district_id,
            max_pages=max_pages,
            match_mode=match_mode,
            physical_only=True,
            force_refresh=force_refresh,
            stop=StopRule(top_k, match_mode, True, price_tolerance_rub),
            _all_offers=True,
        )
        warnings = list(full.warnings)
        city, _, location_warnings = await self._location_names(city_id, 0)
        warnings.extend(location_warnings)
        deadline = self._monotonic() + self._s.geocode_deadline_seconds

        async def geocode(address: str, *, persist: bool) -> GeocodeResult:
            remaining = deadline - self._monotonic()
            if self._geo is None or remaining <= 0:
                return GeocodeResult(status="skipped")
            try:
                return await asyncio.wait_for(self._geo.geocode(address, city, persist=persist), remaining)
            except TimeoutError:
                return GeocodeResult(status="skipped", warning="geocoding deadline reached")
            except (httpx2.HTTPError, OSError, ValueError):
                return GeocodeResult(status="unavailable", warning="geocoder unavailable")

        if near is not None:
            point = await geocode(near, persist=False)
            origin = NearbyOrigin(
                source="geocoded", lat=point.lat, lon=point.lon, geocode_status=_geocode_status(point)
            )
            if point.status != "ok":
                warnings.append("origin could not be geocoded; distances and nearby offers are unavailable")
        else:
            origin = NearbyOrigin(source="coords", lat=near_lat, lon=near_lon, geocode_status="ok")

        located: list[NearbyOffer] = []
        points: dict[str, GeocodeResult] = {}
        for offer in full.offers:
            point = GeocodeResult(status="skipped")
            if origin.lat is not None and origin.lon is not None and offer.pharmacy_address:
                key = normalize_address(offer.pharmacy_address).casefold()
                if key not in points:
                    points[key] = await geocode(offer.pharmacy_address, persist=True)
                point = points[key]
            distance = None
            if (
                point.lat is not None
                and point.lon is not None
                and origin.lat is not None
                and origin.lon is not None
            ):
                distance = haversine_km(origin.lat, origin.lon, point.lat, point.lon)
            located.append(
                NearbyOffer(**offer.model_dump(), distance_km=distance, geocode_status=_geocode_status(point))
            )
        missing = sum(offer.distance_km is None for offer in located)
        if missing:
            warnings.append(
                f"{missing} loaded physical offer(s) have no distance; distance rankings are partial"
            )
        if any(point.warning is not None for point in points.values()):
            warnings.append("geocoder reported a failure or cache warning; some locations may be unavailable")

        minimum = min((offer.price_rub for offer in located), default=None)
        threshold = minimum + price_tolerance_rub if minimum is not None else None
        tier = [offer for offer in located if threshold is not None and offer.price_rub <= threshold]
        tier.sort(key=_distance_order)
        nearby = [
            offer
            for offer in located
            if offer.distance_km is not None and offer.distance_km <= nearby_radius_km
        ]
        nearby.sort(key=_distance_order)
        tier_complete = full.complete or (
            threshold is not None
            and full.unfetched_min_price_rub is not None
            and full.unfetched_min_price_rub > threshold
        )
        if any(warning.endswith("row(s) skipped: unparseable price") for warning in full.warnings):
            tier_complete = False
        distance_complete = (
            tier_complete
            and origin.geocode_status == "ok"
            and all(offer.distance_km is not None for offer in tier)
        )
        note = (
            f"price tier {'complete' if tier_complete else 'partial'} "
            f"across {full.rows_fetched} loaded rows; "
            "nearby list uses only loaded matching physical offers with known coordinates, "
            "so a closer pharmacy on later pages can be missing. Distances are straight-line kilometres."
        )
        return CheapestNearResult(
            search=full.model_copy(update={"offers": [], "warnings": warnings}),
            origin=origin,
            min_price_rub=minimum,
            price_tolerance_rub=price_tolerance_rub,
            price_tier_complete=tier_complete,
            distance_ranking_complete=distance_complete,
            cheapest=tier[:top_k],
            nearby=nearby[:top_k],
            nearby_radius_km=nearby_radius_km,
            coverage_note=note,
        )


# ---------------------------------------------------------------------- helpers


def _geocode_status(point: GeocodeResult) -> Literal["ok", "not_found", "skipped"]:
    return "skipped" if point.status == "unavailable" else point.status


def _distance_order(offer: NearbyOffer) -> tuple[float, Decimal, str]:
    distance = offer.distance_km if offer.distance_km is not None else math.inf
    return distance, offer.price_rub, offer.pharmacy_name_address_raw


def _price_floor(rows: list[RawRow]) -> Decimal | None:
    """Last price when the fetched rows ascend by price, as the site sorts them; None if they do not."""
    prices: list[Decimal] = []
    for r in rows:
        try:
            prices.append(nz.parse_price(r.price_raw))
        except ValueError:
            continue
    if not prices or any(a > b for a, b in itertools.pairwise(prices)):
        return None
    return prices[-1]


def _status(p: Pharmacy, now: dt.datetime, stale_days: float) -> PharmacyStatus:
    age = (now - p.prices_updated_at).total_seconds() / 3600 if p.prices_updated_at is not None else None
    return PharmacyStatus(
        **p.model_dump(),
        price_list_age_hours=round(age, 1) if age is not None else None,
        is_stale=age is None or age > stale_days * 24,
    )


def _signature(rows: list[RawRow]) -> tuple[tuple[str, str, str], ...]:
    return tuple((r.product_name_raw, r.price_raw, r.pharmacy_raw) for r in rows)


def _identity(r: RawRow) -> tuple[str, ...]:
    """Everything visible in a row except its position."""
    return (
        r.product_name_raw,
        r.quantity_raw,
        r.price_raw,
        r.pharmacy_raw,
        r.pharmacy_href or "",
        r.phone_raw or "",
        r.date_raw,
    )


def _build_offers(rows: list[RawRow], query: str, mode: MatchMode) -> tuple[list[Offer], int, int]:
    offers: list[Offer] = []
    seen: set[tuple[str, str, Decimal]] = set()
    bad = dupes = 0
    for r in rows:
        if not nz.is_match(query, r.product_name_raw, mode):
            continue
        try:
            price = nz.parse_price(r.price_raw)
        except ValueError:
            bad += 1
            continue
        where = str(r.pharmacy_id) if r.pharmacy_id is not None else nz.fold(r.pharmacy_raw)
        key = (where, nz.fold(r.product_name_raw), price)
        if key in seen:
            dupes += 1
            continue
        seen.add(key)
        attrs = nz.extract_attrs(r.product_name_raw)
        offers.append(
            Offer(
                product_name_raw=r.product_name_raw,
                product_key=attrs.product_key,
                pack_size=attrs.pack_size,
                dosage=attrs.dosage,
                form=attrs.form,
                route=attrs.route,
                price_rub=price,
                quantity_raw=r.quantity_raw,
                quantity=nz.parse_quantity(r.quantity_raw),
                offer_kind=nz.offer_kind(r.pharmacy_href, r.pharmacy_id, r.pharmacy_raw),
                pharmacy_name_address_raw=r.pharmacy_raw,
                pharmacy_id=r.pharmacy_id,
                pharmacy_url=nz.safe_url(r.pharmacy_href),
                phone_raw=r.phone_raw,
                phone_digits=nz.phone_digits(r.phone_raw),
                stock_date=nz.parse_stock_date(r.date_raw),
                stock_date_raw=r.date_raw,
                relevance=nz.match_score(query, r.product_name_raw),
            )
        )
    return offers, bad, dupes


def _group(offers: list[Offer]) -> list[ProductGroup]:
    by_key: dict[str, list[Offer]] = {}
    for o in offers:
        by_key.setdefault(o.product_key, []).append(o)
    groups = [
        ProductGroup(
            product_key=k,
            sample_name_raw=v[0].product_name_raw,
            offers_count=len(v),
            min_price_rub=min(o.price_rub for o in v),
            max_price_rub=max(o.price_rub for o in v),
        )
        for k, v in by_key.items()
    ]
    groups.sort(key=lambda g: (g.min_price_rub, g.product_key))
    return groups
