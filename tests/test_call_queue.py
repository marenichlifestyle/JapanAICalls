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
    async def extract_listing(self, *, url: str, text: str, html_fragments: str) -> ExtractionResult:
        return ExtractionResult(
            source="openai_fallback",
            listing_url=url,
            car="BMW 5",
            car_full="BMW 5 Series 523i",
            car_short="BMW 5 Series 523i",
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
            dealer_business_hours=None,
            dealer_closed_days=None,
            phone_from_listing="0438-41-1300",
            carsensor_free_phone=None,
            dealer_direct_phone="0438-41-1300",
            extraction_confidence=0.96,
            missing_fields=[],
        )

    async def normalize_spoken(self, extracted: ExtractionResult, *, call_language: str = "ru") -> SpokenNormalizationResult:
        return SpokenNormalizationResult(
            car_spoken_ru="бмв пятая серия пятьсот двадцать три",
            price_used_spoken_ru="два миллиона триста девяносто тысяч иен",
            price_total_spoken_ru="два миллиона триста девяносто тысяч иен",
            vehicle_price_spoken_ru="два миллиона сто девяносто тысяч иен",
            year_spoken_ru="две тысячи восемнадцатый",
            mileage_spoken_ru="сорок пять тысяч километров",
            inspection_spoken_ru="до апреля две тысячи двадцать седьмого",
        )

    async def analyze_call(self, transcript: str, summary: str) -> CallAnalysisResult:
        return CallAnalysisResult(
            available=None,
            price_confirmed=None,
            actual_price=None,
            price_change_reason=None,
            condition_notes=None,
            seller_mood=None,
            next_step=None,
            final_summary_ru=None,
            conclusion=None,
        )


class FakeElevenStart(ElevenLabsService):
    async def start_outbound_call(
        self, *, call_phone: str, dynamic_variables: dict, agent_id_override: str | None = None
    ):
        return {"success": True, "conversation_id": "conv-queue-1", "callSid": "sid-queue-1"}


class FakeElevenMonitor(ElevenLabsService):
    def __init__(self, settings: Settings, status: str):
        super().__init__(settings)
        self.status = status

    async def start_outbound_call(
        self, *, call_phone: str, dynamic_variables: dict, agent_id_override: str | None = None
    ):
        return {"success": True, "conversation_id": "conv-monitor", "callSid": "sid-monitor"}

    async def fetch_conversation_details(self, conversation_id: str) -> dict:
        return {
            "conversation_id": conversation_id,
            "status": self.status,
            "transcript": [],
            "analysis": {},
            "metadata": {},
        }

    async def fetch_conversation_audio(self, conversation_id: str) -> bytes | None:
        return None


class FakeElevenInitiatedNoProgress(FakeElevenMonitor):
    async def fetch_conversation_details(self, conversation_id: str) -> dict:
        return {
            "conversation_id": conversation_id,
            "status": "initiated",
            "transcript": [],
            "analysis": None,
            "metadata": {
                "accepted_time_unix_secs": None,
                "call_duration_secs": 0,
                "phone_call": {
                    "call_sid": "sid-initiated-stuck",
                    "stream_sid": "",
                },
            },
            "has_audio": False,
        }


class FakeOpenAIResolved(FakeOpenAI):
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
            listing_phone_type="normal",
            resolved_phone_raw="0438-41-1300",
            resolved_phone_e164="+81438411300",
            resolved_phone_source_url=listing_url,
            source_type="carsensor",
            confidence_score=100,
            resolution_status="resolved",
            evidence=[],
            candidates=[],
            error_reason=None,
        )


@pytest.mark.asyncio
async def test_queue_when_office_closed_for_ja(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        TEST_CALL_PHONE="+33768013446",
        OFFICE_HOURS_FALLBACK="09:00-19:00",
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
            source_message_id=101,
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
            openai_service=FakeOpenAIResolved(settings),
            elevenlabs_service=FakeElevenStart(settings),
        )
        monkeypatch.setattr(
            workflow,
            "_job_now",
            lambda _job: datetime(2026, 5, 6, 22, 30, tzinfo=timezone(timedelta(hours=9))),
        )

        await workflow.run(session=session, job=job, bot=fake_bot, call_language="ja")
        refreshed = await get_job(session, job.id)
        assert refreshed is not None
        assert refreshed.status == "queued"
        assert refreshed.next_attempt_at is not None
        assert any("нерабочее время" in msg["text"] for msg in fake_bot.messages)

    await engine.dispose()


@pytest.mark.asyncio
async def test_bypass_office_hours_for_carsensor_ru(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        TEST_CALL_PHONE="+33768013446",
        OFFICE_HOURS_FALLBACK="09:00-19:00",
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
            source_message_id=201,
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
            openai_service=FakeOpenAIResolved(settings),
            elevenlabs_service=FakeElevenStart(settings),
        )
        monkeypatch.setattr(
            workflow,
            "_job_now",
            lambda _job: datetime(2026, 5, 6, 22, 30, tzinfo=timezone(timedelta(hours=9))),
        )

        await workflow.run(session=session, job=job, bot=fake_bot, call_language="ru")
        refreshed = await get_job(session, job.id)
        assert refreshed is not None
        assert refreshed.status == "call_created"
        assert refreshed.next_attempt_at is None
        assert any("график работы игнорируется" in msg["text"] for msg in fake_bot.messages)
        assert not any("нерабочее время" in msg["text"] for msg in fake_bot.messages)

    await engine.dispose()


