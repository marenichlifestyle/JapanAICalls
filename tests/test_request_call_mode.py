from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.models import Base, CallReport, Job
from app.repositories import (
    create_request_campaign,
    get_latest_input_request_campaign,
    get_latest_open_request_campaign,
)
from app.schemas import GoalGenerationResult, RequestCallReportResult
from app.services.elevenlabs_client import ElevenLabsService
from app.services.openai_client import OpenAIService
from app.services.request_call import (
    REQUEST_GOAL_MAX_WORDS,
    REQUEST_CALL_PROCESSING_MESSAGE,
    ParsedDealerLine,
    RequestCallService,
    _word_count,
    build_request_campaign_summary_html_chunks,
    build_request_confirmation_text,
    build_request_target_report_html,
    fallback_goal_generation,
    is_goal_too_vague,
    parse_request_call_input,
)


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.documents: list[dict] = []
        self.deleted_messages: list[tuple[int, int]] = []
        self._next_message_id = 100

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self._next_message_id += 1
        self.messages.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        return type("Msg", (), {"message_id": self._next_message_id})()

    async def send_document(self, chat_id: int, document, **kwargs):
        self._next_message_id += 1
        self.documents.append({"chat_id": chat_id, "document": document, "kwargs": kwargs})
        return type("Msg", (), {"message_id": self._next_message_id})()

    async def delete_message(self, chat_id: int, message_id: int):
        self.deleted_messages.append((chat_id, message_id))
        return True


class FakeOpenAI(OpenAIService):
    goal_status = "ready"

    async def generate_goal_ru(
        self,
        *,
        dealer_name: str,
        city: str | None,
        phone_e164: str,
        raw_user_goal: str,
        call_language: str = "en",
        vehicle_context: list[dict] | None = None,
    ) -> GoalGenerationResult:
        self.goal_calls = getattr(self, "goal_calls", [])
        self.goal_calls.append(
            {
                "dealer_name": dealer_name,
                "phone_e164": phone_e164,
                "call_language": call_language,
            }
        )
        if call_language == "ja":
            return GoalGenerationResult(
                status=self.goal_status,
                goal_ru="販売部門に電話し、Ford Raptor Rの在庫または入庫予定、価格、VIN/stock、支払い条件を確認する。",
                target_vehicle="Ford Raptor R",
                main_intent="availability_or_nearest_incoming_unit",
                constraints=["no_credit"],
                required_questions=["availability", "price", "vin_or_stock_number"],
                fallback_questions=["nearest_expected_delivery"],
                completion_criteria=["availability_answer_received"],
            )
        return GoalGenerationResult(
            status=self.goal_status,
            goal_ru=(
                f"Call the sales department at {dealer_name}. Ask about Ford Raptor R. "
                "The customer is interested in a vehicle that is available now or the nearest incoming unit. "
                "Customer constraints: no financing, no leasing, payment by bank wire. "
                "Mandatory questions: availability, nearest incoming unit, price, configuration, color, "
                "VIN or stock number, paperwork timing, and payment terms. "
                "If the vehicle is incoming, ask for ETA, whether allocation is confirmed, reservation options, "
                "deposit amount, VIN or stock number if assigned, expected price, configuration, and color. "
                "If Ford Raptor R is unavailable, ask about the nearest expected delivery or similar configuration. "
                "Follow-up policy: ask one question at a time; if the answer is incomplete, rephrase and ask "
                "for specifics. Do not end the call until every mandatory item has an answer or the seller "
                "explicitly says they do not know. The call is successful if answers are obtained for "
                "availability, price, timing, and payment."
            ),
            target_vehicle="Ford Raptor R",
            main_intent="availability_or_nearest_incoming_unit",
            constraints=["no_credit", "no_lease", "bank_transfer"],
            required_questions=[
                "availability",
                "nearest_incoming_unit",
                "price_or_price_range",
                "configuration",
                "color",
                "vin_or_stock_number",
                "paperwork_timing",
                "payment_terms",
                "follow_up_for_incomplete_answers",
            ],
            fallback_questions=["nearest_expected_delivery", "reservation_or_waitlist", "similar_configuration"],
            completion_criteria=[
                "availability_or_incoming_answer_received",
                "price_answer_received",
                "timing_answer_received",
                "payment_answer_received",
                "missing_answers_marked_or_followed_up",
            ],
        )

    async def extract_request_call_report(self, *, transcript: str, goal_ru: str) -> RequestCallReportResult:
        return RequestCallReportResult(
            call_status="completed",
            reached_sales=True,
            target_vehicle_or_task="Ford Raptor R",
            summary="Есть ближайшая поставка, цену обещали прислать.",
            availability_result="нет в наличии",
            incoming_result="есть ближайшая поставка",
            price_result="обещали прислать",
            configuration_result="уточняют",
            vin_or_stock_result="not_answered",
            payment_result="перевод возможен",
            paperwork_result="not_answered",
            important_notes="попросили написать email",
            next_action="написать дилеру",
            ai_quality_score=88,
            ai_quality_reason="агент получил ключевые ответы",
        )


class FakeEleven(ElevenLabsService):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.calls: list[dict] = []

    async def start_outbound_call(
        self,
        *,
        call_phone: str,
        dynamic_variables: dict,
        agent_id_override: str | None = None,
    ):
        assert set(dynamic_variables.keys()) == {"goal_ru"}
        assert "Ford Raptor R" in dynamic_variables["goal_ru"]
        assert agent_id_override in {"agent_request", "agent_request_ja"}
        self.calls.append(
            {
                "call_phone": call_phone,
                "dynamic_variables": dynamic_variables,
                "agent_id_override": agent_id_override,
            }
        )
        return {"success": True, "conversation_id": "conv-request-1", "callSid": "sid-request-1"}

    async def fetch_conversation_audio(self, conversation_id: str) -> bytes | None:
        assert conversation_id == "conv-request-1"
        return b"fake mp3 bytes"


