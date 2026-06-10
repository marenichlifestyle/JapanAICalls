from __future__ import annotations

import re
import unicodedata

LATIN_DIGIT_RE = re.compile(r"[A-Za-z0-9]")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
TOKEN_SPLIT_RE = re.compile(r"[\s/]+")
PUNCT_RE = re.compile(r"[^\w\s]")
NON_WORD_EDGE_RE = re.compile(r"^[^\w]+|[^\w]+$")
INTERNAL_CODE_RE = re.compile(r"^(?:[A-Z]{2,}\d{3,}|MP\d{3,}|M\d{3,})$")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
NON_ALNUM_UNDERSCORE_RE = re.compile(r"[^\w]+")

MULTIWORD_BRANDS = {
    ("mercedes", "benz"),
    ("land", "rover"),
    ("alfa", "romeo"),
    ("aston", "martin"),
    ("rolls", "royce"),
}

BRAND_SPOKEN_RULES: list[tuple[re.Pattern[str], str, list[str]]] = [
    (re.compile(r"\bbmw\b|ｂｍｗ|ビーエムダブリュー", re.IGNORECASE), "бэ эм вэ", ["бмв", "бэ эм вэ"]),
    (
        re.compile(r"mercedes|benz|メルセデス|ベンツ", re.IGNORECASE),
        "мерседес бенц",
        ["мерседес", "бенц"],
    ),
    (re.compile(r"\bmini\b|ミニ", re.IGNORECASE), "мини", ["мини"]),
    (re.compile(r"\baudi\b|アウディ", re.IGNORECASE), "ауди", ["ауди"]),
    (re.compile(r"\blexus\b|レクサス", re.IGNORECASE), "лексус", ["лексус"]),
    (re.compile(r"\btoyota\b|トヨタ", re.IGNORECASE), "тойота", ["тойота"]),
]

INTRO_STOP_MARKERS = {
    "package",
    "paket",
    "pkg",
    "select",
    "rent",
    "renta",
    "trade-in",
    "tradein",
    "apple",
    "carplay",
    "camera",
    "comfort",
    "plus",
    "option",
    "options",
    "premium",
    "premiumplus",
    "premium-plus",
    "premium+",
    "owner",
    "head-up",
    "display",
    "roof",
    "driving",
    "panoramic",
    "panorama",
    "sunroof",
    "digital",
    "leather",
    "seat",
    "seats",
    "heated",
    "ventilated",
    "entertainment",
    "monitor",
    "system",
    "mbux",
    "certified",
    "used",
    "pre-owned",
    "preowned",
    "dct",
    "本革",
    "サンルーフ",
    "パノラマ",
    "パッケージ",
    "ワンオーナー",
    "禁煙車",
    "シートヒーター",
    "シートベンチレーション",
    "アップルカープレイ",
    "ドラレコ",
    "デジタルミラー",
    "三列",
    "３列",
    "панорамный",
    "панорама",
    "пакет",
    "люк",
    "подогрев",
    "камера",
    "экстерьер",
    "диски",
    "владелец",
    "селект",
    "рента",
    "драйвинг",
    "премиум",
    "гласс",
    "санруф",
    "аллойные",
    "колеса",
    "сертифицированный",
    "подержанный",
    "трейд-ин",
}


_UNITS = {
    0: "ноль",
    1: "один",
    2: "два",
    3: "три",
    4: "четыре",
    5: "пять",
    6: "шесть",
    7: "семь",
    8: "восемь",
    9: "девять",
}
_TEENS = {
    10: "десять",
    11: "одиннадцать",
    12: "двенадцать",
    13: "тринадцать",
    14: "четырнадцать",
    15: "пятнадцать",
    16: "шестнадцать",
    17: "семнадцать",
    18: "восемнадцать",
    19: "девятнадцать",
}
_TENS = {
    2: "двадцать",
    3: "тридцать",
    4: "сорок",
    5: "пятьдесят",
    6: "шестьдесят",
    7: "семьдесят",
    8: "восемьдесят",
    9: "девяносто",
}
_HUNDREDS = {
    1: "сто",
    2: "двести",
    3: "триста",
    4: "четыреста",
    5: "пятьсот",
    6: "шестьсот",
    7: "семьсот",
    8: "восемьсот",
    9: "девятьсот",
}


