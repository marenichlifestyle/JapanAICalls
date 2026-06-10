from __future__ import annotations

import pytest

from app.config import Settings
from app.services import elevenlabs_client as client_module
from app.services.elevenlabs_client import ElevenLabsService


class DummyResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"conversation_id": "conv-x", "callSid": "sid-x"}


@pytest.mark.asyncio
async def test_outbound_payload_without_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_request_with_retry(client, method, url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return DummyResponse()

    monkeypatch.setattr(client_module, "request_with_retry", fake_request_with_retry)

    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="x",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        ELEVENLABS_API_KEY="k",
        ELEVENLABS_AGENT_ID="a",
        ELEVENLABS_PHONE_NUMBER_ID="p",
        ELEVENLABS_ALLOW_FIRST_MESSAGE_OVERRIDE=False,
    )
    svc = ElevenLabsService(settings)

    payload = await svc.start_outbound_call(
        call_phone="+33768013446",
        dynamic_variables={
            "car_spoken_ru": "мерседес",
            "price_used_spoken_ru": "семь миллионов иен",
            "car_full": "Mercedes-Benz",
            "car_short": "Mercedes",
            "price_used_jpy": 7138000,
            "price_used_type": "total_price",
            "listing_url": "https://carsensor.example",
            "extracted_phone": "+81438411300",
            "call_phone": "+33768013446",
            "test_mode": True,
            "job_id": "1",
        },
    )

    assert payload["conversation_id"] == "conv-x"
    cicd = captured["conversation_initiation_client_data"]
    assert "conversation_config_override" not in cicd
    assert "prompt" not in str(captured)