class CapturingOpenAI(OpenAIService):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.last_prompt = ""

    async def _structured(self, *, prompt: str, schema_name: str, schema: dict):
        self.last_prompt = prompt
        if schema_name == "request_call_goal":
            return {
                "status": "ready",
                "goal_ru": "Call the sales department at Duval Ford Jacksonville. Ask about Ford Raptor R.",
                "target_vehicle": "Ford Raptor R",
                "main_intent": "availability_or_nearest_incoming_unit",
                "constraints": ["no_credit"],
                "required_questions": ["availability", "incoming_timing"],
                "fallback_questions": ["nearest_expected_delivery"],
                "completion_criteria": ["all_required_answers_received"],
                "clarification_questions": [],
            }
        return {
            "call_status": "completed",
            "reached_sales": True,
            "target_vehicle_or_task": "Ford Raptor R",
            "summary": "ok",
            "availability_result": "not_answered",
            "incoming_result": "not_answered",
            "price_result": "not_answered",
            "configuration_result": "not_answered",
            "vin_or_stock_result": "not_answered",
            "payment_result": "not_answered",
            "paperwork_result": "not_answered",
            "important_notes": "missing answers",
            "next_action": "call back",
            "ai_quality_score": 82,
            "ai_quality_reason": "агент уточнил поставку, но не получил цену",
        }


class FakeContextExtractor:
    async def extract_many(self, urls: list[str]) -> list[dict]:
        return [
            {
                "source_url": urls[0],
                "vehicle_title": "2024 Ford Raptor R",
                "year": "2024",
                "make": "Ford",
                "model": "Raptor R",
                "trim": None,
                "color": "Shelter Green",
                "power": "700 hp",
                "price": "$112,000",
                "mileage": None,
                "vin": "1FTFW1RG0RF000001",
                "stock_number": "RAPTOR1",
                "dealer_name": "Duval Ford Jacksonville",
                "dealer_phone": "+19043876541",
                "dealer_address": None,
                "confidence": 0.92,
            }
        ]


def _settings() -> Settings:
    return Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        ELEVENLABS_REQUEST_AGENT_ID="agent_request",
        ELEVENLABS_REQUEST_AGENT_ID_JA="agent_request_ja",
    )


async def _session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, session_maker


async def _select_request_country(session: AsyncSession, campaign, region: str = "US"):
    campaign.phone_region = region
    campaign.call_language = "ja" if region == "JP" else "en"
    campaign.status = "draft"
    await session.commit()
    await session.refresh(campaign)
    return campaign


def test_parse_request_call_input_splits_dealers_goal_and_headers() -> None:
    parsed = parse_request_call_input(
        """
        Официальный дилер Город Номер телефона
        Duval Ford Jacksonville +1 (904) 387-6541
        AutoNation Ford Jacksonville Jacksonville +1 (904) 513-3392
        Key Ford of Jacksonville Jacksonville +1 (904) 779-1100
        Bozard Ford St. Augustine +1 (904) 824-1641
        Parks Ford of Gainesville Gainesville +1 (352) 378-1341

        Список дилеров на прозвон по Ford Raptor R из наличия или в ближайшей поставке.
        Интересует покупка без кредитов и лизингов, оплата переводом.
        """
    )
    assert len(parsed.dealers) == 5
    assert parsed.dealers[0].dealer_name == "Duval Ford Jacksonville"
    assert parsed.dealers[0].city == "Jacksonville"
    assert parsed.dealers[0].phone_raw == "+1 (904) 387-6541"
    assert parsed.dealers[0].phone_e164 == "+19043876541"
    assert parsed.dealers[0].phone_region == "US"
    assert parsed.dealers[1].dealer_name == "AutoNation Ford Jacksonville"
    assert "Ford Raptor R" in parsed.raw_user_goal
    assert parsed.status == "ready_to_confirm"


def test_parse_request_call_input_supports_jp_phones_urls_and_mixed_regions() -> None:
    parsed = parse_request_call_input(
        "東京BMW 03-1234-5678\n"
        "https://example.jp/car/123\n"
        "Задача: уточнить BMW M3 в наличии.",
        default_region="JP",
    )
    assert parsed.dealers[0].phone_e164 == "+81312345678"
    assert parsed.dealers[0].phone_region == "JP"
    assert parsed.source_urls == ["https://example.jp/car/123"]
    assert parsed.raw_user_goal == "уточнить BMW M3 в наличии."

    mixed = parse_request_call_input(
        "Duval Ford +1 (904) 387-6541\n"
        "東京BMW 03-1234-5678\n"
        "Задача: уточнить наличие.",
    )
    assert mixed.status == "ready_to_confirm"
    assert mixed.has_mixed_phone_regions
    assert [dealer.phone_region for dealer in mixed.dealers] == ["US", "JP"]

    us_selected_with_jp_number = parse_request_call_input(
        "東京BMW 03-1234-5678\nЗадача: уточнить наличие.",
        default_region="US",
    )
    assert us_selected_with_jp_number.status == "needs_phones"
    assert us_selected_with_jp_number.rejected_phones[0].reason == "wrong_country_expected_us"

    ru = parse_request_call_input("+79385170519\nСпросить про тойоту камри")
    assert ru.status == "ready_to_confirm"
    assert ru.dealers[0].phone_e164 == "+79385170519"
    assert ru.dealers[0].phone_region == "RU"

    fr = parse_request_call_input("Dealer +33 7 68 01 34 46\nЗадача: test")
    assert fr.status == "ready_to_confirm"
    assert fr.dealers[0].phone_e164 == "+33768013446"
    assert fr.dealers[0].phone_region == "FR"


def test_parse_request_call_input_missing_data_statuses_and_invalid_phone() -> None:
    only_phones = parse_request_call_input("Duval Ford Jacksonville +1 (904) 387-6541")
    assert only_phones.status == "needs_goal"

    only_goal = parse_request_call_input("Узнать наличие Ford Raptor R без кредита")
    assert only_goal.status == "needs_phones"

    invalid = parse_request_call_input("Bad Dealer +1 (111) 111-1111\nЗадача: Ford Raptor R")
    assert invalid.status == "needs_phones"
    assert invalid.rejected_phones[0].reason == "invalid_phone"

    assert is_goal_too_vague("узнать по машинам")


