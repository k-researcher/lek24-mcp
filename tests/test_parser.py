from pathlib import Path

import pytest

from lek24_mcp.models import ParserContractChanged
from lek24_mcp.parser import (
    EXPECTED_COLUMNS,
    parse_fragment,
    parse_locations,
    parse_search_page,
    parse_suggestions,
)


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


def test_search_isla_moos(fixtures_dir):
    html = (fixtures_dir / "search_isla_moos.html").read_text(encoding="utf-8")
    page = parse_search_page(html)

    assert page.meta.total == 40
    assert page.meta.code is None
    assert page.meta.page_size == 50
    assert page.meta.query_echo == "Исла Моос"
    assert page.meta.site_updated_at_raw == "08.10.2026 08:45:06"
    assert page.no_results is False

    # First row
    row0 = page.rows[0]
    assert row0.price_raw == "783.00"
    assert row0.quantity_raw == "98"
    assert row0.pharmacy_href.startswith("https://apteka.ru/")
    assert row0.pharmacy_id is None
    assert row0.phone_raw == "8-800-700-88-88"
    assert row0.date_raw == "08.10.2026"
    assert "&" in row0.product_name_raw
    assert "&amp;" not in row0.product_name_raw

    # Last row (40th row is index 39)
    row39 = page.rows[39]
    assert row39.pharmacy_id == 918
    assert row39.price_raw == "1,284.99"
    assert len(page.rows) == 40
    assert [r.position for r in page.rows] == list(range(40))


def test_search_isla_mint(fixtures_dir):
    html = (fixtures_dir / "search_isla_mint.html").read_text(encoding="utf-8")
    page = parse_search_page(html)
    assert len(page.rows) == page.meta.total


def test_search_isla_broad(fixtures_dir):
    html = (fixtures_dir / "search_isla_broad.html").read_text(encoding="utf-8")
    page = parse_search_page(html)
    assert page.meta.total == 1357
    assert page.meta.code == "ec6617ca44"
    assert len(page.rows) == 50


def test_parse_fragment_page2(fixtures_dir):
    body = (fixtures_dir / "page2_isla_broad_num50.html").read_text(encoding="utf-8")
    fragment = parse_fragment(body)
    assert len(fragment.rows) == 50
    assert [r.position for r in fragment.rows] == list(range(50))
    assert fragment.rows[0].product_name_raw.startswith("РАДОГРАД")
    assert fragment.rows[0].price_raw == "132.99"
    assert fragment.rows[0].pharmacy_id == 833
    assert fragment.is_end is False


@pytest.mark.parametrize("content", ["", "0", "  \n"])
def test_parse_fragment_end(content):
    fragment = parse_fragment(content)
    assert fragment.is_end is True
    assert len(fragment.rows) == 0


def test_search_empty(fixtures_dir):
    html = (fixtures_dir / "search_empty.html").read_text(encoding="utf-8")
    page = parse_search_page(html)
    assert page.no_results is True
    assert len(page.rows) == 0
    assert page.meta.total == 0


def test_locations(fixtures_dir):
    html = (fixtures_dir / "index.html").read_text(encoding="utf-8")
    locs = parse_locations(html)

    assert len(locs.regions) == 4
    assert len(locs.cities) == 57
    assert len(locs.districts) == 8

    # District 0 = "Все районы"
    assert locs.districts[0].name == "Все районы"

    # City id 0 = "Красноярск" with region_id 0
    city0 = next(c for c in locs.cities if c.id == 0)
    assert city0.name == "Красноярск"
    assert city0.region_id == 0

    # Two cities "Дудинка" with different ids
    dudinkas = [c for c in locs.cities if c.name == "Дудинка"]
    assert len(dudinkas) == 2
    assert dudinkas[0].id != dudinkas[1].id


def test_suggestions(fixtures_dir):
    text = (fixtures_dir / "suggest_isla.txt").read_text(encoding="utf-8")
    assert parse_suggestions(text) == ["Исландика", "Исландский"]


HEADER = "<div class='th'>" + "".join(f"<span>{c}</span>" for c in EXPECTED_COLUMNS) + "</div>"


def test_synthetic_errors(fixtures_dir):
    # 1. HTML without maintable
    with pytest.raises(ParserContractChanged, match="maintable not found"):
        parse_search_page("<div>no table</div>")

    # 2. Row with 5 spans
    spans = "".join(f"<span>{i}</span>" for i in range(5))
    bad_row_html = f"<div id='maintable'>{HEADER}<div id='article1'>{spans}</div></div>"
    with pytest.raises(ParserContractChanged, match="expected 6 spans, got 5"):
        parse_search_page(bad_row_html)

    # 3. Table without rows, but total > 0
    no_rows_but_total = (
        """
    <div id='maintable'>"""
        + HEADER
        + """</div>
    <script>var num_rows = "5";</script>
    """
    )
    with pytest.raises(ParserContractChanged, match="Empty table but total > 0"):
        parse_search_page(no_rows_but_total)

    # 4. Table without rows and without total
    no_rows_no_total = f"<div id='maintable'>{HEADER}</div>"
    with pytest.raises(ParserContractChanged, match="Empty table but total > 0"):
        parse_search_page(no_rows_no_total)

    # 5. Non-empty fragment without rows
    with pytest.raises(ParserContractChanged, match="non-empty fragment without rows"):
        parse_fragment("<p>oops</p>")
