from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import phone_review_keyboard
from app.config import Settings
from app.db import SessionLocal
from app.models import Job
from app.repositories import (
    add_call_event,
    add_job_error,
    add_provider_error,
    append_service_message_id,
    clear_service_message_ids,
    get_job,
    list_notification_retry_jobs,
    list_active_call_jobs,
    list_due_call_queue_jobs,
)
from app.services.cars_com_parser import parse_cars_com_deterministic
from app.schemas import DealerPhoneResolutionResult, ExtractionResult
from app.services.carsensor_parser import fetch_listing_page, parse_deterministic
from app.services.dealer_phone_resolver import DealerPhoneResolver
from app.services.dealer_phone_resolver_us import DealerPhoneResolverUS
from app.services.call_state import (
    classify_twilio_create_failure,
    extract_twilio_error_code,
    normalize_twilio_call_status,
    sanitize_payload,
)
from app.services.elevenlabs_client import ElevenLabsService, ProviderCallCreateError
from app.services.office_hours import build_office_schedule, is_open_now, next_opening_utc
from app.services.openai_client import OpenAIService
from app.services.telegram_delivery import safe_delete_message, safe_send_document, safe_send_message
from app.services.telegram_presenter import build_final_report_html, build_transcript_expandable_html
from app.services.webhook_processor import flatten_transcript
from app.utils.phone import normalize_jp_phone_to_e164, normalize_us_phone_to_e164
from app.utils.spoken import compact_car_name_for_call, ensure_brand_in_car_name
from app.utils.timezone import resolve_office_timezone

logger = logging.getLogger(__name__)


