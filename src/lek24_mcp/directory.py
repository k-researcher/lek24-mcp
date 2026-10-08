"""Module for parsing the pharmacy directory from 24lek.ru."""

import contextlib
import datetime as dt
import re
import zoneinfo

from bs4 import BeautifulSoup

from lek24_mcp.models import ParserContractChanged, Pharmacy

PHARMACY_COLUMNS = ("Аптека", "Адрес", "Район", "Телефон", "Дата", "Город")
SITE_TZ = zoneinfo.ZoneInfo("Asia/Krasnoyarsk")


def parse_pharmacies(html: str) -> list[Pharmacy]:
    """Parse the list of pharmacies from the provided HTML content."""
    soup = BeautifulSoup(html, "lxml")

    # Find the table container
    table = soup.find("div", class_="table")
    if not table:
        raise ParserContractChanged("pharmacy table not found")

    header = table.find("div", class_="th")
    if not header:
        raise ParserContractChanged("pharmacy table header not found")

    # Check header columns
    header_spans = header.find_all("span", recursive=False)
    header_texts = tuple(" ".join(s.get_text(" ", strip=True).split()) for s in header_spans)
    if header_texts != PHARMACY_COLUMNS:
        raise ParserContractChanged(f"header mismatch: {header_texts}")

    pharmacies: list[Pharmacy] = []
    seen_ids: set[int] = set()

    # rows: every direct child div except the header
    rows = [d for d in table.find_all("div", recursive=False) if "th" not in (d.get("class") or [])]

    for i, row in enumerate(rows):
        spans = row.find_all("span", recursive=False)
        if len(spans) != 6:
            raise ParserContractChanged(f"pharmacy row {i}: expected 6 spans, got {len(spans)}")

        # Extraction logic
        name = " ".join(spans[0].get_text(" ", strip=True).split())
        address = " ".join(spans[1].get_text(" ", strip=True).split())
        district_raw = " ".join(spans[2].get_text(" ", strip=True).split())
        phone_raw = " ".join(spans[3].get_text(" ", strip=True).split())
        date_raw = " ".join(spans[4].get_text(" ", strip=True).split())
        city = " ".join(spans[5].get_text(" ", strip=True).split())

        if not name or not address or not city:
            raise ParserContractChanged(f"pharmacy row {i}: missing mandatory fields")

        # ID extraction from address link
        link = spans[1].find("a")
        if not link or not link.get("href"):
            raise ParserContractChanged(f"pharmacy row {i}: missing address link")

        href = link.get("href")
        match = re.search(r"[?&]apteka=(\d+)", href) if isinstance(href, str) else None
        if not match:
            raise ParserContractChanged(f"pharmacy row {i}: pharmacy id not found in href")

        p_id = int(match.group(1))
        if p_id in seen_ids:
            raise ParserContractChanged(f"duplicate pharmacy id {p_id}")
        seen_ids.add(p_id)

        # Optional fields
        district = district_raw if district_raw else None
        phone = phone_raw if phone_raw else None

        # Date parsing
        prices_updated_at = None
        with contextlib.suppress(ValueError):
            prices_updated_at = dt.datetime.strptime(date_raw, "%d.%m.%Y %H:%M:%S").replace(tzinfo=SITE_TZ)

        pharmacies.append(
            Pharmacy(
                id=p_id,
                name=name,
                address=address,
                district=district,
                phone_raw=phone,
                city=city,
                prices_updated_at_raw=date_raw,
                prices_updated_at=prices_updated_at,
            )
        )

    if not pharmacies:
        raise ParserContractChanged("empty pharmacy registry")

    # Verify count from main_content
    main_content = soup.find("div", class_="main_content")
    if main_content:
        for div in main_content.find_all("div", recursive=False):
            span = div.find("span")
            # Russian plural varies with the number: аптека / аптеки / аптек
            if span and span.get_text(strip=True).lower().startswith("аптек"):
                strong = div.find("strong")
                if strong:
                    count_text = strong.get_text(strip=True)
                    if not count_text.isdigit():
                        raise ParserContractChanged(f"registry count is not a number: {count_text!r}")
                    expected_count = int(count_text)
                    if expected_count != len(pharmacies):
                        raise ParserContractChanged(
                            f"registry count mismatch: page says {expected_count}, parsed {len(pharmacies)}"
                        )
                break

    return pharmacies
