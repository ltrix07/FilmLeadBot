from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.dispatcher import create_dispatcher
from app.bot.fallback import fallback_callback, fallback_message, fallback_router


@pytest.mark.asyncio
async def test_fallback_message_prompts_user_to_restart():
    message = SimpleNamespace(
        content_type="sticker",
        from_user=SimpleNamespace(id=123),
        answer=AsyncMock(),
    )

    await fallback_message(message)

    message.answer.assert_awaited_once_with(
        "Не понял это сообщение. Напиши /start, чтобы начать заново."
    )


@pytest.mark.asyncio
async def test_fallback_callback_shows_expired_action_alert():
    callback = SimpleNamespace(data="outdated_action", from_user=SimpleNamespace(id=123), answer=AsyncMock())

    await fallback_callback(callback)

    callback.answer.assert_awaited_once_with(
        "Это действие устарело — открой меню заново.", show_alert=True
    )


def test_fallback_router_is_registered_last():
    dispatcher = create_dispatcher(SimpleNamespace())

    assert dispatcher.sub_routers[-1] is fallback_router
