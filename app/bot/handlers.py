from __future__ import annotations

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import (
    call_language_keyboard,
    request_call_cancel_keyboard,
    request_call_confirm_keyboard,
    request_call_country_keyboard,
    request_call_language_keyboard,
    start_mode_keyboard,
)
from app.config import Settings
from app.db import SessionLocal
from app.repositories import (
    add_job_error,
    get_job,
    get_latest_input_request_campaign,
    get_latest_open_request_campaign,
    get_request_campaign,
)
from app.services.elevenlabs_client import ElevenLabsService
from app.services.openai_client import OpenAIService
from app.services.request_call import (
    REQUEST_CALL_PROCESSING_MESSAGE,
    RequestCallService,
    build_request_confirmation_text,
)
from app.services.telegram_delivery import safe_delete_message, safe_send_message
from app.services.workflow import CallWorkflow

logger = logging.getLogger(__name__)
REQUEST_START_STATUSES = {"ready_to_confirm", "ready_to_call"}
COUNTRY_LANGUAGE_BY_REGION = {"US": "en", "JP": "ja"}
REQUEST_CALL_INSTRUCTIONS = (
    "Пришлите список дилеров с телефонами, задачу прозвона и при необходимости ссылки на авто "
    "одним сообщением.\n\n"
    "Пример:\n"
    "Duval Ford Jacksonville +1 (904) 387-6541\n"
    "AutoNation Ford Jacksonville +1 (904) 513-3392\n\n"
    "Ссылка: https://example.com/vehicle\n\n"
    "Задача: узнать наличие Ford Raptor R из наличия или ближайшей поставки, "
    "покупка без кредита и лизинга, оплата переводом."
)


def detect_source(url: str) -> str:
    if "cars.com/vehicledetail/" in url:
        return "cars.com"
    return "carsensor"


