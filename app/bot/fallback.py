from __future__ import annotations

import logging

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)

fallback_router = Router(name="fallback")


@fallback_router.message()
async def fallback_message(message: Message) -> None:
    logger.warning(
        "unhandled_message",
        extra={"content_type": message.content_type, "telegram_id": message.from_user.id},
    )
    try:
        await message.answer("Не понял это сообщение. Напиши /start, чтобы начать заново.")
    except TelegramAPIError:
        logger.warning("fallback_message_delivery_failed")


@fallback_router.callback_query()
async def fallback_callback(callback: CallbackQuery) -> None:
    logger.warning(
        "unhandled_callback",
        extra={"callback_data": callback.data, "telegram_id": callback.from_user.id},
    )
    try:
        await callback.answer("Это действие устарело — открой меню заново.", show_alert=True)
    except TelegramAPIError:
        logger.warning("fallback_callback_delivery_failed")