def _triplet_to_words(n: int) -> list[str]:
    parts: list[str] = []
    h = n // 100
    t = (n % 100) // 10
    u = n % 10

    if h:
        parts.append(_HUNDREDS[h])

    if t == 1:
        parts.append(_TEENS[10 + u])
        return parts

    if t >= 2:
        parts.append(_TENS[t])
    if u:
        parts.append(_UNITS[u])

    return parts


def number_to_russian_words(n: int) -> str:
    if n == 0:
        return _UNITS[0]

    if n < 0:
        return "минус " + number_to_russian_words(abs(n))

    millions = n // 1_000_000
    thousands = (n % 1_000_000) // 1_000
    rest = n % 1_000

    parts: list[str] = []

    if millions:
        parts.extend(_triplet_to_words(millions))
        if millions % 10 == 1 and millions % 100 != 11:
            parts.append("миллион")
        elif millions % 10 in {2, 3, 4} and millions % 100 not in {12, 13, 14}:
            parts.append("миллиона")
        else:
            parts.append("миллионов")

    if thousands:
        parts.extend(_triplet_to_words(thousands))
        parts.append("тысяч")

    if rest:
        parts.extend(_triplet_to_words(rest))

    return " ".join(parts)


def jpy_to_spoken_ru(value: int | None) -> str | None:
    if value is None:
        return None
    return f"{number_to_russian_words(value)} иен"


def number_to_english_words(n: int) -> str:
    if n == 0:
        return "zero"
    if n < 0:
        return "minus " + number_to_english_words(abs(n))

    units = [
        "zero",
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
    ]
    teens = {
        10: "ten",
        11: "eleven",
        12: "twelve",
        13: "thirteen",
        14: "fourteen",
        15: "fifteen",
        16: "sixteen",
        17: "seventeen",
        18: "eighteen",
        19: "nineteen",
    }
    tens = {
        2: "twenty",
        3: "thirty",
        4: "forty",
        5: "fifty",
        6: "sixty",
        7: "seventy",
        8: "eighty",
        9: "ninety",
    }

    def triplet(value: int) -> str:
        parts: list[str] = []
        h = value // 100
        t = (value % 100) // 10
        u = value % 10
        if h:
            parts.extend([units[h], "hundred"])
        if t == 1:
            parts.append(teens[10 + u])
            return " ".join(parts)
        if t >= 2:
            parts.append(tens[t])
        if u:
            parts.append(units[u])
        return " ".join(parts)

    billions = n // 1_000_000_000
    millions = (n % 1_000_000_000) // 1_000_000
    thousands = (n % 1_000_000) // 1_000
    rest = n % 1_000
    parts: list[str] = []
    if billions:
        parts.extend([triplet(billions), "billion"])
    if millions:
        parts.extend([triplet(millions), "million"])
    if thousands:
        parts.extend([triplet(thousands), "thousand"])
    if rest:
        parts.append(triplet(rest))
    return normalize_spaces(" ".join([x for x in parts if x])) or ""


def usd_to_spoken_en(value: int | None) -> str | None:
    if value is None:
        return None
    word = "dollar" if value == 1 else "dollars"
    return f"{number_to_english_words(value)} {word}"


def contains_latin_or_digits(value: str | None) -> bool:
    if not value:
        return False
    return bool(LATIN_DIGIT_RE.search(value))


def contains_cyrillic(value: str | None) -> bool:
    if not value:
        return False
    return bool(CYRILLIC_RE.search(value))