def test_parse_request_call_input_accepts_bare_us_ten_digit_numbers() -> None:
    parsed = parse_request_call_input(
        """
        5138125916
        6149141032
        4694608742

        Не RAM и не Ford

        Нужно уточнить по наличию Lexus LC новый, кабриолет, наценку и покупку на компанию.
        """,
        default_region="US",
    )
    assert parsed.status == "ready_to_confirm"
    assert [dealer.phone_e164 for dealer in parsed.dealers] == [
        "+15138125916",
        "+16149141032",
        "+14694608742",
    ]
    assert "Lexus LC" in parsed.raw_user_goal


@pytest.mark.asyncio
async def test_create_draft_accepts_initial_status_for_country_flow() -> None:
    engine, session_maker = await _session_maker()
    service = RequestCallService(
        settings=_settings(),
        openai_service=FakeOpenAI(_settings()),
        elevenlabs_service=FakeEleven(_settings()),
    )
    async with session_maker() as session:
        campaign = await service.create_draft(
            session=session,
            chat_id=10,
            user_id=1,
            status="needs_country",
            username="owner_name",
            display_name="Owner Admin",
        )
        assert campaign.status == "needs_country"
        assert campaign.call_sequence_mode == "manual"
        assert campaign.telegram_username == "owner_name"
        assert campaign.telegram_user_display_name == "Owner Admin"
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_campaign_requires_country_before_parsing_text() -> None:
    engine, session_maker = await _session_maker()
    service = RequestCallService(
        settings=_settings(),
        openai_service=FakeOpenAI(_settings()),
        elevenlabs_service=FakeEleven(_settings()),
    )
    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign.status = "needs_country"
        await session.commit()
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text="5138125916\nЗадача: уточнить Lexus LC",
        )
        targets = await service.list_targets(session, campaign.id)
        assert campaign.status == "needs_country"
        assert campaign.raw_input
        assert targets == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_campaign_blocks_numbers_from_wrong_selected_country() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeEleven(settings),
    )
    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "US")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text="東京BMW 03-1234-5678\nЗадача: уточнить наличие Lexus LC.",
        )
        assert campaign.status == "mixed_phone_regions"
        assert campaign.valid_numbers == 0
        assert campaign.rejected_phones_json[0]["reason"] == "wrong_country_expected_us"
    await engine.dispose()


def test_parse_request_call_input_uses_selected_jp_region_for_national_numbers() -> None:
    parsed = parse_request_call_input(
        "東京トヨタ 03-1234-5678\nLexus LCの在庫を確認したい。",
        default_region="JP",
    )
    assert parsed.status == "ready_to_confirm"
    assert parsed.dealers[0].phone_e164 == "+81312345678"
    assert parsed.dealers[0].phone_region == "JP"

    rejected_proxy = parse_request_call_input(
        "東京トヨタ 0078-6002-648302\nLexus LCの在庫を確認したい。",
        default_region="JP",
    )
    assert rejected_proxy.status == "needs_phones"
    assert rejected_proxy.rejected_phones[0].reason == "invalid_phone"


def test_ram_trx_goal_is_not_treated_as_vague() -> None:
    text = (
        "Дилер RAM — O'Daniel Chrysler Dodge Jeep Ram — Fort Wayne, IN — +1 260 435 5330\n"
        "RAM TRX SRT 2026\n"
        "Когда можно будет заказать? какой ценник? Какая будет цена? Доступные комплектации"
    )
    parsed = parse_request_call_input(text)
    assert parsed.status == "ready_to_confirm"
    assert parsed.dealers[0].phone_e164 == "+12604355330"
    assert not is_goal_too_vague(parsed.raw_user_goal)

    fallback = fallback_goal_generation(parsed.dealers[0], parsed.raw_user_goal, call_language="en")
    assert fallback.status == "ready"
    assert "RAM TRX SRT 2026" in (fallback.goal_ru or "")
    assert "Jeep dealership" not in (fallback.goal_ru or "")
    assert "RAM dealership" not in (fallback.goal_ru or "")
    assert "price" in (fallback.goal_ru or "").lower()


def test_goal_clarification_from_llm_is_respected() -> None:
    result = RequestCallService._ensure_compact_goal(
        SimpleNamespace(dealer_name="O'Daniel Chrysler Dodge Jeep Ram", campaign_id=1, id=1),
        "RAM TRX SRT 2026 Когда можно будет заказать? какой ценник?",
        GoalGenerationResult(status="needs_goal_clarification", goal_ru=None),
        call_language="en",
        vehicle_context=[],
    )
    assert result.status == "needs_goal_clarification"
    assert result.goal_ru is None


@pytest.mark.asyncio
async def test_goal_generation_prompt_requires_follow_up_questions() -> None:
    settings = _settings()
    openai = CapturingOpenAI(settings)
    await openai.generate_goal_ru(
        dealer_name="Duval Ford Jacksonville",
        city="Jacksonville",
        phone_e164="+19043876541",
        raw_user_goal="Узнать наличие Ford Raptor R из наличия или ближайшей поставки.",
        call_language="en",
        vehicle_context=[{"vehicle_title": "2024 Ford Raptor R", "vin": "1FTFW1RG0RF000001"}],
    )
    assert "English-speaking voice agent" in openai.last_prompt
    assert "Generate goal_ru in English" in openai.last_prompt
    assert "70-95 words" in openai.last_prompt
    assert "never more than 110 words" in openai.last_prompt
    assert "never copy or mention the exact dealer_name" in openai.last_prompt
    assert "do not use brand-specific dealer labels" in openai.last_prompt
    assert "a <brand> dealership" in openai.last_prompt
    assert "Do not normalize or correct vehicle brand/model names" in openai.last_prompt
    assert "vehicle_context identifies the vehicle" in openai.last_prompt
    assert "Call the sales department about" in openai.last_prompt
    assert "availability or incoming ETA" in openai.last_prompt
    assert "keep the call natural" in openai.last_prompt
    assert "Do not end the call" not in openai.last_prompt
    assert "a Ford dealership" not in openai.last_prompt
    assert "a Jeep dealership" not in openai.last_prompt