@pytest.mark.asyncio
async def test_monitor_schedules_retry_after_timeout(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        TEST_CALL_PHONE="+33768013446",
        OFFICE_HOURS_FALLBACK="00:00-23:59",
        POST_CALL_FALLBACK_ENABLED=False,
        CALL_RING_TIMEOUT_SEC=60,
        CALL_RETRY_INTERVAL_SEC=7200,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_bot = FakeBot()
    workflow = CallWorkflow(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeElevenMonitor(settings, status="ringing"),
    )
    monkeypatch.setattr(workflow_module, "SessionLocal", session_maker)

    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=102,
            listing_url="https://www.carsensor.net/usedcar/detail/AU1",
        )
        job.status = "call_started"
        job.call_language = "ru"
        job.call_phone = "+33768013446"
        job.elevenlabs_conversation_id = "conv-monitor"
        job.attempt_count = 1
        job.max_attempts = 3
        job.last_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=80)
        await session.commit()

    await workflow._monitor_active_calls(fake_bot)

    async with session_maker() as session:
        refreshed = await get_job(session, 1)
        assert refreshed is not None
        assert refreshed.status == "retry_scheduled"
        assert refreshed.next_attempt_at is not None
        assert refreshed.call_status == "no_answer"

    await engine.dispose()


@pytest.mark.asyncio
async def test_monitor_marks_provider_timeout_before_ringing(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        TEST_CALL_PHONE="+33768013446",
        OFFICE_HOURS_FALLBACK="00:00-23:59",
        POST_CALL_FALLBACK_ENABLED=False,
        CALL_RING_TIMEOUT_SEC=60,
        PROVIDER_PROGRESS_TIMEOUT_SEC=60,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_bot = FakeBot()
    workflow = CallWorkflow(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeElevenMonitor(settings, status="initiated"),
    )
    monkeypatch.setattr(workflow_module, "SessionLocal", session_maker)

    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=105,
            listing_url="https://www.carsensor.net/usedcar/detail/AU1",
        )
        job.status = "call_started"
        job.call_language = "ru"
        job.call_phone = "+33768013446"
        job.elevenlabs_conversation_id = "conv-provider-timeout"
        job.provider_call_sid = "sid-provider-timeout"
        job.attempt_count = 1
        job.max_attempts = 3
        job.last_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=80)
        await session.commit()

    await workflow._monitor_active_calls(fake_bot)

    async with session_maker() as session:
        refreshed = await get_job(session, 1)
        assert refreshed is not None
        assert refreshed.status == "provider_timeout"
        assert refreshed.call_status == "provider_timeout"
        assert refreshed.next_attempt_at is None
        assert any("не начал реальный дозвон" in msg["text"] for msg in fake_bot.messages)
        assert not any("Нет ответа" in msg["text"] for msg in fake_bot.messages)

    await engine.dispose()


@pytest.mark.asyncio
async def test_monitor_sends_provider_progress_only_on_status_change(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        TEST_CALL_PHONE="+33768013446",
        OFFICE_HOURS_FALLBACK="00:00-23:59",
        POST_CALL_FALLBACK_ENABLED=False,
        CALL_PROGRESS_PING_SEC=10,
        PROVIDER_PROGRESS_TIMEOUT_SEC=300,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_bot = FakeBot()
    workflow = CallWorkflow(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeElevenMonitor(settings, status="initiated"),
    )
    monkeypatch.setattr(workflow_module, "SessionLocal", session_maker)

    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=106,
            listing_url="https://www.carsensor.net/usedcar/detail/AU1",
        )
        job.status = "call_created"
        job.call_status = "call_created"
        job.call_language = "ru"
        job.call_phone = "+33768013446"
        job.elevenlabs_conversation_id = "conv-initiated-once"
        job.provider_call_sid = "sid-initiated-once"
        job.attempt_count = 1
        job.max_attempts = 3
        job.last_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=20)
        job.last_progress_at = datetime.now(timezone.utc) - timedelta(seconds=20)
        await session.commit()

    await workflow._monitor_active_calls(fake_bot)
    await workflow._monitor_active_calls(fake_bot)

    initiated_messages = [msg for msg in fake_bot.messages if msg["text"] == "Провайдер начал набор номера"]
    assert len(initiated_messages) == 1

    async with session_maker() as session:
        refreshed = await get_job(session, 1)
        assert refreshed is not None
        assert refreshed.status == "initiated"
        assert refreshed.call_status == "initiated"

    await engine.dispose()


