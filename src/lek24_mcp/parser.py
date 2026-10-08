"""Structural HTML parser for 24lek.ru search results."""

import re

from bs4 import BeautifulSoup, Tag

from lek24_mcp.models import (
    City,
    District,
    Locations,
    PageMeta,
    ParsedFragment,
    ParsedSearchPage,
    ParserContractChanged,
    RawRow,
    Region,
)


def _text(el: Tag) -> str:
    """Visible text with entities decoded and whitespace collapsed."""
    return " ".join(el.get_text(" ", strip=True).split())


def _attr(el: Tag, name: str) -> str | None:
    """Single-valued attribute (bs4 returns multi-valued ones like rel/class as lists)."""
    v = el.get(name)
    if v is None:
        return None
    return v if isinstance(v, str) else (v[0] if v else None)


def _row_divs(container: Tag) -> list[Tag]:
    return [d for d in container.find_all("div", id=_ROW_ID, recursive=False) if isinstance(d, Tag)]


_ROW_ID = re.compile(r"^article\d+$")
# Column order is the contract; a moved or added column must fail loudly, not shift fields.
EXPECTED_COLUMNS = ("Наименование товара", "Кол-во", "Цена", "Аптека", "Телефон", "Дата")
_FOUND = re.compile(r"Найдено:\s*(\d(?:[\d\s,]*\d)?)")
_JS_TOTAL = re.compile(r'num_rows\s*=\s*"(\d+)"')


def _check_header(maintable: Tag) -> None:
    th = next((d for d in maintable.find_all("div", recursive=False) if "th" in (d.get("class") or [])), None)
    if not isinstance(th, Tag):
        raise ParserContractChanged("table header not found")
    labels = tuple(_text(sp) for sp in th.find_all("span", recursive=False))
    if labels != EXPECTED_COLUMNS:
        raise ParserContractChanged(f"unexpected table columns: {labels}")


def _total(page_text: str, html: str) -> int | None:
    """'Найдено: N' (thousand separators allowed) cross-checked against JS num_rows."""
    found_m = _FOUND.search(page_text)
    found = int(re.sub(r"[\s,]", "", found_m.group(1))) if found_m else None
    js_m = _JS_TOTAL.search(html)
    js = int(js_m.group(1)) if js_m else None
    if found is not None and js is not None and found != js:
        raise ParserContractChanged(f"result count mismatch: 'Найдено: {found}' vs num_rows={js}")
    return found if found is not None else js


def _parse_row(div: Tag, position: int) -> RawRow:
    """Parses a single row div into a RawRow."""
    spans = div.find_all("span", recursive=False)
    if len(spans) != len(EXPECTED_COLUMNS):
        raise ParserContractChanged(f"row {position}: expected 6 spans, got {len(spans)}")

    product_name_raw = _text(spans[0])
    quantity_raw = _text(spans[1])
    price_raw = _text(spans[2])

    if not product_name_raw or not price_raw:
        raise ParserContractChanged(f"row {position}: missing product_name or price")

    # Pharmacy cell [3]
    pharmacy_cell = spans[3]
    pharmacy_raw = _text(pharmacy_cell)

    pharmacy_href: str | None = None
    a_tag = pharmacy_cell.find("a")
    if isinstance(a_tag, Tag) and (href := _attr(a_tag, "href")):
        pharmacy_href = href.strip()

    pharmacy_id: int | None = None
    if pharmacy_href:
        match = re.fullmatch(r"javascript:open_in_map\((\d+)\)", pharmacy_href)
        if match:
            pharmacy_id = int(match.group(1))

    # Phone cell [4]
    phone_raw: str | None = None
    phone_text = _text(spans[4])
    if phone_text:
        phone_raw = phone_text

    # Date cell [5]
    date_raw = _text(spans[5])

    return RawRow(
        position=position,
        product_name_raw=product_name_raw,
        quantity_raw=quantity_raw,
        price_raw=price_raw,
        pharmacy_raw=pharmacy_raw,
        pharmacy_href=pharmacy_href,
        pharmacy_id=pharmacy_id,
        phone_raw=phone_raw,
        date_raw=date_raw,
    )


