from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import Settings
from app.schemas import (
    CallAnalysisResult,
    DealerPhoneResolutionResult,
    ExtractionResult,
    GoalGenerationResult,
    RequestCallReportResult,
    RequestCallVehicleContext,
    SpokenNormalizationResult,
)
from app.services.http import request_with_retry
from app.utils.spoken import (
    car_name_to_spoken_ru,
    compact_car_name_for_call,
    compact_intro_car_spoken,
    contains_cyrillic,
    ensure_brand_in_car_name,
    ensure_brand_in_spoken,
    ensure_ien_in_spoken_price,
    jpy_to_spoken_ru,
    normalize_model_year,
    normalize_spaces,
    normalize_spoken_text,
)


class OpenAIService:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def _structured(
        self,
        *,
        prompt: str,
        schema_name: str,
        schema: dict[str, Any],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        if not self.settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")

        payload = {
            "model": self.settings.openai_model,
            "input": [
                {
                    "role": "system",
                    "content": "You are a strict data extraction assistant. Return only schema-compliant JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        client_timeout = timeout_sec or self.settings.request_timeout_sec
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            response = await request_with_retry(
                client,
                "POST",
                "https://api.openai.com/v1/responses",
                json=payload,
                headers=headers,
            )
        if response.status_code >= 400:
            body = response.text[:2000]
            raise RuntimeError(
                f"OpenAI responses error {response.status_code}: {body}"
            )
        data = response.json()

        if data.get("output_text"):
            return json.loads(data["output_text"])

        for item in data.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    return json.loads(content["text"])
        raise RuntimeError("Unable to parse OpenAI structured output")

    async def extract_listing(self, *, url: str, text: str, html_fragments: str) -> ExtractionResult:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "source",
                "listing_url",
                "car",
                "car_full",
                "car_short",
                "price_total_jpy",
                "vehicle_price_jpy",
                "price_total_source_text",
                "vehicle_price_source_text",
                "price_confidence",
                "price_used_jpy",
                "price_used_type",
                "year",
                "mileage",
                "repair_history",
                "inspection",
                "dealer",
                "dealer_address",
                "dealer_business_hours",
                "dealer_closed_days",
                "phone_from_listing",
                "carsensor_free_phone",
                "dealer_direct_phone",
                "extraction_confidence",
                "missing_fields",
            ],
            "properties": {
                "source": {"type": "string"},
                "listing_url": {"type": "string"},
                "car": {"type": ["string", "null"]},
                "car_full": {"type": ["string", "null"]},
                "car_short": {"type": ["string", "null"]},
                "price_total_jpy": {"type": ["integer", "null"]},
                "vehicle_price_jpy": {"type": ["integer", "null"]},
                "price_total_source_text": {"type": ["string", "null"]},
                "vehicle_price_source_text": {"type": ["string", "null"]},
                "price_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "price_used_jpy": {"type": ["integer", "null"]},
                "price_used_type": {"type": ["string", "null"]},
                "year": {"type": ["string", "null"]},
                "mileage": {"type": ["string", "null"]},
                "repair_history": {"type": ["string", "null"]},
                "inspection": {"type": ["string", "null"]},
                "dealer": {"type": ["string", "null"]},
                "dealer_address": {"type": ["string", "null"]},
                "dealer_business_hours": {"type": ["string", "null"]},
                "dealer_closed_days": {"type": ["string", "null"]},
                "phone_from_listing": {"type": ["string", "null"]},
                "carsensor_free_phone": {"type": ["string", "null"]},
                "dealer_direct_phone": {"type": ["string", "null"]},
                "extraction_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "missing_fields": {"type": "array", "items": {"type": "string"}},
            },
        }
        prompt = (
            "Извлеки поля из объявления Carsensor. Верни строго JSON по схеме. "
            "Источник: openai_fallback. "
            "price_total_jpy = 支払総額/total price, vehicle_price_jpy = 車両本体価格. "
            "price_used_jpy выбирай total если есть, иначе vehicle. "
            "car_short должен исключать внутренние коды типа ISG/MP202502/M5000 и длинные хвосты с пакетами/опциями. "
            "car_short должен быть коротким (обычно 3-7 токенов), без дублей вроде 'MINI MINI'. "
            "Заполни dealer_business_hours, dealer_closed_days и phone_from_listing, если есть. "
            f"URL: {url}\n\nОчищенный текст:\n{text[:18000]}\n\n"
            f"HTML фрагменты:\n{html_fragments[:12000]}"
        )
        obj = await self._structured(prompt=prompt, schema_name="carsensor_extraction", schema=schema)
        obj["source"] = "openai_fallback"
        obj["listing_url"] = url
        compact = compact_car_name_for_call(obj.get("car_short") or obj.get("car") or obj.get("car_full"))
        obj["car_short"] = ensure_brand_in_car_name(compact, obj.get("car_full"))
        return ExtractionResult.model_validate(obj)

    async def extract_cars_com_with_web_search(self, *, url: str) -> ExtractionResult:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "source",
                "listing_url",
                "car",
                "car_full",
                "car_short",
                "vehicle_title",
                "year",
                "make",
                "model",
                "trim",
                "price_total_jpy",
                "vehicle_price_jpy",
                "price_total_source_text",
                "vehicle_price_source_text",
                "price_confidence",
                "price_used_jpy",
                "price_used_type",
                "mileage",
                "dealer",
                "dealer_address",
                "dealer_business_hours",
                "dealer_closed_days",
                "dealer_website_url",
                "dealer_vehicle_url",
                "vin",
                "stock_number",
                "exterior_color",
                "interior_color",
                "fuel_type",
                "drivetrain",
                "transmission",
                "accident_history",
                "title_status",
                "owner_count",
                "recall_status",
                "seller_notes",
                "phone_from_listing",
                "carsensor_free_phone",
                "dealer_direct_phone",
                "extraction_confidence",
                "missing_fields",
            ],
            "properties": {
                "source": {"type": "string"},
                "listing_url": {"type": "string"},
                "car": {"type": ["string", "null"]},
                "car_full": {"type": ["string", "null"]},
                "car_short": {"type": ["string", "null"]},
                "vehicle_title": {"type": ["string", "null"]},
                "year": {"type": ["string", "null"]},
                "make": {"type": ["string", "null"]},
                "model": {"type": ["string", "null"]},
                "trim": {"type": ["string", "null"]},
                "price_total_jpy": {"type": ["integer", "null"]},
                "vehicle_price_jpy": {"type": ["integer", "null"]},
                "price_total_source_text": {"type": ["string", "null"]},
                "vehicle_price_source_text": {"type": ["string", "null"]},
                "price_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "price_used_jpy": {"type": ["integer", "null"]},
                "price_used_type": {"type": ["string", "null"]},
                "mileage": {"type": ["string", "null"]},
                "dealer": {"type": ["string", "null"]},
                "dealer_address": {"type": ["string", "null"]},
                "dealer_business_hours": {"type": ["string", "null"]},
                "dealer_closed_days": {"type": ["string", "null"]},
                "dealer_website_url": {"type": ["string", "null"]},
                "dealer_vehicle_url": {"type": ["string", "null"]},
                "vin": {"type": ["string", "null"]},
                "stock_number": {"type": ["string", "null"]},
                "exterior_color": {"type": ["string", "null"]},
                "interior_color": {"type": ["string", "null"]},
                "fuel_type": {"type": ["string", "null"]},
                "drivetrain": {"type": ["string", "null"]},
                "transmission": {"type": ["string", "null"]},
                "accident_history": {"type": ["string", "null"]},
                "title_status": {"type": ["string", "null"]},
                "owner_count": {"type": ["string", "null"]},
                "recall_status": {"type": ["string", "null"]},
                "seller_notes": {"type": ["string", "null"]},
                "phone_from_listing": {"type": ["string", "null"]},
                "carsensor_free_phone": {"type": ["string", "null"]},
                "dealer_direct_phone": {"type": ["string", "null"]},
                "extraction_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "missing_fields": {"type": "array", "items": {"type": "string"}},
            },
        }
        prompt = (
            "Extract vehicle and dealer fields for a Cars.com listing using web search. "
            "Return strict JSON matching schema. "
            "If Cars.com page is blocked by Cloudflare, use authoritative dealer pages and trusted sources. "
            "Use Sales phone when available and set dealer_direct_phone to that phone. "
            "listing_url="
            f"{url}. "
            "price_total_jpy/vehicle_price_jpy/price_used_jpy must be integers in USD amount (no symbols). "
            "price_used_type should be 'listing_price_usd'."
        )
        last_exc: Exception | None = None
        for tool_type in ("web_search", "web_search_preview"):
            try:
                obj = await self._structured(
                    prompt=prompt,
                    schema_name="cars_com_web_extract",
                    schema=schema,
                    tools=[{"type": tool_type}],
                    tool_choice="auto",
                    timeout_sec=max(90.0, float(self.settings.request_timeout_sec)),
                )
                obj["source"] = "cars.com_openai_websearch"
                obj["listing_url"] = url
                compact = compact_car_name_for_call(obj.get("car_short") or obj.get("car") or obj.get("car_full"))
                obj["car_short"] = ensure_brand_in_car_name(compact, obj.get("car_full"))
                return ExtractionResult.model_validate(obj)
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Cars.com web-search extraction failed without explicit error")

    @staticmethod
    def _request_call_context_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "source_url",
                "vehicle_title",
                "year",
                "make",
                "model",
                "trim",
                "color",
                "power",
                "price",
                "mileage",
                "vin",
                "stock_number",
                "dealer_name",
                "dealer_phone",
                "dealer_address",
                "confidence",
            ],
            "properties": {
                "source_url": {"type": "string"},
                "vehicle_title": {"type": ["string", "null"]},
                "year": {"type": ["string", "null"]},
                "make": {"type": ["string", "null"]},
                "model": {"type": ["string", "null"]},
                "trim": {"type": ["string", "null"]},
                "color": {"type": ["string", "null"]},
                "power": {"type": ["string", "null"]},
                "price": {"type": ["string", "null"]},
                "mileage": {"type": ["string", "null"]},
                "vin": {"type": ["string", "null"]},
                "stock_number": {"type": ["string", "null"]},
                "dealer_name": {"type": ["string", "null"]},
                "dealer_phone": {"type": ["string", "null"]},
                "dealer_address": {"type": ["string", "null"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        }

    async def extract_request_call_vehicle_context(
        self,
        *,
        url: str,
        text: str,
        html_fragments: str,
    ) -> RequestCallVehicleContext:
        prompt = (
            "Extract compact vehicle context for an outbound dealership call request. "
            "Return only facts that are visible in the page text/html. Do not invent missing values. "
            "Keep vehicle_title concise but complete enough for identification. "
            "Use dealer_phone only if a real callable phone is visible; otherwise null. "
            f"URL: {url}\n\nVisible text:\n{text[:16000]}\n\nHTML fragments:\n{html_fragments[:8000]}"
        )
        obj = await self._structured(
            prompt=prompt,
            schema_name="request_call_vehicle_context",
            schema=self._request_call_context_schema(),
        )
        obj["source_url"] = url
        return RequestCallVehicleContext.model_validate(obj)

    async def extract_request_call_vehicle_context_with_web_search(
        self,
        *,
        url: str,
    ) -> RequestCallVehicleContext:
        prompt = (
            "Use web search to extract compact vehicle context for this outbound dealership call request. "
            "Prioritize the listing page, official dealer pages, and authoritative inventory pages. "
            "Return only facts found in sources. Do not invent missing price, VIN, stock number, color, power, "
            "dealer phone, or availability. "
            f"URL: {url}"
        )
        last_exc: Exception | None = None
        for tool_type in ("web_search", "web_search_preview"):
            try:
                obj = await self._structured(
                    prompt=prompt,
                    schema_name="request_call_vehicle_context_web",
                    schema=self._request_call_context_schema(),
                    tools=[{"type": tool_type}],
                    tool_choice="auto",
                    timeout_sec=max(90.0, float(self.settings.request_timeout_sec)),
                )
                obj["source_url"] = url
                return RequestCallVehicleContext.model_validate(obj)
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Request-call vehicle context web-search failed without explicit error")

    async def resolve_dealer_phone_with_web_search(
        self,
        *,
        listing_url: str,
        dealer_name: str | None,
        dealer_address: str | None,
        listing_phone_raw: str | None,
    ) -> DealerPhoneResolutionResult:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "listing_url",
                "dealer_name",
                "dealer_address",
                "listing_phone_raw",
                "listing_phone_type",
                "resolved_phone_raw",
                "resolved_phone_e164",
                "resolved_phone_source_url",
                "source_type",
                "confidence_score",
                "resolution_status",
                "evidence",
                "candidates",
                "error_reason",
            ],
            "properties": {
                "listing_url": {"type": "string"},
                "dealer_name": {"type": ["string", "null"]},
                "dealer_address": {"type": ["string", "null"]},
                "listing_phone_raw": {"type": ["string", "null"]},
                "listing_phone_type": {"type": "string"},
                "resolved_phone_raw": {"type": ["string", "null"]},
                "resolved_phone_e164": {"type": ["string", "null"]},
                "resolved_phone_source_url": {"type": ["string", "null"]},
                "source_type": {"type": ["string", "null"]},
                "confidence_score": {"type": "integer", "minimum": 0, "maximum": 100},
                "resolution_status": {"type": "string"},
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["source_url", "dealer_name_match", "address_match", "phone_found"],
                        "properties": {
                            "source_url": {"type": "string"},
                            "dealer_name_match": {"type": "boolean"},
                            "address_match": {"type": "boolean"},
                            "phone_found": {"type": "string"},
                        },
                    },
                },
                "candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "phone",
                            "source_url",
                            "source_type",
                            "score",
                            "dealer_name_match",
                            "address_match",
                            "notes",
                        ],
                        "properties": {
                            "phone": {"type": ["string", "null"]},
                            "source_url": {"type": ["string", "null"]},
                            "source_type": {"type": ["string", "null"]},
                            "score": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
                            "dealer_name_match": {"type": ["boolean", "null"]},
                            "address_match": {"type": ["boolean", "null"]},
                            "notes": {"type": ["string", "null"]},
                        },
                    },
                },
                "error_reason": {"type": ["string", "null"]},
            },
        }
        prompt = (
            "Resolve direct dealer phone for a Japanese used-car listing. "
            "Use web search results and return ONLY schema-valid JSON. "
            "Classify listing phone type as one of: proxy_or_special|normal|missing. "
            "Reject and never select phones starting with 0078/0120/0800/0570. "
            "Candidate JP phone regex: (?:0\\d{1,4}-\\d{1,4}-\\d{3,4}|0\\d{9,10}). "
            "Scoring rules: official site +50, address match +30, dealer name match +25, "
            "brand official page +20, directory/aggregator +15, address mismatch -40, fuzzy-name -15. "
            "If selected score >=80 -> resolved; 50-79 -> needs_review; <50 -> not_found or needs_review; "
            "only proxy phones -> proxy_only.\n"
            "Search queries to use:\n"
            f"1) {dealer_name or ''} 電話番号\n"
            f"2) {dealer_name or ''} 店舗情報\n"
            f"3) {dealer_name or ''} 公式\n"
            f"4) {dealer_address or ''} 電話番号\n"
            f"5) {dealer_name or ''} TEL\n"
            f"listing_url={listing_url}\n"
            f"dealer_name={dealer_name}\n"
            f"dealer_address={dealer_address}\n"
            f"listing_phone_raw={listing_phone_raw}\n"
        )
        last_exc: Exception | None = None
        for tool_type in ("web_search", "web_search_preview"):
            try:
                obj = await self._structured(
                    prompt=prompt,
                    schema_name="dealer_phone_resolution",
                    schema=schema,
                    tools=[{"type": tool_type}],
                    tool_choice="auto",
                    timeout_sec=max(90.0, float(self.settings.request_timeout_sec)),
                )
                return DealerPhoneResolutionResult.model_validate(obj)
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("OpenAI resolver failed without explicit error")

    async def normalize_spoken(self, extracted: ExtractionResult, *, call_language: str = "ru") -> SpokenNormalizationResult:
        if call_language not in {"ru", "ja", "en"}:
            call_language = "ru"
        compact_car = compact_intro_car_spoken(extracted.car_short or extracted.car or extracted.car_full, max_tokens=10) or ""
        fallback_car_spoken = car_name_to_spoken_ru(compact_car) if compact_car else ""
        source = (extracted.source or "").lower()
        currency_name = "US dollars" if source == "cars.com" else "Japanese yen"

        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "car_spoken_ru",
                "price_used_spoken_ru",
                "price_total_spoken_ru",
                "vehicle_price_spoken_ru",
                "year_spoken_ru",
                "mileage_spoken_ru",
                "inspection_spoken_ru",
            ],
            "properties": {
                "car_spoken_ru": {"type": "string"},
                "price_used_spoken_ru": {"type": "string"},
                "price_total_spoken_ru": {"type": ["string", "null"]},
                "vehicle_price_spoken_ru": {"type": ["string", "null"]},
                "year_spoken_ru": {"type": ["string", "null"]},
                "mileage_spoken_ru": {"type": ["string", "null"]},
                "inspection_spoken_ru": {"type": ["string", "null"]},
            },
        }
        if call_language == "ja":
            prompt = (
                "Преобразуй данные авто в японский язык (кана/кандзи). Верни только JSON по схеме.\n"
                "Жёсткие правила:\n"
                "1) car_spoken_ru должен содержать только бренд + базовую модель. "
                "Например: メルセデス・ベンツ GLS или BMW 5シリーズ. "
                "Запрещено добавлять комплектацию, пакеты, опции, тип топлива, привод, "
                "описание оснащения, seller notes, one owner, AMGライン, M Sport, Premium Package.\n"
                "2) year_spoken_ru должен быть только в формате YYYY (четыре цифры), либо null если год отсутствует.\n"
                "3) В car_spoken_ru нельзя добавлять год.\n"
                f"4) Цена в spoken полях должна соответствовать числу без изменения; валюта: {currency_name}.\n"
                "5) Не пытайся транслитерировать известные модели любой ценой: латиница и цифры допустимы "
                "для модельных обозначений вроде GLS, 400, 5シリーズ.\n"
                f"car_short={compact_car}\n"
                f"car_full={extracted.car_full}\n"
                f"price_used_jpy={extracted.price_used_jpy}\n"
                f"price_total_jpy={extracted.price_total_jpy}\n"
                f"vehicle_price_jpy={extracted.vehicle_price_jpy}\n"
                f"year={extracted.year}\n"
                f"mileage={extracted.mileage}\n"
                f"inspection={extracted.inspection}\n"
            )
        elif call_language == "en":
            prompt = (
                "Convert vehicle data to natural English speech strings and return strict JSON.\n"
                "Hard rules:\n"
                "1) car_spoken_ru must contain only brand + base model, e.g. Mercedes-Benz GLS or BMW 5 Series.\n"
                "2) Do NOT include trim/package/features/fuel/drivetrain/condition notes/seller notes in car_spoken_ru.\n"
                "3) year_spoken_ru must be YYYY (4 digits) or null if unavailable.\n"
                "4) Do NOT include year inside car_spoken_ru.\n"
                f"5) price spoken fields must match numeric values without changing amounts; currency: {currency_name}.\n"
                f"car_short={compact_car}\n"
                f"car_full={extracted.car_full}\n"
                f"price_used_jpy={extracted.price_used_jpy}\n"
                f"price_total_jpy={extracted.price_total_jpy}\n"
                f"vehicle_price_jpy={extracted.vehicle_price_jpy}\n"
                f"year={extracted.year}\n"
                f"mileage={extracted.mileage}\n"
                f"inspection={extracted.inspection}\n"
            )
        else:
            prompt = (
                "Преобразуй данные авто в русское произношение. Верни строгий JSON.\n"
                "Жёсткие правила:\n"
                "1) car_spoken_ru = только бренд + базовая модель. Например: Мерседес Бенц GLS или БМВ пятая серия. "
                "Без комплектации, пакетов, опций, топлива, привода, оснащения и комментариев продавца.\n"
                "2) year_spoken_ru должен быть только в формате YYYY (четыре цифры), либо null.\n"
                "3) В car_spoken_ru нельзя добавлять год.\n"
                f"4) Цена должна соответствовать числу без изменения; валюта: {currency_name}.\n"
                f"car_short={compact_car}\n"
                f"car_full={extracted.car_full}\n"
                f"price_used_jpy={extracted.price_used_jpy}\n"
                f"price_total_jpy={extracted.price_total_jpy}\n"
                f"vehicle_price_jpy={extracted.vehicle_price_jpy}\n"
                f"year={extracted.year}\n"
                f"mileage={extracted.mileage}\n"
                f"inspection={extracted.inspection}\n"
            )
        obj = await self._structured(prompt=prompt, schema_name="spoken_ru", schema=schema)
        result = SpokenNormalizationResult.model_validate(obj)

        if not (result.car_spoken_ru or "").strip():
            raise RuntimeError("normalization_failed: car_spoken_ru is empty")
        if call_language == "ja" and len((result.car_spoken_ru or "").strip()) < 2:
            raise RuntimeError("normalization_failed: car_spoken_ru is too short")

        if call_language == "ru":
            if not isinstance(extracted.price_used_jpy, int) or extracted.price_used_jpy <= 0:
                raise RuntimeError("normalization_failed: price_used_jpy must be integer > 0")
            if not (result.price_used_spoken_ru or "").strip():
                raise RuntimeError("normalization_failed: price_used_spoken_ru must be non-empty")
        elif call_language == "ja":
            if not isinstance(extracted.price_used_jpy, int) or extracted.price_used_jpy <= 0:
                raise RuntimeError("normalization_failed: price_used_jpy must be integer > 0")
            if not (result.price_used_spoken_ru or "").strip():
                raise RuntimeError("normalization_failed: price_used_spoken_ru must be non-empty")
        else:
            if not isinstance(extracted.price_used_jpy, int) or extracted.price_used_jpy <= 0:
                raise RuntimeError("normalization_failed: price_used_jpy must be integer > 0")
            if not (result.price_used_spoken_ru or "").strip():
                raise RuntimeError("normalization_failed: price_used_spoken_ru must be non-empty")

        actual_price_spoken = normalize_spaces(result.price_used_spoken_ru)
        if call_language in {"ja", "en"}:
            actual_price_output = actual_price_spoken or ""
        else:
            actual_price_output = ensure_ien_in_spoken_price(actual_price_spoken or "")
        if call_language == "ru":
            expected_price_spoken = normalize_spoken_text(jpy_to_spoken_ru(extracted.price_used_jpy) or "")
            actual_price_normalized = normalize_spoken_text(actual_price_output or "")
            # Allow morphology variations while keeping numeric consistency checks lightweight.
            if expected_price_spoken and "иен" not in actual_price_normalized:
                raise RuntimeError("normalization_failed: price_used_spoken_ru must mention иен")
        if call_language == "en":
            if contains_cyrillic(actual_price_spoken):
                raise RuntimeError("normalization_failed: price_used_spoken_ru should be English for en mode")
            if contains_cyrillic(result.car_spoken_ru):
                raise RuntimeError("normalization_failed: car_spoken_ru should be English for en mode")

        if call_language == "en":
            car_spoken = normalize_spaces(result.car_spoken_ru) or normalize_spaces(compact_car) or ""
        elif call_language == "ru":
            car_spoken = normalize_spaces(result.car_spoken_ru) or ""
            if not car_spoken and fallback_car_spoken:
                car_spoken = fallback_car_spoken
            car_spoken = ensure_brand_in_spoken(car_spoken, extracted.car_full or compact_car)
            if not car_spoken and fallback_car_spoken:
                car_spoken = ensure_brand_in_spoken(fallback_car_spoken, extracted.car_full or compact_car)
        else:
            car_spoken = normalize_spaces(result.car_spoken_ru) or ""

        max_intro_tokens = {"ru": 6, "ja": 4, "en": 5}.get(call_language, 6)
        token_count = len((car_spoken or "").split())
        if token_count > max_intro_tokens:
            if call_language == "ru" and fallback_car_spoken:
                car_spoken = fallback_car_spoken
            else:
                car_spoken = compact_intro_car_spoken(compact_car, max_tokens=max_intro_tokens) or car_spoken
        car_spoken = compact_intro_car_spoken(car_spoken, max_tokens=max_intro_tokens) or car_spoken
        if not car_spoken:
            raise RuntimeError("normalization_failed: car_spoken_ru is empty")

        year_spoken = normalize_model_year(result.year_spoken_ru) or normalize_model_year(extracted.year)

        return SpokenNormalizationResult(
            car_spoken_ru=car_spoken,
            price_used_spoken_ru=actual_price_output,
            price_total_spoken_ru=normalize_spaces(result.price_total_spoken_ru),
            vehicle_price_spoken_ru=normalize_spaces(result.vehicle_price_spoken_ru),
            year_spoken_ru=year_spoken,
            mileage_spoken_ru=normalize_spaces(result.mileage_spoken_ru),
            inspection_spoken_ru=normalize_spaces(result.inspection_spoken_ru),
        )

    async def normalize_spoken_ru(self, extracted: ExtractionResult) -> SpokenNormalizationResult:
        return await self.normalize_spoken(extracted, call_language="ru")

    async def generate_goal_ru(
        self,
        *,
        dealer_name: str,
        city: str | None,
        phone_e164: str,
        raw_user_goal: str,
        call_language: str = "en",
        vehicle_context: list[dict[str, Any]] | None = None,
    ) -> GoalGenerationResult:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "status",
                "goal_ru",
                "target_vehicle",
                "main_intent",
                "constraints",
                "required_questions",
                "fallback_questions",
                "completion_criteria",
                "clarification_questions",
            ],
            "properties": {
                "status": {"type": "string"},
                "goal_ru": {"type": ["string", "null"]},
                "target_vehicle": {"type": ["string", "null"]},
                "main_intent": {"type": ["string", "null"]},
                "constraints": {"type": "array", "items": {"type": "string"}},
                "required_questions": {"type": "array", "items": {"type": "string"}},
                "fallback_questions": {"type": "array", "items": {"type": "string"}},
                "completion_criteria": {"type": "array", "items": {"type": "string"}},
                "clarification_questions": {"type": "array", "items": {"type": "string"}},
            },
        }
        call_language = "ja" if call_language == "ja" else "en"
        context_json = json.dumps(vehicle_context or [], ensure_ascii=False)
        if call_language == "ja":
            prompt = (
                "You convert a user's dealership call request into a compact operational goal for a Japanese-speaking "
                "voice agent calling a Japanese dealer or seller. Keep the JSON field name goal_ru for compatibility, "
                "but the value must be Japanese.\n\n"
                "Inputs: dealer_name, city, phone_e164, raw_user_goal, vehicle_context.\n"
                "Length rule: goal_ru must be compact and natural, roughly comparable to a 70-110 word English goal. "
                "Do not overload the call or make it sound like an interrogation.\n\n"
                "Style rule: do not copy the exact dealer_name in goal_ru and do not use brand-specific dealer labels. "
                "Start with the sales department and the vehicle/task only.\n\n"
                "Use vehicle_context only for concise factual identification: brand, model, year, color, power, price, "
                "VIN/stock, mileage, if present. Do not invent facts. If user intent is vague, return "
                "status=needs_goal_clarification, goal_ru=null, and clarification_questions.\n\n"
                "The Japanese goal should cover: what vehicle/task to ask about, availability or incoming timing, "
                "price/fees, configuration/color, VIN/stock if available, payment/paperwork constraints, fallback if "
                "unavailable, and best next contact. Ask short questions one at a time; if a critical answer is vague, "
                "ask one polite follow-up and then accept unknown/refusal/message request as final for that item.\n\n"
                f"dealer_name={dealer_name}\ncity={city}\nphone_e164={phone_e164}\n"
                f"raw_user_goal={raw_user_goal}\nvehicle_context={context_json}"
            )
        else:
            prompt = (
                "You convert a user's dealership call request into a precise operational call goal. "
                "This is not a summary; it is the working script/instruction for an English-speaking "
                "voice agent calling dealership sales departments.\n\n"
                "Inputs: dealer_name, city, phone_e164, raw_user_goal, vehicle_context.\n"
                "Generate goal_ru in English. Keep the JSON field name goal_ru for compatibility, but "
                "the value must be an English instruction.\n\n"
                "Length rule: goal_ru must be compact, about 70-95 words, and never more than 110 words. "
                "Group related questions into short clauses; do not turn the call into an interrogation.\n\n"
                "Privacy/style rule: never copy or mention the exact dealer_name in goal_ru. Also do not use "
                "brand-specific dealer labels like 'a <brand> dealership'. "
                "Start goal_ru like: 'Call the sales department about ...'. The user still sees dealer_name in Telegram; "
                "the voice agent does not need it in the goal.\n\n"
                "Use vehicle_context only for concise factual identification: brand, model, year, color, power, price, "
                "VIN/stock, mileage, if present. Do not invent missing facts.\n\n"
                "The goal_ru must cover only the key facts: vehicle/task, availability or incoming ETA, price/OOD "
                "plus MSRP/markup/fees, configuration/color, VIN/stock, hold/deposit, payment constraints, paperwork "
                "timing, fallback if unavailable, and the best next contact.\n\n"
                "Follow-up policy: ask short questions one at a time and keep the call natural. If a critical answer "
                "is vague, ask one concise follow-up. Accept 'I do not know', refusal, or 'message us' as the final "
                "answer for that item.\n\n"
                "Do not add facts that are not in the input. Do not invent budget, price, VIN, timing, color, "
                "configuration, or availability. If the task is too vague, return status=needs_goal_clarification, "
                "goal_ru=null, and clarification_questions.\n\n"
                f"dealer_name={dealer_name}\ncity={city}\nphone_e164={phone_e164}\n"
                f"raw_user_goal={raw_user_goal}\nvehicle_context={context_json}"
            )
        obj = await self._structured(prompt=prompt, schema_name="request_call_goal", schema=schema)
        return GoalGenerationResult.model_validate(obj)

    async def extract_request_call_report(self, *, transcript: str, goal_ru: str) -> RequestCallReportResult:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "call_status",
                "reached_sales",
                "target_vehicle_or_task",
                "summary",
                "availability_result",
                "incoming_result",
                "price_result",
                "configuration_result",
                "vin_or_stock_result",
                "payment_result",
                "paperwork_result",
                "important_notes",
                "next_action",
                "ai_quality_score",
                "ai_quality_reason",
            ],
            "properties": {
                "call_status": {"type": "string"},
                "reached_sales": {"type": ["boolean", "null"]},
                "target_vehicle_or_task": {"type": ["string", "null"]},
                "summary": {"type": ["string", "null"]},
                "availability_result": {"type": ["string", "null"]},
                "incoming_result": {"type": ["string", "null"]},
                "price_result": {"type": ["string", "null"]},
                "configuration_result": {"type": ["string", "null"]},
                "vin_or_stock_result": {"type": ["string", "null"]},
                "payment_result": {"type": ["string", "null"]},
                "paperwork_result": {"type": ["string", "null"]},
                "important_notes": {"type": ["string", "null"]},
                "next_action": {"type": ["string", "null"]},
                "ai_quality_score": {"type": ["integer", "null"], "minimum": 1, "maximum": 100},
                "ai_quality_reason": {"type": ["string", "null"]},
            },
        }
        prompt = (
            "Извлеки структурированный отчёт из звонка дилеру и верни строгий JSON.\n"
            "goal_ru и transcript могут быть на английском или японском языке, потому что request-call режим "
            "может прозванивать США или Японию.\n"
            "Все человекочитаемые поля отчёта верни на русском языке: summary, availability_result, "
            "incoming_result, price_result, configuration_result, vin_or_stock_result, payment_result, "
            "paperwork_result, important_notes, next_action. Технические enum/status values оставь на английском.\n"
            "Пиши максимально точно и по делу: каждое человекочитаемое поле максимум 1 короткое предложение, "
            "без пересказа диалога, вводных фраз и воды. Итог должен сразу отвечать, чем закончился звонок. "
            "Если есть конкретная дата, цена, VIN/stock, имя контакта или next step — ставь их первыми. "
            "Если факта нет, не заполняй поле общими словами.\n"
            "Не придумывай ответы, которых нет в transcript. Если данных нет, ставь null или not_answered.\n"
            "Сверяй transcript с goal_ru: если обязательный вопрос из goal_ru не был задан или ответ не получен, "
            "пиши not_answered в соответствующее поле и укажи недостающие пункты в important_notes/next_action. "
            "Если продавец сказал, что автомобиль incoming/в поставке, но не назвал ETA/дату/срок, "
            "incoming_result должен явно сказать по-русски, что поставка есть, но срок не получен. "
            "Если продавец ответил общо без цены/VIN/stock/условий оплаты, не заполняй эти поля догадками.\n"
            "call_status должен быть одним из: completed, no_answer, busy, failed, refused, asked_to_message.\n"
            "Оцени качество работы голосового AI-агента, а не продавца и не качество этого анализа. "
            "ai_quality_score: 1-100, если был содержательный разговор; null, если транскрипт пустой или звонок "
            "не состоялся. Учитывай: следовал ли агент goal_ru, задал ли ключевые вопросы, получил ли конкретные "
            "ответы или корректно зафиксировал отказ/неизвестно, не говорил ли слишком длинно, не завершил ли "
            "разговор преждевременно, не выдумывал ли факты, использовал ли правильный язык. "
            "ai_quality_reason верни коротко на русском: почему такая оценка.\n\n"
            f"goal_ru:\n{goal_ru[:4000]}\n\ntranscript:\n{transcript[:15000]}"
        )
        obj = await self._structured(prompt=prompt, schema_name="request_call_report", schema=schema)
        return RequestCallReportResult.model_validate(obj)

    async def analyze_call(self, transcript: str, summary: str) -> CallAnalysisResult:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "available",
                "price_confirmed",
                "actual_price",
                "price_change_reason",
                "condition_notes",
                "seller_mood",
                "next_step",
                "final_summary_ru",
                "conclusion",
                "ai_quality_score",
                "ai_quality_reason",
            ],
            "properties": {
                "available": {"type": ["boolean", "null"]},
                "price_confirmed": {"type": ["boolean", "null"]},
                "actual_price": {"type": ["string", "null"]},
                "price_change_reason": {"type": ["string", "null"]},
                "condition_notes": {"type": ["string", "null"]},
                "seller_mood": {"type": ["string", "null"]},
                "next_step": {"type": ["string", "null"]},
                "final_summary_ru": {"type": ["string", "null"]},
                "conclusion": {"type": ["string", "null"]},
                "ai_quality_score": {"type": ["integer", "null"], "minimum": 1, "maximum": 100},
                "ai_quality_reason": {"type": ["string", "null"]},
            },
        }
        prompt = (
            "Проанализируй звонок по продаже авто и верни строго JSON.\n"
            "Оцени качество работы голосового AI-агента, а не продавца и не качество этого анализа. "
            "ai_quality_score: 1-100, если был содержательный разговор; null, если transcript пустой или звонок "
            "не состоялся. Критерии: агент следовал цели звонка, задал ключевые вопросы, получил конкретные ответы "
            "или корректно зафиксировал отказ/неизвестно, не говорил слишком длинно, не завершил преждевременно, "
            "не выдумывал факты, использовал правильный язык. ai_quality_reason верни коротко на русском в формате "
            "'почему такая оценка'.\n"
            f"summary:\n{summary[:4000]}\n\ntranscript:\n{transcript[:15000]}"
        )
        obj = await self._structured(prompt=prompt, schema_name="call_analysis", schema=schema)
        return CallAnalysisResult.model_validate(obj)
