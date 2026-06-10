from __future__ import annotations

import re


US_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

# State-based defaults with sane IANA names. ZIP is still required as primary signal,
# and this map helps pick the best zone for dealer addresses.
STATE_TZ_DEFAULT: dict[str, str] = {
    "AL": "America/Chicago",
    "AK": "America/Anchorage",
    "AZ": "America/Phoenix",
    "AR": "America/Chicago",
    "CA": "America/Los_Angeles",
    "CO": "America/Denver",
    "CT": "America/New_York",
    "DE": "America/New_York",
    "FL": "America/New_York",
    "GA": "America/New_York",
    "HI": "Pacific/Honolulu",
    "ID": "America/Boise",
    "IL": "America/Chicago",
    "IN": "America/Indiana/Indianapolis",
    "IA": "America/Chicago",
    "KS": "America/Chicago",
    "KY": "America/New_York",
    "LA": "America/Chicago",
    "ME": "America/New_York",
    "MD": "America/New_York",
    "MA": "America/New_York",
    "MI": "America/Detroit",
    "MN": "America/Chicago",
    "MS": "America/Chicago",
    "MO": "America/Chicago",
    "MT": "America/Denver",
    "NE": "America/Chicago",
    "NV": "America/Los_Angeles",
    "NH": "America/New_York",
    "NJ": "America/New_York",
    "NM": "America/Denver",
    "NY": "America/New_York",
    "NC": "America/New_York",
    "ND": "America/Chicago",
    "OH": "America/New_York",
    "OK": "America/Chicago",
    "OR": "America/Los_Angeles",
    "PA": "America/New_York",
    "RI": "America/New_York",
    "SC": "America/New_York",
    "SD": "America/Chicago",
    "TN": "America/Chicago",
    "TX": "America/Chicago",
    "UT": "America/Denver",
    "VT": "America/New_York",
    "VA": "America/New_York",
    "WA": "America/Los_Angeles",
    "WV": "America/New_York",
    "WI": "America/Chicago",
    "WY": "America/Denver",
    "DC": "America/New_York",
}

STATE_RE = re.compile(r"\b([A-Z]{2})\s+\d{5}(?:-\d{4})?\b")


def _extract_us_zip(value: str | None) -> str | None:
    if not value:
        return None
    match = US_ZIP_RE.search(value)
    return match.group(1) if match else None


def _extract_us_state(value: str | None) -> str | None:
    if not value:
        return None
    upper = value.upper()
    match = STATE_RE.search(upper)
    return match.group(1) if match else None


def _timezone_from_zip(zip_code: str) -> str | None:
    zip5 = int(zip_code)
    zip3 = zip5 // 100

    # Territories / special ranges first.
    if 99500 <= zip5 <= 99999:
        return "America/Anchorage"
    if zip3 in {967, 968}:
        return "Pacific/Honolulu"
    if 96900 <= zip5 <= 96999:
        return "Pacific/Guam"
    if 600 <= zip5 <= 999:
        return "America/Puerto_Rico"

    # Coarse fallback by geographic zip blocks (east -> west).
    first_digit = int(zip_code[0])
    if first_digit in {0, 1, 2, 3}:
        return "America/New_York"
    if first_digit in {4, 5, 6, 7}:
        return "America/Chicago"
    if first_digit == 8:
        return "America/Denver"
    if first_digit == 9:
        return "America/Los_Angeles"
    return None


def resolve_office_timezone(
    *,
    source: str | None,
    dealer_address: str | None,
    listing_url: str | None,
    jp_default: str,
    us_fallback: str,
) -> tuple[str, str]:
    normalized_source = (source or "").lower()
    if normalized_source != "cars.com":
        return jp_default, "source_default_japan"

    zip_code = _extract_us_zip(dealer_address)
    state = _extract_us_state(dealer_address)
    if zip_code:
        if state and state in STATE_TZ_DEFAULT:
            return STATE_TZ_DEFAULT[state], f"zip_and_state:{zip_code}:{state}"
        tz_by_zip = _timezone_from_zip(zip_code)
        if tz_by_zip:
            return tz_by_zip, f"zip:{zip_code}"

    return us_fallback, "fallback_us_timezone"
