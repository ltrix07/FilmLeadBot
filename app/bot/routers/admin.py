from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from collections.abc import Coroutine

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import joinedload

from app.bot.filters import IsAdmin
from app.db.models import Campaign, CampaignLimitHistory, CampaignStatus, MovieCode, MovieCodeStatus, ReferralPartner, Sponsor, SponsorType
from app.services.admins import bot_status_allows_access, get_admin_id
from app.services.broadcasts import create_broadcast, get_broadcast_recipients, run_broadcast
from app.services.campaign_jobs import attempt_resume_campaign
from app.services.movie_codes import create_code, deactivate_code, get_code_history, restore_code, update_title
from app.services.partners import approve_partner, list_partners, revoke_partner
from app.services.bulk_import import (
    ImportPlan,
    apply_import_plan,
    build_conflicts_report_csv,
    build_import_plan,
    build_template_xlsx,
    parse_csv,
    parse_xlsx,
)


admin_router = Router(name="admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())

_background_tasks: set[asyncio.Task] = set()


def _spawn(coro: Coroutine) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


class SponsorForm(StatesGroup):
    waiting_chat = State()
    waiting_invite_link = State()


class CampaignForm(StatesGroup):
    waiting_sponsor = State()
    waiting_limit = State()
    waiting_schedule = State()


class LimitForm(StatesGroup):
    waiting_new_limit = State()


class CodeSearchForm(StatesGroup):
    waiting_code = State()


class CodeAddForm(StatesGroup):
    waiting_code = State()
    waiting_title = State()


class CodeEditForm(StatesGroup):
    waiting_new_title = State()


class CodeRestoreForm(StatesGroup):
    waiting_title = State()


class ImportForm(StatesGroup):
    waiting_file = State()


class PartnerForm(StatesGroup):
    waiting_user = State()


class BroadcastForm(StatesGroup):
    waiting_content = State()


def _admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📡 Спонсоры", callback_data="admin:sponsors")],
            [InlineKeyboardButton(text="📢 Кампании", callback_data="admin:campaigns")],
            [InlineKeyboardButton(text="🎬 Коды", callback_data="admin:codes")],
            [InlineKeyboardButton(text="👥 Партнёры", callback_data="admin:partners")],
            [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast")],
        ]
    )


def _sponsors_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить спонсора", callback_data="admin:sponsor:add")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
        ]
    )


def _sponsors_text(sponsors: list[Sponsor]) -> str:
    if not sponsors:
        return "Спонсоров пока нет."
    return "\n".join(
        f"{'✅' if sponsor.bot_has_access else '⚠️'} {sponsor.title} ({sponsor.type.value})"
        for sponsor in sponsors
    )


async def _get_sponsors(session_factory) -> list[Sponsor]:
    async with session_factory() as session:
        return list((await session.scalars(select(Sponsor).order_by(Sponsor.title))).all())


async def _send_sponsors(message: Message, session_factory) -> None:
    sponsors = await _get_sponsors(session_factory)
    await message.answer(_sponsors_text(sponsors), reply_markup=_sponsors_keyboard())


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    await message.answer("Админ-панель", reply_markup=_admin_menu_keyboard())


@admin_router.callback_query(F.data == "admin:menu")
async def admin_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Админ-панель", reply_markup=_admin_menu_keyboard())
    await callback.answer()


@admin_router.callback_query(F.data == "admin:broadcast")
async def request_broadcast_content(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastForm.waiting_content)
    await callback.message.answer(
        "Пришли сообщение, которое нужно разослать всем пользователям "
        "(текст, фото с подписью — что угодно)."
    )
    await callback.answer()


@admin_router.message(StateFilter(BroadcastForm.waiting_content))
async def receive_broadcast_content(
    message: Message, state: FSMContext, session_factory
) -> None:
    preview = (message.html_text or message.caption or "<медиа без подписи>")[:200]
    async with session_factory() as session:
        recipients = await get_broadcast_recipients(session)
    if not recipients:
        await state.clear()
        await message.answer("Получателей нет.")
        return
    await state.update_data(
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        preview=preview,
        recipient_count=len(recipients),
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="admin:broadcast:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="admin:broadcast:cancel"),
    ]])
    await message.answer(
        f"Разослать это сообщение {len(recipients)} пользователям?\n\nПревью: {preview}",
        reply_markup=keyboard,
    )


@admin_router.callback_query(
    StateFilter(BroadcastForm.waiting_content), F.data == "admin:broadcast:confirm"
)
async def confirm_broadcast(
    callback: CallbackQuery, state: FSMContext, bot: Bot, session_factory
) -> None:
    data = await state.get_data()
    async with session_factory() as session:
        recipients = await get_broadcast_recipients(session)
        broadcast = await create_broadcast(
            session,
            callback.from_user.id,
            data["source_chat_id"],
            data["source_message_id"],
            data["preview"],
        )
    _spawn(run_broadcast(
        bot, session_factory, broadcast.id, data["source_chat_id"], data["source_message_id"],
        recipients, callback.from_user.id,
    ))
    await state.clear()
    await callback.message.answer("Рассылка запущена, пришлю итог по завершении.")
    await callback.answer()


@admin_router.callback_query(
    StateFilter(BroadcastForm.waiting_content), F.data == "admin:broadcast:cancel"
)
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("Рассылка отменена.")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:sponsors")
async def list_sponsors(callback: CallbackQuery, session_factory) -> None:
    sponsors = await _get_sponsors(session_factory)
    await callback.message.edit_text(_sponsors_text(sponsors), reply_markup=_sponsors_keyboard())
    await callback.answer()


