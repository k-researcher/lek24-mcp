"""Data contracts shared by every layer.

Raw* models carry exactly what the upstream HTML contained (already stripped of tags and
whitespace-trimmed). Normalized models add typed values but always keep the raw strings,
so nothing the site said is lost or invented.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OfferKind = Literal["online", "physical", "unknown"]
MatchMode = Literal["tokens", "strict", "raw"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ErrorCode(StrEnum):
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_RATE_LIMITED = "upstream_rate_limited"
    UPSTREAM_REFUSED = "upstream_refused"
    PARSER_CONTRACT_CHANGED = "parser_contract_changed"
    UNSUPPORTED_LOCATION = "unsupported_location"
    INVALID_ARGUMENT = "invalid_argument"


class Lek24Error(Exception):
    """Structured error surfaced to MCP clients as {code, message}."""

    def __init__(self, code: ErrorCode, message: str, *, status: int | None = None) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message
        self.status = status


class ParserContractChanged(Lek24Error):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.PARSER_CONTRACT_CHANGED, message)


# ---------------------------------------------------------------------------
# Parser output (raw, untrusted text only)
# ---------------------------------------------------------------------------


class RawRow(_Frozen):
    """One <div id='articleN'> row of #maintable or of an action2.php fragment."""

    position: int = Field(description="0-based index of the row inside the page/fragment it came from")
    product_name_raw: str
    quantity_raw: str
    price_raw: str
    pharmacy_raw: str = Field(description="Visible text of the pharmacy cell (name + address or site)")
    pharmacy_href: str | None = Field(description="Exact href of the pharmacy link, if any")
    pharmacy_id: int | None = Field(description="N from javascript:open_in_map(N); None otherwise")
    phone_raw: str | None
    date_raw: str


class PageMeta(_Frozen):
    total: int | None = Field(description="'Найдено: N' / num_rows; None if absent")
    code: str | None = Field(description="Send['code'] from inline JS; '' and None both mean 'no code'")
    page_size: int | None = Field(description="'var num = N' from inline JS")
    site_updated_at_raw: str | None = Field(description="'Последнее обновление: ...' value, raw")
    query_echo: str | None = Field(description="Send['a'] — query as the site understood it")


class ParsedSearchPage(_Frozen):
    meta: PageMeta
    rows: list[RawRow]
    no_results: bool = Field(description="True only when the page positively shows an empty result")


class ParsedFragment(_Frozen):
    rows: list[RawRow]
    is_end: bool = Field(description="Body was empty / whitespace or exactly '0'")


class Region(_Frozen):
    id: int
    name: str


class City(_Frozen):
    id: int
    name: str
    region_id: int | None


class District(_Frozen):
    id: int
    name: str


class Locations(_Frozen):
    regions: list[Region]
    cities: list[City]
    districts: list[District] = Field(description="Districts of Krasnoyarsk (city_id=0) from #raon_select")


# ---------------------------------------------------------------------------
# Normalized product facts
# ---------------------------------------------------------------------------


class ProductAttrs(_Frozen):
    normalized_name: str
    pack_size: int | None = Field(description="From N30 / №30 / № 30")
    dosage: str | None = Field(description="Normalized like '1000мг', '3.2г'; None if not found")
    form: str | None = Field(description="Normalized dosage-form keyword, e.g. 'пастилки'")
    route: str | None = Field(description="Where it is applied: 'нос', 'горло', 'глаза', 'уши', 'местно'")
    product_key: str = Field(
        description="Synthetic local key 'v2:<brand>|<pack>|<dosage>|<form>|<route>' — NOT an upstream id"
    )


# ---------------------------------------------------------------------------
# Service output
# ---------------------------------------------------------------------------


class Offer(_Frozen):
    product_name_raw: str
    product_key: str
    pack_size: int | None
    dosage: str | None
    form: str | None
    route: str | None
    price_rub: Decimal
    quantity_raw: str
    quantity: Decimal | None
    offer_kind: OfferKind
    pharmacy_name_address_raw: str
    pharmacy_id: int | None
    pharmacy_url: str | None = Field(description="Exact upstream http(s) href; javascript: hrefs are dropped")
    phone_raw: str | None
    phone_digits: str | None
    stock_date: dt.date | None
    stock_date_raw: str
    relevance: float = Field(ge=0.0, le=1.0)


class SearchResult(_Frozen):
    query: str
    city_id: int
    region_id: int
    district_id: int
    source_url: str
    fetched_at: dt.datetime
    site_updated_at_raw: str | None
    source_total: int | None
    rows_fetched: int
    matched_count: int
    complete: bool
    incomplete_reason: str | None
    unfetched_min_price_rub: Decimal | None = Field(
        description="Only when incomplete: the site lists offers by ascending price, so every unfetched row "
        "costs at least this; null if the listing was not price-ordered"
    )
    pages_fetched: int
    cached: bool
    warnings: list[str]
    offers: list[Offer]


class ProductGroup(_Frozen):
    product_key: str = Field(description="Synthetic local grouping key, NOT an upstream id")
    sample_name_raw: str
    offers_count: int
    min_price_rub: Decimal
    max_price_rub: Decimal


class CheapestResult(_Frozen):
    search: SearchResult = Field(description="Underlying search; offers are the top_k cheapest")
    coverage_note: str = Field(description="Human-readable statement of how complete the ranking is")
    groups: list[ProductGroup]
