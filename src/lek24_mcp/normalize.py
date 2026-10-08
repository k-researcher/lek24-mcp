import datetime as dt
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal

from lek24_mcp.models import MatchMode, OfferKind, ProductAttrs

ALIASES: dict[str, str] = {
    "isla": "исла",
    "moos": "моос",
    "mint": "минт",
    "med": "мед",
    "voice": "войс",
}


def clean_text(s: str) -> str:
    """Normalize unicode and collapse whitespace. '№' becomes 'N' first: NFKC would turn it into 'No'."""
    s = unicodedata.normalize("NFKC", s.replace("№", "N"))
    return " ".join(s.split())


def fold(s: str) -> str:
    """Fold case, replace ё and №."""
    s = clean_text(s).casefold()
    s = s.replace("ё", "е")
    s = s.replace("№", "n")
    return s


def tokens(s: str) -> list[str]:
    """Split into alphanumeric tokens, normalizing decimal separators."""
    folded = fold(s)
    # Find all word-like or numeric-like tokens
    raw_tokens = re.findall(r"[a-zа-я]+|\d+(?:[.,]\d+)?", folded)
    result: list[str] = []
    for t in raw_tokens:
        # If it's a number, ensure dot separator
        if re.match(r"^\d+[.,]\d+$", t):
            result.append(t.replace(",", "."))
        else:
            result.append(t)
    return result


def parse_price(raw: str) -> Decimal:
    """Parse price string to Decimal(0.01)."""
    # Remove spaces and non-breaking spaces
    s = raw.replace(" ", "").replace("\xa0", "").strip()
    if not s:
        raise ValueError("Empty price")

    # Check for multiple separators
    # "1,284.99" -> comma is thousand, dot is decimal
    # "132,99" -> comma is decimal
    # "1,284" -> comma is thousand

    has_comma = "," in s
    has_dot = "." in s

    if has_comma and has_dot:
        # Assuming comma is thousand separator and dot is decimal
        # e.g. 1,284.99 -> 1284.99
        s = s.replace(",", "")
    elif has_comma:
        # Only comma. Check if it's a decimal (exactly 2 digits after)
        # or a thousand separator.
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) == 2:
            s = s.replace(",", ".")
        else:
            # Treat as thousand separator or integer
            s = s.replace(",", "")
    elif has_dot:
        # Only dot. Standard decimal.
        pass

    try:
        val = Decimal(s)
        if val < 0:
            raise ValueError("Negative price")
        return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception as e:
        raise ValueError(f"Invalid price format: {raw}") from e


def parse_quantity(raw: str) -> Decimal | None:
    """Parse quantity to Decimal. Return None if invalid/negative."""
    s = raw.replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        val = Decimal(s)
        if val < 0:
            return None
        return val
    except Exception:
        return None


def parse_stock_date(raw: str) -> dt.date | None:
    """Parse DD.MM.YYYY date."""
    s = clean_text(raw)
    try:
        return dt.datetime.strptime(s, "%d.%m.%Y").date()
    except ValueError:
        return None


def phone_digits(raw: str | None) -> str | None:
    """Extract digits from phone string."""
    if raw is None:
        return None
    digits = "".join(c for c in raw if c.isdigit())
    return digits if len(digits) >= 5 else None


def safe_url(href: str | None) -> str | None:
    """Validate URL scheme."""
    if href is None:
        return None
    s = href.strip()
    low = s.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return s
    return None


def offer_kind(pharmacy_href: str | None, pharmacy_id: int | None, pharmacy_raw: str) -> OfferKind:
    """Determine offer type: physical, online, or unknown."""
    if pharmacy_id is not None:
        return "physical"

    if safe_url(pharmacy_href) is not None:
        return "online"

    folded_raw = fold(pharmacy_raw)
    if "www." in folded_raw:
        return "online"

    # Match domain pattern: word.ru|com|рф|net|org
    if re.search(r"\b[a-z0-9-]+\.(ru|com|рф|net|org)\b", folded_raw):
        return "online"

    return "unknown"


