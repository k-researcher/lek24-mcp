import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from lek24_mcp.directory import PHARMACY_COLUMNS, parse_pharmacies
from lek24_mcp.models import ParserContractChanged


@pytest.fixture
def real_apteki_html() -> str:
    """Fixture for the real apteki.html file."""
    path = Path(__file__).parent / "fixtures" / "apteki.html"
    return path.read_text(encoding="utf-8")


def test_real_page_parsing(real_apteki_html: str) -> None:
    """Test parsing the real apteki.html file."""
    pharmacies = parse_pharmacies(real_apteki_html)

    assert len(pharmacies) == 597

    # Check unique IDs
    ids = [p.id for p in pharmacies]
    assert len(ids) == len(set(ids))

    # Check specific pharmacy
    adonis = next((p for p in pharmacies if p.id == 1044), None)
    assert adonis is not None
    assert adonis.name == "Адонис"
    assert adonis.address == "Металлургов 8 А"
    assert adonis.district == "Советский"
    assert adonis.phone_raw == "299-40-72"
    assert adonis.city == "Красноярск"
    assert adonis.prices_updated_at_raw == "08.10.2026 14:01:11"
    expected_dt = dt.datetime(2026, 10, 8, 14, 1, 11, tzinfo=ZoneInfo("Asia/Krasnoyarsk"))
    assert adonis.prices_updated_at == expected_dt


def test_real_page_statistics(real_apteki_html: str) -> None:
    """Test statistics from the real apteki.html file."""
    pharmacies = parse_pharmacies(real_apteki_html)

    krasnoyarsk_pharmacies = [p for p in pharmacies if p.city == "Красноярск"]
    assert len(krasnoyarsk_pharmacies) == 364

    sovetsky_krasnoyarsk = [p for p in krasnoyarsk_pharmacies if p.district == "Советский"]
    assert len(sovetsky_krasnoyarsk) == 108

    cities = {p.city for p in pharmacies}
    assert len(cities) == 57


def test_synthetic_errors() -> None:
    """Test various error conditions using synthetic HTML."""

    # 1. Misaligned headers
    html_wrong_header = """
    <div class='table'>
      <div class='th'><span>Wrong</span><span>Address</span><span>District</span><span>Phone</span><span>Date</span><span>City</span></div>
      <div><span>N</span><span><a href="?apteka=1">A</a></span><span>D</span><span>P</span><span>T</span><span>C</span></div>
    </div>
    """
    with pytest.raises(ParserContractChanged, match="header mismatch"):
        parse_pharmacies(html_wrong_header)

    # 2. Row with 5 spans instead of 6
    html_wrong_spans = """
    <div class='table'>
      <div class='th'><span>Аптека</span><span>Адрес</span><span>Район</span><span>Телефон</span><span>Дата</span><span>Город</span></div>
      <div><span>N</span><span><a href="?apteka=1">A</a></span><span>D</span><span>P</span><span>T</span></div>
    </div>
    """
    with pytest.raises(ParserContractChanged, match="expected 6 spans, got 5"):
        parse_pharmacies(html_wrong_spans)

    # 3. Link without apteka=
    html_no_id = """
    <div class='table'>
      <div class='th'><span>Аптека</span><span>Адрес</span><span>Район</span><span>Телефон</span><span>Дата</span><span>Город</span></div>
      <div><span>N</span><span><a href="karta.php">A</a></span><span>D</span><span>P</span><span>T</span><span>C</span></div>
    </div>
    """
    with pytest.raises(ParserContractChanged, match="pharmacy id not found in href"):
        parse_pharmacies(html_no_id)

    # 4. Duplicate IDs
    html_duplicates = """
    <div class='table'>
      <div class='th'><span>Аптека</span><span>Адрес</span><span>Район</span><span>Телефон</span><span>Дата</span><span>Город</span></div>
      <div><span>N1</span><span><a href="?apteka=1">A1</a></span><span>D</span><span>P</span><span>T</span><span>C</span></div>
      <div><span>N2</span><span><a href="?apteka=1">A2</a></span><span>D</span><span>P</span><span>T</span><span>C</span></div>
    </div>
    """
    with pytest.raises(ParserContractChanged, match="duplicate pharmacy id 1"):
        parse_pharmacies(html_duplicates)

    # 5. Count mismatch
    html_mismatch = """
    <div class='table'>
      <div class='th'><span>Аптека</span><span>Адрес</span><span>Район</span><span>Телефон</span><span>Дата</span><span>Город</span></div>
      <div><span>N1</span><span><a href="?apteka=1">A1</a></span><span>D</span><span>P</span><span>T</span><span>C</span></div>
    </div>
    <div class='main_content'>
      <div><strong>3</strong><span>аптеки</span></div>
    </div>
    """
    with pytest.raises(ParserContractChanged, match="registry count mismatch"):
        parse_pharmacies(html_mismatch)

    # 6. Empty district and phone
    html_empty_optional = """
    <div class='table'>
      <div class='th'><span>Аптека</span><span>Адрес</span><span>Район</span><span>Телефон</span><span>Дата</span><span>Город</span></div>
      <div><span>N1</span><span><a href="?apteka=1">A1</a></span><span></span><span></span><span>01.01.2026 00:00:00</span><span>C</span></div>
    </div>
    """
    pharmacies = parse_pharmacies(html_empty_optional)
    assert pharmacies[0].district is None
    assert pharmacies[0].phone_raw is None

    # 7. Invalid date
    html_bad_date = """
    <div class='table'>
      <div class='th'><span>Аптека</span><span>Адрес</span><span>Район</span><span>Телефон</span><span>Дата</span><span>Город</span></div>
      <div><span>N1</span><span><a href="?apteka=1">A1</a></span><span>D</span><span>P</span><span>31.02.2026 10:00:00</span><span>C</span></div>
    </div>
    """
    pharmacies = parse_pharmacies(html_bad_date)
    assert pharmacies[0].prices_updated_at is None
    assert pharmacies[0].prices_updated_at_raw == "31.02.2026 10:00:00"

    # 8. No table
    html_no_table = "<div>No table here</div>"
    with pytest.raises(ParserContractChanged, match="pharmacy table not found"):
        parse_pharmacies(html_no_table)


def test_empty_registry() -> None:
    """Test empty registry case."""
    html_empty = """
    <div class='table'>
      <div class='th'><span>Аптека</span><span>Адрес</span><span>Район</span><span>Телефон</span><span>Дата</span><span>Город</span></div>
    </div>
    """
    with pytest.raises(ParserContractChanged, match="empty pharmacy registry"):
        parse_pharmacies(html_empty)


@pytest.mark.parametrize("word", ["аптек", "аптека", "Аптеки"])
def test_count_check_handles_plural_forms(word: str) -> None:
    head = "<div class='th'>" + "".join(f"<span>{c}</span>" for c in PHARMACY_COLUMNS) + "</div>"
    row = (
        "<div><span>А</span><span><a href='karta.php?apteka=1'>Ул 1</a></span><span></span>"
        "<span></span><span>08.10.2026 10:00:00</span><span>Город</span></div>"
    )
    html = (
        f"<div class='table'>{head}{row}</div>"
        f"<div class='main_content'><div><strong>5</strong><span>{word}</span></div></div>"
    )
    with pytest.raises(ParserContractChanged, match="count mismatch"):
        parse_pharmacies(html)
