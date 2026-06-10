from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.models import Base
from app.repositories import create_job, get_job
from app.schemas import CallAnalysisResult, DealerPhoneResolutionResult, ExtractionResult, SpokenNormalizationResult
from app.services import workflow as workflow_module
from app.services.elevenlabs_client import ElevenLabsService
from app.services.openai_client import OpenAIService
from app.services.webhook_processor import WebhookProcessor
from app.services.workflow import CallWorkflow


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.documents: list[dict] = []
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

    async def send_document(self, chat_id: int, file, **kwargs):
        self.documents.append({"chat_id": chat_id, "data": file.data, "kwargs": kwargs})
        return True

    async def delete_message(self, chat_id: int, message_id: int):
        self.deleted_messages.append(message_id)
        return True


@dataclass
class FakeArtifacts:
    html: str
    text: str
    source: str


class FakeOpenAI(OpenAIService):
    def __init__(self, settings: Settings):
        super().__init__(settings)

    async def extract_listing(self, *, url: str, text: str, html_fragments: str) -> ExtractionResult:
        return ExtractionResult(
            source="openai_fallback",
            listing_url=url,
            car="Mercedes-Benz E-Class E200 Avantgarde",
            car_full="Mercedes-Benz E-Class E200 Avantgarde ISG MP202502",
            car_short="Mercedes-Benz E-Class E200 Avantgarde",
            price_total_jpy=2_390_000,
            vehicle_price_jpy=2_190_000,
            price_total_source_text="支払総額 239.0万円",
            vehicle_price_source_text="車両本体価格 219.0万円",
            price_confidence=0.95,
            price_used_jpy=2_390_000,
            price_used_type="total_price",
            year="2018",
            mileage="45000km",
            repair_history="なし",
            inspection="2027/04",
            dealer="Tokyo Auto",
            dealer_address="Tokyo",
            carsensor_free_phone=None,
            dealer_direct_phone="0438-41-1300",
            extraction_confidence=0.96,
            missing_fields=[],
        )

    async def normalize_spoken(self, extracted: ExtractionResult, *, call_language: str = "ru") -> SpokenNormalizationResult:
        return SpokenNormalizationResult(
            car_spoken_ru="мерседес бенц е класс е двести авангард",
            price_used_spoken_ru="два миллиона триста девяносто тысяч иен",
            price_total_spoken_ru="два миллиона триста девяносто тысяч иен",
            vehicle_price_spoken_ru="два миллиона сто девяносто тысяч иен",
            year_spoken_ru="две тысячи восемнадцатый",
            mileage_spoken_ru="сорок пять тысяч километров",
            inspection_spoken_ru="до апреля две тысячи двадцать седьмого",
        )

    async def analyze_call(self, transcript: str, summary: str) -> CallAnalysisResult:
        return CallAnalysisResult(
            available=True,
            price_confirmed=True,
            actual_price="2390000",
            price_change_reason=None,
            condition_notes="Хорошее состояние",
            seller_mood="Спокойный",
            next_step="Перезвонить завтра",
            final_summary_ru="Авто в продаже, цена подтверждена",
            conclusion="Можно продолжать сделку",
            ai_quality_score=91,
            ai_quality_reason="агент уточнил наличие и цену",
        )


class FakeEleven(ElevenLabsService):
    async def start_outbound_call(
        self, *, call_phone: str, dynamic_variables: dict, agent_id_override: str | None = None
    ):
        assert call_phone == "+33768013446"
        assert agent_id_override is None
        required_keys = {
            "car_spoken_ru",
            "price_used_spoken_ru",
            "vehicle_price_spoken_ru",
            "year_spoken_ru",
            "mileage_spoken_ru",
            "dealer",
            "listing_url",
            "car_full",
            "car_short",
            "price_used_jpy",
            "price_used_type",
            "extracted_phone",
            "call_phone",
            "call_language",
            "test_mode",
            "job_id",
            "vin",
            "stock_number",
        }
        assert required_keys.issubset(set(dynamic_variables.keys()))
        assert dynamic_variables["price_used_jpy"] == 2_390_000
        assert dynamic_variables["call_language"] == "ru"
        assert dynamic_variables["vin"] == "-"
        assert dynamic_variables["stock_number"] == "-"
        return {"success": True, "conversation_id": "conv-1", "callSid": "sid-1"}


