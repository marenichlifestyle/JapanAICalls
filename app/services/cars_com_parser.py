from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

from app.schemas import ExtractionResult
from app.utils.phone import extract_us_phone_candidates_from_text, normalize_us_phone_to_e164
from app.utils.spoken import compact_car_name_for_call, ensure_brand_in_car_name

USD_PRICE_RE = re.compile(r"\$\s*([0-9][0-9,]*)")
MILEAGE_RE = re.compile(r"([0-9][0-9,]*)\s*mi\b", re.IGNORECASE)
VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")


def _extract_json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for node in soup.select("script[type='application/ld+json']"):
        raw = node.string or node.get_text(strip=True)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            items.append(parsed)
        elif isinstance(parsed, list):
            items.extend([row for row in parsed if isinstance(row, dict)])
    return items


def _parse_usd(value: str | None) -> int | None:
    if not value:
        return None
    m = USD_PRICE_RE.search(value)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_label_value(text: str, labels: list[str]) -> str | None:
    for label in labels:
        pattern = re.compile(rf"{re.escape(label)}\s*[:：]?\s*([^\n]+)", re.IGNORECASE)
        match = pattern.search(text)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" -\t")
            if value:
                return value
    return None


def _extract_listing_phone(soup: BeautifulSoup, text: str) -> str | None:
    for a in soup.select("a[href^='tel:']"):
        value = (a.get("href") or "").replace("tel:", "").strip()
        if value and normalize_us_phone_to_e164(value):
            return value
    for phone in extract_us_phone_candidates_from_text(text):
        if normalize_us_phone_to_e164(phone):
            return phone
    return None


def _split_title_parts(title: str | None) -> tuple[str | None, str | None, str | None]:
    if not title:
        return None, None, None
    normalized = re.sub(r"\s+", " ", title).strip()
    tokens = normalized.split(" ")
    if not tokens:
        return None, None, None
    year = tokens[0] if tokens and re.fullmatch(r"(19|20)\d{2}", tokens[0]) else None
    make = tokens[1] if year and len(tokens) > 1 else (tokens[0] if tokens else None)
    model = tokens[2] if year and len(tokens) > 2 else (tokens[1] if len(tokens) > 1 else None)
    trim = " ".join(tokens[3:]) if year and len(tokens) > 3 else (" ".join(tokens[2:]) if len(tokens) > 2 else None)
    return make, model, trim or None


def _extract_dealer_urls(soup: BeautifulSoup, listing_url: str) -> tuple[str | None, str | None]:
    dealer_website_url = None
    dealer_vehicle_url = None
    for link in soup.find_all("a", href=True):
        href = (link.get("href") or "").strip()
        text = link.get_text(" ", strip=True).lower()
        if not href:
            continue
        if "dealership website" in text or "dealer website" in text:
            dealer_website_url = href
        if "see vehicle on dealership website" in text:
            dealer_vehicle_url = href
    return dealer_website_url, dealer_vehicle_url