@pytest.mark.asyncio
async def test_request_report_prompt_marks_missing_required_answers() -> None:
    settings = _settings()
    openai = CapturingOpenAI(settings)
    await openai.extract_request_call_report(
        transcript="agent: Is it incoming?\nuser: yes",
        goal_ru="Mandatory questions: ETA, price, VIN.",
    )
    assert "goal_ru и transcript могут быть на английском или японском" in openai.last_prompt
    assert "Все человекочитаемые поля отчёта верни на русском" in openai.last_prompt
    assert "Строй отчёт исходя из goal_ru" in openai.last_prompt
    assert "закрыт" in openai.last_prompt
    assert "not_answered" in openai.last_prompt
    assert "поставка есть, но срок не получен" in openai.last_prompt
    assert "ai_quality_score" in openai.last_prompt
    assert "качество работы голосового AI-агента" in openai.last_prompt


@pytest.mark.asyncio
async def test_request_campaign_generates_confirmation_and_goal_ru() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeEleven(settings),
    )
    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "US")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text=(
                "Duval Ford Jacksonville +1 (904) 387-6541\n"
                "Задача: узнать наличие Ford Raptor R из наличия или ближайшей поставки, "
                "покупка без кредита и лизинга, оплата переводом."
            ),
        )
        targets = await service.list_targets(session, campaign.id)
        assert campaign.status == "ready_to_confirm"
        campaign = await service.set_language_and_generate_goals(
            session=session,
            campaign=campaign,
            call_language="en",
        )
        targets = await service.list_targets(session, campaign.id)
        assert campaign.status == "ready_to_confirm"
        assert campaign.valid_numbers == 1
        assert campaign.call_language == "en"
        assert "Duval Ford Jacksonville" not in targets[0].goal_ru
        assert "Ford dealership" not in targets[0].goal_ru
        assert "Call the sales department about" in targets[0].goal_ru
        assert "Ford Raptor R" in targets[0].goal_ru
        assert _word_count(targets[0].goal_ru) <= REQUEST_GOAL_MAX_WORDS
        assert "VIN/stock" in targets[0].goal_ru
        assert "availability or ETA" in targets[0].goal_ru
        assert "concise follow-up" in targets[0].goal_ru
        assert "Do not end" not in targets[0].goal_ru
        text = build_request_confirmation_text(campaign, targets)
        assert "Нашёл 1 валидных номеров" in text
        assert "Язык звонка: английский" in text
        assert "Цель для агента (EN):" in text
        assert "Call the sales department" in text
        assert "Ключевые вопросы:" in text
        assert "наличие/поставка" in text
        assert "Выберите режим запуска прозвона 1 номеров" in text
    await engine.dispose()


def test_fallback_goal_generation_is_compact_english() -> None:
    dealer = ParsedDealerLine(
        dealer_name="Duval Ford Jacksonville",
        city="Jacksonville",
        phone_raw="+1 (904) 387-6541",
        phone_e164="+19043876541",
        phone_region="US",
        original_line="Duval Ford Jacksonville +1 (904) 387-6541",
    )
    result = fallback_goal_generation(
        dealer,
        "Задача: узнать наличие Ford Raptor R из наличия или ближайшей поставки, "
        "покупка без кредита и лизинга, оплата переводом.",
    )

    assert result.goal_ru is not None
    assert _word_count(result.goal_ru) <= REQUEST_GOAL_MAX_WORDS
    assert "Call the sales department" in result.goal_ru
    assert "Duval Ford Jacksonville" not in result.goal_ru
    assert "Ford dealership" not in result.goal_ru
    assert "Call the sales department about" in result.goal_ru
    assert "availability or ETA" in result.goal_ru
    assert "VIN/stock" in result.goal_ru
    assert "no financing" in result.goal_ru
    assert "no leasing" in result.goal_ru
    assert "Do not end" not in result.goal_ru


def test_fallback_goal_generation_supports_japanese() -> None:
    dealer = ParsedDealerLine(
        dealer_name="東京Ford",
        city="Tokyo",
        phone_raw="03-1234-5678",
        phone_e164="+81312345678",
        phone_region="JP",
        original_line="東京Ford 03-1234-5678",
    )
    result = fallback_goal_generation(
        dealer,
        "Ford Raptor Rの在庫と価格を確認したい。",
        call_language="ja",
    )
    assert result.goal_ru is not None
    assert "販売部門" in result.goal_ru
    assert "販売店" not in result.goal_ru
    assert "在庫" in result.goal_ru
    assert "価格" in result.goal_ru


@pytest.mark.asyncio
async def test_request_campaign_followup_preserves_phones_and_accepts_ok_status() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    openai = FakeOpenAI(settings)
    openai.goal_status = "ok"
    service = RequestCallService(
        settings=settings,
        openai_service=openai,
        elevenlabs_service=FakeEleven(settings),
    )
    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "US")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text="Duval Ford Jacksonville +1 (904) 387-6541",
        )
        assert campaign.status == "needs_goal"
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text="Узнать наличие Ford Raptor R из наличия или ближайшей поставки, покупка без кредита.",
        )
        assert campaign.status == "ready_to_confirm"
        campaign = await service.set_language_and_generate_goals(
            session=session,
            campaign=campaign,
            call_language="en",
        )
        targets = await service.list_targets(session, campaign.id)
        assert campaign.status == "ready_to_confirm"
        assert campaign.valid_numbers == 1
        assert targets[0].phone_e164 == "+19043876541"
        assert "Ford Raptor R" in targets[0].goal_ru
        assert "availability or ETA" in targets[0].goal_ru
    await engine.dispose()


