"""Build a local coverage snapshot independently of MCP requests."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import re
import shlex
import sys
import tempfile
from pathlib import Path

import httpx2

from lek24_mcp.coverage import (
    NOTICE,
    SOURCE_PAGE,
    CoverageSnapshot,
    ReferenceSnapshot,
    locate_and_match,
    read_archive,
    save_snapshot,
    snapshot_path,
)
from lek24_mcp.geo import Geocoder
from lek24_mcp.matching_hints import add_hints
from lek24_mcp.models import ErrorCode, Lek24Error
from lek24_mcp.service import SearchService


async def download_register(path: Path) -> tuple[str, dt.date, dt.datetime]:
    async with httpx2.AsyncClient(
        timeout=90, follow_redirects=False, headers={"User-Agent": "lek24-mcp/0.1 coverage snapshot"}
    ) as client:
        page = await client.get(SOURCE_PAGE)
        page.raise_for_status()
        paths = re.findall(r"/opendata/7710537160-ls_licenses/data-(\d{8})-structure-(\d{8})\.zip", page.text)
        if not paths:
            raise Lek24Error(ErrorCode.PARSER_CONTRACT_CHANGED, "No dated license archive on official page")
        date, structure = max(paths)
        published = dt.datetime.strptime(date, "%Y%m%d").date()
        url = f"https://roszdravnadzor.gov.ru/opendata/7710537160-ls_licenses/data-{date}-structure-{structure}.zip"
        size = 0
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with path.open("wb") as stream:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 50_000_000:
                        raise Lek24Error(ErrorCode.PARSER_CONTRACT_CHANGED, "License archive too large")
                    stream.write(chunk)
        return url, published, dt.datetime.now(dt.UTC)


async def build(args: argparse.Namespace, service: SearchService) -> Path:
    directory = await service.list_pharmacies(
        city_id=args.city_id, region_id=args.region_id, force_refresh=True
    )
    if not directory.pharmacies:
        raise Lek24Error(ErrorCode.PARSER_CONTRACT_CHANGED, "Empty pharmacy directory; snapshot unchanged")
    locations = await service.list_locations(force_refresh=True)
    registry = await service._registry()
    warnings = [NOTICE, *directory.warnings]
    with tempfile.TemporaryDirectory(prefix="lek24-coverage-") as folder:
        fetched: dt.datetime | None = None
        source_date: dt.date | None = None
        if args.licenses:
            source = Path(args.licenses)
            source_url = SOURCE_PAGE
            warnings.append("License register read from a local file; its publication date is unknown.")
        else:
            source = Path(folder) / "licenses.zip"
            source_url, source_date, fetched = await download_register(source)
        sites = read_archive(source, directory.city_name)
        print(f"Retail places: {len(sites)}; resolving districts...", file=sys.stderr)
        geo = Geocoder(cache_only=not args.geocode, min_interval=15)
        try:
            sites = await locate_and_match(sites, directory, geo)
        finally:
            await geo.aclose()
        if args.matching_helper:
            try:
                command = shlex.split(args.matching_helper)
            except ValueError:
                warnings.append("Optional matching helper command invalid; ordinary matching completed.")
            else:
                sites, hint_warnings = await add_hints(sites, command)
                warnings.extend(hint_warnings)
        unlocated = sum(site.district is None for site in sites)
        if unlocated:
            warnings.append(
                f"District unknown for {unlocated} places; excluded from district-filtered lists."
            )
        snapshot = CoverageSnapshot(
            city_id=args.city_id,
            region_id=args.region_id,
            city_name=directory.city_name,
            built_at=dt.datetime.now(dt.UTC),
            licenses_source_url=source_url,
            licenses_source_date=source_date,
            licenses_fetched_at=fetched,
            directory_source_url=directory.source_url,
            directory_fetched_at=directory.fetched_at,
            warnings=warnings,
            sites=sites,
            pharmacies=list(directory.pharmacies),
            districts=locations.districts if args.city_id == 0 else [],
        )
        path = snapshot_path(args.city_id)
        references = ReferenceSnapshot(
            fetched_at=registry.fetched_at,
            locations_fetched_at=dt.datetime.now(dt.UTC),
            source_url=registry.source_url,
            locations=locations,
            pharmacies=registry.pharmacies,
        )
        save_snapshot(references, path.parent / "references-v1.json")
        save_snapshot(snapshot, path)
    return path


def main(argv: list[str], service: SearchService) -> None:
    parser = argparse.ArgumentParser(prog="lek24-mcp build-coverage")
    parser.add_argument("--city-id", type=int, default=0)
    parser.add_argument("--region-id", type=int, default=0)
    parser.add_argument(
        "--geocode",
        action="store_true",
        help="Resolve uncached business addresses online (15s between requests)",
    )
    parser.add_argument("--licenses", help="Local official XML/ZIP instead of download")
    parser.add_argument(
        "--matching-helper", help="Optional executable command; failures do not stop the build"
    )
    args = parser.parse_args(argv)
    try:
        path = asyncio.run(build(args, service))
    except (Lek24Error, OSError, httpx2.HTTPError) as exc:
        parser.exit(1, f"Coverage build failed: {exc}\n")
    print(path)
