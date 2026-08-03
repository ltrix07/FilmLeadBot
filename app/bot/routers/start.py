from __future__ import annotations

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.gate import render_gate_text, render_menu_text
from app.bot.keyboards import build_gate_keyboard
from app.config import Settings
from app.db.models import ReferralEvent, ReferralPartner, Sponsor, User
from app.services.notifications import (
    format_campaign_completed,
    format_campaign_error,
    notify_admins,
)
from app.services.partners import get_partner_menu_keyboard
from app.services.subscription import SubscriptionAccessService
from app.services.settings import get_welcome_message

start_router = Router(name="start")


async def _send_welcome_message(message: Message, bot: Bot, session_factory, settings: Settings) -> None:
    async with session_factory() as session:
        welcome = await get_welcome_message(session)
    if welcome is None:
        await message.answer(settings.welcome_message)
        return
    try:
        await bot.copy_message(
            chat_id=message.from_user.id,
            from_chat_id=welcome.source_chat_id,
            message_id=welcome.source_message_id,
        )
    except TelegramAPIError:
        await message.answer(settings.welcome_message)


async def _ensure_user_and_referral(
    session: AsyncSession,
    telegram_id: int,
    payload: str | None,
    username: str | None = None,
    full_name: str | None = None,
) -> None:
    user = await session.get(User, telegram_id)
    if user is not None:
        user.username = username
        user.full_name = full_name
        if user.blocked_at is not None:
            user.blocked_at = None
        await session.commit()
        return

    referrer_telegram_id: int | None = None
    if payload and payload.startswith("ref_"):
        referral_code = payload.removeprefix("ref_")
        partner = await session.scalar(
            select(ReferralPartner).where(
                ReferralPartner.referral_code == referral_code,
                ReferralPartner.revoked_at.is_(None),
            )
        )
        if partner is not None and partner.telegram_id != telegram_id:
            referrer_telegram_id = partner.telegram_id

    session.add(User(
        telegram_id=telegram_id,
        referrer_telegram_id=referrer_telegram_id,
        username=username,
        full_name=full_name,
    ))
    await session.flush()
    if referrer_telegram_id is not None:
        session.add(
            ReferralEvent(
                referred_user_telegram_id=telegram_id,
                referrer_telegram_id=referrer_telegram_id,
            )
        )
    await session.commit()


@start_router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    session_factory,
    subscription_service: SubscriptionAccessService,
    bot: Bot,
    settings: Settings,
) -> None:
    async with session_factory() as session:
        await _ensure_user_and_referral(
            session,
            message.from_user.id,
            command.args,
            message.from_user.username,
            message.from_user.full_name,
        )
        result = await subscription_service.evaluate_user_access(session, bot, message.from_user.id)
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

    await _send_welcome_message(message, bot, session_factory, settings)
    if result.passed:
        keyboard = get_partner_menu_keyboard() if result.is_partner else None
        await message.answer(render_menu_text(result), reply_markup=keyboard)
    else:
        await message.answer(
            render_gate_text(result.missing_sponsors),
            reply_markup=build_gate_keyboard(result.subscribed_sponsors, result.missing_sponsors),
        )
