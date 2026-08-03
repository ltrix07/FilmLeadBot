from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from math import ceil
from datetime import datetime, timezone
from collections.abc import Coroutine

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    KeyboardButtonRequestUsers,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import joinedload

from app.bot.filters import IsAdmin
from app.config import Settings
from app.db.models import (
    Broadcast,
    BroadcastStatus,
    Campaign,
    CampaignLimitHistory,
    CampaignStatus,
    MovieCode,
    MovieCodeStatus,
    ReferralPartner,
    Sponsor,
    SponsorType,
    WelcomeMessage,
    User,
    Admin,
)
from app.services.admins import (
    add_admin,
    bot_status_allows_access,
    can_manage_admins,
    can_manage_payouts,
    get_admin,
    get_admin_id,
    list_admins,
    queue_pending_admin_grant,
    revoke_admin,
)
from app.services.broadcasts import (
    cancel_scheduled_broadcast,
    create_broadcast,
    get_broadcast_recipients,
    run_broadcast,
)
from app.services.campaign_jobs import attempt_resume_campaign
from app.services.movie_codes import (
    build_all_codes_export_xlsx,
    create_code,
    deactivate_code,
    get_code_history,
    restore_code,
    update_title,
)
from app.services.partner_balance import (
    add_partner_balance_adjustment,
    get_partner_balance,
    zero_out_partner_balance,
)
from app.services.partners import (
    approve_partner,
    count_partners,
    format_partner_label,
    format_user_label,
    format_partner_stats_text,
    get_partner_stats,
    list_partners,
    queue_pending_partner_grant,
    revoke_partner,
)
from app.services.settings import (
    get_subscription_price,
    get_welcome_message,
    set_subscription_price,
    set_welcome_message,
)
from app.services.sponsors import (
    archive_sponsor,
    count_sponsors,
    list_sponsors as list_sponsors_service,
    unarchive_sponsor,
)
from app.services.stats import get_overview_stats, get_top_codes, get_top_partners
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
    waiting_request_mode = State()
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


class PricingForm(StatesGroup):
    waiting_price = State()


class AdminGrantForm(StatesGroup):
    waiting_user = State()


class PartnerBalanceForm(StatesGroup):
    waiting_amount = State()
    waiting_title = State()


class BroadcastForm(StatesGroup):
    waiting_content = State()
    waiting_schedule = State()


class WelcomeForm(StatesGroup):
    waiting_content = State()


_sponsor_picker_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(
            text="📢 Выбрать канал",
            request_chat=KeyboardButtonRequestChat(
                request_id=1,
                chat_is_channel=True,
                request_title=True,
                request_username=True,
            ),
        )],
        [KeyboardButton(
            text="👥 Выбрать группу",
            request_chat=KeyboardButtonRequestChat(
                request_id=2,
                chat_is_channel=False,
                request_title=True,
                request_username=True,
            ),
        )],
        [KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

_partner_picker_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(
            text="👤 Выбрать пользователя",
            request_users=KeyboardButtonRequestUsers(
                request_id=1,
                user_is_bot=False,
                max_quantity=1,
            ),
        )],
        [KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def _admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📡 Спонсоры", callback_data="admin:sponsors")],
            [InlineKeyboardButton(text="📢 Кампании", callback_data="admin:campaigns")],
            [InlineKeyboardButton(text="🎬 Коды", callback_data="admin:codes")],
            [InlineKeyboardButton(text="👥 Рефоводы", callback_data="admin:partners")],
            [InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast")],
            [InlineKeyboardButton(text="✏️ Приветствие", callback_data="admin:welcome")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton(text="👤 Администраторы", callback_data="admin:admins")],
        ]
    )


def _with_cancel(rows: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
    rows = list(rows or [])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_SPONSOR_TYPE_LABELS = {
    SponsorType.CHANNEL: "канал",
    SponsorType.GROUP: "группа",
    SponsorType.SUPERGROUP: "группа",
}


_SPONSORS_PER_PAGE = 10


def _sponsor_type_label(sponsor: Sponsor) -> str:
    return _SPONSOR_TYPE_LABELS.get(sponsor.type, sponsor.type.value)


def _sponsors_menu_text(page: int, total_pages: int, *, archived: bool) -> str:
    if total_pages == 0:
        return "Архив пуст." if archived else "Спонсоров пока нет."
    heading = "Архив спонсоров" if archived else "Спонсоры"
    return f"{heading} (стр. {page + 1}/{total_pages}):"


async def _get_sponsors_page(session_factory, page: int, *, archived: bool) -> tuple[list[Sponsor], int]:
    async with session_factory() as session:
        total = await count_sponsors(session, archived=archived)
        total_pages = ceil(total / _SPONSORS_PER_PAGE)
        actual_page = max(0, min(page, total_pages - 1)) if total_pages else 0
        sponsors = await list_sponsors_service(
            session, archived=archived, limit=_SPONSORS_PER_PAGE,
            offset=actual_page * _SPONSORS_PER_PAGE,
        )
    return sponsors, total_pages


def _sponsors_page_keyboard(
    sponsors: list[Sponsor], page: int, total_pages: int, *, archived: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if not archived:
        rows.append([InlineKeyboardButton(text="➕ Добавить спонсора", callback_data="admin:sponsor:add")])
    for sponsor in sponsors:
        status = "✅" if sponsor.bot_has_access else "⚠️"
        rows.append([InlineKeyboardButton(
            text=f"{status} {sponsor.title} ({_sponsor_type_label(sponsor)})",
            callback_data=f"admin:sponsor:{sponsor.chat_id}:card",
        )])
    if total_pages > 1:
        navigation = []
        if page > 0:
            prefix = "admin:sponsors:archived" if archived else "admin:sponsors:page"
            navigation.append(InlineKeyboardButton(text="◀️ Пред", callback_data=f"{prefix}:{page - 1}"))
        if page + 1 < total_pages:
            prefix = "admin:sponsors:archived" if archived else "admin:sponsors:page"
            navigation.append(InlineKeyboardButton(text="▶️ След", callback_data=f"{prefix}:{page + 1}"))
        rows.append(navigation)
    if not archived:
        rows.append([InlineKeyboardButton(text="📦 Архив", callback_data="admin:sponsors:archived:0")])
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")])
    else:
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:sponsors:page:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit_sponsors_page(callback: CallbackQuery, session_factory, page: int, *, archived: bool) -> None:
    sponsors, total_pages = await _get_sponsors_page(session_factory, page, archived=archived)
    actual_page = max(0, min(page, total_pages - 1)) if total_pages else 0
    await callback.message.edit_text(
        _sponsors_menu_text(actual_page, total_pages, archived=archived),
        reply_markup=_sponsors_page_keyboard(sponsors, actual_page, total_pages, archived=archived),
    )


async def _send_sponsors(message: Message, session_factory) -> None:
    sponsors, total_pages = await _get_sponsors_page(session_factory, 0, archived=False)
    await message.answer(
        _sponsors_menu_text(0, total_pages, archived=False),
        reply_markup=_sponsors_page_keyboard(sponsors, 0, total_pages, archived=False),
    )


async def _send_admin_menu(message: Message) -> None:
    await message.answer("Админ-панель:", reply_markup=_admin_menu_keyboard())


@admin_router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    await _send_admin_menu(message)


@admin_router.message(F.text.strip().casefold() == "админка")
async def text_admin_menu(message: Message) -> None:
    await _send_admin_menu(message)


@admin_router.callback_query(F.data == "admin:menu")
async def admin_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Админ-панель:", reply_markup=_admin_menu_keyboard())
    await callback.answer()


@admin_router.callback_query(F.data == "admin:stats")
async def show_stats(callback: CallbackQuery, session_factory) -> None:
    async with session_factory() as session:
        overview = await get_overview_stats(session)
        partners = await get_top_partners(session)
        codes = await get_top_codes(session)

    partner_lines = [
        f"#{partner.telegram_id} — {confirmed} подтверждено (всего приведено {started})"
        for partner, started, confirmed in partners
    ]
    code_lines = [
        f"{code.code} — {code.title} — {code.lookup_count} обращений" for code in codes
    ]
    text = (
        "📊 Статистика\n\n"
        f"Пользователи: {overview.users_total} (заблокировали бота: {overview.users_blocked})\n"
        f"Спонсоры: {overview.sponsors_total} (доступны боту: {overview.sponsors_with_access})\n"
        f"Кампании: {overview.campaigns_total} (активны сейчас: {overview.campaigns_active})\n"
        f"Коды: {overview.codes_active} активных, {overview.codes_inactive} деактивированных\n"
        f"Рефоводы: {overview.partners_total} (активны: {overview.partners_active})\n\n"
        "🏆 Топ рефоводов:\n"
        f"{'\n'.join(partner_lines) if partner_lines else 'Пока нет данных.'}\n\n"
        "🎬 Популярные коды:\n"
        f"{'\n'.join(code_lines) if code_lines else 'Пока нет обращений.'}"
    )
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")
        ]]),
    )
    await callback.answer()


