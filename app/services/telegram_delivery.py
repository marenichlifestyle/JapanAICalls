from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot

logger = logging.getLogger(__name__)


async def _with_retry(action: str, coro_factory, *, attempts: int = 3, base_delay: float = 1.0):
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "telegram %s failed",
                action,
                extra={"status": f"attempt {attempt}/{attempts}: {type(exc).__name__}: {exc}"},
            )
            if attempt < attempts:
                await asyncio.sleep(base_delay * attempt)
    logger.error(
        "telegram %s failed after retries",
        action,
        extra={"status": f"{type(last_exc).__name__}: {last_exc}"},
    )
    return None


async def safe_send_message(bot: Bot, chat_id: int, text: str, **kwargs: Any):
    return await _with_retry(
        "send_message",
        lambda: bot.send_message(chat_id, text, **kwargs),
    )


async def safe_send_document(bot: Bot, chat_id: int, document, **kwargs: Any):
    return await _with_retry(
        "send_document",
        lambda: bot.send_document(chat_id, document, **kwargs),
    )


async def safe_delete_message(bot: Bot, chat_id: int, message_id: int) -> bool:
    result = await _with_retry(
        "delete_message",
        lambda: bot.delete_message(chat_id, message_id),
        attempts=2,
        base_delay=0.5,
    )
    return bool(result)