class FakeElevenShouldNotStart(ElevenLabsService):
    async def start_outbound_call(
        self, *, call_phone: str, dynamic_variables: dict, agent_id_override: str | None = None
    ):
        raise AssertionError("Outbound call must not start when resolver score < 80")


class FakeElevenTestFallback(ElevenLabsService):
    async def start_outbound_call(
        self, *, call_phone: str, dynamic_variables: dict, agent_id_override: str | None = None
    ):
        assert call_phone == "+33768013446"
        return {"success": True, "conversation_id": "conv-test-fallback", "callSid": "sid-test-fallback"}


@pytest.mark.asyncio
async def test_happy_path(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        TEST_CALL_PHONE="+33768013446",
        OFFICE_HOURS_FALLBACK="00:00-23:59",
        POST_CALL_FALLBACK_ENABLED=False,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_bot = FakeBot()

    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=77,
            listing_url="https://www.carsensor.net/usedcar/detail/AU1",
        )

        async def fake_fetch(url: str, timeout: float = 30.0):
            return FakeArtifacts(html="<html><title>x</title></html>", text="no phone", source="httpx")

        def fake_parse(url: str, html: str, text: str):
            return ExtractionResult(
                source="deterministic",
                listing_url=url,
                car=None,
                car_full=None,
                car_short=None,
                price_total_jpy=None,
                vehicle_price_jpy=None,
                price_total_source_text=None,
                vehicle_price_source_text=None,
                price_confidence=0,
                price_used_jpy=None,
                price_used_type=None,
                year=None,
                mileage=None,
                repair_history=None,
                inspection=None,
                dealer=None,
                dealer_address=None,
                carsensor_free_phone=None,
                dealer_direct_phone=None,
                extraction_confidence=0.2,
                missing_fields=["car", "price", "dealer", "phone"],
            )

        monkeypatch.setattr(workflow_module, "fetch_listing_page", fake_fetch)
        monkeypatch.setattr(workflow_module, "parse_deterministic", fake_parse)

        workflow = CallWorkflow(
            settings=settings,
            openai_service=FakeOpenAI(settings),
            elevenlabs_service=FakeEleven(settings),
        )

        await workflow.run(session=session, job=job, bot=fake_bot)

        refreshed = await get_job(session, job.id)
        assert refreshed is not None
        assert refreshed.status == "call_created"
        assert refreshed.extracted_phone == "+81438411300"
        assert refreshed.call_phone == "+33768013446"
        assert refreshed.call_language == "ru"
        assert refreshed.car_full == "Mercedes-Benz E-Class E200 Avantgarde ISG MP202502"
        assert refreshed.car_short == "Mercedes-Benz E-Class E200 Avantgarde"
        assert refreshed.price_used_jpy == 2_390_000
        assert refreshed.price_used_type == "total_price"
        assert refreshed.telegram_source_message_id == 77
        assert refreshed.telegram_service_message_ids
        assert any(
            "Тестовый режим: звонок выполнен на +33768013446" in msg["text"] for msg in fake_bot.messages
        )

        payload = {
            "type": "post_call_transcription",
            "event_timestamp": 1739537297,
            "data": {
                "conversation_id": "conv-1",
                "status": "done",
                "transcript": [
                    {"role": "agent", "message": "Здравствуйте"},
                    {"role": "user", "message": "Да, машина в продаже"},
                ],
                "analysis": {"summary": "available and confirmed"},
                "conversation_initiation_client_data": {
                    "dynamic_variables": {"job_id": str(job.id)}
                },
                "metadata": {},
            },
        }

        processor = WebhookProcessor(
            elevenlabs=FakeEleven(settings),
            openai_service=FakeOpenAI(settings),
        )
        await processor.handle(session=session, bot=fake_bot, payload=payload)

        refreshed = await get_job(session, job.id)
        assert refreshed is not None
        assert refreshed.status == "completed"
        assert refreshed.analysis_price_confirmed is True
        assert refreshed.analysis_ai_quality_score == 91
        assert refreshed.analysis_ai_quality_reason == "агент уточнил наличие и цену"
        assert refreshed.telegram_service_message_ids == []
        assert fake_bot.deleted_messages
        assert any("<blockquote expandable>" in msg["text"] for msg in fake_bot.messages)
        assert any("Финальный отчёт" in msg["text"] for msg in fake_bot.messages)
        assert any("Оценка AI" in msg["text"] and "91/100" in msg["text"] for msg in fake_bot.messages)
        assert any(msg["kwargs"].get("parse_mode") == "HTML" for msg in fake_bot.messages)
        assert all(msg["kwargs"].get("reply_to_message_id") == 77 for msg in fake_bot.messages[-2:])

        message_count = len(fake_bot.messages)
        await processor.handle(session=session, bot=fake_bot, payload=payload)
        assert len(fake_bot.messages) == message_count

    await engine.dispose()