@pytest.mark.asyncio
async def test_monitor_fast_fails_initiated_without_twilio_progress(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        TEST_CALL_PHONE="+33768013446",
        OFFICE_HOURS_FALLBACK="00:00-23:59",
        POST_CALL_FALLBACK_ENABLED=False,
        CALL_PROGRESS_PING_SEC=15,
        PROVIDER_PROGRESS_TIMEOUT_SEC=180,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_bot = FakeBot()
    workflow = CallWorkflow(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeElevenInitiatedNoProgress(settings, status="initiated"),
    )
    monkeypatch.setattr(workflow_module, "SessionLocal", session_maker)

    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=107,
            listing_url="https://www.carsensor.net/usedcar/detail/AU1",
            source="carsensor",
        )
        job.status = "call_created"
        job.call_status = "call_created"
        job.call_language = "en"
        job.call_phone = "+12604355330"
        job.elevenlabs_conversation_id = "conv-initiated-stuck"
        job.provider_call_sid = "sid-initiated-stuck"
        job.attempt_count = 1
        job.max_attempts = 1
        job.last_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=35)
        job.last_progress_at = datetime.now(timezone.utc) - timedelta(seconds=35)
        await session.commit()

    await workflow._monitor_active_calls(fake_bot)

    async with session_maker() as session:
        refreshed = await get_job(session, 1)
        assert refreshed is not None
        assert refreshed.status == "provider_timeout"
        assert refreshed.call_status == "provider_timeout"
        assert any("не начал реальный дозвон" in msg["text"] for msg in fake_bot.messages)

    await engine.dispose()


@pytest.mark.asyncio
async def test_monitor_does_not_retry_processing_after_answer(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        TEST_CALL_PHONE="+33768013446",
        OFFICE_HOURS_FALLBACK="00:00-23:59",
        POST_CALL_FALLBACK_ENABLED=False,
        CALL_RING_TIMEOUT_SEC=60,
        CALL_RETRY_INTERVAL_SEC=7200,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_bot = FakeBot()
    workflow = CallWorkflow(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeElevenMonitor(settings, status="processing"),
    )
    monkeypatch.setattr(workflow_module, "SessionLocal", session_maker)

    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=104,
            listing_url="https://www.carsensor.net/usedcar/detail/AU1",
        )
        answered_at = datetime.now(timezone.utc) - timedelta(seconds=80)
        job.status = "answered"
        job.call_language = "ja"
        job.call_phone = "+81155585711"
        job.elevenlabs_conversation_id = "conv-answered-processing"
        job.provider_call_sid = "sid-answered-processing"
        job.call_status = "in_progress"
        job.attempt_count = 2
        job.max_attempts = 3
        job.last_attempt_at = answered_at - timedelta(seconds=20)
        job.answered_at = answered_at
        job.first_answered_at = answered_at
        await session.commit()

    await workflow._monitor_active_calls(fake_bot)

    async with session_maker() as session:
        refreshed = await get_job(session, 1)
        assert refreshed is not None
        assert refreshed.status == "in_progress"
        assert refreshed.call_status == "in_progress"
        assert refreshed.next_attempt_at is None
        assert refreshed.elevenlabs_conversation_id == "conv-answered-processing"
        assert refreshed.provider_call_sid == "sid-answered-processing"
        assert not any("Нет ответа" in msg["text"] for msg in fake_bot.messages)

    await engine.dispose()


@pytest.mark.asyncio
async def test_monitor_finishes_after_third_no_answer(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        TEST_MODE=True,
        TEST_CALL_PHONE="+33768013446",
        OFFICE_HOURS_FALLBACK="00:00-23:59",
        POST_CALL_FALLBACK_ENABLED=False,
        CALL_RING_TIMEOUT_SEC=60,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fake_bot = FakeBot()
    workflow = CallWorkflow(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeElevenMonitor(settings, status="failed"),
    )
    monkeypatch.setattr(workflow_module, "SessionLocal", session_maker)

    async with session_maker() as session:
        job = await create_job(
            session,
            chat_id=1,
            user_id=1,
            source_message_id=103,
            listing_url="https://www.carsensor.net/usedcar/detail/AU1",
        )
        job.status = "call_started"
        job.call_language = "ru"
        job.call_phone = "+33768013446"
        job.elevenlabs_conversation_id = "conv-monitor"
        job.attempt_count = 3
        job.max_attempts = 3
        job.last_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=80)
        await session.commit()

    await workflow._monitor_active_calls(fake_bot)

    async with session_maker() as session:
        refreshed = await get_job(session, 1)
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert refreshed.call_status == "failed"
    await engine.dispose()


def test_stale_carsensor_ru_office_queue_is_canceled() -> None:
    job = type(
        "JobLike",
        (),
        {
            "source": "carsensor",
            "call_language": "ru",
            "queued_reason": "outside_office_hours",
            "elevenlabs_conversation_id": None,
            "provider_call_sid": None,
            "created_at": datetime.now(timezone.utc) - timedelta(minutes=20),
        },
    )()

    assert CallWorkflow._is_stale_carsensor_ru_office_queue(job, datetime.now(timezone.utc))
