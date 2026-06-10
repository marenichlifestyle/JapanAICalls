from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.schemas import ExtractionResult
from app.services.http import request_with_retry
from app.utils.phone import find_phones, is_special_or_proxy_phone
from app.utils.spoken import compact_car_name_for_call, ensure_brand_in_car_name
from app.utils.text import clean_text_from_html

logger = logging.getLogger(__name__)

PRICE_YEN_RE = re.compile(r"([0-9]{1,4}(?:,[0-9]{3})+)\s*円")
PRICE_MAN_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*万円")
PHONE_CONTEXT_SPLIT = re.compile(r"\n+|\s{2,}")
DEALER_INVALID_VALUES = {
    "保険/ローン/他",
    "中古車販売店",
    "車買取",
    "お役立ち記事",
}


@dataclass
class ParseArtifacts:
    html: str
    text: str
    source: str


def _parse_price_jpy(value: str) -> int | None:
    normalized = value
    # Carsensor sometimes splits decimals as "569 .8 万円" across adjacent tokens.
    normalized = re.sub(r"(\d)\s*[.,．]\s*(\d)", r"\1.\2", normalized)
    normalized = normalized.replace("．", ".")
    normalized = re.sub(r"(\d)\s*,\s*(\d{3})", r"\1,\2", normalized)

    m = PRICE_YEN_RE.search(normalized)
    if m:
        return int(m.group(1).replace(",", ""))
    m = PRICE_MAN_RE.search(normalized)
    if m:
        return int(round(float(m.group(1)) * 10000))
    return None


def _extract_json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for node in soup.select("script[type='application/ld+json']"):
        raw = node.string or node.get_text(strip=True)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                items.append(parsed)
            elif isinstance(parsed, list):
                items.extend([x for x in parsed if isinstance(x, dict)])
        except json.JSONDecodeError:
            continue
    return items


def _extract_label_value(text: str, label: str) -> str | None:
    pattern = re.compile(rf"{re.escape(label)}\s*[:：]?\s*([^\n]+)")
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def _extract_dealer_name(text: str) -> str | None:
    for pattern in (
        r"販売店名\s*[:：]\s*([^\n]+)",
        r"店舗名\s*[:：]\s*([^\n]+)",
    ):
        m = re.search(pattern, text)
        if not m:
            continue
        value = re.sub(r"\s+", " ", m.group(1)).strip()
        if value:
            return value
    return None


