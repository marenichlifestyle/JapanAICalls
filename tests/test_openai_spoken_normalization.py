from __future__ import annotations

import pytest

from app.config import Settings
from app.schemas import ExtractionResult
from app.services.openai_client import OpenAIService


class FakeOpenAIService(OpenAIService):
    def __init__(self, settings: Settings, response: dict):
        super().__init__(settings)
        self.response = response

    async def _structured(self, **kwargs):  # type: ignore[override]
        return self.response


def _base_extracted() -> ExtractionResult:
    return ExtractionResult(
        source="carsensor",
        listing_url="https://example.com/listing",
        car="Mercedes-Benz GLS 400 d AMG Line",
        car_full="Mercedes-Benz GLS 400 d 4MATIC AMG Line One Owner Panoramic Roof",
        car_short="Mercedes-Benz GLS 400 d AMG Line",
        price_total_jpy=4_973_000,
        vehicle_price_jpy=4_900_000,
        price_total_source_text="4,973,000円",
        vehicle_price_source_text="4,900,000円",
        price_confidence=0.95,
        price_used_jpy=4_973_000,
        price_used_type="total_price",
        year="2026",
        mileage="45,000 km",
        extraction_confidence=0.95,
        missing_fields=[],
    )


@pytest.mark.asyncio
async def test_normalize_spoken_ru_compacts_car_and_year() -> None:
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="x",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        OPENAI_API_KEY="k",
    )
    response = {
        "car_spoken_ru": "мерседес бенц глс четыреста амг лайн панорама люк подогрев сидений",
        "price_used_spoken_ru": "четыре миллиона девятьсот семьдесят три тысяч иен",
        "price_total_spoken_ru": "четыре миллиона девятьсот семьдесят три тысяч иен",
        "vehicle_price_spoken_ru": "четыре миллиона девятьсот тысяч иен",
        "year_spoken_ru": "две тысячи двадцать шестой (2026)",
        "mileage_spoken_ru": "сорок пять тысяч километров",
        "inspection_spoken_ru": "без данных",
    }
    service = FakeOpenAIService(settings, response)

    result = await service.normalize_spoken(_base_extracted(), call_language="ru")
    assert result.car_spoken_ru == "мерседес бенц глс четыреста"
    assert result.year_spoken_ru == "2026"


@pytest.mark.asyncio
async def test_normalize_spoken_ja_compacts_car_and_year() -> None:
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="x",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        OPENAI_API_KEY="k",
    )
    response = {
        "car_spoken_ru": "メルセデス ベンツ ジーエルエス パノラマ サンルーフ ワンオーナー",
        "price_used_spoken_ru": "よんひゃくきゅうじゅうななまんさんぜんえん",
        "price_total_spoken_ru": "よんひゃくきゅうじゅうななまんさんぜんえん",
        "vehicle_price_spoken_ru": "よんひゃくきゅうじゅうまんえん",
        "year_spoken_ru": "2021年",
        "mileage_spoken_ru": "よんまんごせんキロ",
        "inspection_spoken_ru": "なし",
    }
    service = FakeOpenAIService(settings, response)

    result = await service.normalize_spoken(_base_extracted(), call_language="ja")
    assert result.car_spoken_ru == "Mercedes-Benz GLS 400 d"
    assert result.year_spoken_ru == "2021"


@pytest.mark.asyncio
async def test_normalize_spoken_en_compacts_car_and_year() -> None:
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="x",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        OPENAI_API_KEY="k",
    )
    response = {
        "car_spoken_ru": "Mercedes Benz GLS 400 AMG Line Premium Plus Package Panoramic Roof",
        "price_used_spoken_ru": "four hundred ninety seven thousand three hundred dollars",
        "price_total_spoken_ru": "four hundred ninety seven thousand three hundred dollars",
        "vehicle_price_spoken_ru": "four hundred ninety thousand dollars",
        "year_spoken_ru": "Model year 2021",
        "mileage_spoken_ru": "forty five thousand miles",
        "inspection_spoken_ru": "none",
    }
    service = FakeOpenAIService(settings, response)

    result = await service.normalize_spoken(_base_extracted(), call_language="en")
    assert result.car_spoken_ru == "Mercedes-Benz GLS 400 d AMG"
    assert result.year_spoken_ru == "2021"


@pytest.mark.asyncio
async def test_normalize_spoken_ja_allows_model_latin_and_digits() -> None:
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="x",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        OPENAI_API_KEY="k",
    )
    response = {
        "car_spoken_ru": "メルセデス・ベンツ GLS 400 d",
        "price_used_spoken_ru": "718万6000円",
        "price_total_spoken_ru": "718万6000円",
        "vehicle_price_spoken_ru": "700万円",
        "year_spoken_ru": "2021",
        "mileage_spoken_ru": "4万5千キロ",
        "inspection_spoken_ru": "なし",
    }
    service = FakeOpenAIService(settings, response)

    result = await service.normalize_spoken(_base_extracted(), call_language="ja")
    assert result.car_spoken_ru == "メルセデス・ベンツ GLS 400 d"
    assert result.price_used_spoken_ru == "718万6000円"
    assert result.year_spoken_ru == "2021"