def _broadcast_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Новая рассылка", callback_data="admin:broadcast:new")],
        [InlineKeyboardButton(text="🕒 Запланированные", callback_data="admin:broadcast:scheduled")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
    ])


def _scheduled_broadcasts_text(broadcasts: list[Broadcast]) -> str:
    if not broadcasts:
        return "Запланированных рассылок нет."
    return "\n".join(
        f"#{broadcast.id} — {broadcast.content} — {broadcast.scheduled_at:%d.%m.%Y %H:%M}"
        for broadcast in broadcasts
    )


def _scheduled_broadcasts_keyboard(broadcasts: list[Broadcast]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"🚫 Отменить #{broadcast.id}",
            callback_data=f"admin:broadcast:{broadcast.id}:cancel_scheduled",
        )]
        for broadcast in broadcasts
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:broadcast")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _get_scheduled_broadcasts(session_factory) -> list[Broadcast]:
    async with session_factory() as session:
        return list((await session.scalars(
            select(Broadcast)
            .where(Broadcast.status == BroadcastStatus.SCHEDULED)
            .order_by(Broadcast.scheduled_at)
        )).all())


async def _edit_scheduled_broadcasts(callback: CallbackQuery, session_factory) -> None:
    broadcasts = await _get_scheduled_broadcasts(session_factory)
    await callback.message.edit_text(
        _scheduled_broadcasts_text(broadcasts),
        reply_markup=_scheduled_broadcasts_keyboard(broadcasts),
    )


@admin_router.callback_query(F.data == "admin:broadcast")
async def broadcast_menu(callback: CallbackQuery, session_factory) -> None:
    await callback.message.edit_text("Рассылка", reply_markup=_broadcast_menu_keyboard())
    await callback.answer()


def _welcome_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin:welcome:edit")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")],
    ])


def _welcome_menu_text(welcome: WelcomeMessage | None, settings: Settings) -> str:
    if welcome is not None:
        return f"Текущее приветствие (превью): {welcome.preview}"
    return (
        "Приветствие не настроено, используется текст по умолчанию:\n\n"
        f"{settings.welcome_message}"
    )


async def _get_welcome_message(session_factory) -> WelcomeMessage | None:
    async with session_factory() as session:
        return await get_welcome_message(session)


async def _send_welcome_menu(message: Message, session_factory, settings: Settings) -> None:
    welcome = await _get_welcome_message(session_factory)
    await message.answer(_welcome_menu_text(welcome, settings), reply_markup=_welcome_menu_keyboard())


@admin_router.callback_query(F.data == "admin:welcome")
async def welcome_menu(callback: CallbackQuery, session_factory, settings: Settings) -> None:
    welcome = await _get_welcome_message(session_factory)
    await callback.message.edit_text(
        _welcome_menu_text(welcome, settings), reply_markup=_welcome_menu_keyboard()
    )
    await callback.answer()