def normalize_spaces(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", value).strip()


def normalize_spoken_text(value: str) -> str:
    text = (value or "").lower().strip()
    text = text.replace("ё", "е")
    text = text.replace("¥", " иен ")
    text = text.replace("йен", "иен")
    text = PUNCT_RE.sub(" ", text)
    # Canonicalize russian number unit forms to avoid false mismatches
    # like "тысячи" vs "тысяч" in spoken normalization checks.
    text = re.sub(r"\bтысяч(?:а|и)?\b", "тысяч", text)
    text = re.sub(r"\bмиллион(?:а|ов)?\b", "миллион", text)
    text = re.sub(r"\bмиллиард(?:а|ов)?\b", "миллиард", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def validate_price_spoken_text(price_used_jpy: int | None, spoken: str) -> tuple[bool, str]:
    if not isinstance(price_used_jpy, int) or price_used_jpy <= 0:
        return False, "price_used_jpy must be integer > 0"

    normalized = normalize_spoken_text(spoken)
    if not normalized:
        return False, "price_used_spoken_ru must be non-empty"

    if contains_latin_or_digits(normalized):
        return False, "price_used_spoken_ru must not contain latin letters or digits"

    return True, ""


def ensure_ien_in_spoken_price(spoken: str) -> str:
    base = normalize_spaces(spoken) or ""
    if not base:
        return "иен"
    normalized = normalize_spoken_text(base)
    if "иен" in normalized:
        fixed = base.replace("¥", "иен")
        fixed = re.sub(r"\bйен\b", "иен", fixed, flags=re.IGNORECASE)
        return normalize_spaces(fixed) or "иен"
    return f"{base} иен"


def car_name_to_spoken_ru(value: str) -> str:
    normalized = value.strip()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.replace("Mercedes-Benz", "Mercedes Benz")
    normalized = normalized.replace("E-Class", "EClass")

    token_map = {
        "bmw": "бэ эм вэ",
        "mercedes": "мерседес",
        "benz": "бенц",
        "gls": "глс",
        "eclass": "е класс",
        "series": "серия",
        "mini": "мини",
        "cooper": "купер",
        "exclusive": "эксклюзив",
        "avantgarde": "авангард",
        "xdrive": "икс драйв",
        "s": "эс",
        "i": "ай",
    }

    spoken_parts: list[str] = []
    for raw in TOKEN_SPLIT_RE.split(normalized.replace("—", "-").replace("–", "-").strip()):
        if not raw:
            continue
        token = NON_WORD_EDGE_RE.sub("", raw.lower())
        if token in token_map:
            spoken_parts.append(token_map[token])
            continue
        if re.fullmatch(r"\d{2,4}[a-zA-Z]", token):
            digits = int(token[:-1])
            letter = token[-1].lower()
            spoken_parts.append(f"{number_to_russian_words(digits)} {letter_to_ru(letter)}")
            continue
        if re.fullmatch(r"[a-zA-Z]\d{2,4}", token):
            letter = token[0].lower()
            digits = int(token[1:])
            spoken_parts.append(f"{letter_to_ru(letter)} {number_to_russian_words(digits)}")
            continue
        if re.fullmatch(r"\d+", token):
            spoken_parts.append(number_to_russian_words(int(token)))
            continue
        cleaned = re.sub(r"[^A-Za-zА-Яа-я0-9-]", "", token)
        if cleaned and re.search(r"[А-Яа-я]", cleaned):
            spoken_parts.append(cleaned)
    return normalize_spaces(" ".join(spoken_parts)) or ""


def compact_car_name_for_call(value: str | None, max_tokens: int = 7) -> str | None:
    if not value:
        return value
    text = normalize_spaces(value) or ""
    if not text:
        return value

    tokens = [t for t in re.split(r"\s+", text) if t]

    stop_markers = set(INTRO_STOP_MARKERS)
    stop_markers.update({"trade", "in", "one"})

    compact: list[str] = []
    seen_first: set[str] = set()
    for token in tokens:
        cleaned = NON_WORD_EDGE_RE.sub("", token.lower())
        cleaned = cleaned.replace("_", "-")
        cleaned_no_dash = cleaned.replace("-", "")
        if INTERNAL_CODE_RE.fullmatch(token):
            continue
        has_marker = cleaned in stop_markers or cleaned_no_dash in stop_markers
        if not has_marker:
            has_marker = any(
                marker in token
                for marker in stop_markers
                if len(marker) >= 3 and re.search(r"[^\x00-\x7f]", marker)
            )
        if has_marker:
            break
        if compact and cleaned == NON_WORD_EDGE_RE.sub("", compact[-1].lower()):
            continue
        if len(compact) < 2:
            # Avoid duplicated brand at the beginning: "MINI MINI Cooper"
            if cleaned in seen_first:
                continue
            seen_first.add(cleaned)
        if cleaned in stop_markers:
            break
        compact.append(token)
        if len(compact) >= max_tokens:
            break

    if not compact:
        compact = tokens[:max_tokens]
    return " ".join(compact)


def _norm_brand_token(value: str) -> str:
    return NON_ALNUM_RE.sub("", value.lower())


def _extract_brand_prefix(car_full: str | None) -> str | None:
    if not car_full:
        return None
    full = normalize_spaces(car_full) or ""
    tokens = [t for t in full.split(" ") if t]
    if not tokens:
        return None

    if len(tokens) >= 2:
        first_two = (_norm_brand_token(tokens[0]), _norm_brand_token(tokens[1]))
        if first_two in MULTIWORD_BRANDS:
            return f"{tokens[0]} {tokens[1]}"

    first = tokens[0]
    if re.fullmatch(r"\d{2,4}", first):
        return None
    return first


def ensure_brand_in_car_name(car_short: str | None, car_full: str | None) -> str | None:
    if not car_short:
        return car_short
    short = normalize_spaces(car_short) or ""
    if not short:
        return car_short
    brand = _extract_brand_prefix(car_full)
    if not brand:
        return short

    short_tokens = [t for t in short.split(" ") if t]
    brand_tokens = [t for t in brand.split(" ") if t]
    if len(short_tokens) >= len(brand_tokens):
        short_head = [_norm_brand_token(x) for x in short_tokens[: len(brand_tokens)]]
        brand_norm = [_norm_brand_token(x) for x in brand_tokens]
        if short_head == brand_norm:
            return short

    return normalize_spaces(f"{brand} {short}") or short


def infer_brand_spoken_prefix(value: str | None) -> str | None:
    if not value:
        return None
    normalized = unicodedata.normalize("NFKC", value)
    for pattern, prefix, _markers in BRAND_SPOKEN_RULES:
        if pattern.search(normalized):
            return prefix
    return None


def spoken_has_brand(spoken: str | None, source_value: str | None) -> bool:
    if not spoken:
        return False
    normalized = normalize_spoken_text(spoken)
    if not normalized:
        return False
    source = unicodedata.normalize("NFKC", source_value or "")
    for pattern, _prefix, markers in BRAND_SPOKEN_RULES:
        if pattern.search(source):
            return any(marker in normalized for marker in markers)
    return True


def ensure_brand_in_spoken(spoken: str | None, source_value: str | None) -> str:
    base = normalize_spaces(spoken) or ""
    if spoken_has_brand(base, source_value):
        return base
    prefix = infer_brand_spoken_prefix(source_value)
    if not prefix:
        return base
    if not base:
        return prefix
    return normalize_spaces(f"{prefix} {base}") or base


def letter_to_ru(letter: str) -> str:
    mapping = {
        "a": "а",
        "b": "бэ",
        "c": "цэ",
        "d": "дэ",
        "e": "е",
        "f": "эф",
        "g": "гэ",
        "h": "аш",
        "i": "ай",
        "j": "джей",
        "k": "ка",
        "l": "эль",
        "m": "эм",
        "n": "эн",
        "o": "о",
        "p": "пэ",
        "q": "ку",
        "r": "эр",
        "s": "эс",
        "t": "тэ",
        "u": "у",
        "v": "вэ",
        "w": "дабл ю",
        "x": "икс",
        "y": "игрек",
        "z": "зэт",
    }
    return mapping.get(letter.lower(), letter.lower())


def compact_intro_car_spoken(value: str | None, *, max_tokens: int = 8) -> str:
    base = normalize_spaces(value) or ""
    if not base:
        return ""

    # Keep only the head before explicit separators.
    head = re.split(r"(?:\s*[|｜/;；,:：、，]\s*)", base, maxsplit=1)[0].strip() or base
    tokens = [t for t in re.split(r"\s+", head) if t]
    if not tokens:
        return head

    compact: list[str] = []
    seen_first: set[str] = set()
    for token in tokens:
        cleaned = NON_WORD_EDGE_RE.sub("", token.lower())
        if compact and cleaned == NON_WORD_EDGE_RE.sub("", compact[-1].lower()):
            continue
        if len(compact) < 2 and cleaned in seen_first:
            continue
        seen_first.add(cleaned)
        compact.append(token)
        if len(compact) >= max_tokens:
            break

    if not compact:
        compact = tokens[:max_tokens]
    return normalize_spaces(" ".join(compact)) or ""


def normalize_model_year(value: str | None) -> str | None:
    if value is None:
        return None
    text = normalize_spaces(value) or ""
    if not text:
        return None
    m = re.search(r"(19|20)\d{2}", text)
    if m:
        return m.group(0)
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 4:
        return digits[:4]
    return None