def test_request_call_processing_message_text() -> None:
    assert REQUEST_CALL_PROCESSING_MESSAGE == "Принято, формирую список дилеров, контекст авто и цель прозвона..."


def test_settings_admin_ids_parse_new_allowed_users() -> None:
    settings = Settings(
        TELEGRAM_BOT_TOKEN="TEST_TOKEN",
        TELEGRAM_ADMIN_IDS="929466979,1560959066,90791472",
        TELEGRAM_ALLOWED_CHAT_IDS="5288422605,-1003969464648",
    )
    assert {929466979, 1560959066, 90791472}.issubset(settings.admin_ids)
    assert settings.is_allowed_telegram_chat(123, "private")
    assert settings.is_allowed_telegram_chat(5288422605, "group")
    assert settings.is_allowed_telegram_chat(-5288422605, "group")
    assert settings.is_allowed_telegram_chat(-1005288422605, "supergroup")
    assert settings.is_allowed_telegram_chat(-1003969464648, "supergroup")
    assert not settings.is_allowed_telegram_chat(-1001111111111, "supergroup")


def test_request_target_report_html_is_escaped_and_compact() -> None:
    target = SimpleNamespace(dealer_name="A&B Ford <Sales>", phone_e164="+19043876541")
    campaign = SimpleNamespace(
        telegram_user_id=929466979,
        telegram_username="owner_name",
        telegram_user_display_name="Owner <Admin>",
    )
    report = SimpleNamespace(
        call_status="completed",
        ai_quality_score=87,
        ai_quality_reason="получил цену <VIN не спросил>",
        summary="Есть авто & цена подтверждена",
        availability_result="в наличии",
        incoming_result="not_answered",
        price_result="$95,000",
        configuration_result="—",
        vin_or_stock_result="not_answered",
        payment_result="wire возможен",
        paperwork_result="not_answered",
        next_action="написать менеджеру",
        important_notes="not_answered",
    )
    html = build_request_target_report_html(target, report, campaign=campaign)

    assert "<b>Отчёт: A&amp;B Ford &lt;Sales&gt;</b>" in html
    assert "<b>Поставил задачу:</b> @owner_name" in html
    assert "<b>Номер:</b> <code>+19043876541</code>" in html
    assert "<b>Оценка AI:</b> <code>87/100</code> (получил цену &lt;VIN не спросил&gt;)" in html
    assert "not_answered" not in html
    assert "Конфигурация" not in html
    assert "Оплата/документы" in html


def test_request_target_report_mentions_owner_by_tg_link_without_username() -> None:
    target = SimpleNamespace(dealer_name="Dealer", phone_e164="+19043876541")
    campaign = SimpleNamespace(
        telegram_user_id=1560959066,
        telegram_username=None,
        telegram_user_display_name="Alex <Admin>",
    )
    report = SimpleNamespace(
        call_status="completed",
        ai_quality_score=None,
        ai_quality_reason=None,
        summary="Итог",
        availability_result="not_answered",
        incoming_result="not_answered",
        price_result="not_answered",
        configuration_result="not_answered",
        vin_or_stock_result="not_answered",
        payment_result="not_answered",
        paperwork_result="not_answered",
        next_action="not_answered",
        important_notes="not_answered",
    )

    html = build_request_target_report_html(target, report, campaign=campaign)

    assert '<a href="tg://user?id=1560959066">Alex &lt;Admin&gt;</a>' in html


def test_request_campaign_summary_shows_all_useful_results_without_top_three_cap() -> None:
    reports = [
        CallReport(
            campaign_id=1,
            target_id=idx,
            dealer_name=f"Dealer {idx}",
            phone_e164=f"+1904000000{idx}",
            call_status="completed",
            reached_sales=True,
            summary=f"Полезный итог {idx}",
            availability_result="есть вариант",
            incoming_result="not_answered",
            price_result="$90,000",
        )
        for idx in range(1, 6)
    ]
    chunks = build_request_campaign_summary_html_chunks(reports)
    html = "\n".join(chunks)

    assert "Dealer 1" in html
    assert "Dealer 5" in html
    assert html.count("<b>Dealer ") == 5
    assert "Полезных вариантов не найдено" not in html


def test_request_campaign_summary_handles_no_useful_results() -> None:
    reports = [
        CallReport(
            campaign_id=1,
            target_id=1,
            dealer_name="No Answer Ford",
            phone_e164="+19040000001",
            call_status="no_answer",
            reached_sales=False,
            summary="not_answered",
            availability_result="not_answered",
            incoming_result="not_answered",
            next_action="not_answered",
        )
    ]
    html = "\n".join(build_request_campaign_summary_html_chunks(reports))
    assert "Полезных вариантов не найдено" in html
    assert "No Answer Ford" not in html


def test_request_campaign_summary_chunks_long_html() -> None:
    reports = [
        CallReport(
            campaign_id=1,
            target_id=idx,
            dealer_name=f"Dealer {idx}",
            phone_e164=f"+1904000{idx:04d}",
            call_status="completed",
            reached_sales=True,
            summary="полезный результат " + ("x" * 180),
            availability_result="есть вариант",
        )
        for idx in range(1, 45)
    ]
    chunks = build_request_campaign_summary_html_chunks(reports)
    assert len(chunks) > 1
    assert all(len(chunk) <= 4096 for chunk in chunks)