def parse_search_page(html: str) -> ParsedSearchPage:
    """Parses a full search results page."""
    soup = BeautifulSoup(html, "lxml")
    maintable = soup.find(id="maintable")
    if not maintable:
        raise ParserContractChanged("maintable not found")

    if not isinstance(maintable, Tag):
        raise ParserContractChanged("maintable is not an element")
    _check_header(maintable)
    total = _total(soup.get_text(" "), html)

    code: str | None = None
    code_match = re.search(r"Send\['code'\]\s*=\s*\"([^\" ]*)\"", html)
    if code_match:
        code_val = code_match.group(1)
        code = code_val if code_val else None

    page_size: int | None = None
    page_size_match = re.search(r"var\s+num\s*=\s*(\d+)", html)
    if page_size_match:
        page_size = int(page_size_match.group(1))

    query_echo: str | None = None
    query_match = re.search(r"Send\['a'\]\s*=\s*\"((?:[^\"\\]|\\.)*)\"", html)
    if query_match:
        query_echo = query_match.group(1).replace('\\"', '"').replace("\\\\", "\\")

    site_updated_at_raw: str | None = None
    updated_match = re.search(r"Последнее обновление:\s*(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2})", html)
    if updated_match:
        site_updated_at_raw = updated_match.group(1)

    meta = PageMeta(
        total=total,
        code=code,
        page_size=page_size,
        query_echo=query_echo,
        site_updated_at_raw=site_updated_at_raw,
    )

    # Rows extraction
    rows: list[RawRow] = []
    for idx, div in enumerate(_row_divs(maintable)):
        rows.append(_parse_row(div, idx))

    # Consistency checks
    no_results = False
    if len(rows) == 0:
        if total is None or total > 0:
            raise ParserContractChanged("Empty table but total > 0 or total is None")
        no_results = True
    elif total == 0:
        raise ParserContractChanged("Rows found but total is 0")
    else:
        no_results = False

    return ParsedSearchPage(meta=meta, rows=rows, no_results=no_results)


def parse_fragment(body: str) -> ParsedFragment:
    """Parses an AJAX fragment (action2.php response)."""
    stripped_body = body.strip()
    if not stripped_body or stripped_body == "0":
        return ParsedFragment(rows=[], is_end=True)

    soup = BeautifulSoup(f"<div id='lek24root'>{body}</div>", "lxml")
    root = soup.find(id="lek24root")
    if not root:
        raise ParserContractChanged("lek24root not found in fragment")

    rows: list[RawRow] = []
    row_divs = _row_divs(root) if isinstance(root, Tag) else []
    if not row_divs:
        raise ParserContractChanged("non-empty fragment without rows")

    for idx, div in enumerate(row_divs):
        rows.append(_parse_row(div, idx))

    return ParsedFragment(rows=rows, is_end=False)


def parse_locations(html: str) -> Locations:
    """Parses location selection lists."""
    soup = BeautifulSoup(html, "lxml")

    def get_elements(selector: str, id_prefix: str) -> list[Tag]:
        container = soup.select_one(selector)
        if not container:
            return []
        found = container.find_all("a", id=re.compile(f"^{id_prefix}_\\d+$"))
        return [el for el in found if isinstance(el, Tag)]

    regions_els = get_elements("#kray_select", "kray")
    cities_els = get_elements("#city_select", "city")
    districts_els = get_elements("#raon_select", "raon")

    if not regions_els or not cities_els or not districts_els:
        raise ParserContractChanged("One or more location containers are missing or empty")

    regions = []
    for el in regions_els:
        rid_str = (_attr(el, "id") or "").split("_")[1]
        regions.append(Region(id=int(rid_str), name=_text(el)))

    cities = []
    for el in cities_els:
        cid_str = (_attr(el, "id") or "").split("_")[1]
        rel = _attr(el, "rel")
        region_id = int(rel) if rel is not None and rel.isdigit() else None
        cities.append(City(id=int(cid_str), name=_text(el), region_id=region_id))

    districts = []
    for el in districts_els:
        did_str = (_attr(el, "id") or "").split("_")[1]
        districts.append(District(id=int(did_str), name=_text(el)))

    return Locations(regions=regions, cities=cities, districts=districts)


def parse_suggestions(text: str) -> list[str]:
    """Parses suggestion text into a list of unique strings."""
    lines = text.splitlines()
    result = []
    seen = set()
    for line in lines:
        clean = line.strip()
        if clean and clean not in seen:
            result.append(clean)
            seen.add(clean)
    return result
