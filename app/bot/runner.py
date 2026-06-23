from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    MenuButtonCommands,
)

from app.bot.handlers import create_router
from app.config import get_settings
from app.logging_config import setup_logging
from app.services.elevenlabs_client import ElevenLabsService
from app.services.openai_client import OpenAIService
from app.services.workflow import CallWorkflow

logger = logging.getLogger(__name__)


async def configure_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Выбрать режим прозвона"),
        BotCommand(command="request", description="Начать прозвон по запросу"),
        BotCommand(command="cancel", description="Отменить активную задачу прозвона"),
    ]
    scopes = (
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
    )
    for scope in scopes:
        try:
            await bot.set_my_commands(commands, scope=scope)
        except Exception:
            logger.exception("failed to set telegram bot commands", extra={"status": scope.type})
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands(type="commands"))
    except Exception:
        logger.exception("failed to set telegram bot command menu button")


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    logger.info("WEBHOOK_BASE_URL=%s", settings.normalized_webhook_base_url)
    logger.info("ElevenLabs webhook URL: %s", settings.elevenlabs_webhook_endpoint)
    logger.info("TEST_MODE=%s", settings.test_mode)
    logger.info("TEST_CALL_PHONE=%s", settings.test_call_phone)
    for warning in settings.runtime_warnings():
        logger.warning(warning)

    bot = Bot(token=settings.telegram_bot_token)
    await configure_bot_commands(bot)
    dp = Dispatcher()
    workflow = CallWorkflow(
        settings=settings,
        openai_service=OpenAIService(settings),
        elevenlabs_service=ElevenLabsService(settings),
    )
    dp.include_router(create_router(settings, workflow=workflow))

    queue_task = asyncio.create_task(workflow.run_queue_worker(bot))
    try:
        await dp.start_polling(bot)
    finally:
        queue_task.cancel()
        try:
            await queue_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