@admin_router.callback_query(F.data == "admin:sponsor:add")
async def add_sponsor(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SponsorForm.waiting_chat)
    await callback.message.answer(
        "Перешли сюда любое сообщение из канала/группы, которую нужно добавить "
        "спонсором. Если это невозможно — пришли числовой chat_id вручную.\n\n"
        "Для отмены отправь /cancel."
    )
    await callback.answer()


@admin_router.message(
    Command("cancel"),
    StateFilter(SponsorForm.waiting_chat, SponsorForm.waiting_invite_link),
)
async def cancel_sponsor_addition(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Добавление спонсора отменено.")


def _sponsor_type(chat_type: object) -> SponsorType | None:
    value = getattr(chat_type, "value", chat_type)
    try:
        return SponsorType(value)
    except ValueError:
        return None


async def _chat_from_message(message: Message, bot: Bot):
    forwarded_chat = getattr(message, "forward_from_chat", None)
    if forwarded_chat is not None:
        return forwarded_chat

    # Telegram replaced the legacy ``forward_from_chat`` field with
    # ``forward_origin``. Keep the legacy path above for compatibility with
    # forwarded updates from older Bot API versions.
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        origin_chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
        if origin_chat is not None:
            return origin_chat

    if message.text is not None:
        try:
            chat_id = int(message.text.strip())
        except ValueError:
            return None
        return await bot.get_chat(chat_id)
    return None


@admin_router.message(StateFilter(SponsorForm.waiting_chat))
async def receive_sponsor_chat(message: Message, state: FSMContext, bot: Bot) -> None:
    chat = await _chat_from_message(message, bot)
    if chat is None:
        await message.answer("Не понял, пришли пересланное сообщение или числовой chat_id.")
        return

    chat_type = _sponsor_type(chat.type)
    if chat_type is None:
        await message.answer("Не понял, пришли пересланное сообщение или числовой chat_id.")
        return

    member = await bot.get_chat_member(chat.id, bot.id)
    if not bot_status_allows_access(chat_type, member.status):
        channel_note = ", и для канала — с правами администратора" if chat_type is SponsorType.CHANNEL else ""
        await message.answer(
            "Бот должен быть добавлен в этот канал/группу"
            f"{channel_note}, прежде чем его можно добавить спонсором."
        )
        return

    await state.update_data(
        chat_id=chat.id,
        title=chat.title or chat.username or str(chat.id),
        username=chat.username,
        type=chat_type.value,
    )
    await state.set_state(SponsorForm.waiting_invite_link)
    await message.answer(
        "Пришли постоянную инвайт-ссылку на этот чат (или `-`, если у канала "
        "есть публичный username и ссылка не нужна)."
    )


@admin_router.message(StateFilter(SponsorForm.waiting_invite_link))
async def receive_invite_link(message: Message, state: FSMContext, session_factory) -> None:
    if message.text is None:
        await message.answer("Пришли инвайт-ссылку текстом или `-`.")
        return

    data = await state.get_data()
    invite_link = None if message.text == "-" else message.text
    statement = insert(Sponsor).values(
        chat_id=data["chat_id"],
        title=data["title"],
        username=data["username"],
        type=data["type"],
        invite_link=invite_link,
        bot_has_access=True,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[Sponsor.chat_id],
        set_={
            "title": statement.excluded.title,
            "username": statement.excluded.username,
            "type": statement.excluded.type,
            "invite_link": statement.excluded.invite_link,
            "bot_has_access": True,
        },
    )
    async with session_factory() as session:
        await session.execute(statement)
        await session.commit()

    await state.clear()
    await message.answer(f"Спонсор «{data['title']}» сохранён.")
    await _send_sponsors(message, session_factory)


VISIBLE_CAMPAIGN_STATUSES = (
    CampaignStatus.DRAFT,
    CampaignStatus.SCHEDULED,
    CampaignStatus.ACTIVE,
    CampaignStatus.PAUSED,
    CampaignStatus.ERROR,
)


def _campaigns_text(campaigns: list[Campaign]) -> str:
    if not campaigns:
        return "Кампаний пока нет."
    return "\n".join(
        f"{campaign.sponsor.title} — {campaign.counter}/{campaign.limit_current} — {campaign.status.value}"
        for campaign in campaigns
    )


def _campaigns_keyboard(campaigns: list[Campaign]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="➕ Новая кампания", callback_data="admin:campaign:add")]]
    rows.extend(
        [InlineKeyboardButton(text=f"{campaign.sponsor.title} (#{campaign.id})", callback_data=f"admin:campaign:{campaign.id}")]
        for campaign in campaigns
    )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _get_campaigns(session_factory) -> list[Campaign]:
    async with session_factory() as session:
        return list(
            (await session.scalars(
                select(Campaign)
                .where(Campaign.status.in_(VISIBLE_CAMPAIGN_STATUSES))
                .options(joinedload(Campaign.sponsor))
                .order_by(Campaign.id.desc())
            )).all()
        )


async def _edit_campaigns(callback: CallbackQuery, session_factory) -> None:
    campaigns = await _get_campaigns(session_factory)
    await callback.message.edit_text(_campaigns_text(campaigns), reply_markup=_campaigns_keyboard(campaigns))


async def _send_campaigns(message: Message, session_factory) -> None:
    campaigns = await _get_campaigns(session_factory)
    await message.answer(_campaigns_text(campaigns), reply_markup=_campaigns_keyboard(campaigns))