class FakeUSResolverResolved:
    async def resolve(self, *, extracted: ExtractionResult, listing_html: str | None = None) -> DealerPhoneResolutionResult:
        return DealerPhoneResolutionResult(
            listing_url=extracted.listing_url,
            source="cars.com",
            dealer_name=extracted.dealer,
            dealer_address=extracted.dealer_address,
            listing_phone_raw=None,
            listing_phone_type="missing",
            resolved_phone_raw="(708) 716-4497",
            resolved_phone_e164="+17087164497",
            resolved_phone_source_url=extracted.dealer_website_url,
            source_type="official_dealer_website",
            phone_type="sales",
            confidence_score=95,
            resolution_status="resolved",
            evidence=[],
            candidates=[],
        )


class FakeUSResolverNeedsReview:
    async def resolve(self, *, extracted: ExtractionResult, listing_html: str | None = None) -> DealerPhoneResolutionResult:
        return DealerPhoneResolutionResult(
            listing_url=extracted.listing_url,
            source="cars.com",
            dealer_name=extracted.dealer,
            dealer_address=extracted.dealer_address,
            listing_phone_raw=None,
            listing_phone_type="missing",
            resolved_phone_raw="(708) 555-1000",
            resolved_phone_e164="+17085551000",
            resolved_phone_source_url="https://directory.example",
            source_type="directory",
            phone_type="service",
            confidence_score=60,
            resolution_status="needs_review",
            evidence=[],
            candidates=[{"phone": "(708) 555-1000", "score": 60, "source_type": "directory"}],
        )


class FakeOpenAICarsCom(FakeOpenAI):
    async def extract_cars_com_with_web_search(self, *, url: str) -> ExtractionResult:
        return ExtractionResult(
            source="cars.com_openai_websearch",
            listing_url=url,
            car="2023 Tesla Model Y Performance",
            car_full="2023 Tesla Model Y Performance Dual Motor All-Wheel Drive",
            car_short="2023 Tesla Model Y Performance",
            vehicle_title="2023 Tesla Model Y Performance Dual Motor All-Wheel Drive",
            price_total_jpy=28972,
            vehicle_price_jpy=28972,
            price_total_source_text="$28,972",
            vehicle_price_source_text="$28,972",
            price_confidence=0.95,
            price_used_jpy=28972,
            price_used_type="listing_price_usd",
            year="2023",
            mileage="67,157 mi",
            dealer="Continental Toyota",
            dealer_address="6701 South La Grange Road, Hodgkins, IL 60525",
            dealer_website_url="https://www.continentaltoyota.com",
            dealer_vehicle_url="https://www.continentaltoyota.com/used/Tesla",
            vin="7SAYGDEF1PF795089",
            stock_number="P11988A",
            phone_from_listing=None,
            carsensor_free_phone=None,
            dealer_direct_phone=None,
            extraction_confidence=0.95,
            missing_fields=[],
        )

    async def normalize_spoken(self, extracted: ExtractionResult, *, call_language: str = "ru") -> SpokenNormalizationResult:
        if call_language == "en":
            return SpokenNormalizationResult(
                car_spoken_ru="Tesla Model Y Performance",
                price_used_spoken_ru="twenty eight thousand nine hundred seventy two dollars",
                price_total_spoken_ru="twenty eight thousand nine hundred seventy two dollars",
                vehicle_price_spoken_ru="twenty eight thousand nine hundred seventy two dollars",
                year_spoken_ru="two thousand twenty three",
                mileage_spoken_ru="sixty seven thousand one hundred fifty seven miles",
                inspection_spoken_ru="not applicable",
            )
        return await super().normalize_spoken(extracted, call_language=call_language)