@admin_router.callback_query(F.data == "admin:welcome:edit")
async def request_welcome_content(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(WelcomeForm.waiting_content)
    await callback.message.answer(
        "Пришли новое приветственное сообщение (текст, фото с подписью — что угодно).",
        reply_markup=_with_cancel(),
    )
    await callback.answer()


@admin_router.message(StateFilter(WelcomeForm.waiting_content))
async def receive_welcome_content(
    message: Message, state: FSMContext, session_factory, settings: Settings
) -> None:
    preview = (message.html_text or message.caption or "<медиа без подписи>")[:200]
    async with session_factory() as session:
        await set_welcome_message(session, message.chat.id, message.message_id, preview)
    await state.clear()
    await message.answer("Приветствие обновлено.")
    await _send_welcome_menu(message, session_factory, settings)


@admin_router.callback_query(F.data == "admin:broadcast:new")
async def request_broadcast_content(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastForm.waiting_content)
    await callback.message.answer(
        "Пришли сообщение, которое нужно разослать всем пользователям "
        "(текст, фото с подписью — что угодно).",
        reply_markup=_with_cancel(),
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
        await _send_admin_menu(message)
        return
    await state.update_data(
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        preview=preview,
        recipient_count=len(recipients),
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Отправить сейчас", callback_data="admin:broadcast:confirm"),
        InlineKeyboardButton(text="🕒 Запланировать", callback_data="admin:broadcast:schedule"),
    ], [
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
    await _send_admin_menu(callback.message)
    await callback.answer()


@admin_router.callback_query(
    StateFilter(BroadcastForm.waiting_content), F.data == "admin:broadcast:schedule"
)
async def request_broadcast_schedule(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastForm.waiting_schedule)
    await callback.message.answer(
        "На когда запланировать рассылку? Пришли дату и время в формате "
        "`ДД.ММ.ГГГГ ЧЧ:ММ` (UTC).",
        reply_markup=_with_cancel(),
    )
    await callback.answer()


@admin_router.message(StateFilter(BroadcastForm.waiting_schedule))
async def receive_broadcast_schedule(message: Message, state: FSMContext, session_factory) -> None:
    try:
        scheduled_at = datetime.strptime(
            (message.text or "").strip(), "%d.%m.%Y %H:%M"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        await message.answer("Не понял формат даты, пример: 25.12.2026 18:00", reply_markup=_with_cancel())
        return
    if scheduled_at <= datetime.now(timezone.utc):
        await message.answer("Дата должна быть в будущем.", reply_markup=_with_cancel())
        return

    data = await state.get_data()
    async with session_factory() as session:
        recipients = await get_broadcast_recipients(session)
        await create_broadcast(
            session,
            message.from_user.id,
            data["source_chat_id"],
            data["source_message_id"],
            data["preview"],
            status=BroadcastStatus.SCHEDULED,
            scheduled_at=scheduled_at,
        )
    await state.clear()
    await message.answer(
        f"Рассылка запланирована на {scheduled_at:%d.%m.%Y %H:%M} UTC для "
        f"{len(recipients)} пользователей."
    )
    await _send_admin_menu(message)


@admin_router.callback_query(
    StateFilter(BroadcastForm.waiting_content), F.data == "admin:broadcast:cancel"
)
async def cancel_broadcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("Рассылка отменена.")
    await _send_admin_menu(callback.message)
    await callback.answer()


@admin_router.callback_query(F.data == "admin:broadcast:scheduled")
async def list_scheduled_broadcasts(callback: CallbackQuery, session_factory) -> None:
    await _edit_scheduled_broadcasts(callback, session_factory)
    await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:broadcast:\d+:cancel_scheduled$"))
async def cancel_scheduled_broadcast_handler(callback: CallbackQuery, session_factory) -> None:
    broadcast_id = int(callback.data.split(":")[2])
    async with session_factory() as session:
        broadcast = await cancel_scheduled_broadcast(session, broadcast_id)
    if broadcast is None:
        await callback.answer("Рассылка уже обрабатывается или отменена.", show_alert=True)
        return
    await _edit_scheduled_broadcasts(callback, session_factory)
    await callback.answer()


@admin_router.callback_query(F.data == "admin:sponsors")
async def list_sponsors(callback: CallbackQuery, session_factory) -> None:
    await _edit_sponsors_page(callback, session_factory, 0, archived=False)
    await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:sponsors:page:\d+$"))
async def sponsors_page(callback: CallbackQuery, session_factory) -> None:
    await _edit_sponsors_page(callback, session_factory, int(callback.data.rsplit(":", 1)[1]), archived=False)
    await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:sponsors:archived:\d+$"))
async def archived_sponsors_page(callback: CallbackQuery, session_factory) -> None:
    await _edit_sponsors_page(callback, session_factory, int(callback.data.rsplit(":", 1)[1]), archived=True)
    await callback.answer()


def _sponsor_card_text(sponsor: Sponsor) -> str:
    lines = [
        f"{sponsor.title} ({_sponsor_type_label(sponsor)})",
        f"Статус доступа: {'✅ доступен' if sponsor.bot_has_access else '⚠️ нет доступа'}",
    ]
    if sponsor.own_channel:
        lines.append("· своё")
    if sponsor.request_mode:
        lines.append("· заявки")
    if sponsor.archived_at is not None:
        lines.append("· в архиве")
    return "\n".join(lines)


def _sponsor_card_keyboard(sponsor: Sponsor) -> InlineKeyboardMarkup:
    archived = sponsor.archived_at is not None
    rows = [[
        InlineKeyboardButton(
            text=f"{'✅' if sponsor.own_channel else '⬜'} Своё",
            callback_data=f"admin:sponsor:{sponsor.chat_id}:toggle_own",
        ),
        InlineKeyboardButton(
            text=f"{'✅' if sponsor.request_mode else '⬜'} Заявки",
            callback_data=f"admin:sponsor:{sponsor.chat_id}:toggle_request_mode",
        ),
    ]]
    rows.append([InlineKeyboardButton(
        text="♻️ Разархивировать" if archived else "📦 Архивировать",
        callback_data=f"admin:sponsor:{sponsor.chat_id}:{'unarchive' if archived else 'archive'}",
    )])
    rows.append([InlineKeyboardButton(
        text="⬅️ К списку",
        callback_data="admin:sponsors:archived:0" if archived else "admin:sponsors:page:0",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit_sponsor_card(callback: CallbackQuery, session_factory, chat_id: int) -> bool:
    async with session_factory() as session:
        sponsor = await session.get(Sponsor, chat_id)
    if sponsor is None:
        await callback.answer("Спонсор не найден.", show_alert=True)
        return False
    await callback.message.edit_text(_sponsor_card_text(sponsor), reply_markup=_sponsor_card_keyboard(sponsor))
    return True


@admin_router.callback_query(F.data.regexp(r"^admin:sponsor:-?\d+:card$"))
async def sponsor_card(callback: CallbackQuery, session_factory) -> None:
    if await _edit_sponsor_card(callback, session_factory, int(callback.data.split(":")[2])):
        await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:sponsor:-?\d+:toggle_own$"))
async def toggle_sponsor_own_channel(callback: CallbackQuery, session_factory) -> None:
    chat_id = int(callback.data.split(":")[2])
    async with session_factory() as session:
        await session.execute(
            update(Sponsor)
            .where(Sponsor.chat_id == chat_id)
            .values(own_channel=~Sponsor.own_channel)
        )
        await session.commit()
    if await _edit_sponsor_card(callback, session_factory, chat_id):
        await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:sponsor:-?\d+:toggle_request_mode$"))
async def toggle_sponsor_request_mode(callback: CallbackQuery, session_factory) -> None:
    chat_id = int(callback.data.split(":")[2])
    async with session_factory() as session:
        await session.execute(
            update(Sponsor)
            .where(Sponsor.chat_id == chat_id)
            .values(request_mode=~Sponsor.request_mode)
        )
        await session.commit()
    if await _edit_sponsor_card(callback, session_factory, chat_id):
        await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:sponsor:-?\d+:archive$"))
async def archive_sponsor_handler(callback: CallbackQuery, session_factory) -> None:
    chat_id = int(callback.data.split(":")[2])
    async with session_factory() as session:
        sponsor = await archive_sponsor(session, chat_id)
    if sponsor is None:
        await callback.answer("Спонсор не найден.", show_alert=True)
        return
    await callback.message.edit_text(_sponsor_card_text(sponsor), reply_markup=_sponsor_card_keyboard(sponsor))
    await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:sponsor:-?\d+:unarchive$"))
async def unarchive_sponsor_handler(callback: CallbackQuery, session_factory) -> None:
    chat_id = int(callback.data.split(":")[2])
    async with session_factory() as session:
        sponsor = await unarchive_sponsor(session, chat_id)
    if sponsor is None:
        await callback.answer("Спонсор не найден.", show_alert=True)
        return
    await callback.message.edit_text(_sponsor_card_text(sponsor), reply_markup=_sponsor_card_keyboard(sponsor))
    await callback.answer()


@admin_router.callback_query(F.data == "admin:sponsor:add")
async def add_sponsor(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SponsorForm.waiting_chat)
    await callback.message.answer(
        "Выбери канал/группу кнопкой ниже, перешли сюда любое сообщение из чата, "
        "или пришли числовой chat_id вручную.",
        reply_markup=_sponsor_picker_keyboard,
    )
    await callback.message.answer("Или отмени добавление.", reply_markup=_with_cancel())
    await callback.answer()


@admin_router.callback_query(F.data == "admin:cancel")
async def cancel_admin_input(callback: CallbackQuery, state: FSMContext) -> None:
    await _cancel_admin_input_message(callback.message, state)
    await callback.answer()


@admin_router.message(Command("cancel"), StateFilter("*"))
async def cancel_admin_input_command(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        return
    await _cancel_admin_input_message(message, state)


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


def _is_cancel_text(message: Message) -> bool:
    return message.text == "❌ Отмена"


async def _cancel_admin_input_message(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Отменено.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await _send_admin_menu(message)


async def _process_sponsor_chat(chat, message: Message, state: FSMContext, bot: Bot) -> None:
    if chat is None:
        await message.answer("Не понял, пришли пересланное сообщение или числовой chat_id.", reply_markup=_with_cancel())
        return

    chat_type = _sponsor_type(chat.type)
    if chat_type is None:
        await message.answer("Не понял, пришли пересланное сообщение или числовой chat_id.", reply_markup=_with_cancel())
        return

    member = await bot.get_chat_member(chat.id, bot.id)
    if not bot_status_allows_access(chat_type, member.status):
        channel_note = ", и для канала — с правами администратора" if chat_type is SponsorType.CHANNEL else ""
        await message.answer(
            "Бот должен быть добавлен в этот канал/группу"
            f"{channel_note}, прежде чем его можно добавить спонсором.",
            reply_markup=_with_cancel(),
        )
        return

    await state.update_data(
        chat_id=chat.id,
        title=chat.title or chat.username or str(chat.id),
        username=chat.username,
        type=chat_type.value,
    )
    await state.set_state(SponsorForm.waiting_request_mode)
    await message.answer(
        "Это закрытый канал с заявками на вступление (Join Requests)?",
        reply_markup=_with_cancel(
            [
                [
                    InlineKeyboardButton(
                        text="✅ Да, с заявками",
                        callback_data="admin:sponsor:request_mode:yes",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Нет, обычный",
                        callback_data="admin:sponsor:request_mode:no",
                    ),
                ],
            ]
        ),
    )


async def _request_sponsor_invite_link(message: Message, state: FSMContext) -> None:
    await state.set_state(SponsorForm.waiting_invite_link)
    await message.answer(
        "Пришли постоянную инвайт-ссылку на этот чат (или `-`, если у канала "
        "есть публичный username и ссылка не нужна).",
        reply_markup=_with_cancel(),
    )


@admin_router.callback_query(
    StateFilter(SponsorForm.waiting_request_mode),
    F.data.regexp(r"^admin:sponsor:request_mode:(yes|no)$"),
)
async def choose_sponsor_request_mode(callback: CallbackQuery, state: FSMContext) -> None:
    request_mode = callback.data.endswith(":yes")
    await state.update_data(request_mode=request_mode)
    if request_mode:
        await callback.message.answer(
            "Чтобы бот видел заявки на вступление, добавь его администратором в этот "
            "канал/группу с правом «Приглашение пользователей по ссылке» (управление "
            "заявками на вступление)."
        )
    await _request_sponsor_invite_link(callback.message, state)
    await callback.answer()


@admin_router.message(StateFilter(SponsorForm.waiting_chat), F.chat_shared)
async def receive_sponsor_chat_shared(message: Message, state: FSMContext, bot: Bot) -> None:
    chat = await bot.get_chat(message.chat_shared.chat_id)
    await _process_sponsor_chat(chat, message, state, bot)


@admin_router.message(StateFilter(SponsorForm.waiting_chat))
async def receive_sponsor_chat(message: Message, state: FSMContext, bot: Bot) -> None:
    if _is_cancel_text(message):
        await _cancel_admin_input_message(message, state)
        return

    chat = await _chat_from_message(message, bot)
    await _process_sponsor_chat(chat, message, state, bot)


@admin_router.message(StateFilter(SponsorForm.waiting_invite_link))
async def receive_invite_link(message: Message, state: FSMContext, session_factory) -> None:
    if _is_cancel_text(message):
        await _cancel_admin_input_message(message, state)
        return

    if message.text is None:
        await message.answer("Пришли инвайт-ссылку текстом или `-`.")
        return

    data = await state.get_data()
    if message.text == "-" and not data["username"]:
        await message.answer(
            "У этого чата нет публичного username, поэтому нужна постоянная инвайт-ссылка.",
            reply_markup=_with_cancel(),
        )
        return
    invite_link = None if message.text == "-" else message.text
    statement = insert(Sponsor).values(
        chat_id=data["chat_id"],
        title=data["title"],
        username=data["username"],
        type=data["type"],
        invite_link=invite_link,
        bot_has_access=True,
        request_mode=data.get("request_mode", False),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[Sponsor.chat_id],
        set_={
            "title": statement.excluded.title,
            "username": statement.excluded.username,
            "type": statement.excluded.type,
            "invite_link": statement.excluded.invite_link,
            "bot_has_access": True,
            "request_mode": statement.excluded.request_mode,
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
    async with session_factory() as session:
        sponsors = await list_sponsors_service(session, archived=False)
    if not sponsors:
        await callback.answer("Сначала добавь хотя бы одного спонсора.", show_alert=True)
        return
    rows = [
        [InlineKeyboardButton(text=sponsor.title, callback_data=f"admin:campaign:add:sponsor:{sponsor.chat_id}")]
        for sponsor in sponsors
    ]
    await state.set_state(CampaignForm.waiting_sponsor)
    await callback.message.answer("Выбери спонсора для кампании.", reply_markup=_with_cancel(rows))
    await callback.answer()


@admin_router.callback_query(
    StateFilter(CampaignForm.waiting_sponsor), F.data.regexp(r"^admin:campaign:add:sponsor:-?\d+$")
)
async def choose_campaign_sponsor(callback: CallbackQuery, state: FSMContext) -> None:
    sponsor_chat_id = int(callback.data.rsplit(":", 1)[1])
    await state.update_data(sponsor_chat_id=sponsor_chat_id)
    await state.set_state(CampaignForm.waiting_limit)
    await callback.message.answer("Укажи лимит кампании (целое число).", reply_markup=_with_cancel())
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
        await message.answer("Нужно целое положительное число.", reply_markup=_with_cancel())
        return
    await state.update_data(limit=limit)
    await state.set_state(CampaignForm.waiting_schedule)
    await message.answer(
        "Когда запустить кампанию? Пришли `сейчас` для немедленного запуска или дату и время "
        "в формате `ДД.ММ.ГГГГ ЧЧ:ММ` (UTC) для отложенного.",
        reply_markup=_with_cancel(),
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
            await message.answer("Не понял формат даты, пример: 25.12.2026 18:00", reply_markup=_with_cancel())
            return
        if scheduled_at <= now:
            await message.answer("Дата должна быть в будущем.", reply_markup=_with_cancel())
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
    await callback.message.answer("Укажи новый лимит (целое положительное число).", reply_markup=_with_cancel())
    await callback.answer()


@admin_router.message(StateFilter(LimitForm.waiting_new_limit))
async def receive_new_campaign_limit(message: Message, state: FSMContext, session_factory) -> None:
    new_limit = _positive_int(message.text)
    if new_limit is None:
        await message.answer("Нужно целое положительное число.", reply_markup=_with_cancel())
        return
    campaign_id = (await state.get_data())["campaign_id"]
    async with session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
        if campaign is None:
            await state.clear()
            await message.answer("Кампания не найдена.")
            await _send_admin_menu(message)
            return
        counter = campaign.counter
    if new_limit >= counter:
        async with session_factory() as session:
            await update_campaign_limit(session, campaign_id, new_limit, message.from_user.id)
        await state.clear()
        await message.answer(f"Лимит обновлён: {new_limit}.")
        await _send_admin_menu(message)
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
    await _send_admin_menu(callback.message)
    await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:campaign:\d+:limit:cancel$"))
async def cancel_campaign_limit_decrease(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("Изменение лимита отменено.")
    await _send_admin_menu(callback.message)
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


def _admin_permissions_text(admin: Admin) -> tuple[str, str]:
    if admin.revoked_at is not None:
        return "🚫", "отозван"
    if admin.role == "owner":
        return "👑", "owner (полные права)"
    return "✅", (
        "admin "
        f"(админы: {'да' if admin.can_manage_admins else 'нет'}, "
        f"выплаты: {'да' if admin.can_manage_payouts else 'нет'})"
    )


async def _get_admins_data(session_factory) -> tuple[list[Admin], dict[int, User]]:
    async with session_factory() as session:
        admins = await list_admins(session)
        ids = [admin.telegram_id for admin in admins]
        users = [] if not ids else list(await session.scalars(
            select(User).where(User.telegram_id.in_(ids))
        ))
    return admins, {user.telegram_id: user for user in users}


def _admins_text(admins: list[Admin], users_by_id: dict[int, User]) -> str:
    if not admins:
        return "Администраторов пока нет."
    lines = []
    for admin in admins:
        emoji, details = _admin_permissions_text(admin)
        label = format_user_label(admin.telegram_id, users_by_id.get(admin.telegram_id))
        lines.append(f"{emoji} {label} — {details}")
    return "Администраторы:\n\n" + "\n".join(lines)


def _admins_keyboard(
    admins: list[Admin], users_by_id: dict[int, User], *, viewer_can_manage: bool
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if viewer_can_manage:
        rows.append([InlineKeyboardButton(text="➕ Добавить администратора", callback_data="admin:admins:add")])
        rows.extend([
            [InlineKeyboardButton(
                text=format_user_label(admin.telegram_id, users_by_id.get(admin.telegram_id)),
                callback_data=f"admin:admins:{admin.telegram_id}:card",
            )]
            for admin in admins
            if admin.role != "owner" and admin.revoked_at is None
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit_admins_list(callback: CallbackQuery, session_factory, viewer: Admin) -> None:
    admins, users_by_id = await _get_admins_data(session_factory)
    await callback.message.edit_text(
        _admins_text(admins, users_by_id),
        reply_markup=_admins_keyboard(
            admins, users_by_id, viewer_can_manage=can_manage_admins(viewer)
        ),
    )


@admin_router.callback_query(F.data == "admin:admins")
async def admins_menu(callback: CallbackQuery, session_factory) -> None:
    async with session_factory() as session:
        viewer = await get_admin(session, callback.from_user.id)
    if viewer is None:
        await callback.answer("Администратор не найден.", show_alert=True)
        return
    await _edit_admins_list(callback, session_factory, viewer)
    await callback.answer()


async def _admin_card(callback: CallbackQuery, session_factory, telegram_id: int) -> bool:
    async with session_factory() as session:
        admin = await get_admin(session, telegram_id)
        user = await session.get(User, telegram_id)
    if admin is None or admin.role == "owner" or admin.revoked_at is not None:
        await callback.answer("Администратор не найден или недоступен.", show_alert=True)
        return False
    label = format_user_label(telegram_id, user)
    text = (
        f"{label}\n"
        f"Права: добавление админов — {'да' if admin.can_manage_admins else 'нет'}, "
        f"выплаты — {'да' if admin.can_manage_payouts else 'нет'}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{'✅' if admin.can_manage_admins else '⬜'} Админы",
            callback_data=f"admin:admins:{telegram_id}:toggle:manage_admins",
        )],
        [InlineKeyboardButton(
            text=f"{'✅' if admin.can_manage_payouts else '⬜'} Выплаты",
            callback_data=f"admin:admins:{telegram_id}:toggle:payouts",
        )],
        [InlineKeyboardButton(text="🚫 Отозвать", callback_data=f"admin:admins:{telegram_id}:revoke")],
        [InlineKeyboardButton(text="⬅️ К списку", callback_data="admin:admins")],
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    return True


async def _viewer_can_manage_admins(session_factory, telegram_id: int) -> bool:
    async with session_factory() as session:
        viewer = await get_admin(session, telegram_id)
    return viewer is not None and can_manage_admins(viewer)


@admin_router.callback_query(F.data.regexp(r"^admin:admins:\d+:card$"))
async def show_admin_card(callback: CallbackQuery, session_factory) -> None:
    if not await _viewer_can_manage_admins(session_factory, callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[2])
    if await _admin_card(callback, session_factory, telegram_id):
        await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:admins:\d+:toggle:(manage_admins|payouts)$"))
async def toggle_admin_permission(callback: CallbackQuery, session_factory) -> None:
    if not await _viewer_can_manage_admins(session_factory, callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    parts = callback.data.split(":")
    telegram_id, permission = int(parts[2]), parts[4]
    column = Admin.can_manage_admins if permission == "manage_admins" else Admin.can_manage_payouts
    async with session_factory() as session:
        admin = await session.scalar(select(Admin).where(Admin.telegram_id == telegram_id))
        if admin is None or admin.role == "owner" or admin.revoked_at is not None:
            await callback.answer("Администратор недоступен.", show_alert=True)
            return
        await session.execute(
            update(Admin).where(Admin.telegram_id == telegram_id).values({column.key: ~column})
        )
        await session.commit()
    if await _admin_card(callback, session_factory, telegram_id):
        await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:admins:\d+:revoke$"))
async def request_admin_revoke(callback: CallbackQuery, session_factory) -> None:
    if not await _viewer_can_manage_admins(session_factory, callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[2])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin:admins:{telegram_id}:revoke:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:admins:{telegram_id}:revoke:abort"),
    ]])
    await callback.message.answer("Отозвать администратора?", reply_markup=keyboard)
    await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:admins:\d+:revoke:confirm$"))
async def confirm_admin_revoke(callback: CallbackQuery, session_factory) -> None:
    if not await _viewer_can_manage_admins(session_factory, callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[2])
    try:
        async with session_factory() as session:
            admin = await revoke_admin(session, telegram_id)
            viewer = await get_admin(session, callback.from_user.id)
    except LookupError:
        await callback.answer("Нельзя отозвать owner-а.", show_alert=True)
        return
    if admin is None or viewer is None:
        await callback.answer("Администратор не найден.", show_alert=True)
        return
    await _edit_admins_list(callback, session_factory, viewer)
    await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:admins:\d+:revoke:abort$"))
async def abort_admin_revoke(callback: CallbackQuery, session_factory) -> None:
    if not await _viewer_can_manage_admins(session_factory, callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    telegram_id = int(callback.data.split(":")[2])
    if await _admin_card(callback, session_factory, telegram_id):
        await callback.answer()


@admin_router.callback_query(F.data == "admin:admins:add")
async def add_admin_start(callback: CallbackQuery, state: FSMContext, session_factory) -> None:
    if not await _viewer_can_manage_admins(session_factory, callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.set_state(AdminGrantForm.waiting_user)
    await callback.message.answer(
        "Выбери пользователя кнопкой ниже, перешли сюда его сообщение, или пришли числовой telegram_id вручную.",
        reply_markup=_partner_picker_keyboard,
    )
    await callback.message.answer("Или отмени добавление.", reply_markup=_with_cancel())
    await callback.answer()


async def _ask_admin_manage_permission(message: Message, state: FSMContext, telegram_id: int) -> None:
    await state.update_data(admin_grant_telegram_id=telegram_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data="admin:admins:grant:manage_admins:yes"),
        InlineKeyboardButton(text="❌ Нет", callback_data="admin:admins:grant:manage_admins:no"),
    ]])
    await message.answer("Разрешить этому админу самому добавлять и отзывать других админов?", reply_markup=keyboard)


@admin_router.message(StateFilter(AdminGrantForm.waiting_user), F.users_shared)
async def receive_admin_user_shared(message: Message, state: FSMContext) -> None:
    await _ask_admin_manage_permission(message, state, message.users_shared.users[0].user_id)


@admin_router.message(StateFilter(AdminGrantForm.waiting_user))
async def receive_admin_user(message: Message, state: FSMContext) -> None:
    if _is_cancel_text(message):
        await _cancel_admin_input_message(message, state)
        return
    telegram_id = _partner_telegram_id_from_message(message)
    if telegram_id is None:
        await message.answer("Не понял, пришли пересланное сообщение или числовой telegram_id.", reply_markup=_with_cancel())
        return
    await _ask_admin_manage_permission(message, state, telegram_id)


@admin_router.callback_query(F.data.regexp(r"^admin:admins:grant:manage_admins:(yes|no)$"))
async def choose_admin_manage_permission(
    callback: CallbackQuery, state: FSMContext, session_factory
) -> None:
    if not await _viewer_can_manage_admins(session_factory, callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    await state.update_data(admin_grant_can_manage_admins=callback.data.endswith(":yes"))
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data="admin:admins:grant:payouts:yes"),
        InlineKeyboardButton(text="❌ Нет", callback_data="admin:admins:grant:payouts:no"),
    ]])
    await callback.message.edit_text(
        "Разрешить доступ к выплатам рефоводам (обнуление и пополнение баланса)?",
        reply_markup=keyboard,
    )
    await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:admins:grant:payouts:(yes|no)$"))
async def choose_admin_payout_permission(callback: CallbackQuery, state: FSMContext, session_factory) -> None:
    if not await _viewer_can_manage_admins(session_factory, callback.from_user.id):
        await callback.answer("Недостаточно прав.", show_alert=True)
        return
    data = await state.get_data()
    telegram_id = data.get("admin_grant_telegram_id")
    if telegram_id is None:
        await callback.answer("Добавление администратора уже отменено.", show_alert=True)
        return
    payouts = callback.data.endswith(":yes")
    try:
        async with session_factory() as session:
            admin = await add_admin(
                session, int(telegram_id),
                can_manage_admins=bool(data.get("admin_grant_can_manage_admins")),
                can_manage_payouts=payouts,
            )
            viewer = await get_admin(session, callback.from_user.id)
    except LookupError as error:
        if str(error) == "user_not_started":
            async with session_factory() as session:
                await queue_pending_admin_grant(
                    session,
                    int(telegram_id),
                    callback.from_user.id,
                    can_manage_admins=bool(data.get("admin_grant_can_manage_admins")),
                    can_manage_payouts=payouts,
                )
            await state.clear()
            await callback.message.answer(
                "Этот пользователь ещё не запускал бота. Заявка сохранена — как только он "
                "нажмёт /start, права администратора будут выданы автоматически."
            )
            await callback.answer()
            await _send_admin_menu(callback.message)
            return
        await state.clear()
        await callback.message.answer("Этот пользователь — owner, его права менять нельзя.")
        await callback.answer("Не удалось выдать права.", show_alert=True)
        return
    await state.clear()
    await callback.message.answer(
        f"Администратор #{admin.telegram_id}: админы — {'да' if admin.can_manage_admins else 'нет'}, выплаты — {'да' if admin.can_manage_payouts else 'нет'}."
    )
    if viewer is not None:
        await _edit_admins_list(callback, session_factory, viewer)
    await callback.answer()


_PARTNERS_PER_PAGE = 10


def _partner_status(partner: ReferralPartner) -> tuple[str, str]:
    if partner.revoked_at is not None:
        return "🚫", "отозван"
    if partner.activated_at is not None:
        return "✅", "активен"
    return "⏳", "не активирован"


def _partners_menu_text(page: int, total_pages: int, price: Decimal) -> str:
    if total_pages == 0:
        return f"Рефоводов пока нет.\nЦена подписки: {price:.2f} ₽."
    return (
        f"Рефоводы (стр. {page + 1}/{max(total_pages, 1)}) · "
        f"цена подписки: {price:.2f} ₽:"
    )


async def _get_partners_page(
    session_factory, page: int
) -> tuple[list[ReferralPartner], dict[int, User], int, Decimal]:
    async with session_factory() as session:
        total = await count_partners(session)
        price = await get_subscription_price(session)
        total_pages = ceil(total / _PARTNERS_PER_PAGE)
        page = max(0, min(page, total_pages - 1)) if total_pages else 0
        partners = await list_partners(
            session, limit=_PARTNERS_PER_PAGE, offset=page * _PARTNERS_PER_PAGE
        )
        ids = [partner.telegram_id for partner in partners]
        users = [] if not ids else list(await session.scalars(
            select(User).where(User.telegram_id.in_(ids))
        ))
    return partners, {user.telegram_id: user for user in users}, total_pages, price


def _partners_page_keyboard(
    partners: list[ReferralPartner], users_by_id: dict[int, User], page: int,
    total_pages: int, price: Decimal,
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"💵 Цена подписки: {price:.2f} ₽ (изменить)",
            callback_data="admin:partners:price",
        )],
        [InlineKeyboardButton(text="➕ Добавить рефовода", callback_data="admin:partner:add")],
    ]
    for partner in partners:
        emoji, _ = _partner_status(partner)
        rows.append([InlineKeyboardButton(
            text=f"{emoji} {format_partner_label(partner, users_by_id.get(partner.telegram_id))}",
            callback_data=f"admin:partner:{partner.telegram_id}:card",
        )])
    if total_pages > 1:
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(
                text="◀️ Пред", callback_data=f"admin:partners:page:{page - 1}"
            ))
        if page + 1 < total_pages:
            navigation.append(InlineKeyboardButton(
                text="▶️ След", callback_data=f"admin:partners:page:{page + 1}"
            ))
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit_partners_page(callback: CallbackQuery, session_factory, page: int) -> None:
    partners, users_by_id, total_pages, price = await _get_partners_page(session_factory, page)
    actual_page = max(0, min(page, total_pages - 1)) if total_pages else 0
    await callback.message.edit_text(
        _partners_menu_text(actual_page, total_pages, price),
        reply_markup=_partners_page_keyboard(partners, users_by_id, actual_page, total_pages, price),
    )


@admin_router.callback_query(F.data == "admin:partners")
async def partners_menu(callback: CallbackQuery, session_factory) -> None:
    await _edit_partners_page(callback, session_factory, 0)
    await callback.answer()


@admin_router.callback_query(F.data.regexp(r"^admin:partners:page:\d+$"))
async def partners_page(callback: CallbackQuery, session_factory) -> None:
    await _edit_partners_page(callback, session_factory, int(callback.data.rsplit(":", 1)[1]))
    await callback.answer()


@admin_router.callback_query(F.data == "admin:partners:price")
async def request_subscription_price(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PricingForm.waiting_price)
    await callback.message.answer(
        "Пришли новую цену за одну подписку в рублях, например: 0.5",
        reply_markup=_with_cancel(),
    )
    await callback.answer()


@admin_router.message(StateFilter(PricingForm.waiting_price))
async def receive_subscription_price(message: Message, state: FSMContext, session_factory) -> None:
    if _is_cancel_text(message):
        await _cancel_admin_input_message(message, state)
        return

    try:
        price = Decimal((message.text or "").strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        price = Decimal("-1")

    if not price.is_finite() or price < 0:
        await message.answer(
            "Пришли неотрицательную конечную цену числом, например: 0.5",
            reply_markup=_with_cancel(),
        )
        return

    async with session_factory() as session:
        await set_subscription_price(session, price)
    await state.clear()
    await message.answer(f"Цена подписки обновлена: {price:.2f} ₽.")
    partners, users_by_id, total_pages, current_price = await _get_partners_page(session_factory, 0)
    await message.answer(
        _partners_menu_text(0, total_pages, current_price),
        reply_markup=_partners_page_keyboard(
            partners, users_by_id, 0, total_pages, current_price
        ),
    )


@admin_router.callback_query(F.data == "admin:partner:add")
async def add_partner(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PartnerForm.waiting_user)
    await callback.message.answer(
        "Выбери пользователя кнопкой ниже, перешли сюда его сообщение, или пришли "
        "числовой telegram_id вручную (пользователь должен был хотя бы раз "
        "запустить бота).",
        reply_markup=_partner_picker_keyboard,
    )
    await callback.message.answer("Или отмени добавление.", reply_markup=_with_cancel())
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


async def _process_partner_user(telegram_id: int, message: Message, state: FSMContext, session_factory) -> None:
    try:
        async with session_factory() as session:
            partner, is_new = await approve_partner(session, telegram_id, message.from_user.id)
    except LookupError as error:
        if str(error) == "user_not_started":
            async with session_factory() as session:
                await queue_pending_partner_grant(session, telegram_id, message.from_user.id)
            await state.clear()
            await message.answer(
                "Этот пользователь ещё не запускал бота. Заявка сохранена — как только он "
                "нажмёт /start, права рефовода будут выданы автоматически."
            )
            await _send_admin_menu(message)
            return
        raise

    await state.clear()
    if is_new:
        await message.answer(
            f"Рефовод #{telegram_id} добавлен. Код: `{partner.referral_code}`. "
            "Он должен написать боту слово «партнер», чтобы активировать кабинет."
        )
    else:
        await message.answer(f"Пользователь #{telegram_id} уже рефовод (код: `{partner.referral_code}`).")
    partners, users_by_id, total_pages, price = await _get_partners_page(session_factory, 0)
    await message.answer(
        _partners_menu_text(0, total_pages, price),
        reply_markup=_partners_page_keyboard(partners, users_by_id, 0, total_pages, price),
    )


@admin_router.message(StateFilter(PartnerForm.waiting_user), F.users_shared)
async def receive_partner_user_shared(message: Message, state: FSMContext, session_factory) -> None:
    telegram_id = message.users_shared.users[0].user_id
    await _process_partner_user(telegram_id, message, state, session_factory)


@admin_router.message(StateFilter(PartnerForm.waiting_user))
async def receive_partner_user(message: Message, state: FSMContext, session_factory) -> None:
    if _is_cancel_text(message):
        await _cancel_admin_input_message(message, state)
        return

    telegram_id = _partner_telegram_id_from_message(message)
    if telegram_id is None:
        await message.answer("Не понял, пришли пересланное сообщение или числовой telegram_id.", reply_markup=_with_cancel())
        return

    await _process_partner_user(telegram_id, message, state, session_factory)


def _partner_id_from_callback(data: str, suffix: str) -> int | None:
    value = data.removeprefix("admin:partner:").removesuffix(suffix)
    return int(value) if value.isdecimal() and int(value) > 0 else None


async def _get_partner_card_data(session_factory, telegram_id: int):
    async with session_factory() as session:
        partner = await session.scalar(
            select(ReferralPartner).where(ReferralPartner.telegram_id == telegram_id)
        )
        if partner is None:
            return None
        user = await session.get(User, telegram_id)
        started, confirmed = await get_partner_stats(session, telegram_id)
        balance = await get_partner_balance(session, telegram_id)
    return partner, user, started, confirmed, balance


def _partner_card_text(partner: ReferralPartner, user: User | None, started: int, confirmed: int, balance: Decimal) -> str:
    emoji, status = _partner_status(partner)
    return (
        f"{emoji} {format_partner_label(partner, user)}\n"
        f"Статус: {status}\n"
        f"Код: {partner.referral_code}\n\n"
        f"{format_partner_stats_text(started, confirmed)}\n"
        f"Баланс: {balance:.2f} ₽"
    )


def _partner_card_keyboard(
    partner: ReferralPartner, *, viewer_can_manage_payouts: bool
) -> InlineKeyboardMarkup:
    rows = []
    if partner.revoked_at is None:
        rows.append([InlineKeyboardButton(
            text="🚫 Отозвать", callback_data=f"admin:partner:{partner.telegram_id}:revoke"
        )])
    if viewer_can_manage_payouts:
        rows.extend([
            [InlineKeyboardButton(
                text="💵 Обнулить баланс (Вывод)",
                callback_data=f"admin:partner:{partner.telegram_id}:balance:zero",
            )],
            [InlineKeyboardButton(
                text="➕ Добавить средства",
                callback_data=f"admin:partner:{partner.telegram_id}:balance:add",
            )],
        ])
    rows.append([InlineKeyboardButton(text="⬅️ К списку", callback_data="admin:partners:page:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit_partner_card(callback: CallbackQuery, session_factory, telegram_id: int) -> bool:
    card = await _get_partner_card_data(session_factory, telegram_id)
    if card is None:
        await callback.answer("Рефовод не найден.", show_alert=True)
        return False
    partner, user, started, confirmed, balance = card
    async with session_factory() as session:
        viewer = await get_admin(session, callback.from_user.id)
    await callback.message.edit_text(
        _partner_card_text(partner, user, started, confirmed, balance),
        reply_markup=_partner_card_keyboard(
            partner, viewer_can_manage_payouts=viewer is not None and can_manage_payouts(viewer)
        ),
    )
    return True


async def _send_partner_card(message: Message, session_factory, telegram_id: int) -> bool:
    card = await _get_partner_card_data(session_factory, telegram_id)
    if card is None:
        await message.answer("Рефовод не найден.")
        return False
    partner, user, started, confirmed, balance = card
    async with session_factory() as session:
        viewer = await get_admin(session, message.from_user.id)
    await message.answer(
        _partner_card_text(partner, user, started, confirmed, balance),
        reply_markup=_partner_card_keyboard(
            partner, viewer_can_manage_payouts=viewer is not None and can_manage_payouts(viewer)
        ),
    )
    return True


@admin_router.callback_query(F.data.startswith("admin:partner:") & F.data.endswith(":card"))
async def show_partner_card(callback: CallbackQuery, session_factory) -> None:
    telegram_id = _partner_id_from_callback(callback.data, ":card")
    if telegram_id is None:
        await callback.answer("Некорректный идентификатор рефовода.", show_alert=True)
        return
    if await _edit_partner_card(callback, session_factory, telegram_id):
        await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:partner:") & F.data.endswith(":revoke"))
async def request_partner_revoke(callback: CallbackQuery) -> None:
    telegram_id = _partner_id_from_callback(callback.data, ":revoke")
    if telegram_id is None:
        await callback.answer("Некорректный идентификатор рефовода.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admin:partner:{telegram_id}:revoke:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:partner:{telegram_id}:revoke:abort"),
    ]])
    await callback.message.answer("Отозвать рефовода? Это действие можно отменить повторным добавлением.", reply_markup=keyboard)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:partner:") & F.data.endswith(":revoke:confirm"))
async def confirm_partner_revoke(callback: CallbackQuery, session_factory) -> None:
    telegram_id = _partner_id_from_callback(callback.data, ":revoke:confirm")
    if telegram_id is None:
        await callback.answer("Некорректный идентификатор рефовода.", show_alert=True)
        return
    async with session_factory() as session:
        partner = await revoke_partner(session, telegram_id)
    if partner is None:
        await callback.answer("Рефовод не найден.", show_alert=True)
        return
    await _edit_partner_card(callback, session_factory, telegram_id)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin:partner:") & F.data.endswith(":revoke:abort"))
async def abort_partner_revoke(callback: CallbackQuery, session_factory) -> None:
    telegram_id = _partner_id_from_callback(callback.data, ":revoke:abort")
    if telegram_id is None:
        await callback.answer("Некорректный идентификатор рефовода.", show_alert=True)
        return
    if await _edit_partner_card(callback, session_factory, telegram_id):
        await callback.answer()


@admin_router.callback_query(
    F.data.startswith("admin:partner:") & F.data.endswith(":balance:zero")
)
async def request_partner_balance_zero(callback: CallbackQuery, session_factory) -> None:
    telegram_id = _partner_id_from_callback(callback.data, ":balance:zero")
    if telegram_id is None:
        await callback.answer("Некорректный идентификатор рефовода.", show_alert=True)
        return
    async with session_factory() as session:
        viewer = await get_admin(session, callback.from_user.id)
        if viewer is None or not can_manage_payouts(viewer):
            await callback.answer("Недостаточно прав для управления выплатами.", show_alert=True)
            return
        balance = await get_partner_balance(session, telegram_id)
    if balance == 0:
        await callback.answer("Баланс уже 0.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Подтвердить",
            callback_data=f"admin:partner:{telegram_id}:balance:zero:confirm",
        ),
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data=f"admin:partner:{telegram_id}:balance:zero:abort",
        ),
    ]])
    await callback.message.answer(
        "Обнулить баланс рефовода "
        f"(текущий баланс: {balance:.2f} ₽)? Будет создана запись «Вывод» на эту сумму.",
        reply_markup=keyboard,
    )
    await callback.answer()


@admin_router.callback_query(
    F.data.startswith("admin:partner:") & F.data.endswith(":balance:zero:confirm")
)
async def confirm_partner_balance_zero(callback: CallbackQuery, session_factory) -> None:
    telegram_id = _partner_id_from_callback(callback.data, ":balance:zero:confirm")
    if telegram_id is None:
        await callback.answer("Некорректный идентификатор рефовода.", show_alert=True)
        return
    async with session_factory() as session:
        viewer = await get_admin(session, callback.from_user.id)
        if viewer is None or not can_manage_payouts(viewer):
            await callback.answer("Недостаточно прав для управления выплатами.", show_alert=True)
            return
        amount = await zero_out_partner_balance(session, telegram_id, callback.from_user.id)
    await _edit_partner_card(callback, session_factory, telegram_id)
    await callback.answer(f"Обнулено: {amount:.2f} ₽.")


@admin_router.callback_query(
    F.data.startswith("admin:partner:") & F.data.endswith(":balance:zero:abort")
)
async def abort_partner_balance_zero(callback: CallbackQuery, session_factory) -> None:
    telegram_id = _partner_id_from_callback(callback.data, ":balance:zero:abort")
    if telegram_id is None:
        await callback.answer("Некорректный идентификатор рефовода.", show_alert=True)
        return
    if await _edit_partner_card(callback, session_factory, telegram_id):
        await callback.answer()


@admin_router.callback_query(
    F.data.startswith("admin:partner:") & F.data.endswith(":balance:add")
)
async def request_partner_balance_add(
    callback: CallbackQuery, state: FSMContext, session_factory=None
) -> None:
    telegram_id = _partner_id_from_callback(callback.data, ":balance:add")
    if telegram_id is None:
        await callback.answer("Некорректный идентификатор рефовода.", show_alert=True)
        return
    if session_factory is not None:
        async with session_factory() as session:
            viewer = await get_admin(session, callback.from_user.id)
        if viewer is None or not can_manage_payouts(viewer):
            await callback.answer("Недостаточно прав для управления выплатами.", show_alert=True)
            return
    await state.set_state(PartnerBalanceForm.waiting_amount)
    await state.update_data(partner_telegram_id=telegram_id)
    await callback.message.answer(
        "Пришли сумму пополнения в рублях, например: 500 или 500.50",
        reply_markup=_with_cancel(),
    )
    await callback.answer()


@admin_router.message(StateFilter(PartnerBalanceForm.waiting_amount))
async def receive_partner_balance_amount(message: Message, state: FSMContext) -> None:
    if _is_cancel_text(message):
        await _cancel_admin_input_message(message, state)
        return
    try:
        amount = Decimal((message.text or "").strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        amount = Decimal("0")
    if not amount.is_finite() or amount <= 0:
        await message.answer(
            "Пришли положительную сумму в рублях, например: 500 или 500.50.",
            reply_markup=_with_cancel(),
        )
        return
    await state.update_data(amount=str(amount))
    await state.set_state(PartnerBalanceForm.waiting_title)
    await message.answer(
        "Пришли название операции (будет видно в истории), например: Доплата за баг.",
        reply_markup=_with_cancel(),
    )


@admin_router.message(StateFilter(PartnerBalanceForm.waiting_title))
async def receive_partner_balance_title(message: Message, state: FSMContext, session_factory) -> None:
    if _is_cancel_text(message):
        await _cancel_admin_input_message(message, state)
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer("Пришли непустое название операции.", reply_markup=_with_cancel())
        return
    data = await state.get_data()
    amount = Decimal(data["amount"])
    telegram_id = int(data["partner_telegram_id"])
    async with session_factory() as session:
        viewer = await get_admin(session, message.from_user.id)
        if viewer is None or not can_manage_payouts(viewer):
            await state.clear()
            await message.answer("Недостаточно прав для управления выплатами.")
            return
        await add_partner_balance_adjustment(
            session, telegram_id, message.from_user.id, amount, title
        )
    await state.clear()
    await message.answer(f"Баланс пополнен на {amount:.2f} ₽ ({title}).", reply_markup=ReplyKeyboardRemove())
    await _send_partner_card(message, session_factory, telegram_id)


def _codes_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Найти код", callback_data="admin:code:search")],
        [InlineKeyboardButton(text="➕ Добавить код", callback_data="admin:code:add")],
        [InlineKeyboardButton(text="📄 Шаблон для импорта", callback_data="admin:code:template")],
        [InlineKeyboardButton(text="📥 Скачать текущие коды", callback_data="admin:code:export")],
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
    await callback.message.answer("Пришли код для поиска.", reply_markup=_with_cancel())
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
        await _send_admin_menu(message)
        return
    await message.answer(_code_card_text(movie_code), reply_markup=_code_card_keyboard(movie_code))


@admin_router.callback_query(F.data == "admin:code:add")
async def add_code(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CodeAddForm.waiting_code)
    await callback.message.answer("Пришли номер нового кода. Пример: 777.", reply_markup=_with_cancel())
    await callback.answer()


@admin_router.callback_query(F.data == "admin:code:template")
async def send_import_template(callback: CallbackQuery) -> None:
    await callback.message.answer_document(
        BufferedInputFile(build_template_xlsx(), filename="codes_template.xlsx")
    )
    await callback.answer()


@admin_router.callback_query(F.data == "admin:code:export")
async def send_codes_export(callback: CallbackQuery, session_factory) -> None:
    async with session_factory() as session:
        content = await build_all_codes_export_xlsx(session)
    await callback.message.answer_document(
        BufferedInputFile(content, filename="codes_export.xlsx")
    )
    await callback.answer()


@admin_router.callback_query(F.data == "admin:code:import")
async def request_codes_import(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ImportForm.waiting_file)
    await callback.message.answer(
        "Пришли файл `.csv` или `.xlsx` с колонками operation, code, title.\n"
        "operation: `upload` — создать код или обновить/восстановить существующий;\n"
        "`delete` — деактивировать существующий код (колонку title в этом случае можно оставить пустой).",
        reply_markup=_with_cancel(),
    )
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
        await _send_admin_menu(message)
        return
    async with session_factory() as session:
        plan = await build_import_plan(session, rows)
    if not plan.to_create and not plan.to_restore and not plan.to_delete and not plan.conflicts:
        await state.clear()
        await message.answer(_import_plan_text(plan, len(rows)) + "\nИзменений нет, все строки уже актуальны.")
        await _send_admin_menu(message)
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
    await _send_admin_menu(callback.message)
    await callback.answer()


@admin_router.callback_query(StateFilter(ImportForm.waiting_file), F.data == "admin:code:import:cancel")
async def cancel_codes_import(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("Импорт отменён.")
    await _send_admin_menu(callback.message)
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
        await message.answer(
            f"Теперь — пришли название для кода «{code}». Пример: Поднятие уровня в одиночку.",
            reply_markup=_with_cancel(),
        )
    elif movie_code.status is MovieCodeStatus.ACTIVE:
        await state.clear()
        await message.answer(
            f"Код «{code}» уже существует и активен: «{movie_code.title}». Чтобы изменить название, "
            "найди код через поиск и используй «Изменить название»."
        )
        await _send_admin_menu(message)
    else:
        await state.update_data(code=code, mode="restore", old_title=movie_code.title)
        await state.set_state(CodeAddForm.waiting_title)
        await message.answer(
            f"Код «{code}» уже существует, но деактивирован (было: «{movie_code.title}»). "
            "Пришли новое название или `-`, чтобы восстановить со старым.",
            reply_markup=_with_cancel(),
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
    await _send_admin_menu(message)


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
    await callback.message.answer(
        f"Текущее название: «{movie_code.title}». Пришли новое название.", reply_markup=_with_cancel()
    )
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
    await _send_admin_menu(callback.message)
    await callback.answer()


@admin_router.callback_query(StateFilter(CodeEditForm.waiting_new_title), F.data.startswith("admin:code:") & F.data.endswith(":edit:cancel"))
async def cancel_code_title_edit(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.answer("Изменение отменено.")
    await _send_admin_menu(callback.message)
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
        "Пришли новое название или `-`, чтобы восстановить со старым.",
        reply_markup=_with_cancel(),
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
    await _send_admin_menu(message)


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