@admin_router.callback_query(F.data == "admin:campaigns")
async def list_campaigns(callback: CallbackQuery, session_factory) -> None:
    await _edit_campaigns(callback, session_factory)
    await callback.answer()


@admin_router.callback_query(F.data == "admin:campaign:add")
async def add_campaign(callback: CallbackQuery, state: FSMContext, session_factory) -> None:
    sponsors = await _get_sponsors(session_factory)
    if not sponsors:
        await callback.answer("Сначала добавь хотя бы одного спонсора.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=sponsor.title, callback_data=f"admin:campaign:add:sponsor:{sponsor.chat_id}")]
        for sponsor in sponsors
    ])
    await state.set_state(CampaignForm.waiting_sponsor)
    await callback.message.answer("Выбери спонсора для кампании.", reply_markup=keyboard)
    await callback.answer()


@admin_router.callback_query(
    StateFilter(CampaignForm.waiting_sponsor), F.data.regexp(r"^admin:campaign:add:sponsor:-?\d+$")
)
async def choose_campaign_sponsor(callback: CallbackQuery, state: FSMContext) -> None:
    sponsor_chat_id = int(callback.data.rsplit(":", 1)[1])
    await state.update_data(sponsor_chat_id=sponsor_chat_id)
    await state.set_state(CampaignForm.waiting_limit)
    await callback.message.answer("Укажи лимит кампании (целое число).")
    await callback.answer()


def _positive_int(value: str | None) -> int | None:
    try:
        number = int((value or "").strip())
    except ValueError:
        return None
    return number if number > 0 else None


@admin_router.message(StateFilter(CampaignForm.waiting_limit))
async def receive_campaign_limit(message: Message, state: FSMContext) -> None:
    limit = _positive_int(message.text)
    if limit is None:
        await message.answer("Нужно целое положительное число.")
        return
    await state.update_data(limit=limit)
    await state.set_state(CampaignForm.waiting_schedule)
    await message.answer(
        "Когда запустить кампанию? Пришли `сейчас` для немедленного запуска или дату и время "
        "в формате `ДД.ММ.ГГГГ ЧЧ:ММ` (UTC) для отложенного."
    )


async def _create_campaign_from_state(message: Message, state: FSMContext, session_factory) -> None:
    data = await state.get_data()
    async with session_factory() as session:
        session.add(Campaign(
            sponsor_chat_id=data["sponsor_chat_id"], limit_original=data["limit"],
            limit_current=data["limit"], counter=0, status=CampaignStatus(data["status"]),
            scheduled_at=data.get("scheduled_at"), started_at=data.get("started_at"),
        ))
        await session.commit()
    await state.clear()
    await message.answer("Кампания создана.")
    await _send_campaigns(message, session_factory)


@admin_router.message(StateFilter(CampaignForm.waiting_schedule))
async def receive_campaign_schedule(message: Message, state: FSMContext, session_factory) -> None:
    text = (message.text or "").strip()
    now = datetime.now(timezone.utc)
    if text.casefold() == "сейчас":
        status, started_at, scheduled_at = CampaignStatus.ACTIVE, now, None
    else:
        try:
            scheduled_at = datetime.strptime(text, "%d.%m.%Y %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            await message.answer("Не понял формат даты, пример: 25.12.2026 18:00")
            return
        if scheduled_at <= now:
            await message.answer("Дата должна быть в будущем.")
            return
        status, started_at = CampaignStatus.SCHEDULED, None
    await state.update_data(status=status.value, started_at=started_at, scheduled_at=scheduled_at)
    if status is CampaignStatus.ACTIVE:
        async with session_factory() as session:
            active_count = await session.scalar(
                select(func.count()).select_from(Campaign).where(Campaign.status == CampaignStatus.ACTIVE)
            )
        if active_count >= 5:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Продолжить", callback_data="admin:campaign:confirm_create"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="admin:campaign:cancel_create"),
            ]])
            await message.answer(
                f"Вы создаёте {active_count + 1}-ю одновременно активную кампанию. Большое количество "
                "обязательных подписок может снизить конверсию пользователей. Продолжить создание?",
                reply_markup=keyboard,
            )
            return
    await _create_campaign_from_state(message, state, session_factory)


@admin_router.callback_query(StateFilter(CampaignForm.waiting_schedule), F.data == "admin:campaign:confirm_create")
async def confirm_campaign_create(callback: CallbackQuery, state: FSMContext, session_factory) -> None:
    await _create_campaign_from_state(callback.message, state, session_factory)
    await _edit_campaigns(callback, session_factory)
    await callback.answer()


@admin_router.callback_query(F.data == "admin:campaign:cancel_create")
async def cancel_campaign_create(callback: CallbackQuery, state: FSMContext, session_factory) -> None:
    await state.clear()
    await callback.message.answer("Создание кампании отменено.")
    await _edit_campaigns(callback, session_factory)
    await callback.answer()


def _campaign_card_text(campaign: Campaign) -> str:
    lines = [
        f"Кампания #{campaign.id}", f"Спонсор: {campaign.sponsor.title}", f"Статус: {campaign.status.value}",
        f"Исходный лимит: {campaign.limit_original}", f"Текущий лимит: {campaign.limit_current}",
        f"Засчитано: {campaign.counter}",
    ]
    if campaign.started_at:
        lines.append(f"Запущена: {campaign.started_at:%d.%m.%Y %H:%M}")
    if campaign.scheduled_at:
        lines.append(f"Запланирована: {campaign.scheduled_at:%d.%m.%Y %H:%M}")
    return "\n".join(lines)


