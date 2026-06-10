from __future__ import annotations

import pytest

from app.schemas import ExtractionResult
from app.services.dealer_phone_resolver_us import DealerPhoneResolverUS


class StubResolver(DealerPhoneResolverUS):
    def __init__(self, pages: dict[str, str]):
        super().__init__(timeout_sec=1.0)
        self.pages = pages

    async def _fetch_text(self, url: str) -> str:
        if url not in self.pages:
            raise RuntimeError(f"missing page {url}")
        return self.pages[url]


def _cars_com_extracted() -> ExtractionResult:
    return ExtractionResult(
        source="cars.com",
        listing_url="https://www.cars.com/vehicledetail/0c94d8c2-659a-44f7-871e-4392a355428a/",
        car="2023 Tesla Model Y",
        car_full="2023 Tesla Model Y Performance Dual Motor All-Wheel Drive",
        car_short="2023 Tesla Model Y Performance",
        vehicle_title="2023 Tesla Model Y Performance Dual Motor All-Wheel Drive",
        year="2023",
        make="Tesla",
        model="Model Y",
        trim="Performance Dual Motor All-Wheel Drive",
        price_total_jpy=28972,
        vehicle_price_jpy=28972,
        price_total_source_text="$28,972",
        vehicle_price_source_text="$28,972",
        price_confidence=0.95,
        price_used_jpy=28972,
        price_used_type="listing_price_usd",
        mileage="67,157 mi",
        dealer="Continental Toyota",
        dealer_address="6701 South La Grange Road, Hodgkins, IL 60525",
        dealer_website_url="https://www.continentaltoyota.com",
        dealer_vehicle_url="https://www.continentaltoyota.com/used/Tesla/2023-Tesla-Model-Y-0c94d8c2.htm",
        vin="7SAYGDEF1PF795089",
        stock_number="P11988A",
        phone_from_listing=None,
        carsensor_free_phone=None,
        dealer_direct_phone=None,
        extraction_confidence=0.95,
        missing_fields=[],
    )


@pytest.mark.asyncio
async def test_resolver_follows_dealer_website_and_picks_sales() -> None:
    pages = {
        "https://www.continentaltoyota.com/used/Tesla/2023-Tesla-Model-Y-0c94d8c2.htm": """
        <html><body>
        <h1>Continental Toyota</h1>
        <div>6701 South La Grange Road, Hodgkins, IL 60525</div>
        <a href='tel:(708) 716-4497'>Call Sales</a>
        <a href='tel:(708) 555-1000'>Service Department</a>
        </body></html>
        """,
        "https://www.continentaltoyota.com": """
        <html><body>
        <div>Continental Toyota</div>
        <div>6701 South La Grange Road, Hodgkins, IL 60525</div>
        <a href='tel:(708) 716-4497'>Sales Department</a>
        </body></html>
        """,
    }
    resolver = StubResolver(pages)
    result = await resolver.resolve(extracted=_cars_com_extracted(), listing_html="<html></html>")
    assert result.resolution_status == "resolved"
    assert result.resolved_phone_raw == "(708) 716-4497"
    assert result.resolved_phone_e164 == "+17087164497"
    assert result.source_type in {"official_dealer_website", "dealer_vehicle_page"}
    assert result.phone_type == "sales"
    assert result.confidence_score >= 90


@pytest.mark.asyncio
async def test_resolver_only_service_phone_requires_review() -> None:
    pages = {
        "https://www.continentaltoyota.com/used/Tesla/2023-Tesla-Model-Y-0c94d8c2.htm": """
        <html><body>
        <h1>Continental Toyota</h1>
        <div>6701 South La Grange Road, Hodgkins, IL 60525</div>
        <a href='tel:(708) 555-1000'>Service Department</a>
        </body></html>
        """,
        "https://www.continentaltoyota.com": "<html><body>Continental Toyota</body></html>",
    }
    resolver = StubResolver(pages)
    result = await resolver.resolve(extracted=_cars_com_extracted(), listing_html="<html></html>")
    assert result.resolution_status == "needs_review"
    assert result.confidence_score >= 80
    assert result.phone_type in {None, "service"}


@pytest.mark.asyncio
async def test_resolver_name_or_address_mismatch_not_resolved() -> None:
    pages = {
        "https://www.continentaltoyota.com/used/Tesla/2023-Tesla-Model-Y-0c94d8c2.htm": """
        <html><body>
        <h1>Different Dealer</h1>
        <div>999 Unknown Street, Miami, FL 33101</div>
        <a href='tel:(708) 716-4497'>Call Sales</a>
        </body></html>
        """,
        "https://www.continentaltoyota.com": "<html><body>Different Dealer</body></html>",
    }
    resolver = StubResolver(pages)
    result = await resolver.resolve(extracted=_cars_com_extracted(), listing_html="<html></html>")
    assert result.resolution_status != "resolved"


@pytest.mark.asyncio
async def test_resolver_low_confidence_listing_phone_requires_review() -> None:
    extracted = _cars_com_extracted()
    extracted.phone_from_listing = "(855) 215-7369"
    resolver = StubResolver({})
    result = await resolver.resolve(extracted=extracted, listing_html="<html><body>minimal page</body></html>")
    assert result.resolution_status == "needs_review"
    assert result.resolved_phone_e164 == "+18552157369"
    assert result.source_type == "cars.com"