@pytest.mark.asyncio
async def test_request_call_payload_contains_only_goal_ru(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_request_with_retry(client, method, url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return DummyResponse()

    monkeypatch.setattr(client_module, "request_with_retry", fake_request_with_retry)

    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="x",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        ELEVENLABS_API_KEY="k",
        ELEVENLABS_AGENT_ID="a",
        ELEVENLABS_PHONE_NUMBER_ID="p",
        ELEVENLABS_ALLOW_FIRST_MESSAGE_OVERRIDE=True,
    )
    svc = ElevenLabsService(settings)

    await svc.start_outbound_call(
        call_phone="+19043876541",
        dynamic_variables={"goal_ru": "Call the sales department at Duval Ford Jacksonville."},
    )

    cicd = captured["conversation_initiation_client_data"]
    assert cicd["dynamic_variables"] == {
        "goal_ru": "Call the sales department at Duval Ford Jacksonville."
    }
    assert "conversation_config_override" not in cicd


@pytest.mark.asyncio
async def test_outbound_payload_first_message_override_only(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_request_with_retry(client, method, url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return DummyResponse()

    monkeypatch.setattr(client_module, "request_with_retry", fake_request_with_retry)

    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="x",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        ELEVENLABS_API_KEY="k",
        ELEVENLABS_AGENT_ID="a",
        ELEVENLABS_PHONE_NUMBER_ID="p",
        ELEVENLABS_ALLOW_FIRST_MESSAGE_OVERRIDE=True,
    )
    svc = ElevenLabsService(settings)

    await svc.start_outbound_call(
        call_phone="+33768013446",
        dynamic_variables={
            "job_id": "1",
            "call_language": "ru",
            "car_spoken_ru": "мерседес бенц е класс",
            "year_spoken_ru": "2026",
            "price_used_spoken_ru": "цена",
        },
    )

    cicd = captured["conversation_initiation_client_data"]
    assert "conversation_config_override" in cicd
    agent = cicd["conversation_config_override"]["agent"]
    assert "first_message" in agent
    assert "prompt" not in agent
    assert "{{car_spoken_ru}}" in agent["first_message"]
    assert "2026" in agent["first_message"]
    assert "комплекта" not in agent["first_message"].lower()


@pytest.mark.asyncio
async def test_outbound_payload_first_message_override_ja(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_request_with_retry(client, method, url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return DummyResponse()

    monkeypatch.setattr(client_module, "request_with_retry", fake_request_with_retry)

    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="x",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        ELEVENLABS_API_KEY="k",
        ELEVENLABS_AGENT_ID="a",
        ELEVENLABS_PHONE_NUMBER_ID="p",
        ELEVENLABS_ALLOW_FIRST_MESSAGE_OVERRIDE=True,
    )
    svc = ElevenLabsService(settings)

    await svc.start_outbound_call(
        call_phone="+81438411300",
        dynamic_variables={
            "job_id": "1",
            "call_language": "ja",
            "car_spoken_ru": "メルセデス ベンツ ジーエルエス",
            "year_spoken_ru": "2021",
        },
    )
    first_message = captured["conversation_initiation_client_data"]["conversation_config_override"]["agent"][
        "first_message"
    ]
    assert "{{car_spoken_ru}}" in first_message
    assert "2021" in first_message
    assert "パッケージ" not in first_message


@pytest.mark.asyncio
async def test_outbound_payload_first_message_override_en(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_request_with_retry(client, method, url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return DummyResponse()

    monkeypatch.setattr(client_module, "request_with_retry", fake_request_with_retry)

    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="x",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        ELEVENLABS_API_KEY="k",
        ELEVENLABS_AGENT_ID="a",
        ELEVENLABS_PHONE_NUMBER_ID="p",
        ELEVENLABS_ALLOW_FIRST_MESSAGE_OVERRIDE=True,
    )
    svc = ElevenLabsService(settings)

    await svc.start_outbound_call(
        call_phone="+17087164497",
        dynamic_variables={
            "job_id": "1",
            "call_language": "en",
            "car_spoken_ru": "Mercedes Benz GLS",
            "year_spoken_ru": "2021",
        },
    )
    first_message = captured["conversation_initiation_client_data"]["conversation_config_override"]["agent"][
        "first_message"
    ]
    assert "{{car_spoken_ru}}" in first_message
    assert "2021" in first_message
    assert "package" not in first_message.lower()


@pytest.mark.asyncio
async def test_outbound_payload_first_message_without_year(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_request_with_retry(client, method, url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return DummyResponse()

    monkeypatch.setattr(client_module, "request_with_retry", fake_request_with_retry)

    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="x",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        ELEVENLABS_API_KEY="k",
        ELEVENLABS_AGENT_ID="a",
        ELEVENLABS_PHONE_NUMBER_ID="p",
        ELEVENLABS_ALLOW_FIRST_MESSAGE_OVERRIDE=True,
    )
    svc = ElevenLabsService(settings)

    await svc.start_outbound_call(
        call_phone="+33768013446",
        dynamic_variables={"job_id": "1", "call_language": "ru", "car_spoken_ru": "мерседес бенц глс"},
    )

    first_message = captured["conversation_initiation_client_data"]["conversation_config_override"]["agent"][
        "first_message"
    ]
    assert "{{car_spoken_ru}}" in first_message
    assert "года" not in first_message


@pytest.mark.asyncio
async def test_outbound_payload_agent_override(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_request_with_retry(client, method, url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return DummyResponse()

    monkeypatch.setattr(client_module, "request_with_retry", fake_request_with_retry)

    settings = Settings(
        TELEGRAM_ADMIN_IDS="1",
        TELEGRAM_BOT_TOKEN="x",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        ELEVENLABS_API_KEY="k",
        ELEVENLABS_AGENT_ID="agent-ru",
        ELEVENLABS_PHONE_NUMBER_ID="p",
    )
    svc = ElevenLabsService(settings)

    await svc.start_outbound_call(
        call_phone="+81438411300",
        dynamic_variables={"job_id": "1", "car_spoken_ru": "ビーエムダブリュー"},
        agent_id_override="agent-ja",
    )

    assert captured["agent_id"] == "agent-ja"