def _campaign_card_keyboard(campaign: Campaign) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="✏️ Изменить лимит", callback_data=f"admin:campaign:{campaign.id}:limit")]]
    if campaign.status in (CampaignStatus.ERROR, CampaignStatus.PAUSED):
        rows.append([InlineKeyboardButton(text="▶️ Возобновить", callback_data=f"admin:campaign:{campaign.id}:resume")])
    if campaign.status not in (CampaignStatus.COMPLETED, CampaignStatus.CANCELLED):
        rows.append([InlineKeyboardButton(text="🚫 Отменить кампанию", callback_data=f"admin:campaign:{campaign.id}:cancel")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:campaigns")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.callback_query(F.data.regexp(r"^admin:campaign:\d+$"))
async def campaign_card(callback: CallbackQuery, session_factory) -> None:
    campaign_id = int(callback.data.rsplit(":", 1)[1])
    async with session_factory() as session:
        campaign = await session.scalar(
            select(Campaign).where(Campaign.id == campaign_id).options(joinedload(Campaign.sponsor))
        )
        if campaign is None:
            await callback.answer("Кампания не найдена.", show_alert=True)
            return
    await callback.message.edit_text(_campaign_card_text(campaign), reply_markup=_campaign_card_keyboard(campaign))
    await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:campaign:\d+:resume$"))
async def resume_campaign(callback: CallbackQuery, session_factory, bot: Bot) -> None:
    campaign_id = int(callback.data.split(":")[2])
    async with session_factory() as session:
        campaign = await session.scalar(
            select(Campaign).where(Campaign.id == campaign_id).options(joinedload(Campaign.sponsor))
        )
        if campaign is None:
            await callback.answer("Кампания не найдена.", show_alert=True)
            return
        resumed = await attempt_resume_campaign(session, bot, campaign_id)

    if not resumed:
        await callback.answer(
            "Доступ всё ещё не восстановлен. Проверь права бота в канале и попробуй снова.",
            show_alert=True,
        )
        return
    async with session_factory() as session:
        campaign = await session.scalar(
            select(Campaign).where(Campaign.id == campaign_id).options(joinedload(Campaign.sponsor))
        )
    await callback.message.edit_text(_campaign_card_text(campaign), reply_markup=_campaign_card_keyboard(campaign))
    await callback.answer("Кампания возобновлена.")


async def update_campaign_limit(
    session, campaign_id: int, new_limit: int, admin_telegram_id: int, *, complete: bool = False
) -> Campaign | None:
    """Persist a limit revision; a confirmed below-counter revision completes the campaign."""
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        return None
    old_limit = campaign.limit_current
    campaign.limit_current = new_limit
    if complete:
        campaign.status = CampaignStatus.COMPLETED
        campaign.completed_at = datetime.now(timezone.utc)
    session.add(CampaignLimitHistory(
        campaign_id=campaign.id, old_limit=old_limit, new_limit=new_limit,
        changed_by_admin_id=await get_admin_id(session, admin_telegram_id),
    ))
    await session.commit()
    return campaign


@admin_router.callback_query(F.data.regexp(r"^admin:campaign:\d+:limit$"))
async def change_campaign_limit(callback: CallbackQuery, state: FSMContext) -> None:
    campaign_id = int(callback.data.split(":")[2])
    await state.set_state(LimitForm.waiting_new_limit)
    await state.update_data(campaign_id=campaign_id)
    await callback.message.answer("Укажи новый лимит (целое положительное число).")
    await callback.answer()


@admin_router.message(StateFilter(LimitForm.waiting_new_limit))
async def receive_new_campaign_limit(message: Message, state: FSMContext, session_factory) -> None:
    new_limit = _positive_int(message.text)
    if new_limit is None:
        await message.answer("Нужно целое положительное число.")
        return
    campaign_id = (await state.get_data())["campaign_id"]
    async with session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign is None:
            await state.clear()
            await message.answer("Кампания не найдена.")
            return
        counter = campaign.counter
    if new_limit >= counter:
        async with session_factory() as session:
            await update_campaign_limit(session, campaign_id, new_limit, message.from_user.id)
        await state.clear()
        await message.answer(f"Лимит обновлён: {new_limit}.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin:campaign:{campaign_id}:limit:confirm:{new_limit}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:campaign:{campaign_id}:limit:cancel"),
    ]])
    await message.answer(
        f"Текущий счётчик кампании ({counter}) уже больше нового лимита ({new_limit}). Кампания будет "
        f"немедленно завершена с фактическим результатом {counter} из {new_limit}. Подтвердить?",
        reply_markup=keyboard,
    )


@admin_router.callback_query(F.data.regexp(r"^admin:campaign:\d+:limit:confirm:\d+$"))
async def confirm_campaign_limit_decrease(callback: CallbackQuery, state: FSMContext, session_factory) -> None:
    parts = callback.data.split(":")
    campaign_id, new_limit = int(parts[2]), int(parts[5])
    async with session_factory() as session:
        campaign = await update_campaign_limit(
            session, campaign_id, new_limit, callback.from_user.id, complete=True
        )
    await state.clear()
    await callback.message.answer("Кампания завершена." if campaign else "Кампания не найдена.")
    await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:campaign:\d+:limit:cancel$"))