class FakeElevenEN(ElevenLabsService):
    async def start_outbound_call(
        self, *, call_phone: str, dynamic_variables: dict, agent_id_override: str | None = None
    ):
        assert call_phone == "+17087164497"
        assert agent_id_override == "agent-en-test"
        assert dynamic_variables["call_language"] == "en"
        assert dynamic_variables["test_mode"] is False
        assert dynamic_variables["car_spoken_ru"] == "Tesla Model Y Performance"
        assert dynamic_variables["vin"] == "7SAYGDEF1PF795089"
        assert dynamic_variables["stock_number"] == "P11988A"
        return {"success": True, "conversation_id": "conv-en-1", "callSid": "sid-en-1"}


@pytest.mark.asyncio
async def test_cars_com_happy_path_uses_en_agent_and_resolved_phone(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        TEST_CALL_PHONE="+33768013446",
        ELEVENLABS_AGENT_ID_EN="agent-en-test",
        OFFICE_HOURS_FALLBACK="00:00-23:59",
        POST_CALL_FALLBACK_ENABLED=False,
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_bot = FakeBot()
    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=81,
            source="cars.com",
            listing_url="https://www.cars.com/vehicledetail/abc/",
        )

        async def fake_fetch(url: str, timeout: float = 30.0):
            return FakeArtifacts(html="<html><title>x</title></html>", text="cars text", source="httpx")

        def fake_parse(url: str, html: str, text: str):
            return ExtractionResult(
                source="cars.com",
                listing_url=url,
                car="2023 Tesla Model Y Performance",
                car_full="2023 Tesla Model Y Performance Dual Motor All-Wheel Drive",
                car_short="2023 Tesla Model Y Performance",
                vehicle_title="2023 Tesla Model Y Performance Dual Motor All-Wheel Drive",
                price_total_jpy=28972,
                vehicle_price_jpy=28972,
                price_total_source_text="$28,972",
                vehicle_price_source_text="$28,972",
                price_confidence=0.95,
                price_used_jpy=28972,
                price_used_type="listing_price_usd",
                year="2023",
                mileage="67,157 mi",
                dealer="Continental Toyota",
                dealer_address="6701 South La Grange Road, Hodgkins, IL 60525",
                dealer_website_url="https://www.continentaltoyota.com",
                dealer_vehicle_url="https://www.continentaltoyota.com/used/Tesla",
                vin="7SAYGDEF1PF795089",
                stock_number="P11988A",
                phone_from_listing=None,
                carsensor_free_phone=None,
                dealer_direct_phone=None,
                extraction_confidence=0.95,
                missing_fields=[],
            )

        monkeypatch.setattr(workflow_module, "fetch_listing_page", fake_fetch)
        monkeypatch.setattr(workflow_module, "parse_cars_com_deterministic", fake_parse)
        workflow = CallWorkflow(
            settings=settings,
            openai_service=FakeOpenAICarsCom(settings),
            elevenlabs_service=FakeElevenEN(settings),
        )
        workflow.us_phone_resolver = FakeUSResolverResolved()
        await workflow.run(session=session, job=job, bot=fake_bot, call_language="ru")
        refreshed = await get_job(session, job.id)
        assert refreshed is not None
        assert refreshed.status == "call_created"
        assert refreshed.call_language == "en"
        assert refreshed.call_phone == "+17087164497"
        assert refreshed.resolver_status == "resolved"
        assert refreshed.vin == "7SAYGDEF1PF795089"
        assert refreshed.stock_number == "P11988A"
        assert not any("Тестовый режим: звонок выполнен на" in msg["text"] for msg in fake_bot.messages)
    await engine.dispose()


