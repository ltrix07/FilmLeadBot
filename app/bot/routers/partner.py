from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Filter
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import select

from app.db.models import ReferralPartner
from app.services.movie_codes import build_active_codes_export_xlsx
from app.services.partners import (
    format_partner_stats_text,
    get_partner_menu_keyboard,
    get_partner_stats,
    is_active_partner,
)
from app.services.partner_balance import get_partner_balance, get_partner_balance_history


class PartnerTriggerFilter(Filter):
    """Match the secret activation word only for non-revoked partners."""

    async def __call__(self, message: Message, session_factory) -> bool:
        if message.text is None:
            return False
        if message.text.strip().casefold() not in {"партнер", "партнёр"}:
            return False
        async with session_factory() as session:
            partner = await session.scalar(
                select(ReferralPartner).where(
                    ReferralPartner.telegram_id == message.from_user.id,
                    ReferralPartner.revoked_at.is_(None),
                )
            )
        return partner is not None


partner_router = Router(name="partner")
partner_router.message.filter(PartnerTriggerFilter())


@partner_router.message()
async def activate_partner(message: Message, session_factory, bot: Bot) -> None:
    async with session_factory() as session:
        partner = await session.scalar(
            select(ReferralPartner).where(
                ReferralPartner.telegram_id == message.from_user.id,
                ReferralPartner.revoked_at.is_(None),
            )
        )
        # The filter has verified this, but guard against a concurrent revoke.
        if partner is None:
            return
        first_activation = partner.activated_at is None
        if first_activation:
            partner.activated_at = datetime.now(timezone.utc)
            await session.commit()

    if first_activation:
        await message.answer(
            "Добро пожаловать в реферальную программу! Отправляй свою ссылку "
            "новым пользователям — каждый, кто перейдёт по ней и пройдёт "
            "обязательные подписки, будет засчитан в твою статистику."
        )
    await message.answer("Кабинет рефовода.", reply_markup=get_partner_menu_keyboard())


@partner_router.callback_query(F.data == "partner:link")
async def partner_link(callback: CallbackQuery, session_factory, bot: Bot) -> None:
    async with session_factory() as session:
        if not await is_active_partner(session, callback.from_user.id):
            await callback.answer("Доступно только рефоводам.", show_alert=True)
            return
        partner = await session.scalar(
            select(ReferralPartner).where(ReferralPartner.telegram_id == callback.from_user.id)
        )
    username = (await bot.get_me()).username
    await callback.message.answer(
        f"Твоя реферальная ссылка:\nhttps://t.me/{username}?start=ref_{partner.referral_code}\n\n"
        "Отправляй её новым пользователям."
    )
    await callback.answer()


@partner_router.callback_query(F.data == "partner:stats")
async def partner_stats(callback: CallbackQuery, session_factory) -> None:
    async with session_factory() as session:
        if not await is_active_partner(session, callback.from_user.id):
            await callback.answer("Доступно только рефоводам.", show_alert=True)
            return
        started, confirmed = await get_partner_stats(session, callback.from_user.id)
    await callback.message.answer(
        format_partner_stats_text(started, confirmed)
    )
    await callback.answer()


@partner_router.callback_query(F.data == "partner:balance")
async def partner_balance(callback: CallbackQuery, session_factory) -> None:
    async with session_factory() as session:
        if not await is_active_partner(session, callback.from_user.id):
            await callback.answer("Доступно только рефоводам.", show_alert=True)
            return
        balance = await get_partner_balance(session, callback.from_user.id)
        history = await get_partner_balance_history(session, callback.from_user.id)
    await callback.message.answer(
        f"Баланс: {balance:.2f} ₽\n\nИстория (последние 30 дней):\n" + "\n".join(history)
    )
    await callback.answer()


@partner_router.callback_query(F.data == "partner:codes_export")
async def partner_codes_export(callback: CallbackQuery, session_factory) -> None:
    async with session_factory() as session:
        if not await is_active_partner(session, callback.from_user.id):
            await callback.answer("Доступно только рефоводам.", show_alert=True)
            return
        content = await build_active_codes_export_xlsx(session)
    await callback.message.answer_document(BufferedInputFile(content, filename="codes.xlsx"))
    await callback.answer()
