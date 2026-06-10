from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.models import Base
from app.repositories import create_job, find_duplicate_listing_job, get_job
from app.schemas import ExtractionResult, SpokenNormalizationResult
from app.services.elevenlabs_client import ElevenLabsService
from app.services.openai_client import OpenAIService
from app.services.telegram_delivery import safe_send_message
from app.services.workflow import CallWorkflow
from app.utils.listing import listing_fingerprint


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.deleted_messages: list[int] = []
        self._next_message_id = 1000

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self._next_message_id += 1
        self.messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "kwargs": kwargs,
                "message_id": self._next_message_id,
            }
        )
        return type("Msg", (), {"message_id": self._next_message_id})()

    async def delete_message(self, chat_id: int, message_id: int):
        self.deleted_messages.append(message_id)
        return True


class FakeOpenAIForReview(OpenAIService):
    async def normalize_spoken(self, extracted: ExtractionResult, *, call_language: str = "ru") -> SpokenNormalizationResult:
        assert call_language == "en"
        return SpokenNormalizationResult(
            car_spoken_ru="Tesla Model Y Performance",
            price_used_spoken_ru="twenty eight thousand nine hundred seventy two dollars",
            price_total_spoken_ru="twenty eight thousand nine hundred seventy two dollars",
            vehicle_price_spoken_ru="twenty eight thousand nine hundred seventy two dollars",
            year_spoken_ru="two thousand twenty three",
            mileage_spoken_ru="sixty seven thousand one hundred fifty seven miles",
            inspection_spoken_ru=None,
        )


class FakeElevenCapture(ElevenLabsService):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.calls: list[dict] = []

    async def start_outbound_call(
        self, *, call_phone: str, dynamic_variables: dict, agent_id_override: str | None = None
    ):
        self.calls.append(
            {
                "call_phone": call_phone,
                "dynamic_variables": dict(dynamic_variables),
                "agent_id_override": agent_id_override,
            }
        )
        return {"success": True, "conversation_id": "conv-review", "callSid": "sid-review"}


class FakeElevenShouldNotStart(ElevenLabsService):
    async def start_outbound_call(
        self, *, call_phone: str, dynamic_variables: dict, agent_id_override: str | None = None
    ):
        raise AssertionError("Outbound call must not start with invalid dynamic variables")


@dataclass
class FailingThenWorkingBot:
    failures_left: int

    def __post_init__(self) -> None:
        self.calls = 0

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.calls += 1
        if self.failures_left:
            self.failures_left -= 1
            raise OSError("temporary telegram network failure")
        return type("Msg", (), {"message_id": 42})()


@pytest.fixture()
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield maker
    finally:
        await engine.dispose()


def test_listing_fingerprint_ignores_tracking_query_params() -> None:
    assert (
        listing_fingerprint("https://www.cars.com/vehicledetail/0c94d8c2-659a-44f7-871e-4392a355428a/?foo=1")
        == "cars.com:0c94d8c2-659a-44f7-871e-4392a355428a"
    )
    assert (
        listing_fingerprint("https://www.carsensor.net/usedcar/detail/AU6938672228/index.html?TRCD=200002")
        == "carsensor:AU6938672228"
    )


@pytest.mark.asyncio
async def test_duplicate_guard_blocks_same_cars_com_listing(session_maker) -> None:
    async with session_maker() as session:
        existing = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=10,
            source="cars.com",
            listing_url="https://www.cars.com/vehicledetail/0c94d8c2-659a-44f7-871e-4392a355428a/",
        )
        existing.status = "completed"
        existing.provider_call_sid = "CA111"
        existing.listing_fingerprint = None  # Old rows before 0008 are still protected by URL-token fallback.
        await session.commit()

        duplicate = await find_duplicate_listing_job(
            session,
            listing_url="https://www.cars.com/vehicledetail/0c94d8c2-659a-44f7-871e-4392a355428a/?seller=again",
        )

        assert duplicate is not None
        assert duplicate.id == existing.id


@pytest.mark.asyncio
async def test_duplicate_guard_blocks_same_carsensor_listing_with_different_query(session_maker) -> None:
    async with session_maker() as session:
        existing = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=11,
            source="carsensor",
            listing_url="https://www.carsensor.net/usedcar/detail/AU6938672228/index.html?TRCD=200002",
        )
        existing.status = "call_created"
        existing.provider_call_sid = "CA222"
        existing.listing_fingerprint = None
        await session.commit()

        duplicate = await find_duplicate_listing_job(
            session,
            listing_url="https://www.carsensor.net/usedcar/detail/AU6938672228/index.html?RESTID=CS210610",
        )

        assert duplicate is not None
        assert duplicate.id == existing.id