@pytest.mark.asyncio
async def test_cars_com_queues_in_dealer_local_timezone(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        TEST_CALL_PHONE="+33768013446",
        ELEVENLABS_AGENT_ID_EN="agent-en-test",
        OFFICE_HOURS_FALLBACK="09:00-19:00",
        US_TIMEZONE_FALLBACK="America/New_York",
        POST_CALL_FALLBACK_ENABLED=False,
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_bot = FakeBot()
    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=84,
            source="cars.com",
            listing_url="https://www.cars.com/vehicledetail/abc/",
        )

        async def fake_fetch(url: str, timeout: float = 30.0):
            return FakeArtifacts(html="<html><title>x</title></html>", text="cars text", source="httpx")

        def fake_parse(url: str, html: str, text: str):
            return ExtractionResult(
                source="cars.com",
                listing_url=url,
                car="2023 Tesla Model Y Performance",
                car_full="2023 Tesla Model Y Performance Dual Motor All-Wheel Drive",
                car_short="2023 Tesla Model Y Performance",
                vehicle_title="2023 Tesla Model Y Performance Dual Motor All-Wheel Drive",
                price_total_jpy=28972,
                vehicle_price_jpy=28972,
                price_total_source_text="$28,972",
                vehicle_price_source_text="$28,972",
                price_confidence=0.95,
                price_used_jpy=28972,
                price_used_type="listing_price_usd",
                year="2023",
                mileage="67,157 mi",
                dealer="Continental Toyota",
                dealer_address="6701 South La Grange Road, Hodgkins, IL 60525",
                dealer_website_url="https://www.continentaltoyota.com",
                dealer_vehicle_url="https://www.continentaltoyota.com/used/Tesla",
                phone_from_listing=None,
                carsensor_free_phone=None,
                dealer_direct_phone=None,
                extraction_confidence=0.95,
                missing_fields=[],
            )

        monkeypatch.setattr(workflow_module, "fetch_listing_page", fake_fetch)
        monkeypatch.setattr(workflow_module, "parse_cars_com_deterministic", fake_parse)
        workflow = CallWorkflow(
            settings=settings,
            openai_service=FakeOpenAICarsCom(settings),
            elevenlabs_service=FakeElevenShouldNotStart(settings),
        )
        workflow.us_phone_resolver = FakeUSResolverResolved()
        monkeypatch.setattr(
            workflow,
            "_job_now",
            lambda _job: datetime(2026, 5, 6, 22, 30, tzinfo=timezone(timedelta(hours=-5))),
        )

        await workflow.run(session=session, job=job, bot=fake_bot, call_language="ru")
        refreshed = await get_job(session, job.id)
        assert refreshed is not None
        assert refreshed.status == "queued"
        assert refreshed.office_tz == "America/Chicago"
        assert refreshed.next_attempt_at is not None
        assert any("America/Chicago" in msg["text"] for msg in fake_bot.messages)

    await engine.dispose()


