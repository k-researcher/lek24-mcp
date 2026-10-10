"""Offline retail-license coverage snapshots and conservative directory matching."""

from __future__ import annotations

import datetime as dt
import difflib
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import IO, Literal
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import iterparse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from lek24_mcp.geo import Geocoder, normalize_address
from lek24_mcp.models import (
    District,
    ErrorCode,
    Lek24Error,
    Locations,
    Pharmacy,
    PharmacyList,
    PharmacyStatus,
)

SOURCE_PAGE = "https://roszdravnadzor.gov.ru/opendata/7710537160-ls_licenses"
NOTICE = "Лицензия не подтверждает, что аптека сейчас открыта; адреса реестра могут быть неточными."


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class MatchingHint(Record):
    pharmacy_id: int
    choice: Literal["same", "different", "unknown"]
    confidence: float = Field(ge=0, le=1, strict=True)


class LicenseSite(Record):
    license_numbers: list[str]
    inn: str
    names: list[str]
    address: str
    street: str
    code_fias: str
    district: str | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    geocode_status: Literal["ok", "not_found", "skipped", "unavailable"] = "skipped"
    address_uncertain: bool = False
    status: Literal["matched", "missing", "uncertain"] = "uncertain"
    candidates: list[Pharmacy] = Field(default_factory=list)
    hints: list[MatchingHint] = Field(default_factory=list)


class ReferenceSnapshot(Record):
    version: Literal[1] = 1
    fetched_at: AwareDatetime
    locations_fetched_at: AwareDatetime
    source_url: str
    locations: Locations
    pharmacies: list[Pharmacy]


class CoverageSnapshot(Record):
    version: Literal[1] = 1
    city_id: int = Field(ge=0)
    region_id: int = Field(ge=0)
    city_name: str
    built_at: AwareDatetime
    licenses_source_url: str
    licenses_source_date: dt.date | None
    licenses_fetched_at: AwareDatetime | None
    directory_source_url: str
    directory_fetched_at: AwareDatetime
    warnings: list[str]
    sites: list[LicenseSite]
    pharmacies: list[Pharmacy]
    districts: list[District]


class CoverageGaps(Record):
    city_id: int
    district_id: int
    city_name: str
    district_name: str | None
    snapshot_built_at: AwareDatetime
    licenses_source_url: str
    licenses_source_date: dt.date | None
    licenses_fetched_at: AwareDatetime | None
    directory_source_url: str
    directory_fetched_at: AwareDatetime
    prices_checked_at: AwareDatetime | None
    directory_refreshed: bool
    stale_check_available: bool
    missing: list[LicenseSite]
    stale: list[PharmacyStatus]
    uncertain: list[LicenseSite]
    missing_count: int
    stale_count: int
    uncertain_count: int
    next_offset: int | None
    matched_count: int
    unlocated_count: int
    warnings: list[str]


def _text(parent: Element, tag: str) -> str:
    return " ".join((parent.findtext(tag) or "").split())


def _fold(value: str) -> str:
    return " ".join(value.casefold().replace("ё", "е").split())


def city_matches(place: Element, city: str) -> bool:
    structured = re.sub(r"^(?:город\s+|г\.?\s*)", "", _fold(_text(place, "city")))
    if structured:
        return structured == _fold(city)
    return bool(
        re.search(
            r"(?:\bг\.?\s*|\bгород\s+)" + re.escape(_fold(city)) + r"(?=[\s,.;]|$)",
            _fold(_text(place, "address")),
        )
    )