def parse_cars_com_deterministic(url: str, html: str, text: str) -> ExtractionResult:
    soup = BeautifulSoup(html, "html.parser")
    json_ld = _extract_json_ld(soup)

    vehicle_title = None
    dealer_name = None
    dealer_address = None
    dealer_website_url = None
    dealer_vehicle_url = None
    price_total = None
    mileage = None
    vin = None

    for row in json_ld:
        if row.get("@type") in {"Product", "Car", "Vehicle"}:
            vehicle_title = vehicle_title or row.get("name")
            offers = row.get("offers") if isinstance(row.get("offers"), dict) else None
            if offers:
                offer_price = offers.get("price")
                if offer_price is not None:
                    try:
                        price_total = int(float(str(offer_price)))
                    except ValueError:
                        pass
            brand = row.get("brand")
            if isinstance(brand, dict):
                _ = brand.get("name")
            mileage_obj = row.get("mileageFromOdometer")
            if isinstance(mileage_obj, dict):
                mileage = mileage or str(mileage_obj.get("value") or "")
            vin = vin or row.get("vehicleIdentificationNumber")
            seller = row.get("seller")
            if isinstance(seller, dict):
                dealer_name = dealer_name or seller.get("name")
                addr = seller.get("address")
                if isinstance(addr, dict):
                    parts = [addr.get("streetAddress"), addr.get("addressLocality"), addr.get("addressRegion"), addr.get("postalCode")]
                    dealer_address = dealer_address or ", ".join([p for p in parts if p])
                    dealer_website_url = dealer_website_url or seller.get("url")

    title_text = soup.title.string.strip() if soup.title and soup.title.string else None
    vehicle_title = vehicle_title or _extract_label_value(text, ["Vehicle title"]) or title_text
    if vehicle_title:
        vehicle_title = re.sub(r"\s*\|\s*Cars\.com.*$", "", vehicle_title).strip()

    if price_total is None:
        price_total = _parse_usd(text)
    price_source = f"${price_total:,}" if price_total is not None else None

    mileage_match = MILEAGE_RE.search(text)
    if mileage_match and not mileage:
        mileage = f"{mileage_match.group(1)} mi"
    elif mileage and "mi" not in mileage.lower():
        mileage = f"{re.sub(r'\\D', '', mileage)} mi" if re.sub(r"\D", "", mileage) else mileage

    vin = vin or _extract_label_value(text, ["VIN"]) or (VIN_RE.search(text).group(1) if VIN_RE.search(text) else None)
    stock_number = _extract_label_value(text, ["Stock #", "Stock"])
    dealer_name = dealer_name or _extract_label_value(text, ["Dealer", "Dealer name", "Seller"])
    dealer_address = dealer_address or _extract_label_value(text, ["Dealer address", "Address", "Location"])
    dealer_hours = _extract_label_value(text, ["Hours", "Business hours"])
    dealer_website_from_links, dealer_vehicle_from_links = _extract_dealer_urls(soup, url)
    dealer_website_url = dealer_website_url or dealer_website_from_links
    dealer_vehicle_url = dealer_vehicle_from_links

    listing_phone_raw = _extract_listing_phone(soup, text)
    make, model, trim = _split_title_parts(vehicle_title)
    year = None
    if vehicle_title:
        year_match = re.match(r"\s*((?:19|20)\d{2})\b", vehicle_title)
        year = year_match.group(1) if year_match else None
    car_short = ensure_brand_in_car_name(compact_car_name_for_call(vehicle_title, max_tokens=8), vehicle_title)

    missing: list[str] = []
    if not vehicle_title:
        missing.append("car")
    if price_total is None:
        missing.append("price")
    if not dealer_name:
        missing.append("dealer")
    confidence = max(0.0, min(1.0, 1 - (len(missing) / 3)))

    return ExtractionResult(
        source="cars.com",
        listing_url=url,
        car=car_short or vehicle_title,
        car_full=vehicle_title,
        car_short=car_short or vehicle_title,
        vehicle_title=vehicle_title,
        year=year,
        make=make,
        model=model,
        trim=trim,
        price_total_jpy=price_total,
        vehicle_price_jpy=price_total,
        price_total_source_text=price_source,
        vehicle_price_source_text=price_source,
        price_confidence=0.95 if price_total is not None else 0.0,
        price_used_jpy=price_total,
        price_used_type="listing_price_usd" if price_total is not None else None,
        mileage=mileage,
        dealer=dealer_name,
        dealer_address=dealer_address,
        dealer_business_hours=dealer_hours,
        dealer_closed_days=None,
        dealer_website_url=dealer_website_url,
        dealer_vehicle_url=dealer_vehicle_url,
        vin=vin,
        stock_number=stock_number,
        exterior_color=_extract_label_value(text, ["Exterior color"]),
        interior_color=_extract_label_value(text, ["Interior color"]),
        fuel_type=_extract_label_value(text, ["Fuel type", "Fuel"]),
        drivetrain=_extract_label_value(text, ["Drivetrain"]),
        transmission=_extract_label_value(text, ["Transmission"]),
        accident_history=_extract_label_value(text, ["Accidents or damage"]),
        title_status=_extract_label_value(text, ["Title"]),
        owner_count=_extract_label_value(text, ["Owners", "Owner count"]),
        recall_status=_extract_label_value(text, ["Open recalls", "Recalls"]),
        seller_notes=_extract_label_value(text, ["Seller's notes", "Seller notes"]),
        phone_from_listing=listing_phone_raw,
        carsensor_free_phone=None,
        dealer_direct_phone=listing_phone_raw,
        extraction_confidence=confidence,
        missing_fields=missing,
    )
