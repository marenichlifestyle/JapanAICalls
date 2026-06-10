from __future__ import annotations

import pytest

from app.config import Settings
from app.schemas import DealerPhoneResolutionResult, ExtractionResult
from app.services.dealer_phone_resolver import DealerPhoneResolver
from app.services.openai_client import OpenAIService


class FakeOpenAIResolver(OpenAIService):
    async def resolve_dealer_phone_with_web_search(
        self,
        *,
        listing_url: str,
        dealer_name: str | None,
        dealer_address: str | None,
        listing_phone_raw: str | None,
    ) -> DealerPhoneResolutionResult:
        return DealerPhoneResolutionResult(
            listing_url=listing_url,
            dealer_name=dealer_name,
            dealer_address=dealer_address,
            listing_phone_raw=listing_phone_raw,
            listing_phone_type="proxy_or_special",
            resolved_phone_raw="011-780-1184",
            resolved_phone_e164="+81117801184",
            resolved_phone_source_url="https://www.motoren-sapporo.jp",
            source_type="official_site",
            confidence_score=100,
            resolution_status="resolved",
            evidence=[
                {
                    "source_url": "https://www.motoren-sapporo.jp/shop/sapporo-east",
                    "dealer_name_match": True,
                    "address_match": True,
                    "phone_found": "011-780-1184",
                }
            ],
            candidates=[
                {"phone": "011-780-1184", "score": 100, "source_type": "official_site"},
            ],
        )


class FakeOpenAIResolverLowScore(OpenAIService):
    async def resolve_dealer_phone_with_web_search(
        self,
        *,
        listing_url: str,
        dealer_name: str | None,
        dealer_address: str | None,
        listing_phone_raw: str | None,
    ) -> DealerPhoneResolutionResult:
        return DealerPhoneResolutionResult(
            listing_url=listing_url,
            dealer_name=dealer_name,
            dealer_address=dealer_address,
            listing_phone_raw=listing_phone_raw,
            listing_phone_type="proxy_or_special",
            resolved_phone_raw="011-780-1184",
            resolved_phone_e164="+81117801184",
            resolved_phone_source_url="https://directory.example",
            source_type="directory",
            confidence_score=65,
            resolution_status="resolved",
            evidence=[],
            candidates=[{"phone": "011-780-1184", "score": 65, "source_type": "directory"}],
        )


def _proxy_extracted() -> ExtractionResult:
    return ExtractionResult(
        source="deterministic",
        listing_url="https://www.carsensor.net/usedcar/detail/AU6938672228/index.html",
        car="BMW 523i",
        car_full="BMW 523i",
        car_short="BMW 523i",
        price_total_jpy=7_138_000,
        vehicle_price_jpy=6_980_000,
        price_total_source_text="支払総額 713.8万円",
        vehicle_price_source_text="車両本体価格 698.0万円",
        price_confidence=0.95,
        price_used_jpy=7_138_000,
        price_used_type="total_price",
        year="2022",
        mileage="12000km",
        repair_history="なし",
        inspection="2027/04",
        dealer="Sapporo-Higashi BMW BMW Premium Selection 札幌東",
        dealer_address="北海道札幌市東区東苗穂3条3丁目2-15",
        dealer_business_hours=None,
        dealer_closed_days=None,
        phone_from_listing="0078-6002-648302",
        carsensor_free_phone="0078-6002-648302",
        dealer_direct_phone=None,
        extraction_confidence=0.9,
        missing_fields=[],
    )


@pytest.mark.asyncio
async def test_resolver_proxy_case_expected_output() -> None:
    settings = Settings(TELEGRAM_ADMIN_IDS="1", TELEGRAM_BOT_TOKEN="x")
    resolver = DealerPhoneResolver(openai_service=FakeOpenAIResolver(settings))

    result = await resolver.resolve(extracted=_proxy_extracted())
    assert result.listing_phone_raw == "0078-6002-648302"
    assert result.listing_phone_type == "proxy_or_special"
    assert result.resolved_phone_raw == "011-780-1184"
    assert result.resolved_phone_e164 == "+81117801184"
    assert result.source_type == "official_site"
    assert result.confidence_score == 100
    assert result.resolution_status == "resolved"


@pytest.mark.asyncio
async def test_resolver_score_below_80_needs_review() -> None:
    settings = Settings(TELEGRAM_ADMIN_IDS="1", TELEGRAM_BOT_TOKEN="x")
    resolver = DealerPhoneResolver(openai_service=FakeOpenAIResolverLowScore(settings))

    result = await resolver.resolve(extracted=_proxy_extracted())
    assert result.confidence_score == 65
    assert result.resolution_status == "needs_review"
