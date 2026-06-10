from __future__ import annotations

import json
import logging
from urllib.parse import parse_qsl

from aiogram import Bot
from aiogram.utils.token import TokenValidationError
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import engine, get_session
from app.logging_config import setup_logging
from app.models import Base
from app.services.elevenlabs_client import ElevenLabsService
from app.services.openai_client import OpenAIService
from app.services.twilio_status_processor import TwilioStatusProcessor
from app.services.twilio_webhook import twilio_signature_candidate_urls, verify_twilio_signature
from app.services.webhook_processor import WebhookProcessor

logger = logging.getLogger(__name__)
settings = get_settings()
setup_logging(settings.log_level)

app = FastAPI(title="Japan AI Calls API")


@app.on_event("startup")
async def startup() -> None:
    if settings.auto_create_tables:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.warning("AUTO_CREATE_TABLES=true; use Alembic migrations as the production source of truth")
    logger.info("WEBHOOK_BASE_URL=%s", settings.normalized_webhook_base_url)
    logger.info("ElevenLabs webhook URL: %s", settings.elevenlabs_webhook_endpoint)
    logger.info("TEST_MODE=%s", settings.test_mode)
    logger.info("TEST_CALL_PHONE=%s", settings.test_call_phone)
    logger.info("US_TIMEZONE_FALLBACK=%s", settings.us_timezone_fallback)
    logger.info("Twilio call status webhook URL: %s", settings.twilio_call_status_callback_endpoint)
    for warning in settings.runtime_warnings():
        logger.warning(warning)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/debug/routes")
async def debug_routes() -> dict:
    endpoints = [route.path for route in app.routes if hasattr(route, "path")]
    active = ["/health", "/webhooks/elevenlabs", "/webhooks/twilio/call-status"]
    if settings.telegram_webhook_enabled:
        active.append(settings.telegram_webhook_path)
    return {"active_webhook_endpoints": active, "routes": sorted(set(endpoints))}


@app.post("/webhooks/elevenlabs")
async def elevenlabs_webhook(request: Request, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    raw = await request.body()
    signature = request.headers.get("ElevenLabs-Signature") or request.headers.get("X-ElevenLabs-Signature")

    elevenlabs = ElevenLabsService(settings)
    if not elevenlabs.verify_webhook(raw_body=raw, signature=signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    bot = None
    if payload.get("type") == "post_call_transcription":
        try:
            bot = Bot(settings.telegram_bot_token)
        except TokenValidationError as exc:
            raise HTTPException(status_code=500, detail=f"Invalid TELEGRAM_BOT_TOKEN: {exc}") from exc

    processor = WebhookProcessor(elevenlabs=elevenlabs, openai_service=OpenAIService(settings))
    try:
        await processor.handle(session=session, bot=bot, payload=payload)
    except Exception as exc:
        logger.exception("Webhook processing failed")
        raise HTTPException(status_code=500, detail=f"webhook_failed: {exc}") from exc
    finally:
        if bot is not None:
            await bot.session.close()

    return JSONResponse({"ok": True})


@app.post("/webhooks/twilio/call-status")
async def twilio_call_status_webhook(request: Request, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    content_type = (request.headers.get("content-type") or "").lower()
    raw = await request.body()
    if "application/json" in content_type:
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc
        signature_params = list(payload.items()) if isinstance(payload, dict) else []
    else:
        signature_params = parse_qsl(raw.decode("utf-8"), keep_blank_values=True)
        payload = dict(signature_params)

    if settings.twilio_webhook_auth_token:
        signature = request.headers.get("X-Twilio-Signature")
        valid = verify_twilio_signature(
            auth_token=settings.twilio_webhook_auth_token,
            signature=signature,
            urls=twilio_signature_candidate_urls(request, settings),
            params=signature_params,
        )
        if not valid:
            raise HTTPException(status_code=401, detail="Invalid Twilio signature")

    bot = None
    try:
        try:
            bot = Bot(settings.telegram_bot_token)
        except TokenValidationError:
            bot = None
        processor = TwilioStatusProcessor()
        await processor.handle(session=session, bot=bot, payload=payload)
    except Exception as exc:
        logger.exception("Twilio call status webhook failed")
        raise HTTPException(status_code=500, detail=f"twilio_status_webhook_failed: {exc}") from exc
    finally:
        if bot is not None:
            await bot.session.close()
    return JSONResponse({"ok": True})


async def _telegram_webhook_handler(request: Request) -> JSONResponse:
    payload = await request.json()
    logger.info("telegram webhook payload received", extra={"status": "ignored"})
    logger.debug("telegram payload keys=%s", list(payload.keys()))
    return JSONResponse({"ok": True, "mode": "polling_recommended"})


if settings.telegram_webhook_enabled:
    app.add_api_route(settings.telegram_webhook_path, _telegram_webhook_handler, methods=["POST"])