def clean_street(value: str, city: str = "") -> tuple[str, bool]:
    """Keep the source address; remove premises only from the street used for matching."""
    value = value.replace("«", "").replace("»", "").replace('"', "")
    if city:
        marker = re.search(r"(?:\bг\.?\s*|\bгород\s+)" + re.escape(city) + r"(?=[\s,.;]|$)", value, re.I)
        if marker:
            value = value[marker.end() :].lstrip(" ,.")
    value = re.sub(r"^[^,]*\bрайон\s*,\s*", "", value, flags=re.I)
    uncertain = bool(re.search(r"\b(?:корп\w*|строен\w*|стр\.)|\d\s*/\s*\d", value, re.I))
    value = re.split(
        r"[,;]?\s*(?:часть\b|нежил\w*\b|помещ\w*\b|пом\.|комнат\w*\b|этаж\b)", value, maxsplit=1, flags=re.I
    )[0]
    value = re.sub(r"\b(?:дом|д\.)\s*|№\s*", "", value, flags=re.I)
    value = re.sub(r"78-о[йя]", "78-й", value, flags=re.I)
    value = re.sub(r"\bим\.\s*газеты", "имени газеты", value, flags=re.I)
    value = re.sub(r"\bпр\.(?=имени)", "проспект ", value, flags=re.I)
    value = normalize_address(value.replace(",", " ").strip(" ."))
    if not re.search(r"\d", value) or len(value) < 4:
        uncertain = True
    return value, uncertain


def address_key(value: str) -> str:
    street, _ = clean_street(value)
    street = re.sub(r"\b(?:улица|проспект|переулок)\b", "", _fold(street))
    return re.sub(r"[^\w/]+", " ", street).strip()


def read_retail_sites(stream: IO[bytes], city: str) -> list[LicenseSite]:
    sites: dict[tuple[str, str], LicenseSite] = {}
    root: Element | None = None
    licenses = 0
    try:
        for event, node in iterparse(stream, events=("start", "end"), forbid_dtd=True):
            node.tag = node.tag.rsplit("}", 1)[-1]
            if root is None:
                root = node
                if root.tag != "licenses_list":
                    raise ValueError("unexpected license-register root")
            if event != "end" or node.tag != "licenses":
                continue
            licenses += 1
            if not _text(node, "termination") and not _text(node, "date_termination"):
                names = list(
                    dict.fromkeys(
                        filter(
                            None,
                            (
                                _text(node, t)
                                for t in (
                                    "brand_name_licensee",
                                    "abbreviated_name_licensee",
                                    "full_name_licensee",
                                )
                            ),
                        )
                    )
                )
                for place in node.findall("./work_address_list/address_place"):
                    retail = any(
                        "розничная торговля лекарственными препаратами" in _fold(w.text or "")
                        and "ветеринар" not in _fold(w.text or "")
                        for w in place.findall("./works/work")
                    )
                    if not retail or not city_matches(place, city):
                        continue
                    address = _text(place, "address")
                    street, uncertain = clean_street(_text(place, "street") or address, city)
                    inn = _text(node, "inn")
                    # Full addresses keep premises and slash numbers distinct during deduplication.
                    key = (inn, _fold(address))
                    number = _text(node, "number")
                    if key in sites:
                        old = sites[key]
                        sites[key] = old.model_copy(
                            update={
                                "license_numbers": list(dict.fromkeys([*old.license_numbers, number])),
                                "names": list(dict.fromkeys([*old.names, *names])),
                            }
                        )
                    else:
                        sites[key] = LicenseSite(
                            license_numbers=[number],
                            inn=inn,
                            names=names,
                            address=address,
                            street=street,
                            code_fias=_text(place, "code_fias"),
                            address_uncertain=uncertain or not address,
                        )
            node.clear()
            if root is not node:
                root.remove(node)
    except Exception as exc:
        raise Lek24Error(ErrorCode.PARSER_CONTRACT_CHANGED, "Cannot parse the license register") from exc
    if not licenses or not sites:
        raise Lek24Error(
            ErrorCode.PARSER_CONTRACT_CHANGED, f"No retail places found for {city}; snapshot unchanged"
        )
    return list(sites.values())


def read_archive(path: Path, city: str) -> list[LicenseSite]:
    if path.suffix.lower() == ".xml":
        with path.open("rb") as stream:
            return read_retail_sites(stream, city)
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) != 1 or not members[0].filename.lower().endswith(".xml"):
                raise ValueError("expected one XML member")
            if members[0].file_size > 500_000_000 or members[0].flag_bits & 1:
                raise ValueError("oversized or encrypted register")
            with archive.open(members[0]) as stream:
                return read_retail_sites(stream, city)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
        raise Lek24Error(ErrorCode.PARSER_CONTRACT_CHANGED, "Invalid license archive") from exc