async def cancel_campaign_limit_decrease(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("Изменение лимита отменено.")
    await callback.answer()


async def cancel_campaign(session, campaign_id: int, admin_telegram_id: int) -> Campaign | None:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        return None
    campaign.status = CampaignStatus.CANCELLED
    campaign.cancelled_at = datetime.now(timezone.utc)
    campaign.cancelled_by_admin_id = await get_admin_id(session, admin_telegram_id)
    await session.commit()
    return campaign


@admin_router.callback_query(F.data.regexp(r"^admin:campaign:\d+:cancel$"))
async def request_campaign_cancel(callback: CallbackQuery) -> None:
    campaign_id = int(callback.data.split(":")[2])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin:campaign:{campaign_id}:cancel:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:campaign:{campaign_id}:cancel:abort"),
    ]])
    await callback.message.answer("Отменить кампанию? Это действие необратимо.", reply_markup=keyboard)
    await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:campaign:\d+:cancel:confirm$"))
async def confirm_campaign_cancel(callback: CallbackQuery, session_factory) -> None:
    campaign_id = int(callback.data.split(":")[2])
    async with session_factory() as session:
        campaign = await cancel_campaign(session, campaign_id, callback.from_user.id)
    await callback.message.answer("Кампания отменена." if campaign else "Кампания не найдена.")
    await _edit_campaigns(callback, session_factory)
    await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:campaign:\d+:cancel:abort$"))
async def abort_campaign_cancel(callback: CallbackQuery) -> None:
    await callback.message.answer("Отмена кампании не выполнена.")
    await callback.answer()


def _partners_text(partners: list[ReferralPartner]) -> str:
    if not partners:
        return "Партнёров пока нет."
    lines = []
    for partner in partners:
        if partner.revoked_at is not None:
            status = "отозван"
        elif partner.activated_at is not None:
            status = "активен"
        else:
            status = "не активирован"
        lines.append(f"#{partner.telegram_id} — {status} — {partner.referral_code}")
    return "\n".join(lines)


def _partners_keyboard(partners: list[ReferralPartner]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="➕ Добавить партнёра", callback_data="admin:partner:add")]]
    rows.extend(
        [InlineKeyboardButton(
            text=f"🚫 Отозвать #{partner.telegram_id}",
            callback_data=f"admin:partner:{partner.telegram_id}:revoke",
        )]
        for partner in partners
        if partner.revoked_at is None
    )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _get_partners(session_factory) -> list[ReferralPartner]:
    async with session_factory() as session:
        return await list_partners(session)


async def _send_partners(message: Message, session_factory) -> None:
    partners = await _get_partners(session_factory)
    await message.answer(_partners_text(partners), reply_markup=_partners_keyboard(partners))


async def _edit_partners(callback: CallbackQuery, session_factory) -> None:
    partners = await _get_partners(session_factory)
    await callback.message.edit_text(_partners_text(partners), reply_markup=_partners_keyboard(partners))


@admin_router.callback_query(F.data == "admin:partners")
async def partners_menu(callback: CallbackQuery, session_factory) -> None:
    await _edit_partners(callback, session_factory)
    await callback.answer()


@admin_router.callback_query(F.data == "admin:partner:add")
async def add_partner(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PartnerForm.waiting_user)
    await callback.message.answer(
        "Перешли сюда сообщение от пользователя, которого нужно сделать партнёром, или пришли его "
        "числовой telegram_id (пользователь должен был хотя бы раз запустить бота)."
    )
    await callback.answer()


def _partner_telegram_id_from_message(message: Message) -> int | None:
    forwarded_user = getattr(message, "forward_from", None)
    if forwarded_user is not None:
        return forwarded_user.id

    origin = getattr(message, "forward_origin", None)
    sender_user = getattr(origin, "sender_user", None)
    if sender_user is not None:
        return sender_user.id

    return _positive_int(message.text)


@admin_router.message(StateFilter(PartnerForm.waiting_user))
async def receive_partner_user(message: Message, state: FSMContext, session_factory) -> None:
    telegram_id = _partner_telegram_id_from_message(message)
    if telegram_id is None:
        await message.answer("Не понял, пришли пересланное сообщение или числовой telegram_id.")
        return

    try:
        async with session_factory() as session:
            partner, is_new = await approve_partner(session, telegram_id, message.from_user.id)
    except LookupError as error:
        if str(error) == "user_not_started":
            await message.answer("Этот пользователь ещё не запускал бота. Попроси его нажать /start, затем повтори.")
            return
        raise

    await state.clear()
    if is_new:
        await message.answer(
            f"Партнёр #{telegram_id} добавлен. Код: `{partner.referral_code}`. "
            "Он должен написать боту слово «партнер», чтобы активировать кабинет."
        )
    else:
        await message.answer(f"Пользователь #{telegram_id} уже партнёр (код: `{partner.referral_code}`).")
    await _send_partners(message, session_factory)


def _partner_id_from_callback(data: str, suffix: str) -> int | None:
    value = data.removeprefix("admin:partner:").removesuffix(suffix)
    return int(value) if value.isdecimal() and int(value) > 0 else None


@admin_router.callback_query(F.data.startswith("admin:partner:") & F.data.endswith(":revoke"))
async def request_partner_revoke(callback: CallbackQuery) -> None:
    telegram_id = _partner_id_from_callback(callback.data, ":revoke")
    if telegram_id is None:
        await callback.answer("Некорректный идентификатор партнёра.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin:partner:{telegram_id}:revoke:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:partner:{telegram_id}:revoke:abort"),
    ]])
    await callback.message.answer("Отозвать партнёра? Это действие можно отменить повторным добавлением.", reply_markup=keyboard)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:partner:") & F.data.endswith(":revoke:confirm"))