@pytest.mark.asyncio
async def test_duplicate_guard_does_not_block_distinct_or_precall_failed_listing(session_maker) -> None:
    async with session_maker() as session:
        failed = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=12,
            source="cars.com",
            listing_url="https://www.cars.com/vehicledetail/11111111-1111-1111-1111-111111111111/",
        )
        failed.status = "normalization_failed"
        await session.commit()

        same_failed = await find_duplicate_listing_job(
            session,
            listing_url="https://www.cars.com/vehicledetail/11111111-1111-1111-1111-111111111111/",
        )
        different = await find_duplicate_listing_job(
            session,
            listing_url="https://www.cars.com/vehicledetail/22222222-2222-2222-2222-222222222222/",
        )

        assert same_failed is None
        assert different is None


@pytest.mark.asyncio
async def test_phone_review_approve_normalizes_before_outbound(session_maker) -> None:
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        ELEVENLABS_AGENT_ID_EN="agent-en-test",
        OFFICE_HOURS_FALLBACK="00:00-23:59",
        POST_CALL_FALLBACK_ENABLED=False,
    )
    eleven = FakeElevenCapture(settings)
    workflow = CallWorkflow(
        settings=settings,
        openai_service=FakeOpenAIForReview(settings),
        elevenlabs_service=eleven,
    )
    bot = FakeBot()

    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=13,
            source="cars.com",
            listing_url="https://www.cars.com/vehicledetail/0c94d8c2-659a-44f7-871e-4392a355428a/",
        )
        job.status = "dealer_phone_needs_review"
        job.source = "cars.com"
        job.call_language = "en"
        job.office_tz = "America/Chicago"
        job.car = "2023 Tesla Model Y Performance"
        job.car_full = "2023 Tesla Model Y Performance Dual Motor All-Wheel Drive"
        job.car_short = "2023 Tesla Model Y Performance"
        job.vin = "7SAYGDEF1PF795089"
        job.stock_number = "P11988A"
        job.year = "2023"
        job.price_total_jpy = 28_972
        job.vehicle_price_jpy = 28_972
        job.price_used_jpy = 28_972
        job.price_used_type = "listing_price_usd"
        job.mileage = "67,157 mi"
        job.dealer = "Continental Toyota"
        job.dealer_address = "6701 South La Grange Road, Hodgkins, IL 60525"
        job.resolver_result_json = {
            "candidates": [
                {
                    "phone": "(708) 716-4497",
                    "score": 60,
                    "source_type": "official_dealer_website",
                }
            ]
        }
        await session.commit()

        await workflow.approve_phone_review(session=session, job=job, bot=bot, candidate_idx=0)

        refreshed = await get_job(session, job.id)
        assert refreshed is not None
        assert refreshed.status == "call_created"
        assert refreshed.car_spoken_ru == "Tesla Model Y Performance"
        assert refreshed.price_used_spoken_ru == "twenty eight thousand nine hundred seventy two dollars"
        assert eleven.calls
        dynamic = eleven.calls[0]["dynamic_variables"]
        assert dynamic["car_spoken_ru"] == "Tesla Model Y Performance"
        assert dynamic["price_used_spoken_ru"] == "twenty eight thousand nine hundred seventy two dollars"
        assert dynamic["call_language"] == "en"
        assert dynamic["vin"] == "7SAYGDEF1PF795089"
        assert dynamic["stock_number"] == "P11988A"
        assert dynamic["car_spoken_ru"] not in {"None", None, ""}


@pytest.mark.asyncio
async def test_outbound_is_blocked_when_required_dynamic_variables_are_missing(session_maker) -> None:
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        OFFICE_HOURS_FALLBACK="00:00-23:59",
        POST_CALL_FALLBACK_ENABLED=False,
    )
    workflow = CallWorkflow(
        settings=settings,
        openai_service=FakeOpenAIForReview(settings),
        elevenlabs_service=FakeElevenShouldNotStart(settings),
    )
    bot = FakeBot()

    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=14,
            source="cars.com",
            listing_url="https://www.cars.com/vehicledetail/33333333-3333-3333-3333-333333333333/",
        )
        job.source = "cars.com"
        job.call_language = "en"
        job.call_phone = "+17087164497"
        job.office_tz = "America/Chicago"
        job.price_used_spoken_ru = "twenty eight thousand dollars"
        await session.commit()

        await workflow._start_attempt(session=session, job=job, bot=bot, effective_test_mode=False)

        refreshed = await get_job(session, job.id)
        assert refreshed is not None
        assert refreshed.status == "dynamic_variables_invalid"
        assert refreshed.provider_call_sid is None
        assert any("dynamic_variables_invalid" in msg["text"] for msg in bot.messages)


@pytest.mark.asyncio
async def test_safe_telegram_send_retries_transient_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import telegram_delivery

    async def no_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(telegram_delivery.asyncio, "sleep", no_sleep)
    bot = FailingThenWorkingBot(failures_left=2)

    message = await safe_send_message(bot, 1, "hello")

    assert message is not None
    assert message.message_id == 42
    assert bot.calls == 3
