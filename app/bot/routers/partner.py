from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.filters import Filter
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from app.db.models import ReferralPartner
from app.services.movie_codes import build_active_codes_export_xlsx
from app.services.partners import (
    format_partner_self_stats_text,
    get_partner_cabinet_text,
    get_partner_menu_keyboard,
    get_partner_stats,
    is_active_partner,
)
from app.services.partner_balance import get_partner_balance, get_partner_balance_history


WELCOME_HANDSHAKE_EMOJI_ID = "5253700530152165989"

PARTNER_TRIGGER_WORDS = {
    "партнер",
    "партнёр",
    "партнерка",
    "партнёрка",
    "рефка",
    "рефералка",
    "/partner",
}


class PartnerTriggerFilter(Filter):
    """Match the secret activation word only for non-revoked partners."""

    async def __call__(self, message: Message, session_factory) -> bool:
        if message.text is None:
            return False
        if message.text.strip().casefold() not in PARTNER_TRIGGER_WORDS:
            return False
        async with session_factory() as session:
            partner = await session.scalar(
                select(ReferralPartner).where(
                    ReferralPartner.telegram_id == message.from_user.id,
                    ReferralPartner.revoked_at.is_(None),
                )
            )
        return partner is not None


class PartnerTriggerWordFilter(Filter):
    """Match the trigger word regardless of partner status.

    Registered after ``partner_router`` so it only ever sees messages from
    users the trigger filter above already rejected (non-partners).
    """

    async def __call__(self, message: Message) -> bool:
        if message.text is None:
            return False
        return message.text.strip().casefold() in PARTNER_TRIGGER_WORDS


partner_router = Router(name="partner")
partner_router.message.filter(PartnerTriggerFilter())

partner_recruitment_router = Router(name="partner_recruitment")
partner_recruitment_router.message.filter(PartnerTriggerWordFilter())


@partner_recruitment_router.message()
async def prompt_partner_recruitment(message: Message) -> None:
    await message.answer(
        "⭐️ <b>Чтобы стать нашим партнером - напишите @Anatoliy_Here</b>",
        parse_mode="HTML",
    )


def _partner_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Назад", callback_data="partner:menu")
    ]])


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
            "<b>Добро пожаловать в партнерскую программу! "
            f'<tg-emoji emoji-id="{WELCOME_HANDSHAKE_EMOJI_ID}">🤝</tg-emoji></b>\n\n'
            "Набирайте как можно большее количество рефералов. Вы будете получать "
            "вознаграждение - за каждые подписки, приглашенных Вами пользователей.",
            parse_mode="HTML",
        )
    await message.answer(
        get_partner_cabinet_text(),
        reply_markup=get_partner_menu_keyboard(),
        parse_mode="HTML",
    )


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
    await callback.message.edit_text(
        f"Ваша реферальная ссылка:\nhttps://t.me/{username}?start=ref_{partner.referral_code}",
        reply_markup=_partner_back_keyboard(),
    )
    await callback.answer()


@partner_router.callback_query(F.data == "partner:stats")
async def partner_stats(callback: CallbackQuery, session_factory) -> None:
    async with session_factory() as session:
        if not await is_active_partner(session, callback.from_user.id):
            await callback.answer("Доступно только рефоводам.", show_alert=True)
            return
        started, confirmed = await get_partner_stats(session, callback.from_user.id)
    await callback.message.edit_text(
        format_partner_self_stats_text(started, confirmed),
        reply_markup=_partner_back_keyboard(),
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
    await callback.message.edit_text(
        f"Ваш текущий баланс: {balance:.2f} ₽\n\n"
        "Для вывода напишите: @Anatoliy_Here\n\n"
        "История за последние 30 дней:\n" + "\n".join(history),
        reply_markup=_partner_back_keyboard(),
    )
    await callback.answer()


@partner_router.callback_query(F.data == "partner:menu")
async def partner_menu(callback: CallbackQuery, session_factory) -> None:
    async with session_factory() as session:
        if not await is_active_partner(session, callback.from_user.id):
            await callback.answer("Доступно только рефоводам.", show_alert=True)
            return
    await callback.message.edit_text(
        get_partner_cabinet_text(),
        reply_markup=get_partner_menu_keyboard(),
        parse_mode="HTML",
    )
    await callback.answer()


@partner_router.callback_query(F.data == "partner:codes_export")
async def partner_codes_export(callback: CallbackQuery, session_factory) -> None:
    async with session_factory() as session:
        if not await is_active_partner(session, callback.from_user.id):
            await callback.answer("Доступно только рефоводам.", show_alert=True)
            return
        content = await build_active_codes_export_xlsx(session)
    await callback.message.answer_document(
        BufferedInputFile(content, filename="codes.xlsx"),
        caption=(
            "В данном файле - собраны все коды & названия аниме и фильмов, по которым - "
            "бот (сейчас) выдает ответы.\n\n"
            "Если Вы не нашли нужное Вам название, то напишите @Anatoliy_Here, "
            "добавим все в базу."
        ),
    )
    await callback.answer()
