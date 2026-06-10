from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import (
    add_call_event,
    add_job_error,
    get_job_by_provider_call_sid,
)
from app.services.call_state import FINAL_JOB_STATUSES, normalize_twilio_call_status, sanitize_payload
from app.config import get_settings
from app.services.elevenlabs_client import ElevenLabsService
from app.services.openai_client import OpenAIService
from app.services.request_call import RequestCallService
from app.services.telegram_delivery import safe_send_message

logger = logging.getLogger(__name__)


def _to_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except Exception:
        return None


class TwilioStatusProcessor:
    async def handle(self, *, session: AsyncSession, bot: Bot | None, payload: dict[str, Any]) -> None:
        call_sid = str(payload.get("CallSid") or payload.get("call_sid") or "").strip()
        raw_status = str(payload.get("CallStatus") or payload.get("call_status") or "").strip().lower() or None
        normalized = normalize_twilio_call_status(raw_status)
        duration = _to_int(payload.get("CallDuration"))
        error_code = str(payload.get("ErrorCode") or "").strip() or None
        error_message = str(payload.get("ErrorMessage") or "").strip() or None
        from_phone = str(payload.get("From") or "").strip() or None
        to_phone = str(payload.get("To") or "").strip() or None

        job = await get_job_by_provider_call_sid(session, call_sid) if call_sid else None
        if not job and call_sid:
            await add_job_error(
                session,
                code="twilio_callback_unmatched",
                message="Twilio callback without matching job",
                details={"call_sid": call_sid, "payload": sanitize_payload(payload)},
            )
            return

        await add_call_event(
            session,
            job_id=job.id if job else None,
            provider="twilio",
            provider_call_sid=call_sid or None,
            event_type="status_callback",
            raw_call_status=raw_status,
            normalized_status=normalized,
            from_phone=from_phone,
            to_phone=to_phone,
            duration_seconds=duration,
            error_code=error_code,
            error_message=error_message,
            raw_payload_json=sanitize_payload(payload),
        )

        if not job or not normalized:
            return

        if (job.source or "").lower() == "request_call" and normalized in {"busy", "no_answer", "failed", "canceled"}:
            settings = get_settings()
            service = RequestCallService(
                settings=settings,
                openai_service=OpenAIService(settings),
                elevenlabs_service=ElevenLabsService(settings),
            )
            await service.finalize_job_status(
                session=session,
                job=job,
                bot=bot,
                call_status=normalized,
                summary=self._message_for_status(
                    normalized,
                    call_sid=call_sid,
                    attempt_count=job.attempt_count,
                    max_attempts=job.max_attempts,
                )
                or normalized,
            )
            return

        previous = job.status
        job.call_status = normalized
        job.status = normalized if normalized != "in_progress" else "in_progress"
        now = datetime.now(timezone.utc)
        if normalized in {"initiated", "ringing"} and not job.started_at:
            job.started_at = now
        if normalized == "in_progress" and not job.answered_at:
            job.answered_at = now
        if normalized in {"completed", "busy", "no_answer", "failed", "canceled"}:
            job.completed_at = now
            if normalized in FINAL_JOB_STATUSES:
                job.final_outcome = normalized

        retry_allowed = normalized in {"busy", "no_answer"}
        if retry_allowed and int(job.attempt_count or 0) < int(job.max_attempts or 3):
            next_attempt = int(job.attempt_count or 0) + 1
            delay_min = 10 if next_attempt <= 2 else 30
            job.status = "retry_scheduled"
            job.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=delay_min)
            job.queued_reason = f"{normalized}_retry"

        await session.commit()

        if bot is None or previous == job.status:
            return
        text = self._message_for_status(job.status, call_sid=call_sid, attempt_count=job.attempt_count, max_attempts=job.max_attempts)
        if text:
            message = await safe_send_message(bot, job.telegram_chat_id, text)
            if message is None:
                return
            ids = list(job.telegram_service_message_ids or [])
            if message.message_id not in ids:
                ids.append(message.message_id)
                job.telegram_service_message_ids = ids
                await session.commit()

    @staticmethod
    def _message_for_status(status: str, *, call_sid: str, attempt_count: int | None, max_attempts: int | None) -> str | None:
        if status == "queued":
            return "Звонок поставлен в очередь"
        if status == "initiated":
            return "Провайдер начал набор номера"
        if status == "ringing":
            return "Дозваниваюсь, у абонента идёт вызов"
        if status in {"in_progress", "answered"}:
            return "Трубку взяли, разговор начался"
        if status == "completed":
            return "Звонок завершён"
        if status == "busy":
            if attempt_count and max_attempts and attempt_count < max_attempts:
                return f"Линия занята. Запланирован повторный звонок ({attempt_count+1}/{max_attempts})"
            return "Линия занята"
        if status == "no_answer":
            if attempt_count and max_attempts and attempt_count < max_attempts:
                return f"Нет ответа. Запланирован повторный звонок ({attempt_count+1}/{max_attempts})"
            return "Нет ответа, трубку не взяли до таймаута"
        if status == "failed":
            return "Звонок не удался"
        if status == "canceled":
            return "Звонок отменён"
        if status == "call_created":
            return f"Звонок создан. CallSid: {call_sid}\nОжидаю статусы от провайдера"
        return None
