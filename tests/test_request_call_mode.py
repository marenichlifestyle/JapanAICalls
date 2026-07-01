from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.models import Base, CallReport, DealerPhoneContext, Job
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
    RequestCallService,
    _word_count,
    build_request_campaign_summary_html_chunks,
    build_request_confirmation_text,
    build_request_target_report_html,
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
        dealer_targets: list[dict] | None = None,
    ) -> GoalGenerationResult:
        self.goal_calls = getattr(self, "goal_calls", [])
        self.goal_calls.append(
            {
                "dealer_name": dealer_name,
                "phone_e164": phone_e164,
                "call_language": call_language,
                "raw_user_goal": raw_user_goal,
                "dealer_targets": dealer_targets or [],
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
                "Call the sales department about Ford Raptor R. Ask whether a unit is available now or incoming soon. "
                "Confirm availability or ETA, price/OOD plus MSRP/markup/fees, configuration/color, VIN/stock, "
                "payment by bank wire with no financing or lease, and paperwork timing. If unavailable, ask for the "
                "nearest incoming option and best next contact. Ask one concise follow-up only when a critical answer "
                "is vague, then accept unknown or refusal."
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
            goal_answers=[
                {
                    "question": "Наличие автомобиля",
                    "answer": "есть ближайшая поставка",
                    "status": "answered",
                    "reason": "продавец подтвердил incoming unit",
                    "evidence": "incoming unit is possible",
                    "result_marker": "green",
                }
            ],
            critical_missing=[],
            commitments=["Менеджер уточнит детали и вернётся с ответом."],
            contact_details=[
                {"type": "phone", "value": "+17867060777", "purpose": "обратный звонок"},
                {"type": "whatsapp", "value": "+17867060777", "purpose": "WhatsApp доступен"},
                {"type": "email", "value": "b4484298@gmail.com", "purpose": "ответ по email"},
            ],
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


class FailingGoalOpenAI(FakeOpenAI):
    async def generate_goal_ru(self, **kwargs) -> GoalGenerationResult:
        raise RuntimeError("openai unavailable")


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


def test_parse_request_call_input_preserves_dealer_to_dealer_goal_line() -> None:
    parsed = parse_request_call_input(
        """
        +1 (405) 507-7529

        https://www.cars.com/vehicledetail/f4fd33ce-acf2-4cc0-b31c-35981baddc06/?attribution_type=isa

        Задача: сказать, что мы только что по нему уже звонили и вы так и не ответили
        на вопрос dealer to dealer можем ли купить, если да, то готовы рассматривать покупку.
        Больше вопросов задавать не нужно, завершить звонок сказав, что с вами свяжемся
        """,
        default_region="US",
    )

    assert parsed.status == "ready_to_confirm"
    assert parsed.dealers[0].phone_e164 == "+14055077529"
    assert parsed.source_urls == [
        "https://www.cars.com/vehicledetail/f4fd33ce-acf2-4cc0-b31c-35981baddc06/?attribution_type=isa"
    ]
    assert "dealer to dealer можем ли купить" in parsed.raw_user_goal
    assert "Больше вопросов задавать не нужно" in parsed.raw_user_goal
    assert "Задача:" in parsed.raw_user_goal


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
    assert "東京BMW 03-1234-5678" in parsed.raw_user_goal
    assert "https://example.jp/car/123" in parsed.raw_user_goal
    assert "Задача: уточнить BMW M3 в наличии." in parsed.raw_user_goal

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

    vague = parse_request_call_input("Duval Ford +1 (904) 387-6541\nузнать по машинам")
    assert vague.status == "ready_to_confirm"
    assert "узнать по машинам" in vague.raw_user_goal


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
    assert "RAM TRX SRT 2026" in parsed.raw_user_goal
    assert "Доступные комплектации" in parsed.raw_user_goal


def test_goal_clarification_from_llm_is_respected() -> None:
    result = RequestCallService._validate_campaign_goal(
        SimpleNamespace(id=1),
        [SimpleNamespace(dealer_name="O'Daniel Chrysler Dodge Jeep Ram", id=1)],
        GoalGenerationResult(status="needs_goal_clarification", goal_ru=None),
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
    assert "Preserve explicit user instructions from raw_user_goal" in openai.last_prompt
    assert "do not replace the user's request with 'no questions'" in openai.last_prompt
    assert "dealer-to-dealer purchase is possible" in openai.last_prompt
    assert "Call the sales department about" in openai.last_prompt
    assert "Ask only questions needed for the user's stated goal" in openai.last_prompt
    assert "do not add a universal dealership questionnaire" in openai.last_prompt
    assert "do not add markup/MSRP/deposit questions" in openai.last_prompt
    assert "dealer-to-dealer/no-tax tasks" in openai.last_prompt
    assert "what resale/wholesale/dealer documents are required" in openai.last_prompt
    assert "markup/MSRP/market adjustment only for new/order/allocation/rare incoming vehicles" in openai.last_prompt
    assert "fees/tax/OTD/deposit/hold/paperwork only when the user asks" in openai.last_prompt
    assert "nearest allocation or quota" not in openai.last_prompt
    assert "keep the call natural" in openai.last_prompt
    assert "Do not end the call" not in openai.last_prompt
    assert "a Ford dealership" not in openai.last_prompt
    assert "a Jeep dealership" not in openai.last_prompt


@pytest.mark.asyncio
async def test_goal_generation_prompt_keeps_used_car_dealer_to_dealer_focused() -> None:
    settings = _settings()
    openai = CapturingOpenAI(settings)
    await openai.generate_goal_ru(
        dealer_name="Dealer",
        city=None,
        phone_e164="+13055550100",
        raw_user_goal=(
            "Уточнить, что он есть в наличии белый на белом салоне, состояние, могут ли отправить Car Fax "
            "и могут ли продать dealer to dealer без tax на компанию дилерскую во Флориде."
        ),
        call_language="en",
        vehicle_context=[{"vehicle_title": "Used 2025 RAM 1500 Tungsten", "color": "white"}],
    )
    assert "specific used vehicle" in openai.last_prompt
    assert "condition, vehicle history/Carfax" in openai.last_prompt
    assert "dealer-to-dealer no tax" in openai.last_prompt
    assert "do not add markup/MSRP/deposit questions" in openai.last_prompt


@pytest.mark.asyncio
async def test_goal_generation_prompt_has_same_conditional_rules_for_ja() -> None:
    settings = _settings()
    openai = CapturingOpenAI(settings)
    await openai.generate_goal_ru(
        dealer_name="Tokyo Dealer",
        city=None,
        phone_e164="+81312345678",
        raw_user_goal="在庫、状態、業販で買えるか確認する。",
        call_language="ja",
        vehicle_context=[{"vehicle_title": "中古 Lexus LC", "color": "white"}],
    )
    assert "Japanese-speaking voice agent" in openai.last_prompt
    assert "Ask only questions needed for the user's stated goal" in openai.last_prompt
    assert "specific used vehicle" in openai.last_prompt
    assert "dealer-to-dealer/no-tax tasks" in openai.last_prompt
    assert "do not add markup/MSRP/deposit questions" in openai.last_prompt


@pytest.mark.asyncio
async def test_request_report_prompt_marks_missing_required_answers() -> None:
    settings = _settings()
    openai = CapturingOpenAI(settings)
    await openai.extract_request_call_report(
        transcript=(
            "agent: Is it incoming and what color is it?\n"
            "user: It is red, but I do not know the ETA.\n"
            "user: Eric will check with the sales manager and call back.\n"
            "agent: You can reach me at +1 786 706 0777 or b four four eight four two nine eight at gmail dot com."
        ),
        goal_ru="Mandatory questions: ETA, price, VIN, color, best next contact.",
    )
    assert "goal_ru и transcript могут быть на английском или японском" in openai.last_prompt
    assert "Все человекочитаемые поля отчёта верни на русском" in openai.last_prompt
    assert "Сначала выдели из goal_ru короткий чеклист" in openai.last_prompt
    assert "сопоставь каждый пункт чеклиста с transcript" in openai.last_prompt
    assert "goal_answers" in openai.last_prompt
    assert "critical_missing" in openai.last_prompt
    assert "reason" in openai.last_prompt
    assert "result_marker" in openai.last_prompt
    assert "green" in openai.last_prompt
    assert "red" in openai.last_prompt
    assert "yellow" in openai.last_prompt
    assert "менеджер сказал, что уточнит" in openai.last_prompt
    assert "commitments" in openai.last_prompt
    assert "contact_details" in openai.last_prompt
    assert "Eric уточнит у sales manager" in openai.last_prompt
    assert "b four four eight" in openai.last_prompt
    assert "Цвет автомобиля" in openai.last_prompt
    assert "красный" in openai.last_prompt
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
        assert campaign.valid_numbers == 2
        assert campaign.call_language == "en"
        assert len(openai.goal_calls) == 1
        assert len(openai.goal_calls[0]["dealer_targets"]) == 2
        assert "AutoNation Ford Jacksonville +1 (904) 513-3392" in openai.goal_calls[0]["raw_user_goal"]
        assert "Duval Ford Jacksonville" not in targets[0].goal_ru
        assert "Ford dealership" not in targets[0].goal_ru
        assert targets[0].goal_ru == targets[1].goal_ru
        assert "Call the sales department about" in targets[0].goal_ru
        assert "Ford Raptor R" in targets[0].goal_ru
        assert _word_count(targets[0].goal_ru) <= REQUEST_GOAL_MAX_WORDS
        assert "VIN/stock" in targets[0].goal_ru
        assert "availability or ETA" in targets[0].goal_ru
        assert "concise follow-up" in targets[0].goal_ru
        assert "Do not end" not in targets[0].goal_ru
        text = build_request_confirmation_text(campaign, targets)
        assert "Нашёл 2 валидных номеров" in text
        assert "Язык звонка: английский" in text
        assert "Цель для агента (EN):" in text
        assert "Call the sales department" in text
        assert "Ключевые вопросы:" in text
        assert "наличие/поставка" in text
        assert "Выберите режим запуска прозвона 2 номеров" in text
    await engine.dispose()


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


@pytest.mark.asyncio
async def test_request_campaign_does_not_create_fallback_goal_when_openai_fails() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    service = RequestCallService(
        settings=settings,
        openai_service=FailingGoalOpenAI(settings),
        elevenlabs_service=FakeEleven(settings),
    )
    async with session_maker() as session:
        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "US")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text="Duval Ford Jacksonville +1 (904) 387-6541\nЗадача: узнать наличие Ford Raptor R.",
        )
        targets = await service.list_targets(session, campaign.id)

        assert campaign.status == "needs_goal_clarification"
        assert targets[0].goal_ru is None
        assert campaign.goal_meta_json["status"] == "needs_goal_clarification"
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
        raw_report_json={
            "goal_answers": [
                {
                    "question": "Цвет автомобиля",
                    "answer": "красный",
                    "status": "answered",
                    "reason": "продавец сказал red",
                    "evidence": "seller said red",
                    "result_marker": "green",
                },
                {
                    "question": "Цена",
                    "answer": "не получено",
                    "status": "not_answered",
                    "reason": "менеджер сказал, что уточнит у руководства",
                    "evidence": None,
                    "result_marker": "red",
                },
            ],
            "critical_missing": ["цена"],
            "commitments": ["Eric уточнит у sales manager и перезвонит."],
            "contact_details": [
                {"type": "phone", "value": "+17867060777", "purpose": "обратный звонок"},
                {"type": "email", "value": "b4484298@gmail.com", "purpose": "ответ по email"},
            ],
        },
    )
    html = build_request_target_report_html(target, report, campaign=campaign)

    assert "<b>Отчёт: A&amp;B Ford &lt;Sales&gt;</b>" in html
    assert "<b>Поставил задачу:</b> @owner_name" in html
    assert "<b>Номер:</b> <code>+19043876541</code>" in html
    assert "<b>Оценка AI:</b> <code>87/100</code> (получил цену &lt;VIN не спросил&gt;)" in html
    assert "<b>Ответы по цели:</b>" in html
    assert "✅ <b>Цвет автомобиля:</b> красный — продавец сказал red" in html
    assert "❌ <b>Цена:</b> не получено — менеджер сказал, что уточнит у руководства" in html
    assert "<b>Не закрыто:</b> цена" in html
    assert "<b>Договорённости:</b>" in html
    assert "Eric уточнит у sales manager и перезвонит." in html
    assert "<b>Контакты/обратная связь:</b>" in html
    assert "<b>телефон:</b> <code>+17867060777</code> — обратный звонок" in html
    assert "<b>email:</b> <code>b4484298@gmail.com</code> — ответ по email" in html
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
        report_row = (
            await session.execute(select(CallReport).where(CallReport.target_id == targets[0].id))
        ).scalar_one()
        assert report_row.raw_report_json["goal_answers"][0]["question"] == "Наличие автомобиля"
        assert report_row.raw_report_json["contact_details"][0]["value"] == "+17867060777"
        assert "<b>Номер:</b> <code>+19043876541</code>" in report_text
        assert "<b>Оценка AI:</b> <code>88/100</code>" in report_text
        assert "<b>Ответы по цели:</b>" in report_text
        assert "✅ <b>Наличие автомобиля:</b> есть ближайшая поставка — продавец подтвердил incoming unit" in report_text
        assert "<b>Договорённости:</b>" in report_text
        assert "Менеджер уточнит детали и вернётся с ответом." in report_text
        assert "<b>Контакты/обратная связь:</b>" in report_text
        assert "<b>телефон:</b> <code>+17867060777</code> — обратный звонок" in report_text
        assert "<b>WhatsApp:</b> <code>+17867060777</code> — WhatsApp доступен" in report_text
        assert "<b>email:</b> <code>b4484298@gmail.com</code> — ответ по email" in report_text
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
async def test_dealer_phone_context_is_created_and_limited_to_three_items() -> None:
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
            text="Duval Ford Jacksonville +1 (904) 387-6541\nЗадача: узнать наличие Ford Raptor R.",
        )
        target = (await service.list_targets(session, campaign.id))[0]

        for idx in range(4):
            report = await service._persist_report(
                session=session,
                campaign=campaign,
                target=target,
                report=RequestCallReportResult(
                    call_status="completed",
                    reached_sales=True,
                    summary=f"Итог звонка {idx}",
                    availability_result="есть ответ по наличию",
                    incoming_result="есть поставка",
                    price_result="$70,000",
                    next_action="перезвонить с уточнением",
                ),
            )
            await service._update_dealer_phone_context(
                session=session,
                campaign=campaign,
                target=target,
                report=report,
                goal_ru=target.goal_ru or "",
            )

        context = (
            await session.execute(select(DealerPhoneContext).where(DealerPhoneContext.phone_e164 == "+19043876541"))
        ).scalar_one()
        assert context.successful_call_count == 4
        assert len(context.context_items_json) == 3
        assert context.context_items_json[0]["summary"] == "Итог звонка 3"
        assert "Итог звонка 3" in context.context_summary
        assert "Итог звонка 0" not in context.context_summary
    await engine.dispose()