def extract_attrs(product_name_raw: str) -> ProductAttrs:
    """Extract pack size, dosage, and form from name."""
    folded_name = fold(product_name_raw)

    # Pack size: N30, №30, № 30, n 30
    pack_size: int | None = None
    pack_match = re.search(r"(?<![a-zа-я0-9])n\s?(\d{1,4})(?!\d)", folded_name)
    if pack_match:
        pack_size = int(pack_match.group(1))

    # Dosage: 1000мг, 3.2г, 13.6г, 10%
    dosage: str | None = None
    # Dosage pattern: number + unit (mg, mcg, ml, me, g, %)
    # Units: мг, мкг, мл, ме, г, %
    dosage_match = re.search(r"(\d+(?:[.,]\d+)?)\s?(мг|мкг|мл|ме|г|%)(?![a-zа-я])", folded_name)
    if dosage_match:
        val_part = dosage_match.group(1).replace(",", ".")
        unit_part = dosage_match.group(2)
        dosage = f"{val_part}{unit_part}"

    form_idx = _find_form(product_name_raw)
    form = None
    toks = tokens(product_name_raw)
    if re.search(r"(?<![a-zа-я])р-р(?![a-zа-я])", folded_name):
        form = "раствор"
    elif form_idx is not None:
        form = _FORMS[form_idx[1]]

    # brand: leading alphabetic tokens until a number, a 1-letter token (n, ф/п, р-р) or a form (max 3)
    brand_parts: list[str] = []
    for i, t in enumerate(toks):
        if t[0].isdigit() or len(t) == 1 or (form_idx is not None and i == form_idx[0]) or _form_of(t):
            break
        brand_parts.append(t)
        if len(brand_parts) == 3:
            break
    # variant words after the form ("АКВА МАРИС СПРЕЙ НАЗАЛЬНЫЙ СТРОНГ") still name a different product;
    # bracketed text is the manufacturer and is skipped
    for t in tokens(_BRACKETED.sub(" ", product_name_raw)):
        if t in _VARIANTS and t not in brand_parts:
            brand_parts.append(t)
    brand = " ".join(brand_parts) or "-"
    route = _route(folded_name)

    # N1 on a spray or tube means one item, the same as no pack size at all
    pack = f"n{pack_size}" if pack_size not in (None, 1) else "-"
    key = "|".join([brand, pack, dosage or "-", form or "-", route or "-"])
    return ProductAttrs(
        normalized_name=folded_name,
        pack_size=pack_size,
        dosage=dosage,
        form=form,
        route=route,
        product_key=f"v2:{key}",
    )


_BRACKETED = re.compile(r"\([^)]*\)|\[[^\]]*\]")

# words that tell product lines of one brand apart (Аква Марис vs Аква Марис Стронг)
_VARIANTS = frozenset(
    {"стронг", "форте", "плюс", "макс", "беби", "бэби", "нео", "экстра", "ультра", "лайт", "кидс", "актив"}
)

_NOT_LETTER_BEFORE = r"(?<![a-zа-я])"
_NOT_LETTER_AFTER = r"(?![a-zа-я])"
# (route, pattern on the folded name); a name may name several routes, e.g. "для носа и горла"
_ROUTES: list[tuple[str, re.Pattern[str]]] = [
    ("нос", re.compile(rf"назал|интраназ|{_NOT_LETTER_BEFORE}наз{_NOT_LETTER_AFTER}|(?:для|д/)\s?нос")),
    ("горло", re.compile(r"горл")),
    ("глаза", re.compile(r"глазн|офтальм|(?:для|д/)\s?глаз")),
    ("уши", re.compile(rf"ушн|(?:для|д/)\s?уш{_NOT_LETTER_AFTER}")),
]
_LOCAL = re.compile(rf"местн|{_NOT_LETTER_BEFORE}мест{_NOT_LETTER_AFTER}")


def _route(folded_name: str) -> str | None:
    """Where the product is applied. 'для местного применения' alone is not assumed to be throat."""
    found = [r for r, p in _ROUTES if p.search(folded_name)]
    if found:
        return "+".join(found)
    return "местно" if _LOCAL.search(folded_name) else None