@pytest.mark.asyncio
async def test_cars_com_unresolved_blocks_call_even_in_test_mode(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        TEST_CALL_PHONE="+33768013446",
        OFFICE_HOURS_FALLBACK="00:00-23:59",
        POST_CALL_FALLBACK_ENABLED=False,
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_bot = FakeBot()
    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=82,
            source="cars.com",
            listing_url="https://www.cars.com/vehicledetail/abc/",
        )

        async def fake_fetch(url: str, timeout: float = 30.0):
            return FakeArtifacts(html="<html><title>x</title></html>", text="cars text", source="httpx")

        def fake_parse(url: str, html: str, text: str):
            return ExtractionResult(
                source="cars.com",
                listing_url=url,
                car="2023 Tesla Model Y Performance",
                car_full="2023 Tesla Model Y Performance Dual Motor All-Wheel Drive",
                car_short="2023 Tesla Model Y Performance",
                vehicle_title="2023 Tesla Model Y Performance Dual Motor All-Wheel Drive",
                price_total_jpy=28972,
                vehicle_price_jpy=28972,
                price_total_source_text="$28,972",
                vehicle_price_source_text="$28,972",
                price_confidence=0.95,
                price_used_jpy=28972,
                price_used_type="listing_price_usd",
                year="2023",
                mileage="67,157 mi",
                dealer="Continental Toyota",
                dealer_address="6701 South La Grange Road, Hodgkins, IL 60525",
                dealer_website_url="https://www.continentaltoyota.com",
                dealer_vehicle_url="https://www.continentaltoyota.com/used/Tesla",
                phone_from_listing=None,
                carsensor_free_phone=None,
                dealer_direct_phone=None,
                extraction_confidence=0.95,
                missing_fields=[],
            )

        monkeypatch.setattr(workflow_module, "fetch_listing_page", fake_fetch)
        monkeypatch.setattr(workflow_module, "parse_cars_com_deterministic", fake_parse)
        workflow = CallWorkflow(
            settings=settings,
            openai_service=FakeOpenAICarsCom(settings),
            elevenlabs_service=FakeElevenShouldNotStart(settings),
        )
        workflow.us_phone_resolver = FakeUSResolverNeedsReview()
        await workflow.run(session=session, job=job, bot=fake_bot, call_language="ru")
        refreshed = await get_job(session, job.id)
        assert refreshed is not None
        assert refreshed.status == "dealer_phone_needs_review"
        assert refreshed.call_phone is None
        assert refreshed.call_language == "en"
        assert refreshed.resolver_status == "needs_review"

    await engine.dispose()


@pytest.mark.asyncio
async def test_cars_com_cloudflare_fallback_uses_openai_web_search(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        TEST_CALL_PHONE="+33768013446",
        ELEVENLABS_AGENT_ID_EN="agent-en-test",
        OFFICE_HOURS_FALLBACK="00:00-23:59",
        POST_CALL_FALLBACK_ENABLED=False,
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_bot = FakeBot()
    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=83,
            source="cars.com",
            listing_url="https://www.cars.com/vehicledetail/0c94d8c2-659a-44f7-871e-4392a355428a/",
        )

        async def fake_fetch(url: str, timeout: float = 30.0):
            return FakeArtifacts(
                html="<html><title>Attention Required! | Cloudflare</title></html>",
                text="Attention Required! Cloudflare block",
                source="httpx",
            )

        def fake_parse(url: str, html: str, text: str):
            return ExtractionResult(
                source="cars.com",
                listing_url=url,
                car="Attention Required!",
                car_full="Attention Required! | Cloudflare",
                car_short="Attention Required!",
                vehicle_title="Attention Required! | Cloudflare",
                price_total_jpy=None,
                vehicle_price_jpy=None,
                price_total_source_text=None,
                vehicle_price_source_text=None,
                price_confidence=0.0,
                price_used_jpy=None,
                price_used_type=None,
                year=None,
                mileage=None,
                dealer=None,
                dealer_address=None,
                phone_from_listing=None,
                carsensor_free_phone=None,
                dealer_direct_phone=None,
                extraction_confidence=0.2,
                missing_fields=["price", "dealer"],
            )

        monkeypatch.setattr(workflow_module, "fetch_listing_page", fake_fetch)
        monkeypatch.setattr(workflow_module, "parse_cars_com_deterministic", fake_parse)
        workflow = CallWorkflow(
            settings=settings,
            openai_service=FakeOpenAICarsCom(settings),
            elevenlabs_service=FakeElevenEN(settings),
        )
        workflow.us_phone_resolver = FakeUSResolverResolved()
        await workflow.run(session=session, job=job, bot=fake_bot, call_language="ru")
        refreshed = await get_job(session, job.id)
        assert refreshed is not None
        assert refreshed.status == "call_created"
        assert refreshed.source == "cars.com"
        assert refreshed.dealer == "Continental Toyota"
        assert refreshed.call_phone == "+17087164497"
    await engine.dispose()


