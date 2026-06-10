from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


CARS_COM_DETAIL_RE = re.compile(
    r"https?://(?:www\.)?cars\.com/vehicledetail/([0-9a-fA-F-]{36})/?",
    re.IGNORECASE,
)
CARSENSOR_DETAIL_RE = re.compile(
    r"https?://(?:www\.)?carsensor\.net/usedcar/detail/(AU\d+)/",
    re.IGNORECASE,
)


def listing_fingerprint(url: str) -> str | None:
    value = (url or "").strip()
    if not value:
        return None
    cars_com = CARS_COM_DETAIL_RE.search(value)
    if cars_com:
        return f"cars.com:{cars_com.group(1).lower()}"
    carsensor = CARSENSOR_DETAIL_RE.search(value)
    if carsensor:
        return f"carsensor:{carsensor.group(1).upper()}"
    return None


def normalized_listing_url(url: str) -> str:
    parsed = urlsplit((url or "").strip())
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))
