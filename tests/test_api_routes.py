from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api import main
from app.models import Base
from app.services.twilio_webhook import compute_twilio_signature


@pytest.mark.asyncio
async def test_webhook_route_and_root_404() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_session():
        async with session_maker() as session:
            yield session

    old_elevenlabs_secret = main.settings.elevenlabs_webhook_secret
    old_twilio_token = main.settings.twilio_webhook_auth_token
    main.settings.elevenlabs_webhook_secret = ""
    main.settings.twilio_webhook_auth_token = ""
    main.app.dependency_overrides[main.get_session] = override_get_session

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/webhooks/elevenlabs",
            json={"type": "call_initiation_failure", "event_timestamp": 1, "data": {"conversation_id": "x"}},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        root = await client.post("/", json={"ok": True})
        assert root.status_code == 404

        dbg = await client.get("/debug/routes")
        assert dbg.status_code == 200
        body = dbg.json()
        assert "/health" in body["active_webhook_endpoints"]
        assert "/webhooks/elevenlabs" in body["active_webhook_endpoints"]
        assert "/webhooks/twilio/call-status" in body["active_webhook_endpoints"]

        tw = await client.post(
            "/webhooks/twilio/call-status",
            data={"CallSid": "CA123", "CallStatus": "ringing", "From": "+1000000000", "To": "+12223334444"},
        )
        assert tw.status_code == 200
        assert tw.json() == {"ok": True}

    main.app.dependency_overrides.clear()
    main.settings.elevenlabs_webhook_secret = old_elevenlabs_secret
    main.settings.twilio_webhook_auth_token = old_twilio_token
    await engine.dispose()


@pytest.mark.asyncio
async def test_twilio_status_webhook_validates_signature_when_token_is_set() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_session():
        async with session_maker() as session:
            yield session

    main.settings.twilio_webhook_auth_token = "twilio-test-token"
    main.settings.webhook_base_url = "https://calls.example.com"
    main.app.dependency_overrides[main.get_session] = override_get_session

    params = [
        ("CallSid", "CA123"),
        ("CallStatus", "ringing"),
        ("From", "+1000000000"),
        ("To", "+12223334444"),
    ]
    signature = compute_twilio_signature(
        auth_token="twilio-test-token",
        url="https://calls.example.com/webhooks/twilio/call-status",
        params=params,
    )

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ok = await client.post(
            "/webhooks/twilio/call-status",
            data=dict(params),
            headers={"X-Twilio-Signature": signature},
        )
        assert ok.status_code == 200
        assert ok.json() == {"ok": True}

        rejected = await client.post(
            "/webhooks/twilio/call-status",
            data=dict(params),
            headers={"X-Twilio-Signature": "invalid"},
        )
        assert rejected.status_code == 401

    main.settings.twilio_webhook_auth_token = ""
    main.app.dependency_overrides.clear()
    await engine.dispose()
