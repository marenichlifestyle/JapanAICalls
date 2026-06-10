from __future__ import annotations

import logging
from urllib.parse import urlparse

from app.config import Settings
from app.schemas import ExtractionResult, RequestCallVehicleContext
from app.services.cars_com_parser import parse_cars_com_deterministic
from app.services.carsensor_parser import fetch_listing_page, parse_deterministic
from app.services.openai_client import OpenAIService
from app.utils.phone import is_special_or_proxy_phone, normalize_jp_phone_to_e164, normalize_us_phone_to_e164

logger = logging.getLogger(__name__)


def _price_text(extracted: ExtractionResult) -> str | None:
    if extracted.price_total_source_text:
        return extracted.price_total_source_text
    if extracted.vehicle_price_source_text:
        return extracted.vehicle_price_source_text
    if extracted.price_used_jpy is not None:
        if (extracted.source or "").startswith("cars.com") or extracted.price_used_type == "listing_price_usd":
            return f"${extracted.price_used_jpy:,}"
        return f"{extracted.price_used_jpy:,} JPY"
    return None


def _color_text(extracted: ExtractionResult) -> str | None:
    parts = [extracted.exterior_color, extracted.interior_color]
    values = [value for value in parts if value]
    return " / ".join(values) if values else None


def _normalize_context_phone(value: str | None, *, source_url: str) -> str | None:
    if not value:
        return None
    if "carsensor.net" in source_url.lower() or ".jp" in urlparse(source_url).netloc.lower():
        if is_special_or_proxy_phone(value):
            return None
        return normalize_jp_phone_to_e164(value)
    return normalize_us_phone_to_e164(value) or normalize_jp_phone_to_e164(value)


def context_from_extraction(extracted: ExtractionResult) -> RequestCallVehicleContext:
    source_url = extracted.listing_url
    dealer_phone = _normalize_context_phone(
        extracted.dealer_direct_phone or extracted.phone_from_listing or extracted.carsensor_free_phone,
        source_url=source_url,
    )
    title = extracted.vehicle_title or extracted.car_full or extracted.car_short or extracted.car
    return RequestCallVehicleContext(
        source_url=source_url,
        vehicle_title=title,
        year=extracted.year,
        make=extracted.make,
        model=extracted.model,
        trim=extracted.trim,
        color=_color_text(extracted),
        power=None,
        price=_price_text(extracted),
        mileage=extracted.mileage,
        vin=extracted.vin,
        stock_number=extracted.stock_number,
        dealer_name=extracted.dealer,
        dealer_phone=dealer_phone,
        dealer_address=extracted.dealer_address,
        confidence=extracted.extraction_confidence,
    )


class RequestCallContextExtractor:
    def __init__(self, *, settings: Settings, openai_service: OpenAIService) -> None:
        self.settings = settings
        self.openai_service = openai_service

    async def extract_many(self, urls: list[str]) -> list[dict]:
        contexts: list[dict] = []
        seen: set[str] = set()
        for url in urls:
            cleaned = (url or "").strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            try:
                context = await self.extract(cleaned)
            except Exception as exc:
                logger.warning("request-call URL context extraction failed", extra={"url": cleaned, "status": str(exc)})
                continue
            if context.confidence > 0 or any(
                [context.vehicle_title, context.make, context.model, context.dealer_name, context.dealer_phone]
            ):
                contexts.append(context.model_dump())
        return contexts

    async def extract(self, url: str) -> RequestCallVehicleContext:
        normalized = url.lower()
        if "cars.com/vehicledetail/" in normalized:
            return await self._extract_cars_com(url)
        if "carsensor.net/usedcar/detail/" in normalized:
            return await self._extract_carsensor(url)
        return await self._extract_generic(url)

    async def _extract_cars_com(self, url: str) -> RequestCallVehicleContext:
        try:
            artifacts = await fetch_listing_page(url, timeout=self.settings.request_timeout_sec)
            extracted = parse_cars_com_deterministic(url, artifacts.html, artifacts.text)
            if extracted.extraction_confidence >= 0.75 and (extracted.vehicle_title or extracted.car_full):
                return context_from_extraction(extracted)
        except Exception as exc:
            logger.info("request-call cars.com deterministic context failed", extra={"url": url, "status": str(exc)})
        return context_from_extraction(await self.openai_service.extract_cars_com_with_web_search(url=url))

    async def _extract_carsensor(self, url: str) -> RequestCallVehicleContext:
        try:
            artifacts = await fetch_listing_page(url, timeout=self.settings.request_timeout_sec)
            extracted = parse_deterministic(url, artifacts.html, artifacts.text)
            if extracted.extraction_confidence >= 0.75 and (extracted.car_full or extracted.car_short):
                return context_from_extraction(extracted)
            extracted = await self.openai_service.extract_listing(
                url=url,
                text=artifacts.text,
                html_fragments=artifacts.html[:12000],
            )
            return context_from_extraction(extracted)
        except Exception as exc:
            logger.info("request-call carsensor context falling back to web search", extra={"url": url, "status": str(exc)})
            return await self.openai_service.extract_request_call_vehicle_context_with_web_search(url=url)

    async def _extract_generic(self, url: str) -> RequestCallVehicleContext:
        try:
            artifacts = await fetch_listing_page(url, timeout=min(float(self.settings.request_timeout_sec), 20.0))
            return await self.openai_service.extract_request_call_vehicle_context(
                url=url,
                text=artifacts.text,
                html_fragments=artifacts.html[:8000],
            )
        except Exception as exc:
            logger.info("request-call generic URL context falling back to web search", extra={"url": url, "status": str(exc)})
            return await self.openai_service.extract_request_call_vehicle_context_with_web_search(url=url)