async def confirm_partner_revoke(callback: CallbackQuery, session_factory) -> None:
    telegram_id = _partner_id_from_callback(callback.data, ":revoke:confirm")
    if telegram_id is None:
        await callback.answer("Некорректный идентификатор партнёра.", show_alert=True)
        return
    async with session_factory() as session:
        partner = await revoke_partner(session, telegram_id)
    await callback.message.answer(
        f"Партнёр #{telegram_id} отозван." if partner else "Партнёр не найден."
    )
    await _edit_partners(callback, session_factory)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:partner:") & F.data.endswith(":revoke:abort"))
async def abort_partner_revoke(callback: CallbackQuery) -> None:
    await callback.message.answer("Отзыв партнёра отменён.")
    await callback.answer()


def _codes_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Найти код", callback_data="admin:code:search")],
        [InlineKeyboardButton(text="➕ Добавить код", callback_data="admin:code:add")],
        [InlineKeyboardButton(text="📄 Шаблон для импорта", callback_data="admin:code:template")],
        [InlineKeyboardButton(text="📤 Загрузить файл", callback_data="admin:code:import")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
    ])


def _code_card_text(movie_code: MovieCode) -> str:
    return (
        f"Код: {movie_code.code}\nНазвание: {movie_code.title}\nСтатус: {movie_code.status.value}\n"
        f"Создан: {movie_code.created_at:%d.%m.%Y %H:%M}\n"
        f"Обновлён: {movie_code.updated_at:%d.%m.%Y %H:%M}"
    )


def _code_card_keyboard(movie_code: MovieCode) -> InlineKeyboardMarkup:
    code = movie_code.code
    rows = [[InlineKeyboardButton(text="✏️ Изменить название", callback_data=f"admin:code:{code}:edit")]]
    if movie_code.status is MovieCodeStatus.ACTIVE:
        rows.append([InlineKeyboardButton(text="🗑 Деактивировать", callback_data=f"admin:code:{code}:deactivate")])
    else:
        rows.append([InlineKeyboardButton(text="♻️ Восстановить", callback_data=f"admin:code:{code}:restore")])
    rows.extend([
        [InlineKeyboardButton(text="📜 История", callback_data=f"admin:code:{code}:history")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:codes")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit_code_card(callback: CallbackQuery, session_factory, code: str) -> bool:
    async with session_factory() as session:
        movie_code = await session.get(MovieCode, code)
    if movie_code is None:
        await callback.message.edit_text(f"Код «{code}» не найден.", reply_markup=_codes_keyboard())
        return False
    await callback.message.edit_text(_code_card_text(movie_code), reply_markup=_code_card_keyboard(movie_code))
    return True


@admin_router.callback_query(F.data == "admin:codes")
async def codes_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Коды", reply_markup=_codes_keyboard())
    await callback.answer()


@admin_router.callback_query(F.data == "admin:code:search")
async def search_code(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CodeSearchForm.waiting_code)
    await callback.message.answer("Пришли код для поиска.")
    await callback.answer()


@admin_router.message(StateFilter(CodeSearchForm.waiting_code))
async def receive_code_search(message: Message, state: FSMContext, session_factory) -> None:
    if message.text is None:
        await message.answer("Пришли код текстом.")
        return
    code = message.text.strip()
    async with session_factory() as session:
        movie_code = await session.get(MovieCode, code)
    await state.clear()
    if movie_code is None:
        await message.answer(f"Код «{code}» не найден.")
        return
    await message.answer(_code_card_text(movie_code), reply_markup=_code_card_keyboard(movie_code))


@admin_router.callback_query(F.data == "admin:code:add")
async def add_code(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CodeAddForm.waiting_code)
    await callback.message.answer("Пришли новый код.")
    await callback.answer()


@admin_router.callback_query(F.data == "admin:code:template")
async def send_import_template(callback: CallbackQuery) -> None:
    await callback.message.answer_document(
        BufferedInputFile(build_template_xlsx(), filename="codes_template.xlsx")
    )
    await callback.answer()


@admin_router.callback_query(F.data == "admin:code:import")
async def request_codes_import(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ImportForm.waiting_file)
    await callback.message.answer("Пришли файл `.csv` или `.xlsx` с колонками operation, code, title.")
    await callback.answer()


def _import_plan_text(plan: ImportPlan, total_rows: int) -> str:
    lines = [
        f"Импорт: {total_rows}",
        f"Будет создано: {len(plan.to_create)}",
        f"Будет восстановлено: {len(plan.to_restore)}",
        f"Без изменений: {len(plan.unchanged)}",
        f"Конфликтов: {len(plan.conflicts)}",
        f"Будет деактивировано: {len(plan.to_delete)}",
        f"Пропущено при удалении (уже неактивны/не найдены): {len(plan.delete_skipped)}",
    ]
    if plan.numeric_risk_codes:
        codes = ", ".join(plan.numeric_risk_codes[:10])
        lines.append(
            f"⚠️ У {len(plan.numeric_risk_codes)} кодов похоже потеряны ведущие нули в Excel "
            f"(стали числами): {codes}. Проверь исходный файл, если это важно."
        )
    if plan.conflicts:
        lines.append("Конфликты:")
        lines.extend(
            f'• {row.code}: было "{current_title}" -> станет "{row.title}"'
            for row, current_title in plan.conflicts[:15]
        )
    return "\n".join(lines)


@admin_router.message(StateFilter(ImportForm.waiting_file))
async def receive_codes_import(message: Message, state: FSMContext, bot: Bot, session_factory) -> None:
    if message.document is None:
        await message.answer("Пришли файл документом, не текстом.")
        return
    file_name = (message.document.file_name or "").lower()
    if file_name.endswith(".csv"):
        parser = parse_csv
    elif file_name.endswith(".xlsx"):
        parser = parse_xlsx
    else:
        await message.answer("Поддерживаются только .csv и .xlsx.")
        return
    try:
        file = await bot.get_file(message.document.file_id)
        content = (await bot.download_file(file.file_path)).read()
        rows = parser(content)
    except (UnicodeDecodeError, ValueError, OSError) as error:
        await message.answer(f"Не удалось прочитать файл: {error}")
        return
    if rows.parse_errors:
        shown = rows.parse_errors[:20]
        text = "Ошибки в файле:\n" + "\n".join(shown)
        if len(rows.parse_errors) > len(shown):
            text += f"\n...и ещё {len(rows.parse_errors) - len(shown)}"
        await state.clear()
        await message.answer(text + "\nИсправь файл и запусти импорт заново.")
        return
    async with session_factory() as session:
        plan = await build_import_plan(session, rows)
    if not plan.to_create and not plan.to_restore and not plan.to_delete and not plan.conflicts:
        await state.clear()
        await message.answer(_import_plan_text(plan, len(rows)) + "\nИзменений нет, все строки уже актуальны.")
        return
    await state.update_data(plan=plan)
    if len(plan.conflicts) > 15:
        await message.answer_document(
            BufferedInputFile(build_conflicts_report_csv(plan), filename="codes_conflicts.csv"),
            caption="Полный список конфликтов — во вложении.",
        )
    if plan.conflicts:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Заменить все конфликты", callback_data="admin:code:import:apply:replace")],
            [InlineKeyboardButton(text="🚫 Оставить все текущие", callback_data="admin:code:import:apply:keep")],
            [InlineKeyboardButton(text="❌ Отменить импорт", callback_data="admin:code:import:cancel")],
        ])
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Применить", callback_data="admin:code:import:apply:replace")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="admin:code:import:cancel")],
        ])
    await message.answer(_import_plan_text(plan, len(rows)), reply_markup=keyboard)


