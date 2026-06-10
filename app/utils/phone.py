from __future__ import annotations

import re

import phonenumbers
from phonenumbers import PhoneNumberFormat


PHONE_RE = re.compile(r"\+?\d[\d\-\s\(\)]{8,}")
CANDIDATE_PHONE_RE = re.compile(r"(?:0\d{1,4}-\d{1,4}-\d{3,4}|0\d{9,10})")
US_CANDIDATE_PHONE_RE = re.compile(
    r"(?:\+?1[\s\-\.]?)?(?:\(?\d{3}\)?[\s\-\.]?\d{3}[\s\-\.]?\d{4})"
)
SPECIAL_PROXY_PREFIXES = ("0078", "0120", "0800", "0570")


def digits_only(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\D", "", value)


def is_special_or_proxy_phone(value: str | None) -> bool:
    digits = digits_only(value)
    return bool(digits and digits.startswith(SPECIAL_PROXY_PREFIXES))


def classify_listing_phone(value: str | None) -> str:
    digits = digits_only(value)
    if not digits:
        return "missing"
    if digits.startswith(SPECIAL_PROXY_PREFIXES):
        return "proxy_or_special"
    return "normal"


def normalize_jp_phone_to_e164(value: str | None) -> str | None:
    digits = digits_only(value)
    if not digits:
        return None
    if digits.startswith(SPECIAL_PROXY_PREFIXES):
        return None

    if digits.startswith("81") and len(digits) in {11, 12}:
        return f"+{digits}"

    if digits.startswith("0") and len(digits) in {10, 11}:
        return "+81" + digits[1:]

    if (value or "").strip().startswith("+") and len(digits) >= 10:
        return f"+{digits}"

    return None


def normalize_us_phone_to_e164(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = phonenumbers.parse(value, "US")
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, PhoneNumberFormat.E164)


def classify_us_phone(value: str | None) -> str:
    if not digits_only(value):
        return "missing"
    return "normal" if normalize_us_phone_to_e164(value) else "invalid"


def find_phones(text: str) -> list[str]:
    return [m.group(0) for m in PHONE_RE.finditer(text)]


def extract_phone_candidates_from_text(text: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for match in CANDIDATE_PHONE_RE.finditer(text or ""):
        candidate = match.group(0).strip()
        if candidate in seen:
            continue
        seen.add(candidate)
        result.append(candidate)
    return result


def extract_us_phone_candidates_from_text(text: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for match in US_CANDIDATE_PHONE_RE.finditer(text or ""):
        candidate = match.group(0).strip()
        key = digits_only(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result
