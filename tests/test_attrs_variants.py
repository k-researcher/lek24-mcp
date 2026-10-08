import pytest

from lek24_mcp.normalize import extract_attrs, is_match


@pytest.mark.parametrize(
    "name, expected_pack_size",
    [
        ("Но-шпа табл 40 мг x48", 48),
        ("Но-шпа таб 40мг х 48 (Опелла Хелскеа Венгрия Лтд)", 48),
        ("Нурофен форте табл п/о 400 мг х12", 12),
        ("Но-шпа таблетки 40мг 48 шт. (Опелла Хелскеа Венгрия Лтд.)", 48),
        ("Нурофен форте 400мг. 12шт. таблетки покрытые оболочкой", 12),
        ("НО-ШПА 40 МГ N48 ТАБЛ (*ОПЕЛЛА ХЕЛСКЕА ООО*)", 48),
        ("Но-шпа таб. 40мг №48(Хиноин)", 48),
        ("Хилак форте капли 100мл", None),
    ],
)
def test_extract_attrs_pack_size(name: str, expected_pack_size: int | None) -> None:
    attrs = extract_attrs(name)
    assert attrs.pack_size == expected_pack_size


@pytest.mark.parametrize(
    "name, expected_form, expected_key_prefix",
    [
        (
            "Нурофен форте, тбл п/о 400мг №12 (Рекитт Бенкизер Хелс кер Интернешнл ЛТД)",
            "таблетки",
            "v3:нурофен форте|n12|400мг|таблетки|",
        ),
        (
            "Нурофен форте 400мг. тб. №24 (Рекитт Бенкизер Хэлскзар Лтд)",
            "таблетки",
            "v3:нурофен форте|n24|400мг|таблетки|",
        ),
        (
            "Но-шпа табл 40 мг x48",
            "таблетки",
            "v3:но шпа|n48|40мг|таблетки|",
        ),
        (
            "Но-шпа таблетки 40мг 48 шт. (Опелла Хелскеа Венгрия Лтд.)",
            "таблетки",
            "v3:но шпа|n48|40мг|таблетки|",
        ),
        (
            "НО-ШПА 40МГ. №48 ТАБ. БЛИСТЕР /ОПЕЛЛА/",
            "таблетки",
            "v3:но шпа|n48|40мг|таблетки|",
        ),
    ],
)
def test_extract_attrs_form_and_key(name: str, expected_form: str, expected_key_prefix: str) -> None:
    attrs = extract_attrs(name)
    assert attrs.form == expected_form
    assert attrs.product_key.startswith(expected_key_prefix)


def test_product_key_grouping() -> None:
    names = [
        "Но-шпа табл 40 мг x48",
        "Но-шпа таб 40мг х 48 (Опелла Хелскеа Венгрия Лтд)",
        "Но-шпа таблетки 40мг 48 шт. (Опелла Хелскеа Венгрия Лтд.)",
        "Но-шпа таб. 40мг №48(Хиноин)",
    ]
    keys = {extract_attrs(name).product_key for name in names}
    assert len(keys) == 1
    assert None not in keys


@pytest.mark.parametrize(
    "name, expected_dosage",
    [
        ("Но-шпа 0,04 №48 табл.", "40мг"),
        ("Но-шпа 0,04 табл. №24", "40мг"),
        ("Аквалор 0,9% спрей 50мл", "0.9%"),
        ("Мазь 0,05 туба 15г", "15г"),
    ],
)
def test_extract_attrs_dosage(name: str, expected_dosage: str) -> None:
    attrs = extract_attrs(name)
    assert attrs.dosage == expected_dosage


@pytest.mark.parametrize(
    "query, name, expected",
    [
        ("Но-шпа 40 мг №48", "Но-шпа табл 40 мг x48", True),
        ("Но-шпа 40 мг №48", "Но-шпа таб 40мг х 24 (Опелла)", False),
        ("Но-шпа 40 мг", "Но-шпа 0,04 №48 табл.", True),
        ("Но-шпа №48", "Но-шпа таблетки 40мг 48 шт.", True),
    ],
)
def test_is_match_tokens(query: str, name: str, expected: bool) -> None:
    assert is_match(query, name, mode="tokens") == expected


def test_real_noshpa_page_pack_sizes() -> None:
    import json
    from pathlib import Path

    names = json.loads(
        (Path(__file__).parent / "fixtures" / "product_names.json").read_text(encoding="utf-8")
    )
    with_48 = [n for n in names["search_noshpa_48"] if "48" in n]
    assert with_48
    for name in with_48:
        assert extract_attrs(name).pack_size == 48, name


def test_real_nurofen_page_forms() -> None:
    import json
    from pathlib import Path

    names = json.loads(
        (Path(__file__).parent / "fixtures" / "product_names.json").read_text(encoding="utf-8")
    )
    for name in names["search_nurofen_forte"]:
        attrs = extract_attrs(name)
        assert attrs.form is not None, name
        assert attrs.product_key.startswith("v3:нурофен форте|"), name