@admin_router.callback_query(
    StateFilter(ImportForm.waiting_file), F.data.regexp(r"^admin:code:import:apply:(replace|keep)$")
)
async def apply_codes_import(callback: CallbackQuery, state: FSMContext, session_factory) -> None:
    plan = (await state.get_data()).get("plan")
    if not isinstance(plan, ImportPlan):
        await state.clear()
        await callback.message.answer("План импорта не найден. Пришли файл заново.")
        await callback.answer()
        return
    resolution = callback.data.rsplit(":", 1)[1]
    async with session_factory() as session:
        summary = await apply_import_plan(session, plan, callback.from_user.id, resolution)
    await state.clear()
    await callback.message.answer(
        f"Импорт завершён. Создано: {summary.created}; обновлено: {summary.updated}; "
        f"деактивировано: {summary.deactivated}; пропущено: {summary.skipped}."
    )
    await callback.answer()


@admin_router.callback_query(StateFilter(ImportForm.waiting_file), F.data == "admin:code:import:cancel")
async def cancel_codes_import(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("Импорт отменён.")
    await callback.answer()


@admin_router.message(StateFilter(CodeAddForm.waiting_code))
async def receive_new_code(message: Message, state: FSMContext, session_factory) -> None:
    if message.text is None:
        await message.answer("Пришли код текстом.")
        return
    code = message.text.strip()
    async with session_factory() as session:
        movie_code = await session.get(MovieCode, code)
    if movie_code is None:
        await state.update_data(code=code, mode="create")
        await state.set_state(CodeAddForm.waiting_title)
        await message.answer(f"Пришли название для кода «{code}».")
    elif movie_code.status is MovieCodeStatus.ACTIVE:
        await state.clear()
        await message.answer(
            f"Код «{code}» уже существует и активен: «{movie_code.title}». Чтобы изменить название, "
            "найди код через поиск и используй «Изменить название»."
        )
    else:
        await state.update_data(code=code, mode="restore", old_title=movie_code.title)
        await state.set_state(CodeAddForm.waiting_title)
        await message.answer(
            f"Код «{code}» уже существует, но деактивирован (было: «{movie_code.title}»). "
            "Пришли новое название или `-`, чтобы восстановить со старым."
        )


@admin_router.message(StateFilter(CodeAddForm.waiting_title))
async def receive_new_code_title(message: Message, state: FSMContext, session_factory) -> None:
    if message.text is None:
        await message.answer("Пришли название текстом.")
        return
    data = await state.get_data()
    title = message.text.strip()
    try:
        async with session_factory() as session:
            if data["mode"] == "create":
                await create_code(session, data["code"], title, message.from_user.id)
                response = f"Код «{data['code']}» добавлен с названием «{title}»."
            else:
                await restore_code(session, data["code"], message.from_user.id, None if title == "-" else title)
                response = f"Код «{data['code']}» восстановлен."
    except (LookupError, ValueError):
        response = "Состояние кода изменилось. Попробуй найти его заново."
    await state.clear()
    await message.answer(response)


def _code_from_callback(data: str, suffix: str) -> str:
    return data.removeprefix("admin:code:").removesuffix(suffix)


@admin_router.callback_query(F.data.startswith("admin:code:") & F.data.endswith(":edit"))
async def request_code_title_edit(callback: CallbackQuery, state: FSMContext, session_factory) -> None:
    code = _code_from_callback(callback.data, ":edit")
    async with session_factory() as session:
        movie_code = await session.get(MovieCode, code)
    if movie_code is None:
        await callback.answer("Код не найден.", show_alert=True)
        return
    await state.set_state(CodeEditForm.waiting_new_title)
    await state.update_data(code=code, old_title=movie_code.title)
    await callback.message.answer(f"Текущее название: «{movie_code.title}». Пришли новое название.")
    await callback.answer()


@admin_router.message(StateFilter(CodeEditForm.waiting_new_title))
async def receive_code_title_edit(message: Message, state: FSMContext) -> None:
    if message.text is None:
        await message.answer("Пришли название текстом.")
        return
    pending_title = message.text.strip()
    data = await state.get_data()
    await state.update_data(pending_title=pending_title)
    code = data["code"]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin:code:{code}:edit:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:code:{code}:edit:cancel"),
    ]])
    await message.answer(
        f"Заменить «{data['old_title']}» на «{pending_title}» для кода «{code}»?", reply_markup=keyboard
    )