class FakeOpenAIJa(FakeOpenAI):
    async def normalize_spoken(self, extracted: ExtractionResult, *, call_language: str = "ru") -> SpokenNormalizationResult:
        if call_language == "ja":
            return SpokenNormalizationResult(
                car_spoken_ru="ビーエムダブリュー ゴ シリーズ ゴ ニ サン アイ",
                price_used_spoken_ru="ごひゃくろくじゅうきゅうまんはっせんえん",
                price_total_spoken_ru="ごひゃくろくじゅうきゅうまんはっせんえん",
                vehicle_price_spoken_ru="ごひゃくろくじゅういちまんはっせんえん",
                year_spoken_ru="にせんにじゅうにねん",
                mileage_spoken_ru="いちまんにせんきろ",
                inspection_spoken_ru="にせんにじゅうななねんよんがつ",
            )
        return await super().normalize_spoken(extracted, call_language=call_language)


class FakeElevenJa(ElevenLabsService):
    async def start_outbound_call(
        self, *, call_phone: str, dynamic_variables: dict, agent_id_override: str | None = None
    ):
        assert call_phone == "+81438411300"
        assert agent_id_override == "agent-ja-test"
        assert dynamic_variables["call_language"] == "ja"
        assert dynamic_variables["test_mode"] is False
        return {"success": True, "conversation_id": "conv-ja-1", "callSid": "sid-ja-1"}


@pytest.mark.asyncio
async def test_ja_call_ignores_test_mode_and_uses_ja_agent(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        TEST_CALL_PHONE="+33768013446",
        ELEVENLABS_AGENT_ID_JA="agent-ja-test",
        OFFICE_HOURS_FALLBACK="00:00-23:59",
        POST_CALL_FALLBACK_ENABLED=False,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_bot = FakeBot()

    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=78,
            listing_url="https://www.carsensor.net/usedcar/detail/AU1",
        )

        async def fake_fetch(url: str, timeout: float = 30.0):
            return FakeArtifacts(html="<html><title>x</title></html>", text="no phone", source="httpx")

        def fake_parse(url: str, html: str, text: str):
            return ExtractionResult(
                source="deterministic",
                listing_url=url,
                car=None,
                car_full=None,
                car_short=None,
                price_total_jpy=None,
                vehicle_price_jpy=None,
                price_total_source_text=None,
                vehicle_price_source_text=None,
                price_confidence=0,
                price_used_jpy=None,
                price_used_type=None,
                year=None,
                mileage=None,
                repair_history=None,
                inspection=None,
                dealer=None,
                dealer_address=None,
                carsensor_free_phone=None,
                dealer_direct_phone=None,
                extraction_confidence=0.2,
                missing_fields=["car", "price", "dealer", "phone"],
            )

        monkeypatch.setattr(workflow_module, "fetch_listing_page", fake_fetch)
        monkeypatch.setattr(workflow_module, "parse_deterministic", fake_parse)

        workflow = CallWorkflow(
            settings=settings,
            openai_service=FakeOpenAIJa(settings),
            elevenlabs_service=FakeElevenJa(settings),
        )

        await workflow.run(session=session, job=job, bot=fake_bot, call_language="ja")
        refreshed = await get_job(session, job.id)
        assert refreshed is not None
        assert refreshed.status == "call_created"
        assert refreshed.call_language == "ja"
        assert refreshed.extracted_phone == "+81438411300"
        assert refreshed.call_phone == "+81438411300"
        assert not any("Тестовый режим: звонок выполнен на" in msg["text"] for msg in fake_bot.messages)

    await engine.dispose()


class FakeOpenAIProxyLow(FakeOpenAI):
    async def extract_listing(self, *, url: str, text: str, html_fragments: str) -> ExtractionResult:
        result = await super().extract_listing(url=url, text=text, html_fragments=html_fragments)
        result.phone_from_listing = "0078-6002-648302"
        result.carsensor_free_phone = "0078-6002-648302"
        result.dealer_direct_phone = None
        return result

    async def resolve_dealer_phone_with_web_search(
        self,
        *,
        listing_url: str,
        dealer_name: str | None,
        dealer_address: str | None,
        listing_phone_raw: str | None,
    ):
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
            error_reason=None,
        )