def _brand(value: str) -> str:
    value = re.sub(
        r"\b(?:общество с ограниченной ответственностью|ооо|ао|пао|аптечная сеть|аптека)\b", "", _fold(value)
    )
    return re.sub(r"[^\w]+", " ", value).strip()


def match_site(site: LicenseSite, pharmacies: list[Pharmacy]) -> LicenseSite:
    key = address_key(site.street)
    exact = [p for p in pharmacies if address_key(p.address) == key]
    candidates = exact
    if not candidates and key:
        house = re.search(r"\d+[а-яa-z]?$", key)
        candidates = [
            p
            for p in pharmacies
            if house
            and address_key(p.address).endswith(house.group())
            and difflib.SequenceMatcher(None, key, address_key(p.address)).ratio() >= 0.78
        ]
    names = [_brand(name) for name in site.names]
    brand = len(exact) == 1 and any(
        len(n) >= 4
        and (
            n == _brand(exact[0].name)
            or f" {n} " in f" {_brand(exact[0].name)} "
            or (f" {_brand(exact[0].name)} " in f" {n} " and len(_brand(exact[0].name)) >= 4)
        )
        for n in names
    )
    status: Literal["matched", "missing", "uncertain"] = "uncertain"
    if not candidates and not site.address_uncertain:
        status = "missing"
    elif brand and not site.address_uncertain and not clean_street(exact[0].address)[1]:
        status = "matched"
    return site.model_copy(update={"status": status, "candidates": candidates})


async def locate_and_match(
    sites: list[LicenseSite], directory: PharmacyList, geo: Geocoder
) -> list[LicenseSite]:
    result = []
    for site in sites:
        if not site.street:
            result.append(match_site(site, list(directory.pharmacies)))
            continue
        point = await geo.geocode(site.street, directory.city_name)
        district = point.district
        if point.status == "ok" and point.lat is not None and point.lon is not None and not district:
            reverse = await geo.reverse(point.lat, point.lon)
            district = reverse.district
        located = site.model_copy(
            update={"lat": point.lat, "lon": point.lon, "district": district, "geocode_status": point.status}
        )
        matched = match_site(located, list(directory.pharmacies))
        if matched.district is None and matched.status == "matched":
            matched = matched.model_copy(update={"district": matched.candidates[0].district})
        result.append(matched)
    return result


def snapshot_path(city_id: int, directory: Path | None = None) -> Path:
    base = directory or Path(os.getenv("LEK24_COVERAGE_DIR", "~/.cache/lek24-mcp")).expanduser()
    return base / f"coverage-{city_id}.json"


def save_snapshot(snapshot: Record, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temp = stream.name
            stream.write(snapshot.model_dump_json(indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)


def load_snapshot(city_id: int, directory: Path | None = None) -> CoverageSnapshot:
    path = snapshot_path(city_id, directory)
    try:
        snapshot = CoverageSnapshot.model_validate_json(path.read_bytes())
        if snapshot.city_id != city_id or not snapshot.sites:
            raise ValueError("snapshot identity or content invalid")
        return snapshot
    except (OSError, ValueError, ValidationError) as exc:
        raise Lek24Error(
            ErrorCode.COVERAGE_UNAVAILABLE,
            f"Coverage snapshot missing or invalid. Run: lek24-mcp build-coverage --city-id {city_id}",
        ) from exc


def district_key(value: str | None) -> str:
    return _fold(value or "").replace(" район", "").strip()


def load_references() -> ReferenceSnapshot | None:
    path = snapshot_path(0).parent / "references-v1.json"
    try:
        record = ReferenceSnapshot.model_validate_json(path.read_bytes())
        return record if record.pharmacies and record.locations.cities else None
    except (OSError, ValueError):
        return None