@admin_router.callback_query(StateFilter(CodeEditForm.waiting_new_title), F.data.startswith("admin:code:") & F.data.endswith(":edit:confirm"))
async def confirm_code_title_edit(callback: CallbackQuery, state: FSMContext, session_factory) -> None:
    data = await state.get_data()
    try:
        async with session_factory() as session:
            await update_title(session, data["code"], data["pending_title"], callback.from_user.id)
        response = "Название обновлено."
    except (LookupError, ValueError):
        response = "Код не найден или его состояние изменилось."
    await state.clear()
    await callback.message.answer(response)
    await callback.answer()


@admin_router.callback_query(StateFilter(CodeEditForm.waiting_new_title), F.data.startswith("admin:code:") & F.data.endswith(":edit:cancel"))
async def cancel_code_title_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("Изменение отменено.")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:code:") & F.data.endswith(":deactivate"))
async def request_code_deactivation(callback: CallbackQuery, session_factory) -> None:
    code = _code_from_callback(callback.data, ":deactivate")
    async with session_factory() as session:
        movie_code = await session.get(MovieCode, code)
    if movie_code is None:
        await callback.answer("Код не найден.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin:code:{code}:deactivate:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:code:{code}:deactivate:cancel"),
    ]])
    await callback.message.answer(
        f"Код {code} связан с названием «{movie_code.title}». Старые публикации могут продолжать\n"
        "использовать этот код. Вы уверены, что хотите его удалить?", reply_markup=keyboard
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:code:") & F.data.endswith(":deactivate:confirm"))
async def confirm_code_deactivation(callback: CallbackQuery, session_factory) -> None:
    code = _code_from_callback(callback.data, ":deactivate:confirm")
    try:
        async with session_factory() as session:
            await deactivate_code(session, code, callback.from_user.id)
        response = "Код деактивирован."
    except LookupError:
        response = "Код не найден."
    await callback.message.answer(response)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:code:") & F.data.endswith(":deactivate:cancel"))
async def cancel_code_deactivation(callback: CallbackQuery) -> None:
    await callback.message.answer("Деактивация отменена.")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:code:") & F.data.endswith(":restore"))
async def request_code_restore(callback: CallbackQuery, state: FSMContext, session_factory) -> None:
    code = _code_from_callback(callback.data, ":restore")
    async with session_factory() as session:
        movie_code = await session.get(MovieCode, code)
    if movie_code is None:
        await callback.answer("Код не найден.", show_alert=True)
        return
    await state.set_state(CodeRestoreForm.waiting_title)
    await state.update_data(code=code)
    await callback.message.answer(
        f"Код «{code}» деактивирован (было: «{movie_code.title}»). "
        "Пришли новое название или `-`, чтобы восстановить со старым."
    )
    await callback.answer()


@admin_router.message(StateFilter(CodeRestoreForm.waiting_title))
async def receive_code_restore_title(message: Message, state: FSMContext, session_factory) -> None:
    if message.text is None:
        await message.answer("Пришли название текстом.")
        return
    code = (await state.get_data())["code"]
    title = message.text.strip()
    try:
        async with session_factory() as session:
            await restore_code(session, code, message.from_user.id, None if title == "-" else title)
        response = f"Код «{code}» восстановлен."
    except (LookupError, ValueError):
        response = "Код не найден или уже активен."
    await state.clear()
    await message.answer(response)


@admin_router.callback_query(F.data.startswith("admin:code:") & F.data.endswith(":history"))
async def code_history(callback: CallbackQuery, session_factory) -> None:
    code = _code_from_callback(callback.data, ":history")
    async with session_factory() as session:
        history = await get_code_history(session, code)
    if history:
        lines = []
        for entry in history:
            suffix = f": {entry.old_title} -> {entry.new_title}" if entry.old_title is not None and entry.new_title is not None else ""
            lines.append(
                f"{entry.changed_at:%d.%m.%Y %H:%M} — {entry.action.value} — {entry.source.value} — "
                f"админ #{entry.changed_by_admin_id}{suffix}"
            )
        text = "\n".join(lines)
    else:
        text = "История пуста."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin:code:{code}")
    ]]))
    await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:code:.+$"))
async def code_card(callback: CallbackQuery, session_factory) -> None:
    code = callback.data.removeprefix("admin:code:")
    await _edit_code_card(callback, session_factory, code)
    await callback.answer()