def _sanitize_dealer_name(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        return None
    cleaned = re.sub(r"^販売店名?\s*[:：]?\s*", "", cleaned).strip()
    if cleaned in DEALER_INVALID_VALUES:
        return None
    return cleaned


def _extract_dealer_address(text: str) -> str | None:
    for pattern in (
        r"住所\s*[:：]\s*([^\n]+)",
        r"所在地\s*[:：]\s*([^\n]+)",
    ):
        m = re.search(pattern, text)
        if not m:
            continue
        value = re.sub(r"\s+", " ", m.group(1)).strip()
        if value:
            return value
    return None


def _extract_price_by_labels(text: str, labels: list[str]) -> tuple[int | None, str | None]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        if any(label in line for label in labels):
            window = " ".join(lines[idx : min(idx + 12, len(lines))])
            parsed = _parse_price_jpy(window)
            if parsed is not None:
                return parsed, window
            parsed = _parse_price_jpy(line)
            if parsed is not None:
                return parsed, line

    for label in labels:
        val = _extract_label_value(text, label)
        if val:
            parsed = _parse_price_jpy(val)
            if parsed is not None:
                return parsed, val
    return None, None


def _normalize_phone_candidates(text: str) -> tuple[str | None, str | None, str | None]:
    direct = None
    free = None
    raw = None
    for line in PHONE_CONTEXT_SPLIT.split(text):
        phones = find_phones(line)
        if not phones:
            continue
        for ph in phones:
            if raw is None:
                raw = ph
            if "無料" in line or "フリー" in line or is_special_or_proxy_phone(ph):
                free = free or ph
            else:
                direct = direct or ph
    return direct, free, raw


def _build_car_short(car_full: str | None) -> str | None:
    if not car_full:
        return None
    cleaned = re.sub(r"\s+", " ", car_full).strip()
    tokens = cleaned.split(" ")
    keep_suffix = {"AMG", "GT", "GTS", "SUV", "4WD"}

    while tokens:
        t = tokens[-1]
        if t in keep_suffix:
            break
        if re.fullmatch(r"[A-Z]{2,}\d{3,}", t):
            tokens.pop()
            continue
        if re.fullmatch(r"MP\d{3,}", t):
            tokens.pop()
            continue
        if re.fullmatch(r"M\d{3,}", t):
            tokens.pop()
            continue
        if re.fullmatch(r"[A-Z]{2,5}", t):
            tokens.pop()
            continue
        break

    if not tokens:
        return ensure_brand_in_car_name(compact_car_name_for_call(cleaned), cleaned)
    return ensure_brand_in_car_name(compact_car_name_for_call(" ".join(tokens)), cleaned)


async def fetch_listing_page(url: str, timeout: float = 30.0) -> ParseArtifacts:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        try:
            response = await request_with_retry(client, "GET", url)
            if response.status_code == 200 and response.text.strip():
                return ParseArtifacts(html=response.text, text=clean_text_from_html(response.text), source="httpx")
        except Exception:
            logger.exception("httpx fetch failed", extra={"url": url})

    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                wait_until = "domcontentloaded" if "cars.com/vehicledetail/" in url.lower() else "networkidle"
                try:
                    await page.goto(url, timeout=int(timeout * 1000), wait_until=wait_until)
                except PlaywrightTimeoutError:
                    html = await page.content()
                    text = clean_text_from_html(html)
                    if text.strip():
                        logger.warning(
                            "playwright goto timed out; using partial page content",
                            extra={"url": url, "wait_until": wait_until},
                        )
                        return ParseArtifacts(html=html, text=text, source="playwright_partial")
                    raise
                html = await page.content()
                return ParseArtifacts(html=html, text=clean_text_from_html(html), source="playwright")
            finally:
                await browser.close()
    except Exception:
        logger.exception("playwright fetch failed", extra={"url": url})
        raise


def parse_deterministic(url: str, html: str, text: str) -> ExtractionResult:
    soup = BeautifulSoup(html, "html.parser")
    json_ld = _extract_json_ld(soup)

    car_full = None
    dealer = None
    dealer_address = None

    for item in json_ld:
        if not car_full:
            car_full = item.get("name") or item.get("model")
        if not dealer and isinstance(item.get("seller"), dict):
            dealer = item["seller"].get("name")
            dealer_address = item["seller"].get("address") if not dealer_address else dealer_address

    title = (soup.title.string.strip() if soup.title and soup.title.string else None)
    if not car_full and title:
        car_full = re.sub(r"\s*\|.*$", "", title)

    car_short = _build_car_short(car_full)
    dealer = _sanitize_dealer_name(
        dealer
        or _extract_dealer_name(text)
        or _extract_label_value(text, "販売店名")
        or _extract_label_value(text, "販売店")
        or _extract_label_value(text, "店舗")
    )
    dealer_address = dealer_address or _extract_dealer_address(text)
    dealer_business_hours = _extract_label_value(text, "営業時間")
    dealer_closed_days = _extract_label_value(text, "定休日")

    total_price, total_src = _extract_price_by_labels(text, ["支払総額", "総額", "total payment", "total price"])
    vehicle_price, vehicle_src = _extract_price_by_labels(text, ["車両本体価格", "本体価格", "vehicle body price"])

    if total_price is None and vehicle_price is None:
        all_prices = [_parse_price_jpy(chunk) for chunk in re.findall(r"[^\n]{0,40}(?:円|万円)[^\n]{0,40}", text)]
        all_prices = [x for x in all_prices if x]
        if all_prices:
            total_price = max(all_prices)
            total_src = "fallback_max_price"

    price_used_jpy = total_price if total_price is not None else vehicle_price
    price_used_type = "total_price" if total_price is not None else ("vehicle_price" if vehicle_price is not None else None)
    price_confidence = 0.95 if total_price is not None else (0.85 if vehicle_price is not None else 0.0)

    year = _extract_label_value(text, "年式")
    mileage = _extract_label_value(text, "走行距離")
    repair_history = _extract_label_value(text, "修復歴")
    inspection = _extract_label_value(text, "車検")

    phone_text = "\n".join([a.get_text(" ", strip=True) for a in soup.find_all("a")]) + "\n" + text
    direct_phone, free_phone, phone_from_listing = _normalize_phone_candidates(phone_text)

    missing = []
    if not car_short:
        missing.append("car")
    if total_price is None and vehicle_price is None:
        missing.append("price")
    if not dealer:
        missing.append("dealer")
    if not direct_phone and not free_phone:
        missing.append("phone")

    confidence = 1 - (len(missing) / 4)

    return ExtractionResult(
        source="deterministic",
        listing_url=url,
        car=car_short,
        car_full=car_full,
        car_short=car_short,
        price_total_jpy=total_price,
        vehicle_price_jpy=vehicle_price,
        price_total_source_text=total_src,
        vehicle_price_source_text=vehicle_src,
        price_confidence=price_confidence,
        price_used_jpy=price_used_jpy,
        price_used_type=price_used_type,
        year=year,
        mileage=mileage,
        repair_history=repair_history,
        inspection=inspection,
        dealer=dealer,
        dealer_address=dealer_address,
        dealer_business_hours=dealer_business_hours,
        dealer_closed_days=dealer_closed_days,
        phone_from_listing=phone_from_listing,
        carsensor_free_phone=free_phone,
        dealer_direct_phone=direct_phone,
        extraction_confidence=max(0.0, min(1.0, confidence)),
        missing_fields=missing,
    )