def create_router(settings: Settings, workflow: CallWorkflow | None = None) -> Router:
    router = Router()
    workflow = workflow or CallWorkflow(
        settings=settings,
        openai_service=OpenAIService(settings),
        elevenlabs_service=ElevenLabsService(settings),
    )
    request_service = RequestCallService(
        settings=settings,
        openai_service=OpenAIService(settings),
        elevenlabs_service=ElevenLabsService(settings),
    )

    def is_admin(user_id: int | None) -> bool:
        return bool(user_id and user_id in settings.admin_ids)

    def can_access(user_id: int | None, chat_id: int | None, chat_type: str | None = None) -> bool:
        if not is_admin(user_id):
            logger.info(
                "telegram access denied: non-admin user",
                extra={"user_id": user_id, "chat_id": chat_id, "chat_type": chat_type},
            )
            return False
        if not settings.is_allowed_telegram_chat(chat_id, chat_type):
            logger.info(
                "telegram access denied: chat is not allowlisted",
                extra={"user_id": user_id, "chat_id": chat_id, "chat_type": chat_type},
            )
            return False
        return True

    def can_access_message(message: Message) -> bool:
        return can_access(
            message.from_user.id if message.from_user else None,
            message.chat.id,
            message.chat.type,
        )

    def can_access_callback(callback: CallbackQuery) -> bool:
        if callback.message:
            return can_access(
                callback.from_user.id if callback.from_user else None,
                callback.message.chat.id,
                callback.message.chat.type,
            )
        return is_admin(callback.from_user.id if callback.from_user else None)

    async def send_start_menu(message: Message) -> None:
        await message.answer("Выберите режим прозвона:", reply_markup=start_mode_keyboard())

    async def delete_callback_message(callback: CallbackQuery) -> None:
        if callback.message:
            await safe_delete_message(
                callback.bot,
                callback.message.chat.id,
                callback.message.message_id,
            )

    async def ensure_request_campaign_owner(callback: CallbackQuery, campaign) -> bool:
        if campaign.telegram_user_id == callback.from_user.id:
            return True
        await callback.answer("Эту кампанию начал другой админ", show_alert=True)
        return False

    def telegram_display_name(user) -> str | None:
        if user is None:
            return None
        parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
        display = " ".join(part for part in parts if part).strip()
        return display or getattr(user, "username", None)

    def telegram_username(user) -> str | None:
        return getattr(user, "username", None) if user is not None else None

    async def delete_later(bot, chat_id: int, message_id: int, delay_seconds: float = 5.0) -> None:
        await asyncio.sleep(delay_seconds)
        await safe_delete_message(bot, chat_id, message_id)

    async def create_request_session(
        *,
        bot,
        chat_id: int,
        user,
        source_message_id: int | None = None,
    ):
        async with SessionLocal() as session:
            running_campaign = await request_service.get_running_campaign_for_owner(
                session=session,
                chat_id=chat_id,
                user_id=user.id,
            )
            if running_campaign:
                return None
            await request_service.cancel_input_campaigns_for_owner(
                session=session,
                chat_id=chat_id,
                user_id=user.id,
                bot=bot,
            )
            return await request_service.create_draft(
                session=session,
                chat_id=chat_id,
                user_id=user.id,
                username=telegram_username(user),
                display_name=telegram_display_name(user),
                source_message_id=source_message_id,
                status="needs_country",
            )

    async def send_request_country_prompt(*, bot, campaign_id: int) -> None:
        async with SessionLocal() as session:
            campaign = await get_request_campaign(session, campaign_id)
            if campaign:
                await request_service.send_service_message(
                    session=session,
                    campaign=campaign,
                    bot=bot,
                    text="Выберите страну прозвона. Номера без кода страны буду интерпретировать по выбранной стране.",
                    reply_markup=request_call_country_keyboard(campaign.id),
                )

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        if not can_access_message(message):
            return
        await send_start_menu(message)

    @router.message(Command("request"))
    async def request_command(message: Message) -> None:
        if not can_access_message(message):
            return
        campaign = await create_request_session(
            bot=message.bot,
            chat_id=message.chat.id,
            user=message.from_user,
            source_message_id=message.message_id,
        )
        if campaign is None:
            await safe_send_message(
                message.bot,
                message.chat.id,
                "У вас уже есть активный прозвон. Завершите или отмените его через /cancel.",
            )
            return
        await send_request_country_prompt(bot=message.bot, campaign_id=campaign.id)

    @router.message(Command("cancel"))
    async def cancel_request_campaign_command(message: Message) -> None:
        if not can_access_message(message):
            return
        async with SessionLocal() as session:
            campaign = await get_latest_open_request_campaign(
                session,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
            )
            if not campaign:
                sent = await safe_send_message(message.bot, message.chat.id, "Нет активной задачи для отмены.")
                if sent:
                    asyncio.create_task(delete_later(message.bot, message.chat.id, sent.message_id))
                return
            await request_service.cancel_campaign(session=session, campaign=campaign, bot=message.bot)
        sent = await safe_send_message(message.bot, message.chat.id, "Задача отменена.")
        if sent:
            asyncio.create_task(delete_later(message.bot, message.chat.id, sent.message_id))

    @router.callback_query(F.data == "mode:link")
    async def select_link_mode(callback: CallbackQuery) -> None:
        if not can_access_callback(callback):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        await callback.answer("Устаревший режим. Пользуйтесь прозвоном по запросу.", show_alert=True)
        if callback.message:
            await callback.message.answer("Устаревший режим. Пользуйтесь прозвоном по запросу.")

    @router.callback_query(F.data == "mode:request")
    async def select_request_mode(callback: CallbackQuery) -> None:
        if not can_access_callback(callback):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        chat_id = callback.message.chat.id if callback.message else callback.from_user.id
        campaign = await create_request_session(
            bot=callback.bot,
            chat_id=chat_id,
            user=callback.from_user,
        )
        if campaign is None:
            await callback.answer(
                "У вас уже есть активный прозвон. Завершите или отмените его через /cancel.",
                show_alert=True,
            )
            return
        await callback.answer()
        await delete_callback_message(callback)
        await send_request_country_prompt(bot=callback.bot, campaign_id=campaign.id)

    @router.callback_query(F.data.startswith("request:country_soon:"))
    async def request_country_soon(callback: CallbackQuery) -> None:
        if not can_access_callback(callback):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        await callback.answer("Пока работает только США и Япония.", show_alert=True)

    @router.callback_query(F.data.startswith("request:country:"))
    async def request_select_country(callback: CallbackQuery) -> None:
        if not can_access_callback(callback):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        try:
            _prefix, _country_key, country, campaign_raw = callback.data.split(":", 3)
            if country not in COUNTRY_LANGUAGE_BY_REGION:
                raise ValueError("unsupported country")
            campaign_id = int(campaign_raw)
        except Exception:
            await callback.answer("Некорректные данные", show_alert=True)
            return
        async with SessionLocal() as session:
            campaign = await get_request_campaign(session, campaign_id)
            if not campaign:
                await callback.answer("Кампания не найдена", show_alert=True)
                return
            if not await ensure_request_campaign_owner(callback, campaign):
                return
            if campaign.status not in {"needs_country", "draft", "needs_phones", "needs_goal", "needs_phones_and_goal"}:
                await callback.answer(f"Кампания в статусе {campaign.status}")
                return
            campaign.phone_region = country
            campaign.call_language = COUNTRY_LANGUAGE_BY_REGION[country]
            campaign.status = "draft"
            await session.commit()
            await callback.answer("Страна выбрана")
            await delete_callback_message(callback)
            label = "США" if country == "US" else "Япония"
            await request_service.send_service_message(
                session=session,
                campaign=campaign,
                bot=callback.bot,
                text=f"Страна прозвона: {label}.\n\n{REQUEST_CALL_INSTRUCTIONS}",
                reply_markup=request_call_cancel_keyboard(campaign.id),
            )

    @router.message(F.text)
    async def handle_text(message: Message) -> None:
        if not can_access_message(message):
            return

        text = message.text or ""
        async with SessionLocal() as session:
            campaign = await get_latest_input_request_campaign(
                session,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
            )
            if campaign:
                await request_service.update_campaign_owner(
                    session=session,
                    campaign=campaign,
                    user_id=message.from_user.id,
                    username=telegram_username(message.from_user),
                    display_name=telegram_display_name(message.from_user),
                )
                processing_message = await message.answer(REQUEST_CALL_PROCESSING_MESSAGE)
                try:
                    campaign = await request_service.update_campaign_from_text(
                        session=session,
                        campaign=campaign,
                        text=text,
                        source_message_id=message.message_id,
                    )
                    targets = await request_service.list_targets(session, campaign.id)
                    if campaign.status == "mixed_phone_regions":
                        region_label = (
                            "США"
                            if campaign.phone_region == "US"
                            else "Япония"
                            if campaign.phone_region == "JP"
                            else "выбранной стране"
                        )
                        await request_service.send_service_message(
                            session=session,
                            campaign=campaign,
                            bot=message.bot,
                            text=(
                                "Номера не соответствуют выбранной стране или смешаны в одном запросе. "
                                f"Сейчас выбрана страна: {region_label}. Пришлите номера только для этой страны "
                                "или начните отдельную кампанию."
                            ),
                            reply_markup=request_call_cancel_keyboard(campaign.id),
                        )
                    elif campaign.status == "needs_country":
                        await request_service.send_service_message(
                            session=session,
                            campaign=campaign,
                            bot=message.bot,
                            text=(
                                "Сначала выберите страну прозвона. Номера без кода страны буду интерпретировать "
                                "по выбранной стране."
                            ),
                            reply_markup=request_call_country_keyboard(campaign.id),
                        )
                    elif campaign.status == "needs_phones":
                        await request_service.send_service_message(
                            session=session,
                            campaign=campaign,
                            bot=message.bot,
                            text="Цель понял, но не нашёл номера телефонов. Пришлите список дилеров с телефонами.",
                            reply_markup=request_call_cancel_keyboard(campaign.id),
                        )
                    elif campaign.status == "needs_goal":
                        await request_service.send_service_message(
                            session=session,
                            campaign=campaign,
                            bot=message.bot,
                            text="Номера нашёл. Теперь пришлите задачу прозвона: что именно нужно выяснить у дилеров?",
                            reply_markup=request_call_cancel_keyboard(campaign.id),
                        )
                    elif campaign.status == "needs_phones_and_goal":
                        await request_service.send_service_message(
                            session=session,
                            campaign=campaign,
                            bot=message.bot,
                            text="Пришлите список дилеров с телефонами и задачу прозвона одним сообщением.",
                            reply_markup=request_call_cancel_keyboard(campaign.id),
                        )
                    elif campaign.status == "needs_goal_clarification":
                        await request_service.send_service_message(
                            session=session,
                            campaign=campaign,
                            bot=message.bot,
                            text="Уточните, пожалуйста, какую модель или задачу прозванивать и что обязательно выяснить?",
                            reply_markup=request_call_cancel_keyboard(campaign.id),
                        )
                    elif campaign.status == "needs_language":
                        recommended = "ja" if campaign.phone_region == "JP" else "en"
                        await request_service.send_service_message(
                            session=session,
                            campaign=campaign,
                            bot=message.bot,
                            text=(
                                "Выберите язык прозвона.\n"
                                f"Рекомендация по номеру: {'日本語' if recommended == 'ja' else 'English'}."
                            ),
                            reply_markup=request_call_language_keyboard(campaign.id, recommended=recommended),
                        )
                    elif campaign.status == "ready_to_confirm":
                        await request_service.send_service_message(
                            session=session,
                            campaign=campaign,
                            bot=message.bot,
                            text=build_request_confirmation_text(campaign, targets),
                            reply_markup=request_call_confirm_keyboard(campaign.id, campaign.valid_numbers),
                        )
                finally:
                    await safe_delete_message(message.bot, message.chat.id, processing_message.message_id)
                return
            running_campaign = await request_service.get_running_campaign_for_owner(
                session=session,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
            )
            if running_campaign:
                logger.info(
                    "request-call ignored text while campaign is not accepting free-form input",
                    extra={
                        "campaign_id": running_campaign.id,
                        "chat_id": message.chat.id,
                        "user_id": message.from_user.id,
                        "status": running_campaign.status,
                    },
                )
                return

        logger.info(
            "request-call ignored inactive text",
            extra={"chat_id": message.chat.id, "user_id": message.from_user.id, "chat_type": message.chat.type},
        )
        return

    @router.callback_query(F.data.startswith("cancel:"))
    async def cancel_call(callback: CallbackQuery) -> None:
        if not can_access_callback(callback):
            await callback.answer("Доступ запрещён", show_alert=True)
            return

        job_id = int(callback.data.split(":", 1)[1])
        async with SessionLocal() as session:
            job = await get_job(session, job_id)
            if not job:
                await callback.answer("Job не найден", show_alert=True)
                return
            if job.status == "canceled":
                await callback.answer("Уже отменено")
                return

            job.status = "canceled"
            await session.commit()

        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Отменено")

    @router.callback_query(F.data.startswith("call:"))
    async def start_call(callback: CallbackQuery) -> None:
        if not can_access_callback(callback):
            await callback.answer("Доступ запрещён", show_alert=True)
            return

        job_id = int(callback.data.split(":", 1)[1])
        async with SessionLocal() as session:
            job = await get_job(session, job_id)
            if not job:
                await callback.answer("Job не найден", show_alert=True)
                return

            if job.status in {"processing", "call_started", "completed", "canceled"}:
                await callback.answer(f"Job уже в статусе {job.status}")
                return

            if callback.message:
                source = job.source or detect_source(job.listing_url)
                await callback.message.edit_reply_markup(reply_markup=call_language_keyboard(job.id, source=source))
            await callback.answer("Выберите язык")

    @router.callback_query(F.data.startswith("lang:"))
    async def start_call_with_language(callback: CallbackQuery) -> None:
        if not can_access_callback(callback):
            await callback.answer("Доступ запрещён", show_alert=True)
            return

        try:
            _prefix, language, job_raw = callback.data.split(":", 2)
        except Exception:
            await callback.answer("Некорректные данные", show_alert=True)
            return
        if language not in {"ru", "ja", "en"}:
            await callback.answer("Неизвестный язык", show_alert=True)
            return
        job_id = int(job_raw)

        async with SessionLocal() as session:
            job = await get_job(session, job_id)
            if not job:
                await callback.answer("Job не найден", show_alert=True)
                return

            if job.status in {"processing", "call_started", "completed", "canceled"}:
                await callback.answer(f"Job уже в статусе {job.status}")
                return

            job.status = "processing"
            job.call_language = language
            await session.commit()

            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("Запускаю")

            try:
                await workflow.run(session=session, job=job, bot=callback.bot, call_language=language)
            except Exception as exc:
                logger.exception("workflow failed", extra={"job_id": job.id})
                job.status = "parsing_failed"
                await session.commit()
                await add_job_error(
                    session,
                    code="parsing_failed",
                    message=f"Workflow failed: {exc}",
                    job_id=job.id,
                )
                await callback.bot.send_message(job.telegram_chat_id, f"Ошибка: parsing_failed ({exc})")

    @router.callback_query(F.data.startswith("phone_review:approve:"))
    async def approve_phone_review(callback: CallbackQuery) -> None:
        if not can_access_callback(callback):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        try:
            _prefix, action, job_raw, idx_raw = callback.data.split(":", 3)
            if action != "approve":
                raise ValueError("invalid action")
            job_id = int(job_raw)
            candidate_idx = int(idx_raw)
        except Exception:
            await callback.answer("Некорректные данные", show_alert=True)
            return

        async with SessionLocal() as session:
            job = await get_job(session, job_id)
            if not job:
                await callback.answer("Job не найден", show_alert=True)
                return
            if (job.resolver_status or "") != "needs_review":
                await callback.answer("Ревью для job уже не требуется")
                return

            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("Подтверждаю номер и запускаю")
            try:
                await workflow.approve_phone_review(session=session, job=job, bot=callback.bot, candidate_idx=candidate_idx)
            except Exception as exc:
                await add_job_error(
                    session,
                    code="dealer_phone_invalid",
                    message=f"Approve candidate failed: {exc}",
                    job_id=job.id,
                )
                await callback.bot.send_message(job.telegram_chat_id, f"Ошибка подтверждения номера: {exc}")

    @router.callback_query(F.data.startswith("phone_review:reject:"))
    async def reject_phone_review(callback: CallbackQuery) -> None:
        if not can_access_callback(callback):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        try:
            _prefix, action, job_raw = callback.data.split(":", 2)
            if action != "reject":
                raise ValueError("invalid action")
            job_id = int(job_raw)
        except Exception:
            await callback.answer("Некорректные данные", show_alert=True)
            return

        async with SessionLocal() as session:
            job = await get_job(session, job_id)
            if not job:
                await callback.answer("Job не найден", show_alert=True)
                return
            job.status = "canceled_by_review"
            await session.commit()
            await add_job_error(
                session,
                code="dealer_phone_needs_review",
                message="Phone review rejected by admin",
                job_id=job.id,
            )
        if callback.message:
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Отклонено")

    @router.callback_query(F.data.startswith("request:start:"))
    async def request_start_calls(callback: CallbackQuery) -> None:
        if not can_access_callback(callback):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        try:
            parts = callback.data.split(":")
            if len(parts) == 3:
                sequence_mode = "manual"
                campaign_id = int(parts[2])
            elif len(parts) == 4 and parts[2] in {"auto", "manual"}:
                sequence_mode = parts[2]
                campaign_id = int(parts[3])
            else:
                raise ValueError("invalid request start callback")
        except Exception:
            await callback.answer("Некорректные данные", show_alert=True)
            return
        async with SessionLocal() as session:
            campaign = await get_request_campaign(session, campaign_id)
            if not campaign:
                await callback.answer("Кампания не найдена", show_alert=True)
                return
            if not await ensure_request_campaign_owner(callback, campaign):
                return
            if campaign.status not in REQUEST_START_STATUSES:
                await callback.answer(f"Кампания в статусе {campaign.status}")
                return
            campaign.call_sequence_mode = sequence_mode
            await session.commit()
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("Запускаю первый звонок")
            if sequence_mode == "auto":
                await request_service.send_service_message(
                    session=session,
                    campaign=campaign,
                    bot=callback.bot,
                    text="Автоматический режим включён: буду прозванивать номера подряд.",
                )
            await request_service.start_next_call(session=session, campaign=campaign, bot=callback.bot)

    @router.callback_query(F.data.startswith("request:lang:"))
    async def request_select_language(callback: CallbackQuery) -> None:
        if not can_access_callback(callback):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        try:
            _prefix, _lang_key, language, campaign_raw = callback.data.split(":", 3)
            if language not in {"en", "ja"}:
                raise ValueError("invalid language")
            campaign_id = int(campaign_raw)
        except Exception:
            await callback.answer("Некорректные данные", show_alert=True)
            return
        async with SessionLocal() as session:
            campaign = await get_request_campaign(session, campaign_id)
            if not campaign:
                await callback.answer("Кампания не найдена", show_alert=True)
                return
            if not await ensure_request_campaign_owner(callback, campaign):
                return
            if campaign.status not in {"needs_language", "ready_to_confirm"}:
                await callback.answer(f"Кампания в статусе {campaign.status}")
                return
            chat_id = callback.message.chat.id if callback.message else campaign.telegram_chat_id
            await delete_callback_message(callback)
            await callback.answer("Готовлю цель")
            progress_message = await safe_send_message(callback.bot, chat_id, "Формирую цель...")
            try:
                campaign = await request_service.set_language_and_generate_goals(
                    session=session,
                    campaign=campaign,
                    call_language=language,
                )
                targets = await request_service.list_targets(session, campaign.id)
                if campaign.status == "ready_to_confirm":
                    await request_service.send_service_message(
                        session=session,
                        campaign=campaign,
                        bot=callback.bot,
                        text=build_request_confirmation_text(campaign, targets),
                        reply_markup=request_call_confirm_keyboard(campaign.id, campaign.valid_numbers),
                    )
                elif campaign.status == "needs_goal_clarification":
                    await request_service.send_service_message(
                        session=session,
                        campaign=campaign,
                        bot=callback.bot,
                        text="Уточните, пожалуйста, какую модель или задачу прозванивать и что обязательно выяснить?",
                    )
                else:
                    await request_service.send_service_message(
                        session=session,
                        campaign=campaign,
                        bot=callback.bot,
                        text=f"Кампания не готова к запуску: {campaign.status}",
                    )
            finally:
                if progress_message is not None:
                    await safe_delete_message(callback.bot, chat_id, progress_message.message_id)

    @router.callback_query(F.data.startswith("request:next:"))
    async def request_next_call(callback: CallbackQuery) -> None:
        if not can_access_callback(callback):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        campaign_id = int(callback.data.split(":", 2)[2])
        async with SessionLocal() as session:
            campaign = await get_request_campaign(session, campaign_id)
            if not campaign:
                await callback.answer("Кампания не найдена", show_alert=True)
                return
            if not await ensure_request_campaign_owner(callback, campaign):
                return
            if campaign.status in {"completed", "stopped", "canceled"}:
                await callback.answer("Кампания уже завершена")
                if callback.message:
                    await callback.message.answer("Выберите режим прозвона:", reply_markup=start_mode_keyboard())
                return
            if campaign.status not in {"waiting_next"}:
                await callback.answer(f"Кампания в статусе {campaign.status}")
                return
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=None)
            await callback.answer("Звоню следующему")
            await request_service.start_next_call(session=session, campaign=campaign, bot=callback.bot)

    @router.callback_query(F.data.startswith("request:stop:"))
    async def request_stop_campaign(callback: CallbackQuery) -> None:
        if not can_access_callback(callback):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        campaign_id = int(callback.data.split(":", 2)[2])
        async with SessionLocal() as session:
            campaign = await get_request_campaign(session, campaign_id)
            if not campaign:
                await callback.answer("Кампания не найдена", show_alert=True)
                return
            if not await ensure_request_campaign_owner(callback, campaign):
                return
            await request_service.cancel_campaign(session=session, campaign=campaign, bot=callback.bot)
        await callback.answer("Остановлено")

    @router.callback_query(F.data.startswith("request:change_goal:"))
    async def request_change_goal(callback: CallbackQuery) -> None:
        if not can_access_callback(callback):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        campaign_id = int(callback.data.split(":", 2)[2])
        async with SessionLocal() as session:
            campaign = await get_request_campaign(session, campaign_id)
            if not campaign:
                await callback.answer("Кампания не найдена", show_alert=True)
                return
            if not await ensure_request_campaign_owner(callback, campaign):
                return
            campaign.status = "needs_goal"
            await session.commit()
            if callback.message:
                await callback.message.edit_reply_markup(reply_markup=None)
            await request_service.send_service_message(
                session=session,
                campaign=campaign,
                bot=callback.bot,
                text="Пришлите новую задачу прозвона: что именно нужно выяснить у дилеров?",
            )
        await callback.answer("Жду новую цель")

    @router.callback_query(F.data.startswith("request:cancel:"))
    async def request_cancel_campaign(callback: CallbackQuery) -> None:
        if not can_access_callback(callback):
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        campaign_id = int(callback.data.split(":", 2)[2])
        async with SessionLocal() as session:
            campaign = await get_request_campaign(session, campaign_id)
            if campaign:
                if not await ensure_request_campaign_owner(callback, campaign):
                    return
                await request_service.cancel_campaign(session=session, campaign=campaign, bot=callback.bot)
        await callback.answer("Задача отменена")

    return router
