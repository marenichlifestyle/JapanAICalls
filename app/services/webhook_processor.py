from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job
from app.repositories import (
    add_job_error,
    append_service_message_id,
    clear_service_message_ids,
    get_job,
    get_job_by_conversation_id,
    save_webhook_event_if_new,
)
from app.services.elevenlabs_client import ElevenLabsService
from app.services.openai_client import OpenAIService
from app.services.request_call import RequestCallService
from app.services.telegram_delivery import safe_delete_message, safe_send_document, safe_send_message
from app.services.telegram_presenter import build_final_report_html, build_transcript_expandable_html

logger = logging.getLogger(__name__)


def flatten_transcript(transcript: list[dict[str, Any]] | None) -> str:
    if not transcript:
        return ""
    lines = []
    for turn in transcript:
        role = turn.get("role", "unknown")
        message = (turn.get("message") or "").strip()
        if message:
            lines.append(f"{role}: {message}")
    return "\n".join(lines)


class WebhookProcessor:
    def __init__(self, *, elevenlabs: ElevenLabsService, openai_service: OpenAIService):
        self.elevenlabs = elevenlabs
        self.openai_service = openai_service

    async def handle(self, *, session: AsyncSession, bot: Bot | None, payload: dict[str, Any]) -> None:
        event_type = payload.get("type")
        data = payload.get("data") or {}
        conversation_id = data.get("conversation_id")

        idempotency_key = self.elevenlabs.build_idempotency_key(payload)
        is_new = await save_webhook_event_if_new(
            session,
            idempotency_key=idempotency_key,
            event_type=event_type or "unknown",
            conversation_id=conversation_id,
            payload=payload,
        )
        if not is_new:
            return

        if event_type != "post_call_transcription":
            return

        job = await self._resolve_job(session, data)
        if not job:
            await add_job_error(
                session,
                code="webhook_failed",
                message="Job not found for webhook conversation",
                details={"conversation_id": conversation_id},
            )
            return
        if job.final_outcome == "no_answer_3_attempts":
            logger.info("ignoring webhook for finalized no-answer job", extra={"job_id": job.id})
            return

        transcript = flatten_transcript(data.get("transcript"))
        analysis_blob = data.get("analysis") or {}
        summary = analysis_blob.get("transcript_summary") or analysis_blob.get("summary") or ""

        metadata = data.get("metadata") or {}
        recording_url = metadata.get("recording_url") or metadata.get("audio_url")
        call_sid = metadata.get("callSid") or data.get("callSid")

        logger.info(
            "elevenlabs post-call received",
            extra={
                "job_id": job.id,
                "status": data.get("status") or "done",
                "conversation_id": conversation_id,
                "call_sid": call_sid,
                "call_status": data.get("status") or "done",
                "call_language": job.call_language,
            },
        )
        logger.info(
            "post-call payload summary: conversation_id=%s call_sid=%s event_type=%s transcript_len=%s recording_url=%s",
            conversation_id,
            call_sid,
            event_type,
            len(transcript or ""),
            recording_url,
        )

        job.call_status = data.get("status") or "done"
        job.call_transcript = transcript
        job.call_summary = summary
        job.recording_url = recording_url
        if call_sid and not job.elevenlabs_call_sid:
            job.elevenlabs_call_sid = call_sid

        if not transcript:
            if job.status in {"call_started", "call_in_progress"} and not job.first_answered_at:
                await session.commit()
                return
            job.status = "webhook_failed"
            await add_job_error(
                session,
                code="webhook_failed",
                message="post_call_transcription received without transcript",
                job_id=job.id,
            )
            await session.commit()
            return

        if (job.source or "").lower() == "request_call":
            service = RequestCallService(
                settings=self.elevenlabs.settings,
                openai_service=self.openai_service,
                elevenlabs_service=self.elevenlabs,
            )
            await service.finalize_job_from_transcript(
                session=session,
                job=job,
                bot=bot,
                transcript=transcript,
                summary=summary,
            )
            return

        try:
            call_analysis = await self.openai_service.analyze_call(transcript, summary)
            job.analysis_available = call_analysis.available
            job.analysis_price_confirmed = call_analysis.price_confirmed
            job.analysis_actual_price = call_analysis.actual_price
            job.analysis_price_change_reason = call_analysis.price_change_reason
            job.analysis_condition_notes = call_analysis.condition_notes
            job.analysis_seller_mood = call_analysis.seller_mood
            job.analysis_next_step = call_analysis.next_step
            job.analysis_final_summary_ru = call_analysis.final_summary_ru
            job.analysis_conclusion = call_analysis.conclusion
            job.analysis_ai_quality_score = call_analysis.ai_quality_score
            job.analysis_ai_quality_reason = call_analysis.ai_quality_reason
            job.status = "completed"
            job.final_outcome = "success"
        except Exception as exc:
            job.status = "openai_failed"
            job.final_outcome = "analysis_failed"
            await add_job_error(
                session,
                code="openai_failed",
                message=f"Call analysis failed: {exc}",
                job_id=job.id,
            )

        await session.commit()

        if bot is not None:
            await self._send_service_message(session, bot, job, "звонок завершён")
            await self._finalize_user_notifications(
                session=session,
                bot=bot,
                job=job,
                transcript=transcript,
                recording_url=recording_url,
            )

    async def _resolve_job(self, session: AsyncSession, data: dict[str, Any]) -> Job | None:
        conversation_id = data.get("conversation_id")
        init_data = data.get("conversation_initiation_client_data") or {}
        dynamic = init_data.get("dynamic_variables") or {}

        if dynamic.get("job_id"):
            job = await get_job(session, int(dynamic["job_id"]))
            if job:
                return job

        if conversation_id:
            return await get_job_by_conversation_id(session, conversation_id)

        return None

    async def _send_recording(self, bot: Bot, chat_id: int, url: str, reply_to_message_id: int | None) -> None:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(url)
                response.raise_for_status()
            filename = "call_recording.mp3"
            file = BufferedInputFile(response.content, filename=filename)
            await safe_send_document(bot, chat_id, file, reply_to_message_id=reply_to_message_id)
        except Exception:
            logger.exception("Failed to send recording")

    async def _send_service_message(self, session: AsyncSession, bot: Bot, job: Job, text: str) -> None:
        message = await safe_send_message(bot, job.telegram_chat_id, text)
        if message is not None:
            await append_service_message_id(session, job=job, message_id=message.message_id)

    async def _cleanup_service_messages(self, session: AsyncSession, bot: Bot, job: Job) -> None:
        for message_id in list(job.telegram_service_message_ids or []):
            ok = await safe_delete_message(bot, job.telegram_chat_id, message_id)
            if not ok:
                logger.debug(
                    "service message cleanup skipped",
                    extra={"job_id": job.id, "status": f"message_id={message_id}"},
                )
        await clear_service_message_ids(session, job=job)

    async def _finalize_user_notifications(
        self,
        *,
        session: AsyncSession,
        bot: Bot,
        job: Job,
        transcript: str,
        recording_url: str | None,
    ) -> None:
        if job.final_report_sent_at:
            return
        success = True
        await self._cleanup_service_messages(session, bot, job)
        reply_to_message_id = job.telegram_source_message_id

        if recording_url:
            await self._send_recording(
                bot,
                job.telegram_chat_id,
                recording_url,
                reply_to_message_id=reply_to_message_id,
            )

        transcript_html, full_transcript = build_transcript_expandable_html(transcript)
        sent = await safe_send_message(
            bot,
            job.telegram_chat_id,
            transcript_html,
            parse_mode="HTML",
            reply_to_message_id=reply_to_message_id,
        )
        success = success and bool(sent)

        if full_transcript:
            transcript_file = BufferedInputFile(full_transcript.encode("utf-8"), filename="transcript.txt")
            sent = await safe_send_document(
                bot,
                job.telegram_chat_id,
                transcript_file,
                caption="Полная транскрибация звонка",
                reply_to_message_id=reply_to_message_id,
            )
            success = success and bool(sent)

        sent = await safe_send_message(
            bot,
            job.telegram_chat_id,
            build_final_report_html(job),
            parse_mode="HTML",
            reply_to_message_id=reply_to_message_id,
        )
        success = success and bool(sent)
        now = datetime.now(timezone.utc)
        if success:
            job.final_report_sent_at = now
            job.final_report_error = None
            job.next_notification_retry_at = None
        else:
            job.notification_attempt_count = int(job.notification_attempt_count or 0) + 1
            delay_min = min(60, 2 ** min(5, job.notification_attempt_count))
            job.final_report_error = "Telegram delivery failed after retries"
            job.next_notification_retry_at = now + timedelta(minutes=delay_min)
        await session.commit()