@pytest.mark.asyncio
async def test_latest_input_campaign_ignores_completed_and_waiting_next_states() -> None:
    engine, session_maker = await _session_maker()
    async with session_maker() as session:
        stale = await create_request_campaign(session, chat_id=10, user_id=1, status="needs_goal")
        completed = await create_request_campaign(session, chat_id=10, user_id=1, status="completed")

        assert completed.id > stale.id
        assert await get_latest_input_request_campaign(session, chat_id=10, user_id=1) is None

        waiting_next = await create_request_campaign(session, chat_id=10, user_id=1, status="waiting_next")
        assert waiting_next.id > completed.id
        assert await get_latest_input_request_campaign(session, chat_id=10, user_id=1) is None

        fresh = await create_request_campaign(session, chat_id=10, user_id=1, status="draft")
        assert (await get_latest_input_request_campaign(session, chat_id=10, user_id=1)).id == fresh.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_latest_input_campaign_is_owner_scoped_in_group_chat() -> None:
    engine, session_maker = await _session_maker()
    async with session_maker() as session:
        await create_request_campaign(session, chat_id=-1005288422605, user_id=1, status="completed")
        draft = await create_request_campaign(session, chat_id=-1005288422605, user_id=1, status="draft")

        assert await get_latest_input_request_campaign(session, chat_id=-1005288422605, user_id=2) is None
        assert (await get_latest_input_request_campaign(session, chat_id=-1005288422605, user_id=1)).id == draft.id

        draft.status = "needs_phones_and_goal"
        await session.commit()
        assert await get_latest_input_request_campaign(session, chat_id=-1005288422605, user_id=2) is None
        assert (await get_latest_input_request_campaign(session, chat_id=-1005288422605, user_id=1)).id == draft.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_call_new_session_cancels_stale_input_campaigns() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeEleven(settings),
    )
    bot = FakeBot()
    async with session_maker() as session:
        stale = await service.create_draft(session=session, chat_id=10, user_id=1, status="needs_goal")
        service_message = await service.send_service_message(
            session=session,
            campaign=stale,
            bot=bot,
            text="old prompt",
        )
        running = await service.create_draft(session=session, chat_id=10, user_id=2, status="waiting_next")

        canceled_count = await service.cancel_input_campaigns_for_owner(
            session=session,
            chat_id=10,
            user_id=1,
            bot=bot,
        )
        await session.refresh(stale)
        await session.refresh(running)

        assert canceled_count == 1
        assert stale.status == "canceled"
        assert stale.telegram_service_message_ids == []
        assert running.status == "waiting_next"
        assert service_message is not None
        assert (10, service_message.message_id) in bot.deleted_messages
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_call_running_campaign_blocks_new_session_for_owner() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeEleven(settings),
    )
    async with session_maker() as session:
        running = await service.create_draft(session=session, chat_id=10, user_id=1, status="waiting_call_result")
        await service.create_draft(session=session, chat_id=10, user_id=2, status="waiting_next")

        found = await service.get_running_campaign_for_owner(session=session, chat_id=10, user_id=1)

        assert found is not None
        assert found.id == running.id
        assert await service.get_running_campaign_for_owner(session=session, chat_id=10, user_id=3) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_latest_open_campaign_for_cancel_is_owner_scoped_in_group() -> None:
    engine, session_maker = await _session_maker()
    async with session_maker() as session:
        await create_request_campaign(session, chat_id=-1005288422605, user_id=1, status="completed")
        owner_campaign = await create_request_campaign(session, chat_id=-1005288422605, user_id=1, status="waiting_next")
        await create_request_campaign(session, chat_id=-1005288422605, user_id=2, status="calling")

        assert (await get_latest_open_request_campaign(session, chat_id=-1005288422605, user_id=1)).id == owner_campaign.id
        assert await get_latest_open_request_campaign(session, chat_id=-1005288422605, user_id=3) is None

        owner_campaign.status = "canceled"
        await session.commit()
        assert await get_latest_open_request_campaign(session, chat_id=-1005288422605, user_id=1) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_campaign_accepts_supergroup_chat_id() -> None:
    engine, session_maker = await _session_maker()
    async with session_maker() as session:
        campaign = await create_request_campaign(
            session,
            chat_id=-1005288422605,
            user_id=929466979,
            status="draft",
        )
        assert campaign.telegram_chat_id == -1005288422605
        assert campaign.telegram_user_id == 929466979
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_campaign_uses_url_context_in_confirmation() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeEleven(settings),
    )
    service.context_extractor = FakeContextExtractor()
    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "US")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text=(
                "Duval Ford Jacksonville +1 (904) 387-6541\n"
                "https://example.com/raptor-r\n"
                "Задача: узнать наличие и цену."
            ),
        )
        assert campaign.status == "ready_to_confirm"
        assert campaign.source_urls_json == ["https://example.com/raptor-r"]
        assert campaign.vehicle_context_json[0]["vin"] == "1FTFW1RG0RF000001"
        campaign = await service.set_language_and_generate_goals(
            session=session,
            campaign=campaign,
            call_language="en",
        )
        targets = await service.list_targets(session, campaign.id)
        text = build_request_confirmation_text(campaign, targets)
        assert "Контекст из ссылок:" in text
        assert "2024 Ford Raptor R" in text
        assert "VIN: 1FTFW1RG0RF000001" in text
        assert "цвет: Shelter Green" in text
        assert "цена: $112,000" in text
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_call_starts_one_call_with_goal_ru_only_and_waits_next() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    eleven = FakeEleven(settings)
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=eleven,
    )
    bot = FakeBot()
    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "US")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text=(
                "Duval Ford Jacksonville +1 (904) 387-6541\n"
                "AutoNation Ford Jacksonville +1 (904) 513-3392\n"
                "Задача: узнать наличие Ford Raptor R из наличия или ближайшей поставки, "
                "покупка без кредита и лизинга, оплата переводом."
            ),
        )
        campaign = await service.set_language_and_generate_goals(
            session=session,
            campaign=campaign,
            call_language="en",
        )
        job = await service.start_next_call(session=session, campaign=campaign, bot=bot)
        assert job is not None
        assert len(eleven.calls) == 1
        assert eleven.calls[0]["call_phone"] == "+19043876541"
        targets = await service.list_targets(session, campaign.id)
        assert targets[0].status == "waiting_call_result"
        assert targets[1].status == "pending"

        await service.finalize_job_from_transcript(
            session=session,
            job=job,
            bot=bot,
            transcript="agent: hello\nuser: please email us, incoming unit is possible",
            summary="completed",
        )
        targets = await service.list_targets(session, campaign.id)
        assert targets[0].status == "completed"
        assert targets[1].status == "pending"
        assert any("Прозвонить следующего" in str(row["kwargs"].get("reply_markup")) for row in bot.messages)
        report_text = next(row["text"] for row in bot.messages if "Отчёт:" in row["text"])
        report_message = next(row for row in bot.messages if "Отчёт:" in row["text"])
        assert report_message["kwargs"].get("parse_mode") == "HTML"
        assert report_message["kwargs"].get("reply_markup") is None
        assert "<b>Номер:</b> <code>+19043876541</code>" in report_text
        assert "<b>Оценка AI:</b> <code>88/100</code>" in report_text
        assert "агент получил ключевые ответы" in report_text
        assert "not_answered" not in report_text
        assert "VIN/stock" not in report_text
        assert "Оплата/документы" in report_text
        assert bot.documents
        assert bot.documents[0]["kwargs"]["caption"].startswith("Аудио звонка:")
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_call_auto_mode_starts_next_after_report_delivery() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    eleven = FakeEleven(settings)
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=eleven,
    )
    bot = FakeBot()
    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "US")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text=(
                "Duval Ford Jacksonville +1 (904) 387-6541\n"
                "AutoNation Ford Jacksonville +1 (904) 513-3392\n"
                "Задача: узнать наличие Ford Raptor R из наличия или ближайшей поставки."
            ),
        )
        campaign = await service.set_language_and_generate_goals(
            session=session,
            campaign=campaign,
            call_language="en",
        )
        campaign.call_sequence_mode = "auto"
        await session.commit()

        first_job = await service.start_next_call(session=session, campaign=campaign, bot=bot)
        assert first_job is not None
        assert len(eleven.calls) == 1

        await service.finalize_job_from_transcript(
            session=session,
            job=first_job,
            bot=bot,
            transcript="agent: hello\nuser: incoming unit is possible",
            summary="completed",
        )

        targets = await service.list_targets(session, campaign.id)
        assert targets[0].status == "completed"
        assert targets[1].status == "waiting_call_result"
        assert len(eleven.calls) == 2
        assert not any("Прозвонить следующего" in str(row["kwargs"].get("reply_markup")) for row in bot.messages)
        assert any("Автоматический режим" in row["text"] for row in bot.messages)
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_call_finalization_is_idempotent_for_webhook_and_fallback_race() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeEleven(settings),
    )
    bot = FakeBot()
    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "US")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text=(
                "Duval Ford Jacksonville +1 (904) 387-6541\n"
                "Задача: узнать наличие Ford Raptor R из наличия или ближайшей поставки."
            ),
        )
        campaign = await service.set_language_and_generate_goals(
            session=session,
            campaign=campaign,
            call_language="en",
        )
        job = await service.start_next_call(session=session, campaign=campaign, bot=bot)
        assert job is not None

        await service.finalize_job_from_transcript(
            session=session,
            job=job,
            bot=bot,
            transcript="agent: hello\nuser: incoming unit is possible",
            summary="completed",
        )
        await service.finalize_job_from_transcript(
            session=session,
            job=job,
            bot=bot,
            transcript="agent: hello again\nuser: duplicate webhook payload",
            summary="completed duplicate",
        )

        reports = (await session.execute(select(CallReport).where(CallReport.target_id == job.request_target_id))).scalars().all()
        assert len(reports) == 1
        report_messages = [row for row in bot.messages if "Отчёт:" in row["text"]]
        assert len(report_messages) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_campaign_generates_goal_once_for_all_targets() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    openai = FakeOpenAI(settings)
    service = RequestCallService(
        settings=settings,
        openai_service=openai,
        elevenlabs_service=FakeEleven(settings),
    )
    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "US")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text=(
                "Duval Ford Jacksonville +1 (904) 387-6541\n"
                "AutoNation Ford Jacksonville +1 (904) 513-3392\n"
                "Задача: узнать наличие Ford Raptor R из наличия или ближайшей поставки."
            ),
        )
        targets = await service.list_targets(session, campaign.id)
        assert campaign.status == "ready_to_confirm"
        assert len(openai.goal_calls) == 1
        assert targets[0].goal_ru == targets[1].goal_ru
        assert "Duval Ford Jacksonville" not in targets[0].goal_ru
        assert "AutoNation Ford Jacksonville" not in targets[1].goal_ru
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_call_cleans_service_messages_after_campaign_completion() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeEleven(settings),
    )
    bot = FakeBot()
    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        service_message = await service.send_service_message(
            session=session,
            campaign=campaign,
            bot=bot,
            text="служебное сообщение",
        )
        job = Job(
            telegram_chat_id=10,
            telegram_user_id=1,
            listing_url="request-call://campaign/1/target/1",
            source="request_call",
            request_campaign_id=campaign.id,
            telegram_service_message_ids=[999],
            status="completed",
        )
        session.add(job)
        await session.commit()

        await service.cleanup_service_messages(session=session, campaign=campaign, bot=bot)

        assert service_message is not None
        assert (10, service_message.message_id) in bot.deleted_messages
        assert (10, 999) in bot.deleted_messages
        assert campaign.telegram_service_message_ids == []
        assert job.telegram_service_message_ids == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_call_cancel_campaign_marks_state_and_cleans_messages() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeEleven(settings),
    )
    bot = FakeBot()
    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "US")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text="Duval Ford Jacksonville +1 (904) 387-6541\nЗадача: узнать наличие Ford Raptor R.",
        )
        targets = await service.list_targets(session, campaign.id)
        service_message = await service.send_service_message(
            session=session,
            campaign=campaign,
            bot=bot,
            text="служебное сообщение",
        )
        job = Job(
            telegram_chat_id=10,
            telegram_user_id=1,
            listing_url=f"request-call://campaign/{campaign.id}/target/{targets[0].id}",
            source="request_call",
            request_campaign_id=campaign.id,
            request_target_id=targets[0].id,
            telegram_service_message_ids=[999],
            status="waiting_call_result",
        )
        session.add(job)
        await session.commit()

        await service.cancel_campaign(session=session, campaign=campaign, bot=bot)
        await session.refresh(campaign)
        await session.refresh(targets[0])
        await session.refresh(job)

        assert service_message is not None
        assert campaign.status == "canceled"
        assert targets[0].status == "canceled"
        assert job.status == "canceled"
        assert campaign.telegram_service_message_ids == []
        assert job.telegram_service_message_ids == []
        assert (10, service_message.message_id) in bot.deleted_messages
        assert (10, 999) in bot.deleted_messages
    await engine.dispose()