@pytest.mark.asyncio
async def test_dealer_phone_context_skips_no_answer_busy_and_failed_reports() -> None:
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
            text="Duval Ford Jacksonville +1 (904) 387-6541\nЗадача: узнать наличие Ford Raptor R.",
        )
        target = (await service.list_targets(session, campaign.id))[0]
        for status in ("no_answer", "busy", "failed", "call_create_failed"):
            report = await service._create_status_report(
                session=session,
                campaign=campaign,
                target=target,
                call_status=status,
                summary=f"status {status}",
            )
            assert (
                await service._update_dealer_phone_context(
                    session=session,
                    campaign=campaign,
                    target=target,
                    report=report,
                    goal_ru=target.goal_ru or "",
                )
            ) is None
        rows = (await session.execute(select(DealerPhoneContext))).scalars().all()
        assert rows == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_previous_phone_context_is_added_to_repeated_target_goal_only() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    openai = FakeOpenAI(settings)
    service = RequestCallService(
        settings=settings,
        openai_service=openai,
        elevenlabs_service=FakeEleven(settings),
    )
    async with session_maker() as session:
        session.add(
            DealerPhoneContext(
                phone_e164="+19043876541",
                phone_region="US",
                last_dealer_name="Duval Ford Jacksonville",
                successful_call_count=1,
                context_items_json=[
                    {
                        "called_at": "2026-06-29T10:00:00+00:00",
                        "summary": "дилер подтвердил ближайшую поставку",
                        "price": "$70,000",
                        "next_action": "уточнить dealer-to-dealer покупку",
                    }
                ],
                context_summary="дилер подтвердил ближайшую поставку; $70,000; уточнить dealer-to-dealer покупку",
            )
        )
        await session.commit()

        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "US")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text=(
                "Duval Ford Jacksonville +1 (904) 387-6541\n"
                "AutoNation Ford Jacksonville +1 (904) 513-3392\n"
                "Задача: уточнить dealer to dealer покупку."
            ),
        )
        targets = await service.list_targets(session, campaign.id)
        assert len(openai.goal_calls) == 1
        repeated = next(target for target in targets if target.phone_e164 == "+19043876541")
        new = next(target for target in targets if target.phone_e164 == "+19045133392")
        assert repeated.goal_ru.startswith("Previous call context:")
        assert "we contacted them before" in repeated.goal_ru
        assert "New call goal:" in repeated.goal_ru
        assert "Call the sales department about Ford Raptor R" in repeated.goal_ru
        assert not new.goal_ru.startswith("Previous call context:")
        confirmation = build_request_confirmation_text(campaign, targets)
        assert "Есть предыдущий контекст по номеру: +19043876541" in confirmation
    await engine.dispose()


@pytest.mark.asyncio
async def test_previous_phone_context_uses_japanese_prefix_for_ja_calls() -> None:
    engine, session_maker = await _session_maker()
    settings = _settings()
    service = RequestCallService(
        settings=settings,
        openai_service=FakeOpenAI(settings),
        elevenlabs_service=FakeEleven(settings),
    )
    async with session_maker() as session:
        session.add(
            DealerPhoneContext(
                phone_e164="+81312345678",
                phone_region="JP",
                last_dealer_name="東京販売",
                successful_call_count=1,
                context_items_json=[{"summary": "前回は在庫確認をした"}],
                context_summary="前回は在庫確認をした",
            )
        )
        await session.commit()

        campaign = await service.create_draft(session=session, chat_id=10, user_id=1)
        campaign = await _select_request_country(session, campaign, "JP")
        campaign = await service.update_campaign_from_text(
            session=session,
            campaign=campaign,
            text="東京販売 03-1234-5678\nタスク: 追加で価格を確認する。",
        )
        target = (await service.list_targets(session, campaign.id))[0]
        assert target.goal_ru.startswith("前回の通話内容:")
        assert "今回の目的:" in target.goal_ru
        assert "販売部門" in target.goal_ru
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
