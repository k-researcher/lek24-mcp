import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

from lek24_mcp.normalize import (
    clean_text,
    extract_attrs,
    fold,
    is_match,
    offer_kind,
    parse_price,
    parse_quantity,
    parse_stock_date,
    phone_digits,
    query_warnings,
    safe_url,
    tokens,
)

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "product_names.json"


@pytest.fixture
def product_names() -> dict[str, list[str]]:
    with open(FIXTURES_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_clean_text_and_fold():
    assert clean_text("  Hello \xa0 World  ") == "Hello World"
    assert fold("ИСЛА МООС №30") == "исла моос n30"
    assert fold("ёлка") == "елка"


def test_tokens():
    assert tokens("ИСЛА МООС №30 ПАСТ.") == ["исла", "моос", "n", "30", "паст"]
    assert tokens("3,2Г") == ["3.2", "г"]
    assert tokens("10,00") == ["10.00"]


def test_parse_price():
    assert parse_price("783.00") == Decimal("783.00")
    assert parse_price("1,284.99") == Decimal("1284.99")
    assert parse_price("132,99") == Decimal("132.99")
    assert parse_price("1 284.99") == Decimal("1284.99")
    assert parse_price("1,284") == Decimal("1284.00")

    with pytest.raises(ValueError):
        parse_price("")
    with pytest.raises(ValueError):
        parse_price("abc")
    with pytest.raises(ValueError):
        parse_price("-5")


def test_parse_quantity():
    assert parse_quantity("98") == Decimal("98")
    assert parse_quantity("1,5") == Decimal("1.5")
    assert parse_quantity("1.5") == Decimal("1.5")
    assert parse_quantity("") is None
    assert parse_quantity("abc") is None
    assert parse_quantity("-1") is None


def test_parse_stock_date():
    assert parse_stock_date("01.01.2026") == dt.date(2026, 1, 1)
    assert parse_stock_date("31.02.2026") is None
    assert parse_stock_date("not a date") is None
    assert parse_stock_date("1/1/2026") is None


def test_phone_digits():
    assert phone_digits("8-800-700-88-88") == "88007008888"
    assert phone_digits("123") is None
    assert phone_digits(None) is None
    assert phone_digits("abc") is None


def test_safe_url():
    assert safe_url("https://example.com") == "https://example.com"
    assert safe_url("HTTP://EXAMPLE.COM") == "HTTP://EXAMPLE.COM"
    assert safe_url("javascript:open_in_map(918)") is None
    assert safe_url(None) is None


def test_offer_kind():
    assert offer_kind("https://pharmacy.ru", 123, "Pharmacy Name") == "physical"
    assert offer_kind("https://pharmacy.ru", None, "Pharmacy Name") == "online"
    assert offer_kind(None, None, "www.pharmacy.ru") == "online"
    assert offer_kind(None, None, "pharmacy.ru") == "online"
    assert offer_kind(None, None, "just name") == "unknown"


def test_extract_attrs():
    # Test 1: Full attributes
    name1 = "ИСЛА МООС N30 ПАСТИЛ МАССОЙ 1000МГ (Engelhard Arzneimittel GmbH & Co.KG)"
    attrs1 = extract_attrs(name1)
    assert attrs1.pack_size == 30
    assert attrs1.dosage == "1000мг"
    assert attrs1.form == "пастилки"
    assert attrs1.product_key == "v3:исла моос|n30|1000мг|пастилки|-"

    # Test 2: Basic key
    name2 = "ИСЛА МООС №30 ПАСТ. (БАД)"
    attrs2 = extract_attrs(name2)
    assert attrs2.pack_size == 30
    assert attrs2.dosage is None
    assert attrs2.form == "пастилки"
    assert attrs2.product_key == "v3:исла моос|n30|-|пастилки|-"

    # Test 3: No dosage (just number in context of pack/other)
    name3 = "Исландский мох ф/п 2,0 №20 (Камелия (г.Москва))"
    attrs3 = extract_attrs(name3)
    assert attrs3.pack_size == 20
    assert attrs3.dosage is None

    # Test 4: Complex name with dosage and form
    name4 = "РАДОГРАД ЛЕДЕНЦЫ С ИСЛАНДСКИМ МХОМ И СОЛОДКОЙ №10 МЯТА/ИМБИРЬ ЛЕДЕНЦЫ ПО 3,2Г"
    attrs4 = extract_attrs(name4)
    assert attrs4.pack_size == 10
    assert attrs4.dosage == "3.2г"
    assert attrs4.form == "леденцы"


def test_product_key_separates_route_and_variant():
    nose = [
        "Аква Марис стронг спрей назальный 30мл.",
        "Аква марис стронг спрей наз. 30мл",
        "Аква марис стронг спрей 30мл флакон для носа (Ядран)",
        "АКВА МАРИС СПРЕЙ НАЗАЛЬНЫЙ СТРОНГ 30МЛ (*ЯДРАН ООО*)",  # variant word after the form
        "Аква марис стронг спрей наз. 30мл №1 (Ядран Галенский Лабораторий АО)",  # N1 == no pack size
    ]
    throat = [
        "Аква-марис стронг спрей для горла 30мл",
        "Аква-Марис стронг спрей 30мл д/горла (Jadran Co)",
        "АКВА МАРИС СТРОНГ 30МЛ. СПРЕЙ Д/МЕСТ. ПРИМ. (Д/ГОРЛА)",
    ]
    assert {extract_attrs(n).product_key for n in nose} == {"v3:аква марис стронг|-|30мл|спрей|нос"}
    assert {extract_attrs(n).product_key for n in throat} == {"v3:аква марис стронг|-|30мл|спрей|горло"}
    assert extract_attrs(nose[-1]).pack_size == 1
    # "для местного применения" alone is not assumed to be throat
    assert extract_attrs("Аква Марис стронг спрей для местного применения 30мл.").route == "местно"
    # plain Аква Марис must not merge with Стронг; manufacturer text in brackets is not a variant
    assert extract_attrs("Аква Марис спрей назальный 30мл").product_key == "v3:аква марис|-|30мл|спрей|нос"
    assert extract_attrs("Пастилки 30 (Форте Фарма)").product_key.startswith("v3:-|")


def test_match_logic(product_names):
    # 1. All 40 names in search_isla_moos
    for n in product_names["search_isla_moos"]:
        assert is_match("Исла Моос", n, "tokens") is True
        assert is_match("Isla Moos", n, "tokens") is True

    # 2. Negative matches
    # "Исла Моос" (tokens) should not match Isla_mos, Isla_broad, etc.
    for n in product_names["search_isla_mos"]:
        assert is_match("Исла Моос", n, "tokens") is False
    for n in product_names["search_isla_broad"]:
        assert is_match("Исла Моос", n, "tokens") is False
    for n in product_names["page2_isla_broad_num50"]:
        assert is_match("Исла Моос", n, "tokens") is False

    # Check "исла" vs "исландск" / "кисл"
    # "Исландский мох" (after fold) contains "исландск", which shouldn't match "исла" in strict/tokens mode?
    # Actually, tokens("Исла") is ["исла"]. tokens("Исландский") is ["исландский"].
    # "исла" is not in ["исландский", "мох"]. So no match.
    for n in product_names["search_isla_broad"]:
        # Even if 'исландск' starts with 'исла', tokens("исла") is exactly "исла".
        # match_score uses prefix match for long tokens (len >= 5).
        # "исла" has len 4. So it must be an exact token match.
        assert is_match("Исла", n, "tokens") is False

    # 3. Isla Mint
    for n in product_names["search_isla_mint"]:
        assert is_match("Исла Моос", n, "tokens") is False
        assert is_match("Исла Минт", n, "tokens") is True


def test_query_warnings():
    assert len(query_warnings("Исла Мос")) > 0
    assert query_warnings("Исла Моос") == []