@pytest.mark.asyncio
async def test_late_request_call_finalization_is_ignored_after_cancel() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeEleven(settings),
    )
    bot = FakeBot()
    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "US")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text="Duval Ford Jacksonville +1 (904) 387-6541\nЗадача: узнать наличие Ford Raptor R.",
        )
        targets = await service.list_targets(session, campaign.id)
        job = Job(
            telegram_chat_id=10,
            telegram_user_id=1,
            listing_url=f"request-call://campaign/{campaign.id}/target/{targets[0].id}",
            source="request_call",
            request_campaign_id=campaign.id,
            request_target_id=targets[0].id,
            request_goal_ru=targets[0].goal_ru,
            status="waiting_call_result",
        )
        session.add(job)
        await session.commit()

        await service.cancel_campaign(session=session, campaign=campaign, bot=bot)
        await service.finalize_job_from_transcript(
            session=session,
            job=job,
            bot=bot,
            transcript="agent: hello\nuser: answered",
            summary="completed",
        )

        reports = (await session.execute(select(CallReport).where(CallReport.campaign_id == campaign.id))).scalars().all()
        assert reports == []
        assert not any("Отчёт:" in row["text"] for row in bot.messages)
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_call_japanese_uses_ja_agent_and_goal_only() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    eleven = FakeEleven(settings)
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=eleven,
    )
    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "JP")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text="東京Ford 03-1234-5678\nЗадача: Ford Raptor Rの在庫と価格を確認したい。",
        )
        assert campaign.status == "ready_to_confirm"
        assert campaign.phone_region == "JP"
        campaign = await service.set_language_and_generate_goals(
            session=session,
            campaign=campaign,
            call_language="ja",
        )
        job = await service.start_next_call(session=session, campaign=campaign, bot=FakeBot())
        assert job is not None
        assert job.call_language == "ja"
        assert eleven.calls[0]["call_phone"] == "+81312345678"
        assert eleven.calls[0]["agent_id_override"] == "agent_request_ja"
        assert set(eleven.calls[0]["dynamic_variables"].keys()) == {"goal_ru"}
        assert "販売部門" in eleven.calls[0]["dynamic_variables"]["goal_ru"]
        assert "販売店" not in eleven.calls[0]["dynamic_variables"]["goal_ru"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_report_delivery_failure_schedules_retry(monkeypatch) -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeEleven(settings),
    )

    async def fail_send(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.request_call.safe_send_message", fail_send)

    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "US")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text=(
                "Duval Ford Jacksonville +1 (904) 387-6541\n"
                "Задача: узнать наличие Ford Raptor R из наличия или ближайшей поставки."
            ),
        )
        campaign = await service.set_language_and_generate_goals(
            session=session,
            campaign=campaign,
            call_language="en",
        )
        job = await service.start_next_call(session=session, campaign=campaign, bot=FakeBot())
        assert job is not None
        await service.finalize_job_from_transcript(
            session=session,
            job=job,
            bot=FakeBot(),
            transcript="agent: hello\nuser: incoming unit is possible",
            summary="completed",
        )
        assert job.final_report_sent_at is None
        assert job.final_report_error
        assert job.next_notification_retry_at is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_call_auto_mode_does_not_start_next_when_report_delivery_fails(monkeypatch) -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    eleven = FakeEleven(settings)
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=eleven,
    )

    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "US")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text=(
                "Duval Ford Jacksonville +1 (904) 387-6541\n"
                "AutoNation Ford Jacksonville +1 (904) 513-3392\n"
                "Задача: узнать наличие Ford Raptor R из наличия или ближайшей поставки."
            ),
        )
        campaign = await service.set_language_and_generate_goals(
            session=session,
            campaign=campaign,
            call_language="en",
        )
        campaign.call_sequence_mode = "auto"
        await session.commit()

        job = await service.start_next_call(session=session, campaign=campaign, bot=FakeBot())
        assert job is not None
        assert len(eleven.calls) == 1

        async def fail_send(*args, **kwargs):
            return None

        monkeypatch.setattr("app.services.request_call.safe_send_message", fail_send)
        await service.finalize_job_from_transcript(
            session=session,
            job=job,
            bot=FakeBot(),
            transcript="agent: hello\nuser: incoming unit is possible",
            summary="completed",
        )
        assert len(eleven.calls) == 1
        assert job.final_report_error
        assert job.next_notification_retry_at is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_request_call_summary_after_all_targets() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    eleven = FakeEleven(settings)
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=eleven,
    )
    bot = FakeBot()
    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "US")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text=(
                "Duval Ford Jacksonville +1 (904) 387-6541\n"
                "Задача: узнать наличие Ford Raptor R из наличия или ближайшей поставки."
            ),
        )
        campaign = await service.set_language_and_generate_goals(
            session=session,
            campaign=campaign,
            call_language="en",
        )
        job = await service.start_next_call(session=session, campaign=campaign, bot=bot)
        assert job is not None
        await service.finalize_job_from_transcript(
            session=session,
            job=job,
            bot=bot,
            transcript="agent: hello\nuser: incoming unit is possible",
            summary="completed",
        )
        summary = next(row["text"] for row in bot.messages if "Прозвон завершён" in row["text"])
        assert "<b>Duval Ford Jacksonville</b> (<code>+19043876541</code>)" in summary
    await engine.dispose()
