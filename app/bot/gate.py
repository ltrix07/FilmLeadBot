from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, F, Router
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import build_gate_keyboard
from app.db.models import Sponsor
from app.services.partners import get_partner_menu_extra_lines, get_partner_menu_keyboard
from app.services.notifications import (
    format_campaign_completed,
    format_campaign_error,
    notify_admins,
)
from app.services.subscription import AccessResult, SubscriptionAccessService

gate_router = Router(name="gate")


def render_gate_text(missing: list[Sponsor]) -> str:
    return "<b>🤖 Чтобы бот выдал название по найденному коду - подпишитесь на все каналы ниже:</b>"


def render_menu_text(result: AccessResult) -> str:
    lines = ["🍏 Доступ открыт. Чтобы получить название - отправьте найденный Вами код."]
    if result.is_partner:
        lines.extend(get_partner_menu_extra_lines())
    return "\n".join(lines)


class SubscriptionGateMiddleware(BaseMiddleware):
    def __init__(self, subscription_service: SubscriptionAccessService) -> None:
        self.subscription_service = subscription_service

    async def __call__(
        self,
        handler: Callable[[Message | CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        session_factory = data["session_factory"]
        bot = data["bot"]
        async with session_factory() as session:
            result = await self.subscription_service.evaluate_user_access(
                session, bot, event.from_user.id
            )
        if result.newly_completed_campaigns or result.errored_campaigns:
            async with session_factory() as session:
                for campaign in result.newly_completed_campaigns:
                    sponsor = await session.get(Sponsor, campaign.sponsor_chat_id)
                    if sponsor is not None:
                        await notify_admins(bot, session_factory, format_campaign_completed(campaign, sponsor))
                for campaign in result.errored_campaigns:
                    sponsor = await session.get(Sponsor, campaign.sponsor_chat_id)
                    if sponsor is not None:
                        await notify_admins(bot, session_factory, format_campaign_error(campaign, sponsor))

        if not result.passed:
            text = render_gate_text(result.missing_sponsors)
            keyboard = build_gate_keyboard(result.subscribed_sponsors, result.missing_sponsors)
            if isinstance(event, Message):
                await event.answer(text, reply_markup=keyboard, parse_mode="HTML")
            else:
                await event.message.answer(text, reply_markup=keyboard, parse_mode="HTML")
                await event.answer()
            return None

        data["access_result"] = result
        return await handler(event, data)


@gate_router.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery, session_factory, subscription_service, bot) -> None:
    async with session_factory() as session:
        result = await subscription_service.evaluate_user_access(
            session, bot, callback.from_user.id, force_refresh=True
        )
    if result.newly_completed_campaigns or result.errored_campaigns:
        async with session_factory() as session:
            for campaign in result.newly_completed_campaigns:
                sponsor = await session.get(Sponsor, campaign.sponsor_chat_id)
                if sponsor is not None:
                    await notify_admins(bot, session_factory, format_campaign_completed(campaign, sponsor))
            for campaign in result.errored_campaigns:
                sponsor = await session.get(Sponsor, campaign.sponsor_chat_id)
                if sponsor is not None:
                    await notify_admins(bot, session_factory, format_campaign_error(campaign, sponsor))
    if result.passed:
        keyboard = get_partner_menu_keyboard() if result.is_partner else None
        await callback.message.edit_text(render_menu_text(result), reply_markup=keyboard)
    else:
        await callback.message.edit_text(
            render_gate_text(result.missing_sponsors),
            reply_markup=build_gate_keyboard(result.subscribed_sponsors, result.missing_sponsors),
            parse_mode="HTML",
        )
        await callback.answer("Пока не все подписки выполнены")
        return
    await callback.answer()


@gate_router.callback_query(F.data == "sponsor_link_missing")
async def sponsor_link_missing(callback: CallbackQuery) -> None:
    await callback.answer("Ссылка временно недоступна, обратитесь к администратору", show_alert=True)