# prefix -> normalized form; 'паст' is handled as a whole-token abbreviation
_FORMS: dict[str, str] = {
    "пастил": "пастилки",
    "леденц": "леденцы",
    "табл": "таблетки",
    "таб": "таблетки",
    "капс": "капсулы",
    "капл": "капли",
    "сироп": "сироп",
    "спрей": "спрей",
    "раствор": "раствор",
    "мазь": "мазь",
    "крем": "крем",
    "гель": "гель",
    "порош": "порошок",
    "гранул": "гранулы",
    "суппоз": "суппозитории",
    "свеч": "суппозитории",
    "резин": "жевательная резинка",
    "чай": "чай",
    "сбор": "сбор",
}


def _form_of(token: str) -> str | None:
    """Return the _FORMS key matching this token, or None."""
    if token == "паст":
        return "пастил"
    return next((p for p in _FORMS if token.startswith(p)), None)


def _find_form(name: str) -> tuple[int, str] | None:
    """(token index, _FORMS key) of the earliest form token."""
    for i, t in enumerate(tokens(name)):
        if (k := _form_of(t)) is not None:
            return i, k
    return None


def match_score(query: str, product_name_raw: str) -> float:
    """Calculate match score between query and name."""
    q_tokens = tokens(query)
    if not q_tokens:
        return 0.0

    # Deduplicate query tokens while preserving order
    seen = set()
    unique_q_tokens: list[str] = []
    for t in q_tokens:
        # Apply ALIASES
        t_alias = ALIASES.get(t.lower(), t.lower())
        if t_alias not in seen:
            unique_q_tokens.append(t_alias)
            seen.add(t_alias)

    p_tokens = set(tokens(product_name_raw))

    matches = 0
    for qt in unique_q_tokens:
        # Direct match
        if qt in p_tokens:
            matches += 1
            continue

        # Prefix match for long alphabetical tokens
        if len(qt) >= 5 and qt.isalpha() and any(pt.startswith(qt) for pt in p_tokens):
            matches += 1
            continue

    return matches / len(unique_q_tokens)


_DOSAGE = re.compile(r"(\d+(?:[.,]\d+)?)\s?(мг|мкг|мл|ме|г|%)(?![a-zа-я])")


def _dosages(name: str) -> set[str]:
    return {m.group(1).replace(",", ".") + m.group(2) for m in _DOSAGE.finditer(fold(name))}


def _attrs_compatible(query: str, product_name_raw: str) -> bool:
    """Dosages / pack size written in the query must be the product's own, not numbers elsewhere."""
    q_dosages = _dosages(query)
    q_pack = extract_attrs(query).pack_size
    if not q_dosages and q_pack is None:
        return True
    if not q_dosages <= _dosages(product_name_raw):
        return False
    return q_pack is None or q_pack == extract_attrs(product_name_raw).pack_size


def is_match(query: str, product_name_raw: str, mode: MatchMode) -> bool:
    """Determine if query matches name based on mode."""
    if mode == "raw":
        return True
    if not _attrs_compatible(query, product_name_raw):
        return False

    score = match_score(query, product_name_raw)
    if mode == "tokens":
        return score == 1.0

    if mode == "strict":
        # Every token in query (after ALIASES) must be in p (no prefixes)
        q_tokens = tokens(query)
        if not q_tokens:
            return False

        p_tokens = set(tokens(product_name_raw))
        for qt in q_tokens:
            qt_alias = ALIASES.get(qt.lower(), qt.lower())
            if qt_alias not in p_tokens:
                return False
        return True

    return False


def query_warnings(query: str) -> list[str]:
    """Return warnings for ambiguous queries."""
    q_tokens = tokens(query)
    # Check for 'исла' and 'мос'
    has_isla = any(t == "исла" for t in q_tokens)
    has_mos = any(t == "мос" for t in q_tokens)

    if has_isla and has_mos:
        return ["Возможно, имелось в виду «Исла Моос»: на сайте товар пишется «МООС». Запрос не изменён."]

    return []