@pytest.mark.asyncio
async def test_workflow_blocks_autocall_when_resolver_score_low(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=False,
        OFFICE_HOURS_FALLBACK="00:00-23:59",
        POST_CALL_FALLBACK_ENABLED=False,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_bot = FakeBot()
    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=79,
            listing_url="https://www.carsensor.net/usedcar/detail/AU1",
        )

        async def fake_fetch(url: str, timeout: float = 30.0):
            return FakeArtifacts(html="<html><title>x</title></html>", text="no phone", source="httpx")

        def fake_parse(url: str, html: str, text: str):
            return ExtractionResult(
                source="deterministic",
                listing_url=url,
                car=None,
                car_full=None,
                car_short=None,
                price_total_jpy=None,
                vehicle_price_jpy=None,
                price_total_source_text=None,
                vehicle_price_source_text=None,
                price_confidence=0,
                price_used_jpy=None,
                price_used_type=None,
                year=None,
                mileage=None,
                repair_history=None,
                inspection=None,
                dealer=None,
                dealer_address=None,
                dealer_business_hours=None,
                dealer_closed_days=None,
                phone_from_listing=None,
                carsensor_free_phone=None,
                dealer_direct_phone=None,
                extraction_confidence=0.2,
                missing_fields=["car", "price", "dealer", "phone"],
            )

        monkeypatch.setattr(workflow_module, "fetch_listing_page", fake_fetch)
        monkeypatch.setattr(workflow_module, "parse_deterministic", fake_parse)

        workflow = CallWorkflow(
            settings=settings,
            openai_service=FakeOpenAIProxyLow(settings),
            elevenlabs_service=FakeElevenShouldNotStart(settings),
        )
        await workflow.run(session=session, job=job, bot=fake_bot)
        refreshed = await get_job(session, job.id)
        assert refreshed is not None
        assert refreshed.status == "dealer_phone_needs_review"
        assert refreshed.resolver_status == "needs_review"
        assert refreshed.resolver_confidence_score == 65
        assert all("звонок начался" not in msg["text"] for msg in fake_bot.messages)

    await engine.dispose()


@pytest.mark.asyncio
async def test_workflow_allows_testmode_call_when_resolver_unresolved(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        TEST_CALL_PHONE="+33768013446",
        OFFICE_HOURS_FALLBACK="00:00-23:59",
        POST_CALL_FALLBACK_ENABLED=False,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_bot = FakeBot()
    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=80,
            listing_url="https://www.carsensor.net/usedcar/detail/AU1",
        )

        async def fake_fetch(url: str, timeout: float = 30.0):
            return FakeArtifacts(html="<html><title>x</title></html>", text="no phone", source="httpx")

        def fake_parse(url: str, html: str, text: str):
            return ExtractionResult(
                source="deterministic",
                listing_url=url,
                car=None,
                car_full=None,
                car_short=None,
                price_total_jpy=None,
                vehicle_price_jpy=None,
                price_total_source_text=None,
                vehicle_price_source_text=None,
                price_confidence=0,
                price_used_jpy=None,
                price_used_type=None,
                year=None,
                mileage=None,
                repair_history=None,
                inspection=None,
                dealer=None,
                dealer_address=None,
                dealer_business_hours=None,
                dealer_closed_days=None,
                phone_from_listing=None,
                carsensor_free_phone=None,
                dealer_direct_phone=None,
                extraction_confidence=0.2,
                missing_fields=["car", "price", "dealer", "phone"],
            )

        monkeypatch.setattr(workflow_module, "fetch_listing_page", fake_fetch)
        monkeypatch.setattr(workflow_module, "parse_deterministic", fake_parse)

        workflow = CallWorkflow(
            settings=settings,
            openai_service=FakeOpenAIProxyLow(settings),
            elevenlabs_service=FakeElevenTestFallback(settings),
        )
        await workflow.run(session=session, job=job, bot=fake_bot)
        refreshed = await get_job(session, job.id)
        assert refreshed is not None
        assert refreshed.status == "call_created"
        assert refreshed.call_phone == "+33768013446"
        assert refreshed.resolver_status == "needs_review"
        assert any("прямой номер дилера не подтвержден" in msg["text"] for msg in fake_bot.messages)

    await engine.dispose()