class CallWorkflow:
    def __init__(self, *, settings: Settings, openai_service: OpenAIService, elevenlabs_service: ElevenLabsService):
        self.settings = settings
        self.openai_service = openai_service
        self.elevenlabs_service = elevenlabs_service
        self.jp_phone_resolver = DealerPhoneResolver(openai_service=openai_service)
        self.us_phone_resolver = DealerPhoneResolverUS(timeout_sec=self.settings.request_timeout_sec)
        self._fallback_running: set[int] = set()

    async def run(self, *, session: AsyncSession, job: Job, bot: Bot, call_language: str = "ru") -> Job:
        await self._set_job_status(
            session=session,
            job=job,
            bot=bot,
            status="received_link",
            text=f"Получена ссылка: {job.listing_url}\nJob #{job.id}",
        )
        source = self._detect_source(job.listing_url, current=job.source)
        job.source = source
        job.provider = "twilio"
        if source == "cars.com":
            call_language = "en"
        elif call_language == "ja":
            call_language = "ja"
        else:
            call_language = "ru"
        effective_test_mode = self.settings.test_mode and call_language == "ru" and source != "cars.com"
        job.call_language = call_language
        job.max_attempts = int(self.settings.call_attempt_max)
        job.office_tz, office_tz_reason = self._resolve_job_office_timezone(job=job, source=source)
        logger.info(
            "office timezone resolved",
            extra={
                "job_id": job.id,
                "source": source,
                "dealer_address": job.dealer_address,
                "office_tz": job.office_tz,
                "status": office_tz_reason,
            },
        )
        await session.commit()

        await self._set_job_status(session=session, job=job, bot=bot, status="fetching_page", text="Достаю страницу")
        artifacts = await fetch_listing_page(job.listing_url, timeout=self.settings.request_timeout_sec)

        await self._set_job_status(session=session, job=job, bot=bot, status="extracting_data", text="Извлекаю данные")
        if source == "cars.com":
            deterministic = parse_cars_com_deterministic(job.listing_url, artifacts.html, artifacts.text)
        else:
            deterministic = parse_deterministic(job.listing_url, artifacts.html, artifacts.text)

        low_confidence = deterministic.extraction_confidence < 0.75
        has_required = self._has_required_fields(deterministic, source=source)

        extracted = deterministic
        should_fallback_openai = low_confidence or not has_required
        if should_fallback_openai:
            job.raw_html_snapshot = artifacts.html
            job.extracted_text_snapshot = artifacts.text
            await session.commit()

            if low_confidence:
                await self._record_error(
                    session,
                    job,
                    code="low_confidence_extraction",
                    message=f"Deterministic confidence={deterministic.extraction_confidence}",
                )

            try:
                if source == "cars.com":
                    extracted = await self.openai_service.extract_cars_com_with_web_search(url=job.listing_url)
                else:
                    extracted = await self.openai_service.extract_listing(
                        url=job.listing_url,
                        text=artifacts.text,
                        html_fragments=artifacts.html[:12000],
                    )
            except Exception as exc:
                await self._record_error(
                    session,
                    job,
                    code="openai_failed",
                    message=f"OpenAI extraction failed: {exc}",
                )
                job.status = "openai_failed"
                await session.commit()
                await self._status(session, job, bot, f"Ошибка: openai_failed ({exc})")
                return job

        await self._set_job_status(session=session, job=job, bot=bot, status="normalizing_data", text="Нормализую данные")
        self._persist_extraction(job, extracted)
        job.office_tz, office_tz_reason = self._resolve_job_office_timezone(job=job, source=source)
        logger.info(
            "office timezone resolved",
            extra={
                "job_id": job.id,
                "source": source,
                "dealer_address": job.dealer_address,
                "office_tz": job.office_tz,
                "status": office_tz_reason,
            },
        )

        await self._set_job_status(
            session=session,
            job=job,
            bot=bot,
            status="resolving_dealer_phone",
            text="Резолвлю номер дилера",
        )
        if source == "cars.com":
            resolver = await self.us_phone_resolver.resolve(extracted=extracted, listing_html=artifacts.html)
        else:
            resolver = await self.jp_phone_resolver.resolve(extracted=extracted)
        self._persist_resolver_result(job, resolver)

        if resolver.resolved_phone_e164:
            job.extracted_phone = resolver.resolved_phone_e164
            job.possibly_not_callable_internationally = resolver.source_type in {"carsensor", "aggregator", "directory"}
            await self._set_job_status(
                session=session,
                job=job,
                bot=bot,
                status="dealer_phone_resolved",
                text=(
                    f"Номер дилера найден: {resolver.resolved_phone_e164}\n"
                    f"Источник: {resolver.source_type or 'unknown'}\n"
                    f"Уверенность: {resolver.confidence_score}"
                ),
                commit=False,
            )

        unresolved = resolver.resolution_status in {"needs_review", "proxy_only", "not_found", "invalid_number"}
        allow_test_fallback = unresolved and effective_test_mode and source != "cars.com"

        if unresolved and not allow_test_fallback:
            job.status = "dealer_phone_needs_review" if resolver.resolution_status == "needs_review" else "dealer_phone_resolution_failed"
            await session.commit()
            if resolver.resolution_status == "needs_review":
                candidates = resolver.candidates or []
                review_text = self._build_phone_review_text(job, resolver)
                message = await safe_send_message(
                    bot,
                    job.telegram_chat_id,
                    review_text,
                    reply_markup=phone_review_keyboard(job.id, len(candidates)),
                )
                if message is not None:
                    await append_service_message_id(session, job=job, message_id=message.message_id)
                await self._record_error(
                    session,
                    job,
                    code="dealer_phone_needs_review",
                    message=f"Dealer phone resolver requires review (score={resolver.confidence_score})",
                )
            else:
                code = {
                    "proxy_only": "dealer_phone_proxy_only",
                    "not_found": "dealer_phone_not_found",
                    "invalid_number": "dealer_phone_invalid",
                }.get(resolver.resolution_status, "dealer_phone_resolver_failed")
                await self._record_error(
                    session,
                    job,
                    code=code,
                    message=f"Dealer phone resolver status={resolver.resolution_status}; reason={resolver.error_reason}",
                )
                await self._status(session, job, bot, f"Ошибка: {code}")
            return job

        if allow_test_fallback:
            await self._record_error(
                session,
                job,
                code="dealer_phone_resolver_failed",
                message=(
                    "Resolver did not return resolved phone; TEST_MODE fallback call allowed. "
                    f"resolver_status={resolver.resolution_status}"
                ),
            )
            await self._status(
                session,
                job,
                bot,
                "Внимание: прямой номер дилера не подтвержден, в TEST_MODE будет тестовый звонок.",
            )

        await self._set_job_status(session=session, job=job, bot=bot, status="preparing_agent", text="Готовлю агента")
        spoken_ok = await self._ensure_spoken_ready(
            session=session,
            job=job,
            extracted=extracted,
            bot=bot,
            call_language=call_language,
        )
        if not spoken_ok:
            return job

        call_phone = self._compute_call_phone(
            job=job,
            effective_test_mode=effective_test_mode,
            allow_test_fallback=allow_test_fallback,
        )
        if not call_phone:
            job.status = "dealer_phone_not_found"
            await session.commit()
            await self._record_error(
                session,
                job,
                code="dealer_phone_not_found",
                message="No callable phone after resolver",
            )
            await self._status(session, job, bot, "Ошибка: dealer_phone_not_found")
            return job

        preflight_ok, preflight_hint = self._preflight_twilio_plus_one(call_phone)
        if not preflight_ok:
            job.status = "call_create_failed"
            job.last_error_code = "preflight_failed"
            job.last_error_message = "Twilio account is not ready for +1 outbound"
            job.last_error_hint = preflight_hint
            await session.commit()
            await add_provider_error(
                session,
                job_id=job.id,
                provider="twilio",
                stage="preflight",
                http_status=None,
                provider_error_code="preflight_failed",
                provider_error_message=job.last_error_message,
                provider_more_info_url=None,
                from_phone=job.from_phone_e164,
                to_phone=call_phone,
                human_readable_hint=preflight_hint,
                raw_payload_json=sanitize_payload(
                    {
                        "listing_url": job.listing_url,
                        "source": job.source,
                        "to_phone": call_phone,
                        "checks": {
                            "twilio_plus_one_allowed": self.settings.twilio_plus_one_allowed,
                            "twilio_billing_active": self.settings.twilio_billing_active,
                            "twilio_geo_us_ca_enabled": self.settings.twilio_geo_us_ca_enabled,
                            "twilio_from_number_verified": self.settings.twilio_from_number_verified,
                            "twilio_marked_as_allowed_for_plus_one": self.settings.twilio_marked_as_allowed_for_plus_one,
                        },
                    }
                ),
            )
            await self._status(
                session,
                job,
                bot,
                "Звонок на +1 не запущен: аккаунт Twilio не готов для US/Canada outbound. "
                "Проверьте Business Primary Customer Profile, Billing, From number и Geo Permissions.",
            )
            return job

        job.call_phone = call_phone
        schedule = self._build_schedule(job)
        job.office_hours_json = dict(schedule)
        await session.commit()

        return await self._schedule_or_start_attempt(
            session=session,
            job=job,
            bot=bot,
            effective_test_mode=effective_test_mode,
        )

    async def run_queue_worker(self, bot: Bot) -> None:
        poll = max(3, int(self.settings.queue_worker_poll_sec))
        logger.info("call queue worker started", extra={"status": f"poll={poll}s"})
        while True:
            try:
                await self._process_due_queue_jobs(bot)
                await self._monitor_active_calls(bot)
                await self._process_due_notification_jobs(bot)
            except Exception:
                logger.exception("queue worker loop failed")
            await asyncio.sleep(poll)

    async def _process_due_notification_jobs(self, bot: Bot) -> None:
        async with SessionLocal() as session:
            jobs = await list_notification_retry_jobs(session, limit=10)
            for job in jobs:
                if (job.source or "").lower() == "request_call":
                    from app.models import DealerCallTarget, RequestCallCampaign
                    from app.services.request_call import RequestCallService

                    campaign = await session.get(RequestCallCampaign, job.request_campaign_id)
                    target = await session.get(DealerCallTarget, job.request_target_id)
                    if not campaign or not target:
                        job.final_report_error = "Request-call campaign or target not found for notification retry"
                        job.next_notification_retry_at = None
                        await session.commit()
                        continue
                    service = RequestCallService(
                        settings=self.settings,
                        openai_service=self.openai_service,
                        elevenlabs_service=self.elevenlabs_service,
                    )
                    await service.send_target_report(
                        session=session,
                        campaign=campaign,
                        target=target,
                        bot=bot,
                        job=job,
                    )
                    continue
                audio = None
                if job.elevenlabs_conversation_id:
                    try:
                        audio = await self.elevenlabs_service.fetch_conversation_audio(job.elevenlabs_conversation_id)
                    except Exception as exc:
                        logger.warning(
                            "notification retry audio fetch failed",
                            extra={"job_id": job.id, "status": str(exc)},
                        )
                await self._send_final_notifications(session=session, job=job, bot=bot, audio=audio)

    async def _process_due_queue_jobs(self, bot: Bot) -> None:
        async with SessionLocal() as session:
            jobs = await list_due_call_queue_jobs(session, limit=20)
            for job in jobs:
                now_utc = datetime.now(timezone.utc)
                source = self._detect_source(job.listing_url, current=job.source)
                effective_test_mode = self.settings.test_mode and (job.call_language or "ru") == "ru" and source != "cars.com"
                if self._is_stale_carsensor_ru_office_queue(job, now_utc):
                    job.status = "canceled"
                    job.final_outcome = "stale_office_queue_canceled"
                    await session.commit()
                    await self._status(
                        session,
                        job,
                        bot,
                        "Старый отложенный тестовый RU-прозвон Carsensor отменён, "
                        "чтобы он не стартовал внезапно после перезапуска.",
                    )
                    continue
                if job.status in {"retry_scheduled", "queued_retry"}:
                    job.status = "retrying"
                    await session.commit()
                    await self._status(
                        session,
                        job,
                        bot,
                        f"Начинаю повторную попытку {int(job.attempt_count or 0)+1}/{int(job.max_attempts or self.settings.call_attempt_max)}",
                    )
                call_phone = job.call_phone or self._compute_call_phone(
                    job=job,
                    effective_test_mode=effective_test_mode,
                    allow_test_fallback=(
                        source != "cars.com"
                        and effective_test_mode
                        and (job.resolver_status in {"needs_review", "not_found", "proxy_only", "invalid_number"})
                    ),
                )
                if not call_phone:
                    job.status = "dealer_phone_not_found"
                    await session.commit()
                    await self._record_error(
                        session,
                        job,
                        code="dealer_phone_not_found",
                        message="Queued job has no callable phone",
                    )
                    continue
                job.call_phone = call_phone
                await session.commit()
                await self._schedule_or_start_attempt(
                    session=session,
                    job=job,
                    bot=bot,
                    effective_test_mode=effective_test_mode,
                )

    async def _monitor_active_calls(self, bot: Bot) -> None:
        ping_sec = max(10, int(self.settings.call_progress_ping_sec))
        timeout_sec = max(30, int(self.settings.call_ring_timeout_sec))
        create_timeout_sec = max(30, int(self.settings.call_create_timeout_sec))
        provider_progress_timeout = max(60, int(self.settings.provider_progress_timeout_sec))
        max_call_duration = max(120, int(self.settings.max_call_duration_seconds))

        async with SessionLocal() as session:
            jobs = await list_active_call_jobs(session, limit=20)
            for job in jobs:
                now_utc = datetime.now(timezone.utc)
                started = self._as_utc(job.last_attempt_at or job.updated_at) or now_utc

                if job.status == "creating_call":
                    if (now_utc - started).total_seconds() >= create_timeout_sec:
                        job.status = "timeout"
                        job.call_status = "timeout"
                        job.last_error_message = "create_call watchdog timeout"
                        job.last_error_hint = "Провайдер не подтвердил создание звонка вовремя."
                        await session.commit()
                        await self._status(
                            session,
                            job,
                            bot,
                            "Таймаут создания звонка: провайдер не вернул подтверждение за 60 секунд.",
                        )
                    else:
                        await session.commit()
                    continue

                if not job.elevenlabs_conversation_id and not job.provider_call_sid:
                    continue
                try:
                    details = (
                        await self.elevenlabs_service.fetch_conversation_details(job.elevenlabs_conversation_id)
                        if job.elevenlabs_conversation_id
                        else {}
                    )
                except Exception as exc:
                    logger.warning(
                        "monitor: details fetch failed",
                        extra={"job_id": job.id, "status": str(exc)},
                    )
                    progress_base = self._as_utc(job.last_progress_at or job.last_attempt_at or job.updated_at) or now_utc
                    if (now_utc - progress_base).total_seconds() >= provider_progress_timeout:
                        job.status = "provider_timeout"
                        job.call_status = "provider_timeout"
                        await session.commit()
                        await self._status(
                            session,
                            job,
                            bot,
                            "Таймаут провайдера: нет статусных callbacks. Помечаю звонок как provider_timeout.",
                        )
                    continue

                observed_status = str(details.get("status") or job.call_status or "initiated")
                metadata = details.get("metadata") or {}
                phone_call = metadata.get("phone_call") or {}
                call_sid = details.get("callSid") or (details.get("metadata") or {}).get("callSid")
                transcript_rows = details.get("transcript") or []
                has_user_turn = any(
                    (row.get("role") == "user") and (row.get("message") or "").strip() for row in transcript_rows if isinstance(row, dict)
                )
                answered = observed_status == "in-progress" or has_user_turn

                previous_status = job.call_status
                normalized_status = normalize_twilio_call_status(observed_status) or {
                    "in-progress": "in_progress",
                    "processing": "in_progress",
                    "done": "completed",
                }.get(observed_status, None)
                if normalized_status:
                    job.call_status = normalized_status
                    job.status = normalized_status if normalized_status != "in_progress" else "in_progress"
                if call_sid and not job.provider_call_sid:
                    job.elevenlabs_call_sid = call_sid
                    job.provider_call_sid = call_sid

                if normalized_status and normalized_status != previous_status:
                    await session.commit()
                    await add_call_event(
                        session,
                        job_id=job.id,
                        provider="twilio",
                        provider_call_sid=job.provider_call_sid or call_sid,
                        event_type="polling",
                        raw_call_status=observed_status,
                        normalized_status=normalized_status,
                        from_phone=job.from_phone_e164,
                        to_phone=job.call_phone,
                        duration_seconds=None,
                        error_code=None,
                        error_message=None,
                        raw_payload_json={"details_status": observed_status},
                    )
                    status_text = self._call_progress_status_text(normalized_status)
                    if status_text:
                        await self._status(session, job, bot, status_text)

                base_attempt_at = self._as_utc(job.last_attempt_at or job.updated_at) or now_utc
                elapsed = int((now_utc - base_attempt_at).total_seconds())
                last_ping_at = self._as_utc(job.last_progress_at or job.last_attempt_at or job.updated_at) or now_utc
                should_ping = (now_utc - last_ping_at).total_seconds() >= ping_sec
                terminal_without_answer = observed_status in {"done", "failed"} and not answered and not job.first_answered_at

                initiated_without_progress = (
                    observed_status == "initiated"
                    and not answered
                    and not metadata.get("accepted_time_unix_secs")
                    and int(metadata.get("call_duration_secs") or 0) == 0
                    and not (phone_call.get("stream_sid") or "").strip()
                    and not details.get("has_audio")
                    and not transcript_rows
                    and elapsed >= max(30, ping_sec * 2)
                )
                if initiated_without_progress:
                    await self._handle_provider_timeout(
                        session=session,
                        job=job,
                        bot=bot,
                        reason="initiated_without_twilio_progress",
                    )
                    continue

                if answered:
                    if not job.first_answered_at:
                        job.first_answered_at = now_utc
                        job.answered_at = now_utc
                        job.status = "answered"
                        job.last_progress_at = now_utc
                        await session.commit()
                        await self._status(session, job, bot, "Трубку взяли, разговор начался")
                        self._spawn_post_call_fallback(job.id)
                    else:
                        if (now_utc - (self._as_utc(job.answered_at) or now_utc)).total_seconds() >= max_call_duration:
                            job.status = "timeout"
                            job.call_status = "timeout"
                            await session.commit()
                            await self._status(session, job, bot, "Звонок превысил лимит длительности, помечен как timeout")
                            continue
                        await session.commit()
                    continue

                if job.first_answered_at and observed_status in {"processing", "done"}:
                    completed = observed_status == "done"
                    job.status = "completed" if completed else "in_progress"
                    job.call_status = "completed" if completed else "in_progress"
                    job.last_progress_at = now_utc
                    if completed and not job.completed_at:
                        job.completed_at = now_utc
                    await session.commit()
                    self._spawn_post_call_fallback(job.id)
                    continue

                if observed_status in {"busy", "no-answer"}:
                    await self._handle_no_answer(session=session, job=job, bot=bot, reason=observed_status)
                    continue
                if observed_status in {"canceled", "failed"} and not answered:
                    job.status = "failed" if observed_status == "failed" else "canceled"
                    job.call_status = job.status
                    job.completed_at = now_utc
                    await session.commit()
                    await self._status(
                        session,
                        job,
                        bot,
                        "Звонок не удался" if observed_status == "failed" else "Звонок отменён",
                    )
                    continue

                stale_before_ringing = (
                    observed_status in {"queued", "initiated", "call_created"}
                    and not job.first_answered_at
                    and elapsed >= provider_progress_timeout
                )
                if stale_before_ringing:
                    await self._handle_provider_timeout(
                        session=session,
                        job=job,
                        bot=bot,
                        reason=observed_status,
                    )
                    continue

                if terminal_without_answer or (
                    elapsed >= timeout_sec and not job.first_answered_at and observed_status == "ringing"
                ):
                    await self._handle_no_answer(session=session, job=job, bot=bot, reason=observed_status)
                    continue

                if should_ping:
                    job.last_progress_at = now_utc
                    await session.commit()
                else:
                    await session.commit()

    @staticmethod
    def _call_progress_status_text(status: str) -> str | None:
        return {
            "queued": "Звонок поставлен в очередь",
            "initiated": "Провайдер начал набор номера",
            "ringing": "Дозваниваюсь, у абонента идёт вызов",
        }.get(status)

    def _spawn_post_call_fallback(self, job_id: int) -> None:
        if not self.settings.post_call_fallback_enabled:
            return
        if job_id in self._fallback_running:
            return
        self._fallback_running.add(job_id)

        async def runner() -> None:
            try:
                await self._post_call_fallback(job_id)
            except Exception:
                logger.exception("post-call fallback task failed", extra={"job_id": job_id})
            finally:
                self._fallback_running.discard(job_id)

        asyncio.create_task(runner())

    async def _schedule_or_start_attempt(
        self,
        *,
        session: AsyncSession,
        job: Job,
        bot: Bot,
        effective_test_mode: bool,
    ) -> Job:
        if self._should_bypass_office_hours(job):
            logger.info(
                "office hours bypass enabled",
                extra={
                    "job_id": job.id,
                    "source": job.source,
                    "call_language": job.call_language,
                    "status": "carsensor_ru_test_call",
                },
            )
            await self._status(
                session,
                job,
                bot,
                "тестовый RU-прозвон Carsensor: график работы игнорируется, запускаю звонок сейчас",
            )
            return await self._start_attempt(session=session, job=job, bot=bot, effective_test_mode=effective_test_mode)

        if (job.source or "").lower() == "cars.com":
            recalculated_tz, tz_reason = self._resolve_job_office_timezone(job=job, source="cars.com")
            if recalculated_tz != (job.office_tz or ""):
                job.office_tz = recalculated_tz
                await session.commit()
                logger.info(
                    "office timezone recalculated before scheduling",
                    extra={
                        "job_id": job.id,
                        "source": job.source,
                        "dealer_address": job.dealer_address,
                        "office_tz": job.office_tz,
                        "status": tz_reason,
                    },
                )

        schedule = self._build_schedule(job)
        now_local = self._job_now(job)
        if not is_open_now(schedule, now_local):
            next_open_utc = next_opening_utc(schedule, now_local)
            job.status = "queued"
            job.next_attempt_at = next_open_utc
            job.queued_reason = "outside_office_hours"
            await session.commit()
            tz_label = schedule.get("office_tz") or job.office_tz or self.settings.office_timezone
            next_local = next_open_utc.astimezone(self._job_tz(job))
            logger.info(
                "job queued by office hours",
                extra={
                    "job_id": job.id,
                    "source": job.source,
                    "office_tz": tz_label,
                    "schedule_source": schedule.get("source"),
                    "next_attempt_at": next_open_utc.isoformat(),
                    "status": "outside_office_hours",
                },
            )
            await self._status(
                session,
                job,
                bot,
                f"сейчас нерабочее время, ставлю в очередь на {next_local:%Y-%m-%d %H:%M} ({tz_label})",
            )
            return job

        return await self._start_attempt(session=session, job=job, bot=bot, effective_test_mode=effective_test_mode)

    @staticmethod
    def _should_bypass_office_hours(job: Job) -> bool:
        source = (job.source or "").lower()
        call_language = (job.call_language or "ru").lower()
        return source == "carsensor" and call_language == "ru"

    @staticmethod
    def _is_stale_carsensor_ru_office_queue(job: Job, now_utc: datetime) -> bool:
        source = (job.source or "").lower()
        call_language = (job.call_language or "ru").lower()
        if source != "carsensor" or call_language != "ru":
            return False
        if job.queued_reason != "outside_office_hours":
            return False
        if job.elevenlabs_conversation_id or job.provider_call_sid:
            return False
        created_at = CallWorkflow._as_utc(job.created_at) or now_utc
        return (now_utc - created_at).total_seconds() > 15 * 60

    async def _start_attempt(self, *, session: AsyncSession, job: Job, bot: Bot, effective_test_mode: bool) -> Job:
        max_attempts = max(1, int(job.max_attempts or self.settings.call_attempt_max))
        if (job.attempt_count or 0) >= max_attempts:
            await self._finalize_no_answer(session=session, job=job, bot=bot)
            return job

        if not job.call_phone:
            job.status = "dealer_phone_not_found"
            await session.commit()
            await self._record_error(session, job, code="dealer_phone_not_found", message="Call phone is missing")
            return job

        dynamic_variables = self._build_dynamic_variables(job, effective_test_mode=effective_test_mode)
        is_valid, invalid_reason = self._validate_dynamic_variables(dynamic_variables)
        if not is_valid:
            job.status = "dynamic_variables_invalid"
            job.last_error_code = "dynamic_variables_invalid"
            job.last_error_message = invalid_reason
            await session.commit()
            await self._record_error(
                session,
                job,
                code="dynamic_variables_invalid",
                message=f"Outbound dynamic variables invalid: {invalid_reason}",
                details={"dynamic_variables": dynamic_variables},
            )
            await self._status(
                session,
                job,
                bot,
                f"Ошибка: dynamic_variables_invalid ({invalid_reason})",
            )
            return job

        now_utc = datetime.now(timezone.utc)
        job.max_attempts = max_attempts
        job.attempt_count = int(job.attempt_count or 0) + 1
        job.last_attempt_at = now_utc
        job.last_progress_at = now_utc
        job.next_attempt_at = None
        job.queued_reason = None
        job.elevenlabs_conversation_id = None
        job.elevenlabs_call_sid = None
        job.call_status = "creating_call"
        job.status = "creating_call"
        job.started_at = now_utc
        await session.commit()
        await self._status(session, job, bot, "Создаю звонок")

        agent_override = None
        if (job.call_language or "ru") == "ja":
            agent_override = self.settings.effective_elevenlabs_agent_id_ja
        elif (job.call_language or "ru") == "en":
            agent_override = self.settings.effective_elevenlabs_agent_id_en

        selected_agent_id = agent_override or self.settings.elevenlabs_agent_id
        logger.info(
            "selected elevenlabs agent for outbound",
            extra={
                "job_id": job.id,
                "call_language": job.call_language,
                "source": job.source,
                "agent_id": selected_agent_id,
            },
        )

        try:
            payload = await self.elevenlabs_service.start_outbound_call(
                call_phone=job.call_phone,
                dynamic_variables=dynamic_variables,
                agent_id_override=agent_override,
            )
        except ProviderCallCreateError as exc:
            hint, can_retry = classify_twilio_create_failure(
                http_status=exc.http_status,
                provider_error_code=exc.provider_error_code or extract_twilio_error_code(exc.provider_error_message),
                provider_error_message=exc.provider_error_message,
            )
            job.status = "call_create_failed"
            job.call_status = "call_create_failed"
            job.last_error_code = exc.provider_error_code or extract_twilio_error_code(exc.provider_error_message)
            job.last_error_message = exc.provider_error_message
            job.last_error_hint = hint
            await session.commit()
            await add_provider_error(
                session,
                job_id=job.id,
                provider=exc.provider,
                stage=exc.stage,
                http_status=exc.http_status,
                provider_error_code=job.last_error_code,
                provider_error_message=exc.provider_error_message,
                provider_more_info_url=exc.provider_more_info_url,
                from_phone=job.from_phone_e164,
                to_phone=job.call_phone,
                human_readable_hint=hint,
                raw_payload_json=sanitize_payload(exc.payload_without_secrets or {}),
            )
            await self._record_error(
                session,
                job,
                code="call_create_failed",
                message=f"Create call failed: {exc.provider_error_message}",
            )
            await self._status(
                session,
                job,
                bot,
                (
                    "Не удалось создать звонок.\n"
                    f"Провайдер вернул ошибку: {exc.provider_error_message}\n"
                    f"Код ошибки: {job.last_error_code or '—'}\n"
                    f"HTTP статус: {exc.http_status or '—'}\n"
                    f"Номер: {job.call_phone or '—'}\n"
                    f"Что проверить: {hint}"
                ),
            )
            if can_retry and int(job.attempt_count or 0) < max_attempts:
                await self._schedule_retry(session=session, job=job, bot=bot, reason="provider_temporary_error")
            return job
        except Exception as exc:
            hint, can_retry = classify_twilio_create_failure(
                http_status=None,
                provider_error_code=None,
                provider_error_message=str(exc),
            )
            job.status = "call_create_failed"
            job.call_status = "call_create_failed"
            job.last_error_code = extract_twilio_error_code(str(exc))
            job.last_error_message = str(exc)
            job.last_error_hint = hint
            await session.commit()
            await add_provider_error(
                session,
                job_id=job.id,
                provider="twilio",
                stage="create_call",
                http_status=None,
                provider_error_code=job.last_error_code,
                provider_error_message=str(exc),
                provider_more_info_url=None,
                from_phone=job.from_phone_e164,
                to_phone=job.call_phone,
                human_readable_hint=hint,
                raw_payload_json=sanitize_payload({"error": str(exc)}),
            )
            await self._record_error(session, job, code="call_create_failed", message=f"Create call failed: {exc}")
            await self._status(
                session,
                job,
                bot,
                (
                    "Не удалось создать звонок.\n"
                    f"Провайдер вернул ошибку: {exc}\n"
                    f"Код ошибки: {job.last_error_code or '—'}\n"
                    "HTTP статус: —\n"
                    f"Номер: {job.call_phone or '—'}\n"
                    f"Что проверить: {hint}"
                ),
            )
            if can_retry and int(job.attempt_count or 0) < max_attempts:
                await self._schedule_retry(session=session, job=job, bot=bot, reason="provider_temporary_error")
            return job

        call_sid = payload.get("callSid")
        if not call_sid:
            job.status = "call_create_failed"
            job.call_status = "call_create_failed"
            job.last_error_message = "Provider returned success without CallSid"
            job.last_error_hint = "Провайдер не вернул CallSid, звонок не считается созданным."
            await session.commit()
            await add_provider_error(
                session,
                job_id=job.id,
                provider="twilio",
                stage="create_call",
                http_status=200,
                provider_error_code=None,
                provider_error_message=job.last_error_message,
                provider_more_info_url=None,
                from_phone=job.from_phone_e164,
                to_phone=job.call_phone,
                human_readable_hint=job.last_error_hint,
                raw_payload_json=sanitize_payload(payload if isinstance(payload, dict) else {"payload": payload}),
            )
            await self._status(
                session,
                job,
                bot,
                "Не удалось создать звонок.\nПровайдер не вернул CallSid, звонок не создан.",
            )
            return job

        job.elevenlabs_conversation_id = payload.get("conversation_id")
        job.elevenlabs_call_sid = call_sid
        job.provider_call_sid = call_sid
        job.call_status = "call_created"
        job.status = "call_created"
        job.last_progress_at = datetime.now(timezone.utc)
        await session.commit()

        logger.info(
            "call started",
            extra={
                "job_id": job.id,
                "conversation_id": job.elevenlabs_conversation_id,
                "call_sid": job.provider_call_sid,
                "call_status": job.call_status,
                "call_language": job.call_language,
                "status": f"attempt {job.attempt_count}/{job.max_attempts}",
            },
        )
        await self._status(
            session,
            job,
            bot,
            f"Звонок создан. CallSid: {job.provider_call_sid}\nОжидаю статусы от провайдера",
        )

        if effective_test_mode and job.attempt_count == 1:
            await self._status(
                session,
                job,
                bot,
                (
                    f"Тестовый режим: звонок выполнен на {job.call_phone}, "
                    f"номер из объявления: {job.listing_phone_raw or job.phone_from_listing or job.extracted_phone}"
                ),
            )
        return job

    async def _schedule_retry(self, *, session: AsyncSession, job: Job, bot: Bot, reason: str) -> None:
        max_attempts = max(1, int(job.max_attempts or self.settings.call_attempt_max))
        if int(job.attempt_count or 0) >= max_attempts:
            return
        next_attempt = int(job.attempt_count or 0) + 1
        delay_min = self._retry_delay_minutes(next_attempt)
        job.status = "retry_scheduled"
        job.next_attempt_at = datetime.now(timezone.utc) + timedelta(minutes=delay_min)
        job.queued_reason = reason
        await session.commit()
        await self._status(
            session,
            job,
            bot,
            f"Запланирована повторная попытка {next_attempt}/{max_attempts} через {delay_min} минут",
        )

    @staticmethod
    def _retry_delay_minutes(next_attempt: int) -> int:
        if next_attempt <= 2:
            return 10
        return 30

    async def _handle_no_answer(self, *, session: AsyncSession, job: Job, bot: Bot, reason: str) -> None:
        if (job.source or "").lower() == "request_call":
            from app.services.request_call import RequestCallService

            service = RequestCallService(
                settings=self.settings,
                openai_service=self.openai_service,
                elevenlabs_service=self.elevenlabs_service,
            )
            await service.finalize_job_status(
                session=session,
                job=job,
                bot=bot,
                call_status="no_answer" if reason != "busy" else "busy",
                summary="Нет ответа, трубку не взяли." if reason != "busy" else "Линия занята.",
            )
            return

        max_attempts = max(1, int(job.max_attempts or self.settings.call_attempt_max))
        job.call_status = "no_answer"
        job.status = "no_answer"
        job.first_answered_at = None

        if int(job.attempt_count or 0) >= max_attempts:
            await session.commit()
            await self._finalize_no_answer(session=session, job=job, bot=bot)
            return

        delay_min = self._retry_delay_minutes(int(job.attempt_count or 0) + 1)
        next_retry = datetime.now(timezone.utc) + timedelta(minutes=delay_min)
        job.status = "retry_scheduled"
        job.next_attempt_at = next_retry
        job.queued_reason = "no_answer_retry"
        await session.commit()
        await add_call_event(
            session,
            job_id=job.id,
            provider="twilio",
            provider_call_sid=job.provider_call_sid,
            event_type="no_answer",
            raw_call_status=reason,
            normalized_status="no_answer",
            from_phone=job.from_phone_e164,
            to_phone=job.call_phone,
            duration_seconds=None,
            error_code=None,
            error_message=None,
            raw_payload_json={"reason": reason},
        )
        await self._record_error(
            session,
            job,
            code="call_retry_scheduled",
            message=f"No answer, retry scheduled. reason={reason}",
        )
        await self._status(
            session,
            job,
            bot,
            (
                f"Нет ответа, запланирована повторная попытка {int(job.attempt_count or 0)+1}/{max_attempts} "
                f"через {delay_min} минут"
            ),
        )

    async def _handle_provider_timeout(self, *, session: AsyncSession, job: Job, bot: Bot, reason: str) -> None:
        summary = (
            "Провайдер не начал реальный дозвон: звонок оставался в статусе "
            f"{reason}. Вызов не дошёл до ringing/answered."
        )
        if (job.source or "").lower() == "request_call":
            from app.services.request_call import RequestCallService

            service = RequestCallService(
                settings=self.settings,
                openai_service=self.openai_service,
                elevenlabs_service=self.elevenlabs_service,
            )
            await service.finalize_job_status(
                session=session,
                job=job,
                bot=bot,
                call_status="provider_timeout",
                summary=summary,
            )
            return

        job.status = "provider_timeout"
        job.call_status = "provider_timeout"
        job.completed_at = datetime.now(timezone.utc)
        job.last_error_message = summary
        job.last_error_hint = "Проверьте Twilio status, Geo Permissions, маршрут направления и callback-и провайдера."
        await session.commit()
        await add_call_event(
            session,
            job_id=job.id,
            provider="twilio",
            provider_call_sid=job.provider_call_sid,
            event_type="provider_timeout",
            raw_call_status=reason,
            normalized_status="provider_timeout",
            from_phone=job.from_phone_e164,
            to_phone=job.call_phone,
            duration_seconds=None,
            error_code=None,
            error_message=summary,
            raw_payload_json={"reason": reason},
        )
        await self._status(session, job, bot, summary)

    async def _finalize_no_answer(self, *, session: AsyncSession, job: Job, bot: Bot) -> None:
        job.status = "finished"
        job.final_outcome = "no_answer_3_attempts"
        job.call_status = "no_answer"
        job.call_summary = "Три попытки дозвона без ответа."
        job.completed_at = datetime.now(timezone.utc)
        await session.commit()
        await self._status(session, job, bot, "звонок завершён")
        await self._send_final_notifications(session=session, job=job, bot=bot, audio=None)

    def _build_schedule(self, job: Job) -> dict[str, Any]:
        office_tz = job.office_tz or self.settings.office_timezone
        raw_hours = job.dealer_business_hours
        raw_closed = job.dealer_closed_days
        if isinstance(job.office_hours_json, dict):
            raw_hours = raw_hours or job.office_hours_json.get("raw_hours")
            raw_closed = raw_closed or job.office_hours_json.get("raw_closed_days")
        schedule = build_office_schedule(
            raw_hours=raw_hours,
            raw_closed_days=raw_closed,
            office_timezone=office_tz,
            fallback_hours=self.settings.office_hours_fallback,
        )
        logger.info(
            "office schedule prepared",
            extra={
                "job_id": job.id,
                "source": job.source,
                "office_tz": office_tz,
                "schedule_source": schedule.get("source"),
                "status": f"hours={schedule.get('raw_hours') or self.settings.office_hours_fallback}",
            },
        )
        return dict(schedule)

    async def _ensure_spoken_ready(
        self,
        *,
        session: AsyncSession,
        job: Job,
        extracted: ExtractionResult,
        bot: Bot,
        call_language: str,
    ) -> bool:
        if (job.car_spoken_ru or "").strip() and (job.price_used_spoken_ru or "").strip():
            return True
        try:
            spoken = await self.openai_service.normalize_spoken(extracted, call_language=call_language)
        except Exception as exc:
            code = "normalization_failed" if "normalization_failed" in str(exc) else "openai_failed"
            err_text = str(exc)
            detail_rule = err_text.split("normalization_failed:", 1)[-1].strip() if "normalization_failed:" in err_text else err_text
            await self._record_error(
                session,
                job,
                code=code,
                message=f"Spoken normalization failed: {exc}",
                details={
                    "stage": "spoken_normalization",
                    "call_language": call_language,
                    "field": "car_spoken_ru/price_used_spoken_ru/year_spoken_ru",
                    "rule": detail_rule,
                    "value_preview": {
                        "car_short": (extracted.car_short or "")[:120],
                        "car_full": (extracted.car_full or "")[:220],
                        "year": extracted.year,
                        "price_used_jpy": extracted.price_used_jpy,
                    },
                },
            )
            job.status = code
            await session.commit()
            await self._status(session, job, bot, f"Ошибка: {code} ({exc})")
            return False

        job.car_spoken_ru = spoken.car_spoken_ru
        job.price_used_spoken_ru = spoken.price_used_spoken_ru
        job.price_total_spoken_ru = spoken.price_total_spoken_ru
        job.vehicle_price_spoken_ru = spoken.vehicle_price_spoken_ru
        job.year_spoken_ru = spoken.year_spoken_ru
        job.mileage_spoken_ru = spoken.mileage_spoken_ru
        job.inspection_spoken_ru = spoken.inspection_spoken_ru
        await session.commit()
        return True

    def _compute_call_phone(self, *, job: Job, effective_test_mode: bool, allow_test_fallback: bool) -> str | None:
        primary_phone = job.resolved_phone_e164 or job.extracted_phone
        if (job.source or "").lower() == "cars.com":
            return primary_phone
        if (job.call_language or "ru") == "ja":
            return primary_phone
        if effective_test_mode:
            return self.settings.test_call_phone
        if allow_test_fallback:
            return self.settings.test_call_phone
        return primary_phone

    def _build_dynamic_variables(self, job: Job, *, effective_test_mode: bool) -> dict[str, Any]:
        if (job.source or "").lower() == "request_call":
            return {"goal_ru": job.request_goal_ru or ""}
        return {
            "job_id": str(job.id),
            "source": job.source,
            "listing_url": job.listing_url,
            "car_full": job.car_full,
            "car_short": job.car_short,
            "car_spoken_ru": job.car_spoken_ru,
            "price_used_jpy": job.price_used_jpy,
            "price_used_type": job.price_used_type,
            "price_used_spoken_ru": job.price_used_spoken_ru,
            "vehicle_price_spoken_ru": job.vehicle_price_spoken_ru,
            "year_spoken_ru": job.year_spoken_ru,
            "mileage_spoken_ru": job.mileage_spoken_ru,
            "dealer": job.dealer,
            "extracted_phone": job.extracted_phone,
            "listing_phone_raw": job.listing_phone_raw,
            "listing_phone_type": job.listing_phone_type,
            "resolved_phone_e164": job.resolved_phone_e164,
            "resolver_status": job.resolver_status,
            "vin": job.vin or "-",
            "stock_number": job.stock_number or "-",
            "call_phone": job.call_phone,
            "call_language": job.call_language or "ru",
            "test_mode": effective_test_mode,
        }

    @staticmethod
    def _validate_dynamic_variables(dynamic_variables: dict[str, Any]) -> tuple[bool, str]:
        if set(dynamic_variables.keys()) == {"goal_ru"}:
            value = str(dynamic_variables.get("goal_ru") or "").strip()
            if not value or value.lower() in {"none", "null", "undefined"}:
                return False, "goal_ru is empty"
            return True, ""
        required = (
            "job_id",
            "listing_url",
            "car_spoken_ru",
            "price_used_spoken_ru",
            "call_phone",
            "call_language",
        )
        for key in required:
            value = dynamic_variables.get(key)
            if value is None:
                return False, f"{key} is missing"
            if isinstance(value, str):
                normalized = value.strip()
                if not normalized or normalized.lower() in {"none", "null", "undefined"}:
                    return False, f"{key} is empty"
        return True, ""

    def _persist_resolver_result(self, job: Job, resolver: DealerPhoneResolutionResult) -> None:
        job.listing_phone_raw = resolver.listing_phone_raw
        job.listing_phone_type = resolver.listing_phone_type
        job.resolved_phone_raw = resolver.resolved_phone_raw
        job.resolved_phone_e164 = resolver.resolved_phone_e164
        job.resolved_phone_source_url = resolver.resolved_phone_source_url
        job.resolved_phone_source_type = resolver.source_type
        job.resolver_confidence_score = resolver.confidence_score
        job.resolver_status = resolver.resolution_status
        job.resolver_error_reason = resolver.error_reason
        if not job.dealer_business_hours and resolver.dealer_business_hours:
            job.dealer_business_hours = resolver.dealer_business_hours
        job.resolver_result_json = resolver.model_dump()

    def _build_phone_review_text(self, job: Job, resolver: DealerPhoneResolutionResult) -> str:
        lines = [
            "Требуется подтверждение номера дилера.",
            f"Job #{job.id}",
            f"Номер из объявления: {resolver.listing_phone_raw or '—'} ({resolver.listing_phone_type})",
            f"confidence: {resolver.confidence_score}",
            f"status: {resolver.resolution_status}",
        ]
        for idx, candidate in enumerate((resolver.candidates or [])[:5], start=1):
            lines.append(
                f"{idx}. {candidate.get('phone') or candidate.get('phone_found') or '—'} | "
                f"score={candidate.get('score', '—')} | source={candidate.get('source_type', '—')}"
            )
        return "\n".join(lines)

    async def approve_phone_review(self, *, session: AsyncSession, job: Job, bot: Bot, candidate_idx: int) -> Job:
        payload = job.resolver_result_json or {}
        candidates = payload.get("candidates") or []
        if candidate_idx < 0 or candidate_idx >= len(candidates):
            raise RuntimeError("invalid candidate index")
        candidate = candidates[candidate_idx] or {}
        selected_raw = candidate.get("phone") or candidate.get("phone_found")
        if (job.source or "").lower() == "cars.com":
            selected_e164 = normalize_us_phone_to_e164(selected_raw)
        else:
            selected_e164 = normalize_jp_phone_to_e164(selected_raw)
        if not selected_e164:
            raise RuntimeError("selected candidate is not callable")

        job.resolved_phone_raw = selected_raw
        job.resolved_phone_e164 = selected_e164
        job.resolver_status = "resolved"
        job.resolver_confidence_score = max(80, int(candidate.get("score") or job.resolver_confidence_score or 0))
        job.extracted_phone = selected_e164
        await session.commit()

        call_language = job.call_language or "ru"
        effective_test_mode = self.settings.test_mode and call_language == "ru"
        await self._set_job_status(session=session, job=job, bot=bot, status="preparing_agent", text="Готовлю агента")
        spoken_ok = await self._ensure_spoken_ready(
            session=session,
            job=job,
            extracted=self._extraction_from_job(job),
            bot=bot,
            call_language=call_language,
        )
        if not spoken_ok:
            return job
        job.call_phone = self._compute_call_phone(
            job=job,
            effective_test_mode=effective_test_mode,
            allow_test_fallback=False,
        )
        schedule = self._build_schedule(job)
        job.office_hours_json = dict(schedule)
        await session.commit()

        return await self._schedule_or_start_attempt(
            session=session,
            job=job,
            bot=bot,
            effective_test_mode=effective_test_mode,
        )

    @staticmethod
    def _extraction_from_job(job: Job) -> ExtractionResult:
        return ExtractionResult(
            source=job.source or "persisted",
            listing_url=job.listing_url,
            car=job.car,
            car_full=job.car_full,
            car_short=job.car_short,
            vehicle_title=job.car_full,
            vin=job.vin,
            stock_number=job.stock_number,
            price_total_jpy=job.price_total_jpy,
            vehicle_price_jpy=job.vehicle_price_jpy,
            price_total_source_text=job.price_total_source_text,
            vehicle_price_source_text=job.vehicle_price_source_text,
            price_confidence=job.price_confidence or 0,
            price_used_jpy=job.price_used_jpy,
            price_used_type=job.price_used_type,
            year=job.year,
            mileage=job.mileage,
            repair_history=job.repair_history,
            inspection=job.inspection,
            dealer=job.dealer,
            dealer_address=job.dealer_address,
            dealer_business_hours=job.dealer_business_hours,
            dealer_closed_days=job.dealer_closed_days,
            phone_from_listing=job.phone_from_listing,
            carsensor_free_phone=job.carsensor_free_phone,
            dealer_direct_phone=job.dealer_direct_phone,
            extraction_confidence=job.extraction_confidence or 1.0,
            missing_fields=job.missing_fields or [],
        )

    @staticmethod
    def _detect_source(url: str, *, current: str | None = None) -> str:
        normalized_current = (current or "").lower()
        if normalized_current in {"carsensor", "cars.com"}:
            return normalized_current
        normalized = (url or "").lower()
        if "cars.com/vehicledetail/" in normalized:
            return "cars.com"
        return "carsensor"

    def _persist_extraction(self, job: Job, extracted: ExtractionResult) -> None:
        compact_car = compact_car_name_for_call(extracted.car_short or extracted.car or extracted.car_full)
        compact_car = ensure_brand_in_car_name(compact_car, extracted.car_full)
        source = self._detect_source(job.listing_url, current=job.source)
        job.source = source
        job.car_full = extracted.car_full or extracted.vehicle_title or extracted.car
        job.car_short = compact_car or extracted.car_short or extracted.car
        job.car = job.car_short or extracted.car
        job.vin = extracted.vin
        job.stock_number = extracted.stock_number
        job.price_total_jpy = extracted.price_total_jpy
        job.vehicle_price_jpy = extracted.vehicle_price_jpy
        job.price_total_source_text = extracted.price_total_source_text
        job.vehicle_price_source_text = extracted.vehicle_price_source_text
        job.price_confidence = extracted.price_confidence
        job.price_used_jpy = extracted.price_used_jpy
        job.price_used_type = extracted.price_used_type
        job.year = extracted.year
        job.mileage = extracted.mileage
        job.repair_history = extracted.repair_history
        job.inspection = extracted.inspection
        job.dealer = extracted.dealer
        job.dealer_address = extracted.dealer_address
        job.dealer_business_hours = extracted.dealer_business_hours
        job.dealer_closed_days = extracted.dealer_closed_days
        job.phone_from_listing = extracted.phone_from_listing
        if source == "cars.com":
            job.carsensor_free_phone = None
            job.dealer_direct_phone = normalize_us_phone_to_e164(extracted.dealer_direct_phone or extracted.phone_from_listing)
        else:
            job.carsensor_free_phone = normalize_jp_phone_to_e164(extracted.carsensor_free_phone)
            job.dealer_direct_phone = normalize_jp_phone_to_e164(extracted.dealer_direct_phone)
        job.extraction_confidence = extracted.extraction_confidence
        job.missing_fields = extracted.missing_fields

    @staticmethod
    def _has_required_fields(extracted: ExtractionResult, *, source: str) -> bool:
        has_car = bool(extracted.car_short or extracted.car or extracted.car_full)
        has_price = extracted.price_total_jpy is not None or extracted.vehicle_price_jpy is not None
        if source == "cars.com":
            return bool(has_car and has_price and extracted.dealer)
        has_phone = bool(extracted.dealer_direct_phone or extracted.carsensor_free_phone)
        return bool(has_car and has_price and extracted.dealer and has_phone)

    async def _record_error(
        self,
        session: AsyncSession,
        job: Job,
        *,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        logger.error(message, extra={"job_id": job.id, "error_code": code})
        await add_job_error(session, code=code, message=message, job_id=job.id, details=details)

    async def _post_call_fallback(self, job_id: int) -> None:
        await asyncio.sleep(self.settings.post_call_fallback_sec)
        attempts = max(20, int(self.settings.post_call_fallback_attempts))
        interval = max(5, int(self.settings.post_call_fallback_interval_sec))
        max_wait_sec = max(
            int(self.settings.max_call_duration_seconds) + attempts * interval,
            int(self.settings.post_call_fallback_sec) + attempts * interval,
        )
        started_at = datetime.now(timezone.utc)
        last_error: str | None = None
        last_call_status: str | None = None
        processing_attempts = 0

        while True:
            async with SessionLocal() as session:
                job = await get_job(session, job_id)
                if not job:
                    return
                if job.status in {"openai_failed", "webhook_failed"}:
                    return
                if job.call_transcript:
                    return
                if not job.elevenlabs_conversation_id:
                    return

                try:
                    details = await self.elevenlabs_service.fetch_conversation_details(job.elevenlabs_conversation_id)
                    transcript = flatten_transcript(details.get("transcript"))
                    analysis = details.get("analysis") or {}
                    summary = analysis.get("transcript_summary") or analysis.get("summary") or ""
                    call_sid = details.get("callSid") or (details.get("metadata") or {}).get("callSid")
                    if call_sid and not job.elevenlabs_call_sid:
                        job.elevenlabs_call_sid = call_sid

                    job.call_transcript = transcript or job.call_transcript
                    job.call_summary = summary or job.call_summary
                    job.call_status = details.get("status") or job.call_status or "done"
                    last_call_status = job.call_status
                    observed_status = str(job.call_status or "").lower().replace("_", "-")
                    if observed_status in {"processing", "done", "failed"}:
                        processing_attempts += 1
                    else:
                        processing_attempts = 0

                    if transcript:
                        logger.info(
                            "call finished (fallback)",
                            extra={
                                "job_id": job.id,
                                "conversation_id": job.elevenlabs_conversation_id,
                                "call_sid": job.elevenlabs_call_sid,
                                "call_status": job.call_status,
                                "call_language": job.call_language,
                            },
                        )
                        if (job.source or "").lower() == "request_call":
                            from app.services.request_call import RequestCallService

                            service = RequestCallService(
                                settings=self.settings,
                                openai_service=self.openai_service,
                                elevenlabs_service=self.elevenlabs_service,
                            )
                            await service.finalize_job_from_transcript(
                                session=session,
                                job=job,
                                bot=None,
                                transcript=transcript,
                                summary=summary,
                            )
                            await session.commit()
                            try:
                                bot = Bot(self.settings.telegram_bot_token)
                            except Exception:
                                return
                            try:
                                from app.models import DealerCallTarget, RequestCallCampaign

                                campaign = await session.get(RequestCallCampaign, job.request_campaign_id)
                                target = await session.get(DealerCallTarget, job.request_target_id)
                                if not campaign or not target:
                                    return
                                await service.send_target_report(
                                    session=session,
                                    campaign=campaign,
                                    target=target,
                                    bot=bot,
                                    job=job,
                                )
                            finally:
                                await bot.session.close()
                            return
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
                        audio = await self.elevenlabs_service.fetch_conversation_audio(job.elevenlabs_conversation_id)
                        await session.commit()
                        await self._notify_admin_from_fallback(session=session, job=job, audio=audio)
                        return

                    await session.commit()
                    logger.info(
                        "post-call fallback: transcript not ready",
                        extra={
                            "job_id": job.id,
                            "conversation_id": job.elevenlabs_conversation_id,
                            "call_sid": job.elevenlabs_call_sid,
                            "call_status": job.call_status,
                            "status": (
                                f"processing_attempt {processing_attempts}/{attempts}"
                                if processing_attempts
                                else "call_not_finished_yet"
                            ),
                            "call_language": job.call_language,
                        },
                    )
                except Exception as exc:
                    last_error = str(exc)
                    processing_attempts += 1
                    logger.warning(
                        "post-call fallback attempt failed",
                        extra={"job_id": job.id, "status": f"attempt {processing_attempts}/{attempts}: {exc}"},
                    )

            elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
            if processing_attempts >= attempts or elapsed >= max_wait_sec:
                break
            await asyncio.sleep(interval)

        async with SessionLocal() as session:
            job = await get_job(session, job_id)
            if not job:
                return
            if job.call_transcript or job.status in {"openai_failed", "webhook_failed"}:
                return
            job.status = "post_call_fetch_failed"
            job.call_status = job.call_status or "post_call_fetch_failed"
            await session.commit()
            await add_job_error(
                session,
                code="webhook_failed",
                message=(
                    "No transcript in webhook and fallback conversation fetch "
                    f"after {attempts} attempts; last_call_status={last_call_status}; last_error={last_error}"
                ),
                job_id=job.id,
            )
            try:
                bot = Bot(self.settings.telegram_bot_token)
            except Exception:
                return
            try:
                await self._status(
                    session,
                    job,
                    bot,
                    "Не удалось получить транскрипт/запись после завершения звонка. "
                    "Проверьте post-call webhook в ElevenLabs.",
                )
            finally:
                await bot.session.close()

    async def _notify_admin_from_fallback(self, *, session: AsyncSession, job: Job, audio: bytes | None) -> None:
        try:
            bot = Bot(self.settings.telegram_bot_token)
        except Exception:
            return
        try:
            await self._status(session, job, bot, "звонок завершён")
            await self._send_final_notifications(session=session, job=job, bot=bot, audio=audio)
        finally:
            await bot.session.close()

    async def _send_final_notifications(
        self,
        *,
        session: AsyncSession,
        job: Job,
        bot: Bot,
        audio: bytes | None,
    ) -> None:
        if job.final_report_sent_at:
            return

        success = True
        await self._cleanup_service_messages(session, job, bot)
        reply_to_message_id = job.telegram_source_message_id

        if audio:
            file = BufferedInputFile(audio, filename="call_recording.mp3")
            sent = await safe_send_document(bot, job.telegram_chat_id, file, reply_to_message_id=reply_to_message_id)
            success = success and bool(sent)

        transcript_html, full_transcript = build_transcript_expandable_html(job.call_transcript or "")
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

    async def _cleanup_service_messages(self, session: AsyncSession, job: Job, bot: Bot) -> None:
        for message_id in list(job.telegram_service_message_ids or []):
            ok = await safe_delete_message(bot, job.telegram_chat_id, message_id)
            if not ok:
                logger.debug(
                    "service message cleanup skipped",
                    extra={"job_id": job.id, "status": f"message_id={message_id}"},
                )
        await clear_service_message_ids(session, job=job)

    async def _status(self, session: AsyncSession, job: Job, bot: Bot, text: str) -> None:
        message = await safe_send_message(bot, job.telegram_chat_id, text)
        if message is not None:
            await append_service_message_id(session, job=job, message_id=message.message_id)

    async def _set_job_status(
        self,
        *,
        session: AsyncSession,
        job: Job,
        bot: Bot,
        status: str,
        text: str,
        commit: bool = True,
    ) -> None:
        job.status = status
        if commit:
            await session.commit()
        await self._status(session, job, bot, text)

    def _preflight_twilio_plus_one(self, call_phone: str | None) -> tuple[bool, str]:
        if not call_phone or not call_phone.startswith("+1"):
            return True, ""
        checks = [
            self.settings.twilio_plus_one_allowed or self.settings.twilio_marked_as_allowed_for_plus_one,
            self.settings.twilio_billing_active,
            self.settings.twilio_geo_us_ca_enabled,
            self.settings.twilio_from_number_verified,
        ]
        if all(checks):
            return True, ""
        return (
            False,
            "Twilio для +1 направлений не готов. Проверьте Business Primary Customer Profile, "
            "Billing, корректный From number и Geo Permissions US/CA.",
        )

    def _job_tz(self, job: Job):
        try:
            return ZoneInfo(job.office_tz or self.settings.office_timezone)
        except Exception:
            if (job.source or "").lower() == "cars.com":
                try:
                    return ZoneInfo(self.settings.us_timezone_fallback)
                except Exception:
                    pass
            return timezone(timedelta(hours=9))

    def _job_now(self, job: Job) -> datetime:
        return datetime.now(timezone.utc).astimezone(self._job_tz(job))

    def _resolve_job_office_timezone(self, *, job: Job, source: str) -> tuple[str, str]:
        return resolve_office_timezone(
            source=source,
            dealer_address=job.dealer_address,
            listing_url=job.listing_url,
            jp_default=self.settings.office_timezone,
            us_fallback=self.settings.us_timezone_fallback,
        )

    @staticmethod
    def _as_utc(value: datetime | None) -> datetime | None:
        if not value:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
