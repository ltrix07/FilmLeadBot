from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardRemove
from sqlalchemy import func, select

from app.bot.gate import (
    SubscriptionGateMiddleware,
    check_subscription,
    render_gate_text,
    render_menu_text,
)
from app.bot.keyboards import build_gate_keyboard, sponsor_url
from app.bot.routers.admin import (
    AdminGrantForm,
    CodeAddForm,
    CodeSearchForm,
    CampaignForm,
    PartnerBalanceForm,
    PricingForm,
    SponsorForm,
    _admin_menu_keyboard,
    _welcome_menu_keyboard,
    _admins_keyboard,
    _edit_partners_page,
    _get_partner_card_data,
    _get_partners_page,
    _partner_card_text,
    _partner_card_keyboard,
    _partners_page_keyboard,
    add_admin_start,
    admin_router,
    admins_menu,
    archived_partners_page,
    cancel_admin_input,
    cancel_admin_input_command,
    cancel_broadcast,
    cancel_campaign_create,
    cancel_campaign_limit_decrease,
    cancel_code_title_edit,
    cancel_codes_import,
    clear_partner_bonus_handler,
    choose_sponsor_request_mode,
    confirm_partner_balance_zero,
    confirm_partner_revoke,
    unrevoke_partner_handler,
    confirm_campaign_cancel,
    confirm_campaign_create,
    confirm_admin_revoke,
    choose_admin_manage_permission,
    choose_admin_payout_permission,
    receive_admin_user,
    receive_partner_balance_amount,
    receive_partner_balance_title,
    receive_subscription_price,
    request_subscription_price,
    request_partner_balance_add,
    request_partner_balance_zero,
    request_partner_revoke,
    receive_invite_link,
    receive_welcome_content,
    receive_code_search,
    list_scheduled_broadcasts,
    receive_new_code,
    text_admin_menu,
    toggle_admin_permission,
    toggle_sponsor_own_channel,
    toggle_sponsor_request_mode,
)
from app.bot.filters import IsAdmin
from app.bot.routers.menu import handle_movie_code
from app.bot.routers.partner import (
    PartnerTriggerWordFilter,
    activate_partner,
    partner_balance,
    partner_codes_export,
    partner_link,
    partner_menu,
    partner_stats,
    prompt_partner_recruitment,
)
from app.bot.routers.start import _ensure_user_and_referral, _send_welcome_message, cmd_start
from app.db.models import (
    Admin,
    ReferralEvent,
    ReferralPartner,
    PartnerBalanceAdjustment,
    MovieCode,
    MovieCodeStatus,
    PendingAdminGrant,
    Campaign,
    CampaignStatus,
    Sponsor,
    SponsorType,
    User,
)
from app.services.partner_balance import add_partner_balance_adjustment, get_partner_balance
from app.services.admins import is_admin
from app.services.partners import format_partner_stats_text
from app.services.settings import get_subscription_price, set_welcome_message
from app.services.subscription import AccessResult


@pytest.mark.asyncio
async def test_toggle_own_sponsor_channel_updates_flag_and_rerenders(session_factory):
    async with session_factory() as session:
        session.add(Sponsor(chat_id=-100, title="Own project", type=SponsorType.CHANNEL))
        await session.commit()
        assert (await session.get(Sponsor, -100)).own_channel is False

    message = SimpleNamespace(edit_text=AsyncMock())
    callback = SimpleNamespace(
        data="admin:sponsor:-100:toggle_own",
        message=message,
        answer=AsyncMock(),
    )
    await toggle_sponsor_own_channel(callback, session_factory)

    async with session_factory() as session:
        assert (await session.get(Sponsor, -100)).own_channel is True
    text, = message.edit_text.await_args.args
    keyboard = message.edit_text.await_args.kwargs["reply_markup"]
    assert "· своё" in text
    assert keyboard.inline_keyboard[0][0].text == "✅ Своё"
    callback.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_choose_sponsor_request_mode_yes_saves_request_mode(session_factory):
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(SponsorForm.waiting_request_mode)
    await state.update_data(chat_id=-101, title="Join requests", username="requests", type="channel")
    callback = SimpleNamespace(
        data="admin:sponsor:request_mode:yes",
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )

    await choose_sponsor_request_mode(callback, state)

    assert (await state.get_data())["request_mode"] is True
    assert await state.get_state() == SponsorForm.waiting_invite_link.state
    assert any(
        "Приглашение пользователей по ссылке" in call.args[0]
        for call in callback.message.answer.await_args_list
    )

    message = SimpleNamespace(text="https://t.me/+joinrequests", answer=AsyncMock())
    await receive_invite_link(message, state, session_factory)
    async with session_factory() as session:
        assert (await session.get(Sponsor, -101)).request_mode is True
    keyboard = message.answer.await_args.kwargs["reply_markup"]
    assert any(button.text.endswith("Join requests (канал)") for row in keyboard.inline_keyboard for button in row)
    assert not any(call.args == ("Админ-панель:",) for call in message.answer.await_args_list)
    await storage.close()


@pytest.mark.asyncio
async def test_choose_sponsor_request_mode_no_saves_disabled_request_mode(session_factory):
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(SponsorForm.waiting_request_mode)
    await state.update_data(chat_id=-102, title="Regular channel", username="regular", type="channel")
    callback = SimpleNamespace(
        data="admin:sponsor:request_mode:no",
        message=SimpleNamespace(answer=AsyncMock()),
        answer=AsyncMock(),
    )

    await choose_sponsor_request_mode(callback, state)

    assert (await state.get_data())["request_mode"] is False
    assert await state.get_state() == SponsorForm.waiting_invite_link.state
    assert not any(
        "Приглашение пользователей по ссылке" in call.args[0]
        for call in callback.message.answer.await_args_list
    )

    message = SimpleNamespace(text="https://t.me/+regular", answer=AsyncMock())
    await receive_invite_link(message, state, session_factory)
    async with session_factory() as session:
        assert (await session.get(Sponsor, -102)).request_mode is False
    assert not any(call.args == ("Админ-панель:",) for call in message.answer.await_args_list)
    await storage.close()


@pytest.mark.asyncio
async def test_toggle_sponsor_request_mode_updates_flag_and_rerenders(session_factory):
    async with session_factory() as session:
        session.add(Sponsor(chat_id=-103, title="Requests", type=SponsorType.CHANNEL))
        await session.commit()

    message = SimpleNamespace(edit_text=AsyncMock())
    callback = SimpleNamespace(
        data="admin:sponsor:-103:toggle_request_mode",
        message=message,
        answer=AsyncMock(),
    )
    await toggle_sponsor_request_mode(callback, session_factory)

    async with session_factory() as session:
        assert (await session.get(Sponsor, -103)).request_mode is True
    text, = message.edit_text.await_args.args
    keyboard = message.edit_text.await_args.kwargs["reply_markup"]
    assert "· заявки" in text
    assert keyboard.inline_keyboard[0][1].text == "✅ Заявки"

    await toggle_sponsor_request_mode(callback, session_factory)
    async with session_factory() as session:
        assert (await session.get(Sponsor, -103)).request_mode is False


@pytest.mark.asyncio
async def test_ensure_user_without_referral(session_factory):
    async with session_factory() as session:
        await _ensure_user_and_referral(session, 100, None)

    async with session_factory() as session:
        user = await session.get(User, 100)
        events = await session.scalar(select(func.count()).select_from(ReferralEvent))
    assert user.referrer_telegram_id is None
    assert events == 0


@pytest.mark.asyncio
async def test_ensure_user_referral_is_created_once(session_factory):
    async with session_factory() as session:
        session.add(User(telegram_id=10))
        admin = Admin(telegram_id=99, role="owner")
        session.add(admin)
        await session.flush()
        session.add(
            ReferralPartner(
                telegram_id=10,
                referral_code="valid-code",
                approved_by_admin_id=admin.id,
                activated_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    async with session_factory() as session:
        await _ensure_user_and_referral(session, 101, "ref_valid-code")
    async with session_factory() as session:
        await _ensure_user_and_referral(session, 101, "ref_other-code")

    async with session_factory() as session:
        user = await session.get(User, 101)
        events = await session.scalar(select(func.count()).select_from(ReferralEvent))
    assert user.referrer_telegram_id == 10
    assert events == 1


@pytest.mark.asyncio
async def test_invalid_referral_creates_user_without_referrer(session_factory):
    async with session_factory() as session:
        await _ensure_user_and_referral(session, 102, "ref_missing")

    async with session_factory() as session:
        user = await session.get(User, 102)
        events = await session.scalar(select(func.count()).select_from(ReferralEvent))
    assert user.referrer_telegram_id is None
    assert events == 0


@pytest.mark.asyncio
async def test_existing_user_start_clears_blocked_at(session_factory):
    async with session_factory() as session:
        session.add(User(telegram_id=103, blocked_at=datetime.now(timezone.utc)))
        await session.commit()

    async with session_factory() as session:
        await _ensure_user_and_referral(session, 103, None)

    async with session_factory() as session:
        assert (await session.get(User, 103)).blocked_at is None


@pytest.mark.asyncio
async def test_send_welcome_message_copies_custom_content(session_factory):
    async with session_factory() as session:
        await set_welcome_message(session, -100123, 456, "<b>Welcome</b>")

    message = SimpleNamespace(from_user=SimpleNamespace(id=789), answer=AsyncMock())
    bot = SimpleNamespace(copy_message=AsyncMock())
    settings = SimpleNamespace(welcome_message="Default welcome")

    await _send_welcome_message(message, bot, session_factory, settings)

    bot.copy_message.assert_awaited_once_with(
        chat_id=789, from_chat_id=-100123, message_id=456
    )
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_welcome_message_uses_default_when_not_customized(session_factory):
    message = SimpleNamespace(from_user=SimpleNamespace(id=789), answer=AsyncMock())
    bot = SimpleNamespace(copy_message=AsyncMock())
    settings = SimpleNamespace(welcome_message="Default welcome")

    await _send_welcome_message(message, bot, session_factory, settings)

    bot.copy_message.assert_not_awaited()
    message.answer.assert_awaited_once_with("Default welcome")


@pytest.mark.asyncio
async def test_send_welcome_message_falls_back_when_copy_fails(session_factory):
    async with session_factory() as session:
        await set_welcome_message(session, -100123, 456, "Welcome")

    message = SimpleNamespace(from_user=SimpleNamespace(id=789), answer=AsyncMock())
    from aiogram.exceptions import TelegramBadRequest

    bot = SimpleNamespace(copy_message=AsyncMock(side_effect=TelegramBadRequest(
        method=SimpleNamespace(), message="message to copy not found"
    )))
    settings = SimpleNamespace(welcome_message="Default welcome")

    await _send_welcome_message(message, bot, session_factory, settings)

    message.answer.assert_awaited_once_with("Default welcome")


@pytest.mark.asyncio
async def test_subscription_gate_middleware_sends_html_gate_text(session_factory):
    service = SimpleNamespace(evaluate_user_access=AsyncMock(return_value=AccessResult(
        passed=False, is_partner=False, subscribed_sponsors=[], missing_sponsors=[],
        newly_completed_campaigns=[], errored_campaigns=[],
    )))
    message = Message.model_construct(from_user=SimpleNamespace(id=100))
    object.__setattr__(message, "answer", AsyncMock())

    result = await SubscriptionGateMiddleware(service)(
        AsyncMock(), message, {"session_factory": session_factory, "bot": SimpleNamespace()}
    )

    assert result is None
    message.answer.assert_awaited_once()
    assert message.answer.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_check_subscription_renders_failed_gate_as_html(session_factory):
    service = SimpleNamespace(evaluate_user_access=AsyncMock(return_value=AccessResult(
        passed=False, is_partner=False, subscribed_sponsors=[], missing_sponsors=[],
        newly_completed_campaigns=[], errored_campaigns=[],
    )))
    message = SimpleNamespace(edit_text=AsyncMock())
    callback = SimpleNamespace(from_user=SimpleNamespace(id=100), message=message, answer=AsyncMock())

    await check_subscription(callback, session_factory, service, SimpleNamespace())

    assert message.edit_text.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_start_renders_failed_gate_as_html(session_factory):
    service = SimpleNamespace(evaluate_user_access=AsyncMock(return_value=AccessResult(
        passed=False, is_partner=False, subscribed_sponsors=[], missing_sponsors=[],
        newly_completed_campaigns=[], errored_campaigns=[],
    )))
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=100, username="user", full_name="User"), answer=AsyncMock()
    )
    bot = SimpleNamespace(copy_message=AsyncMock())

    await cmd_start(
        message, SimpleNamespace(args=None), session_factory, service, bot,
        SimpleNamespace(welcome_message="Welcome"),
    )

    assert message.answer.await_args_list[-1].kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_start_sends_partner_cabinet_as_separate_message(session_factory):
    service = SimpleNamespace(evaluate_user_access=AsyncMock(return_value=AccessResult(
        passed=True, is_partner=True, subscribed_sponsors=[], missing_sponsors=[],
        newly_completed_campaigns=[], errored_campaigns=[],
    )))
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=100, username="user", full_name="User"), answer=AsyncMock()
    )
    bot = SimpleNamespace(copy_message=AsyncMock())

    await cmd_start(
        message, SimpleNamespace(args=None), session_factory, service, bot,
        SimpleNamespace(welcome_message="Welcome"),
    )

    calls = message.answer.await_args_list
    assert calls[-2].args[0] == (
        "🍏 Доступ открыт. Чтобы получить название - отправьте найденный Вами код."
    )
    assert "reply_markup" not in calls[-2].kwargs
    assert calls[-1].args[0] == "<b>Вам доступен кабинет партнера - используйте кнопки ниже.</b>"
    assert calls[-1].kwargs["parse_mode"] == "HTML"
    assert calls[-1].kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_check_subscription_sends_partner_cabinet_as_separate_message(session_factory):
    service = SimpleNamespace(evaluate_user_access=AsyncMock(return_value=AccessResult(
        passed=True, is_partner=True, subscribed_sponsors=[], missing_sponsors=[],
        newly_completed_campaigns=[], errored_campaigns=[],
    )))
    message = SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock())
    callback = SimpleNamespace(from_user=SimpleNamespace(id=100), message=message, answer=AsyncMock())

    await check_subscription(callback, session_factory, service, SimpleNamespace())

    assert message.edit_text.await_args.args[0] == (
        "🍏 Доступ открыт. Чтобы получить название - отправьте найденный Вами код."
    )
    assert "reply_markup" not in message.edit_text.await_args.kwargs
    assert message.answer.await_args.args[0] == (
        "<b>Вам доступен кабинет партнера - используйте кнопки ниже.</b>"
    )
    assert message.answer.await_args.kwargs["parse_mode"] == "HTML"
    assert message.answer.await_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_activate_partner_sends_welcome_only_on_first_activation(session_factory):
    await _create_partner(session_factory, telegram_id=100)
    first_message = SimpleNamespace(from_user=SimpleNamespace(id=100), answer=AsyncMock())

    await activate_partner(first_message, session_factory, SimpleNamespace())

    first_calls = first_message.answer.await_args_list
    assert "Добро пожаловать в партнерскую программу" in first_calls[0].args[0]
    assert '<tg-emoji emoji-id="5253700530152165989">🤝</tg-emoji>' in first_calls[0].args[0]
    assert first_calls[0].kwargs["parse_mode"] == "HTML"
    assert "Вам доступен кабинет партнера" in first_calls[1].args[0]
    assert first_calls[1].kwargs["parse_mode"] == "HTML"

    repeated_message = SimpleNamespace(from_user=SimpleNamespace(id=100), answer=AsyncMock())
    await activate_partner(repeated_message, session_factory, SimpleNamespace())
    repeated_message.answer.assert_awaited_once()
    assert "Вам доступен кабинет партнера" in repeated_message.answer.await_args.args[0]


@pytest.mark.asyncio
async def test_prompt_partner_recruitment_replies_with_bold_html() -> None:
    message = SimpleNamespace(answer=AsyncMock())

    await prompt_partner_recruitment(message)

    assert message.answer.await_args.args[0] == (
        "⭐️ <b>Чтобы стать нашим партнером - напишите @Anatoliy_Here</b>"
    )
    assert message.answer.await_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["партнер", "Партнёрка", " рефка "])
async def test_partner_trigger_word_filter_matches_known_words(text) -> None:
    message = SimpleNamespace(text=text)
    assert await PartnerTriggerWordFilter()(message) is True


@pytest.mark.asyncio
async def test_partner_trigger_word_filter_rejects_other_text() -> None:
    message = SimpleNamespace(text="случайный код")
    assert await PartnerTriggerWordFilter()(message) is False


async def _activate_partner_record(session_factory, telegram_id: int = 100) -> None:
    async with session_factory() as session:
        partner = await session.scalar(
            select(ReferralPartner).where(ReferralPartner.telegram_id == telegram_id)
        )
        partner.activated_at = datetime.now(timezone.utc)
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "data", "expected"),
    [
        (partner_link, "partner:link", "Ваша реферальная ссылка"),
        (partner_stats, "partner:stats", "Общее количество приведенных"),
        (partner_balance, "partner:balance", "Ваш текущий баланс"),
        (partner_menu, "partner:menu", "Вам доступен кабинет партнера"),
    ],
)
async def test_partner_cabinet_pages_edit_message_with_back_or_menu_keyboard(
    session_factory, handler, data, expected
):
    await _create_partner(session_factory)
    await _activate_partner_record(session_factory)
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    callback = SimpleNamespace(from_user=SimpleNamespace(id=100), message=message, answer=AsyncMock())
    args = (callback, session_factory, SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="bot")))) if handler is partner_link else (callback, session_factory)

    await handler(*args)

    text = message.edit_text.await_args.args[0]
    keyboard = message.edit_text.await_args.kwargs["reply_markup"]
    assert expected in text
    if handler is partner_menu:
        assert keyboard.inline_keyboard[0][0].text == "🔗 Моя реф. ссылка"
    else:
        button = keyboard.inline_keyboard[0][0]
        assert (button.text, button.callback_data) == ("⬅️ Назад", "partner:menu")
    message.answer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "data"),
    [
        (partner_link, "partner:link"),
        (partner_stats, "partner:stats"),
        (partner_balance, "partner:balance"),
        (partner_menu, "partner:menu"),
    ],
)
async def test_partner_cabinet_pages_deny_inactive_partners(session_factory, handler, data):
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    callback = SimpleNamespace(from_user=SimpleNamespace(id=100), message=message, answer=AsyncMock())
    args = (callback, session_factory, SimpleNamespace(get_me=AsyncMock())) if handler is partner_link else (callback, session_factory)

    await handler(*args)

    callback.answer.assert_awaited_once_with("Доступно только рефоводам.", show_alert=True)
    message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_partner_codes_export_includes_descriptive_caption(session_factory):
    await _create_partner(session_factory)
    await _activate_partner_record(session_factory)
    message = SimpleNamespace(answer_document=AsyncMock())
    callback = SimpleNamespace(from_user=SimpleNamespace(id=100), message=message, answer=AsyncMock())

    await partner_codes_export(callback, session_factory)

    caption = message.answer_document.await_args.kwargs["caption"]
    assert "собраны все коды & названия" in caption
    assert "@Anatoliy_Here" in caption


def test_gate_keyboard_uses_username_invite_link_or_fallback_callback():
    username = Sponsor(
        chat_id=-1, username="@public_channel", title="Public", type=SponsorType.CHANNEL
    )
    invite = Sponsor(
        chat_id=-2, invite_link="https://t.me/+private", title="Private", type=SponsorType.GROUP
    )
    unavailable = Sponsor(chat_id=-3, title="Unavailable", type=SponsorType.CHANNEL)

    assert sponsor_url(username) == "https://t.me/public_channel"
    assert sponsor_url(invite) == "https://t.me/+private"
    keyboard = build_gate_keyboard([username], [invite, unavailable])
    public, private, missing, check = (row[0] for row in keyboard.inline_keyboard)
    assert public.url == "https://t.me/public_channel"
    assert private.url == "https://t.me/+private"
    assert missing.callback_data == "sponsor_link_missing"
    assert check.callback_data == "check_subscription"


@pytest.mark.asyncio
async def test_callback_cancel_clears_any_admin_form_state():
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(SponsorForm.waiting_chat)
    callback = SimpleNamespace(message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock())

    await cancel_admin_input(callback, state)

    assert await state.get_state() is None
    assert any(
        isinstance(call.kwargs.get("reply_markup"), ReplyKeyboardRemove)
        for call in callback.message.answer.await_args_list
    )
    callback.answer.assert_awaited_once()
    await storage.close()


@pytest.mark.asyncio
async def test_command_cancel_removes_reply_keyboard_for_sponsor_picker():
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(SponsorForm.waiting_chat)
    message = SimpleNamespace(answer=AsyncMock())

    await cancel_admin_input_command(message, state)

    assert await state.get_state() is None
    assert any(
        isinstance(call.kwargs.get("reply_markup"), ReplyKeyboardRemove)
        for call in message.answer.await_args_list
    )
    await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "needs_session_factory"),
    [
        (cancel_broadcast, False),
        (cancel_campaign_limit_decrease, False),
        (cancel_codes_import, False),
        (cancel_code_title_edit, False),
    ],
)
async def test_specific_admin_cancellations_restore_admin_menu(
    handler, needs_session_factory, session_factory
):
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=1))
    callback = SimpleNamespace(
        message=SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()),
        answer=AsyncMock(),
    )

    if needs_session_factory:
        await handler(callback, state, session_factory)
    else:
        await handler(callback, state)

    assert any(
        call.args == ("Админ-панель:",)
        and call.kwargs.get("reply_markup") == _admin_menu_keyboard()
        for call in callback.message.answer.await_args_list
    )
    await storage.close()


@pytest.mark.asyncio
async def test_cancel_campaign_create_returns_campaigns_list_without_admin_menu(session_factory):
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=1))
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    callback = SimpleNamespace(message=message, answer=AsyncMock())

    await cancel_campaign_create(callback, state, session_factory)

    keyboard = message.edit_text.await_args.kwargs["reply_markup"]
    assert any(button.text == "➕ Новая кампания" for row in keyboard.inline_keyboard for button in row)
    assert not any(call.args == ("Админ-панель:",) for call in message.answer.await_args_list)
    await storage.close()


@pytest.mark.asyncio
async def test_receive_welcome_content_returns_welcome_menu_without_admin_menu(session_factory):
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=1))
    message = SimpleNamespace(
        html_text="<b>New welcome</b>",
        caption=None,
        chat=SimpleNamespace(id=1),
        message_id=42,
        answer=AsyncMock(),
    )
    settings = SimpleNamespace(welcome_message="Default welcome")

    await receive_welcome_content(message, state, session_factory, settings)

    assert any(
        call.kwargs.get("reply_markup") == _welcome_menu_keyboard()
        for call in message.answer.await_args_list
    )
    assert not any(call.args == ("Админ-панель:",) for call in message.answer.await_args_list)
    await storage.close()


@pytest.mark.asyncio
async def test_confirm_campaign_create_returns_campaigns_list_without_admin_menu(session_factory):
    async with session_factory() as session:
        sponsor = Sponsor(chat_id=-100, title="Sponsor", type=SponsorType.CHANNEL)
        session.add(sponsor)
        session.add_all(
            Campaign(
                sponsor_chat_id=sponsor.chat_id,
                limit_original=10,
                limit_current=10,
                status=CampaignStatus.ACTIVE,
            )
            for _ in range(5)
        )
        await session.commit()

    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(CampaignForm.waiting_schedule)
    await state.update_data(sponsor_chat_id=-100, limit=20, status=CampaignStatus.ACTIVE.value)
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    callback = SimpleNamespace(message=message, answer=AsyncMock())

    await confirm_campaign_create(callback, state, session_factory)

    keyboard = message.edit_text.await_args.kwargs["reply_markup"]
    assert any(button.text == "➕ Новая кампания" for row in keyboard.inline_keyboard for button in row)
    assert not any(call.args == ("Админ-панель:",) for call in message.answer.await_args_list)
    await storage.close()


@pytest.mark.asyncio
async def test_confirm_campaign_cancel_returns_updated_list_without_admin_menu(session_factory):
    async with session_factory() as session:
        session.add_all([
            Admin(telegram_id=1),
            Sponsor(chat_id=-100, title="Sponsor", type=SponsorType.CHANNEL),
        ])
        await session.flush()
        campaign = Campaign(
            sponsor_chat_id=-100,
            limit_original=10,
            limit_current=10,
            status=CampaignStatus.ACTIVE,
        )
        session.add(campaign)
        await session.commit()
        campaign_id = campaign.id

    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    callback = SimpleNamespace(
        data=f"admin:campaign:{campaign_id}:cancel:confirm",
        from_user=SimpleNamespace(id=1),
        message=message,
        answer=AsyncMock(),
    )

    await confirm_campaign_cancel(callback, session_factory)

    assert message.edit_text.await_args.args == ("Кампаний пока нет.",)
    assert not any(call.args == ("Админ-панель:",) for call in message.answer.await_args_list)
    async with session_factory() as session:
        assert (await session.get(Campaign, campaign_id)).status is CampaignStatus.CANCELLED


@pytest.mark.asyncio
async def test_scheduled_broadcasts_list_does_not_append_admin_menu(session_factory):
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    callback = SimpleNamespace(message=message, answer=AsyncMock())

    await list_scheduled_broadcasts(callback, session_factory)

    assert message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert not message.answer.await_args_list


@pytest.mark.asyncio
async def test_code_search_found_returns_card_without_admin_menu(session_factory):
    async with session_factory() as session:
        session.add(MovieCode(code="123", title="Film", status=MovieCodeStatus.ACTIVE))
        await session.commit()
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(CodeSearchForm.waiting_code)
    message = SimpleNamespace(text="123", answer=AsyncMock())

    await receive_code_search(message, state, session_factory)

    assert message.answer.await_args.kwargs["reply_markup"].inline_keyboard
    assert not any(call.args == ("Админ-панель:",) for call in message.answer.await_args_list)
    await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Админка", "админка"])
async def test_text_admin_menu_is_admin_only_router_handler(text):
    message = SimpleNamespace(text=text, answer=AsyncMock())

    await text_admin_menu(message)

    message.answer.assert_awaited_once_with(
        "Админ-панель:", reply_markup=_admin_menu_keyboard()
    )
    router_filter = admin_router.message._handler.filters[0].callback
    assert isinstance(router_filter, IsAdmin)


def test_admin_menu_and_gate_copy_are_polished():
    assert _admin_menu_keyboard().inline_keyboard[3][0].text == "👥 Рефоводы"
    assert render_gate_text([]) == render_gate_text([Sponsor(
        chat_id=-1, title="Cinema", type=SponsorType.CHANNEL
    )]) == "<b>🤖 Чтобы бот выдал название по найденному коду - подпишитесь на все каналы ниже:</b>"
    assert render_menu_text(SimpleNamespace(is_partner=False)) == (
        "🍏 Доступ открыт. Чтобы получить название - отправьте найденный Вами код."
    )


@pytest.mark.asyncio
async def test_code_add_copy_uses_entered_code():
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(CodeAddForm.waiting_code)
    message = SimpleNamespace(text="777", answer=AsyncMock())

    async def get_none(*_args):
        return None

    session = SimpleNamespace(get=get_none)
    session_factory = lambda: _AsyncContext(session)
    await receive_new_code(message, state, session_factory)

    assert message.answer.await_args.args[0] == (
        "Теперь — пришли название для кода «777». Пример: Поднятие уровня в одиночку."
    )
    await storage.close()


@pytest.mark.asyncio
async def test_private_sponsor_requires_invite_link(session_factory):
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.set_state(SponsorForm.waiting_invite_link)
    await state.update_data(chat_id=-555, title="Private", username=None, type="channel")
    message = SimpleNamespace(text="-", answer=AsyncMock())

    await receive_invite_link(message, state, session_factory)

    assert await state.get_state() == SponsorForm.waiting_invite_link.state
    assert "нет публичного username" in message.answer.await_args.args[0]
    async with session_factory() as session:
        assert await session.get(Sponsor, -555) is None
    await storage.close()


@pytest.mark.asyncio
async def test_known_client_code_sends_title_then_next_request_prompt(session_factory):
    async with session_factory() as session:
        session.add(MovieCode(code="123", title="Film", status=MovieCodeStatus.ACTIVE))
        await session.commit()
    message = SimpleNamespace(text="123", answer=AsyncMock())

    await handle_movie_code(message, session_factory, SimpleNamespace())

    calls = message.answer.await_args_list
    assert calls[0].args[0] == "Film"
    assert calls[1].args[0] == (
        "Приятного просмотра! Буду ждать - Ваш следующий запрос "
        '<tg-emoji emoji-id="5334917262207889846">⏳</tg-emoji>.'
    )
    assert calls[1].kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_unknown_client_code_uses_new_copy(session_factory):
    message = SimpleNamespace(text="404", answer=AsyncMock())
    await handle_movie_code(message, session_factory, SimpleNamespace())
    message.answer.assert_awaited_once_with(
        "🍎 Код «404» не найден. Возможно - была допущена ошибка, "
        "проверьте правильность написания номера."
    )


async def _create_partner(session_factory, telegram_id: int = 100) -> None:
    async with session_factory() as session:
        admin = Admin(telegram_id=900)
        session.add_all([admin, User(telegram_id=telegram_id, username="partner", full_name="Partner")])
        await session.flush()
        session.add(ReferralPartner(
            telegram_id=telegram_id,
            referral_code=f"partner-{telegram_id}",
            approved_by_admin_id=admin.id,
        ))
        await session.commit()


def _partner_callback(data: str, message=None):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=900),
        message=message or SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_partners_page_paginates_partner_buttons_and_navigation(session_factory):
    async with session_factory() as session:
        admin = Admin(telegram_id=900)
        session.add(admin)
        await session.flush()
        for telegram_id in range(100, 115):
            session.add(User(telegram_id=telegram_id, full_name=f"Partner {telegram_id}"))
        await session.flush()
        for telegram_id in range(100, 115):
            session.add(ReferralPartner(
                telegram_id=telegram_id,
                referral_code=f"code-{telegram_id}",
                approved_by_admin_id=admin.id,
            ))
        await session.commit()

    first, first_users, total_pages, price = await _get_partners_page(session_factory, 0)
    second, second_users, _, _ = await _get_partners_page(session_factory, 1)
    first_keyboard = _partners_page_keyboard(first, first_users, 0, total_pages, price)
    second_keyboard = _partners_page_keyboard(second, second_users, 1, total_pages, price)

    assert len(first) == 10
    assert len(second) == 5
    assert "▶️ След" in [button.text for row in first_keyboard.inline_keyboard for button in row]
    assert "◀️ Пред" not in [button.text for row in first_keyboard.inline_keyboard for button in row]
    assert "◀️ Пред" in [button.text for row in second_keyboard.inline_keyboard for button in row]
    assert "▶️ След" not in [button.text for row in second_keyboard.inline_keyboard for button in row]

    callback = _partner_callback("admin:partners")
    await _edit_partners_page(callback, session_factory, 0)
    assert callback.message.edit_text.await_args.args[0] == "Рефоводы (стр. 1/2) · цена подписки: 0.00 ₽:"
    assert first_keyboard.inline_keyboard[0][0].text == "💵 Цена подписки: 0.00 ₽ (изменить)"


@pytest.mark.asyncio
async def test_subscription_price_flow_saves_comma_value_and_returns_partners_list(session_factory):
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=900))
    callback = _partner_callback("admin:partners:price")

    await request_subscription_price(callback, state)
    assert await state.get_state() == PricingForm.waiting_price.state

    message = SimpleNamespace(text="0,5", answer=AsyncMock())
    await receive_subscription_price(message, state, session_factory)

    async with session_factory() as session:
        assert await get_subscription_price(session) == Decimal("0.50")
    assert await state.get_state() is None
    assert message.answer.await_args_list[0].args == ("Цена подписки обновлена: 0.50 ₽.",)
    partners_menu = message.answer.await_args_list[1]
    assert "Цена подписки: 0.50 ₽" in partners_menu.args[0]
    assert partners_menu.kwargs["reply_markup"].inline_keyboard[0][0].text == (
        "💵 Цена подписки: 0.50 ₽ (изменить)"
    )
    assert not any(call.args == ("Админ-панель:",) for call in message.answer.await_args_list)
    await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["не число", "-1", "NaN", "Infinity"])
async def test_subscription_price_flow_rejects_invalid_values(session_factory, value):
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=900))
    await state.set_state(PricingForm.waiting_price)
    message = SimpleNamespace(text=value, answer=AsyncMock())

    await receive_subscription_price(message, state, session_factory)

    assert await state.get_state() == PricingForm.waiting_price.state
    assert "неотрицательную конечную цену" in message.answer.await_args.args[0]
    async with session_factory() as session:
        assert await get_subscription_price(session) == Decimal("0")
    await storage.close()


@pytest.mark.asyncio
async def test_subscription_price_flow_cancel_does_not_change_price(session_factory):
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=900))
    await state.set_state(PricingForm.waiting_price)
    message = SimpleNamespace(text="❌ Отмена", answer=AsyncMock())

    await receive_subscription_price(message, state, session_factory)

    assert await state.get_state() is None
    async with session_factory() as session:
        assert await get_subscription_price(session) == Decimal("0")
    await storage.close()


@pytest.mark.asyncio
async def test_partner_card_includes_stats_and_formatted_balance(session_factory):
    await _create_partner(session_factory)
    async with session_factory() as session:
        session.add_all([User(telegram_id=201), User(telegram_id=202)])
        await session.flush()
        session.add_all([
            ReferralEvent(referred_user_telegram_id=201, referrer_telegram_id=100),
            ReferralEvent(referred_user_telegram_id=202, referrer_telegram_id=100),
        ])
        await session.commit()
        await add_partner_balance_adjustment(session, 100, 900, Decimal("125.50"), "Bonus")

    partner, user, started, confirmed, balance = await _get_partner_card_data(session_factory, 100)
    text = _partner_card_text(partner, user, started, confirmed, balance)

    assert format_partner_stats_text(2, 0) in text
    assert "Баланс: 125.50 ₽" in text


@pytest.mark.asyncio
async def test_confirm_partner_revoke_returns_updated_card_without_admin_menu(session_factory):
    await _create_partner(session_factory)
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())

    await request_partner_revoke(_partner_callback("admin:partner:100:revoke", message))
    await confirm_partner_revoke(
        _partner_callback("admin:partner:100:revoke:confirm", message), session_factory
    )

    async with session_factory() as session:
        partner = await session.scalar(
            select(ReferralPartner).where(ReferralPartner.telegram_id == 100)
        )
        assert partner.revoked_at is not None
    text = message.edit_text.await_args.args[0]
    keyboard = message.edit_text.await_args.kwargs["reply_markup"]
    assert "Статус: отозван" in text
    assert "🚫 Отозвать" not in [button.text for row in keyboard.inline_keyboard for button in row]
    assert not any(call.args == ("Админ-панель:",) for call in message.answer.await_args_list)


@pytest.mark.asyncio
async def test_zero_partner_balance_creates_withdrawal_and_returns_card(session_factory):
    await _create_partner(session_factory)
    async with session_factory() as session:
        await add_partner_balance_adjustment(session, 100, 900, Decimal("150.75"), "Bonus")
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())

    await request_partner_balance_zero(
        _partner_callback("admin:partner:100:balance:zero", message), session_factory
    )
    assert "текущий баланс: 150.75 ₽" in message.answer.await_args.args[0]
    callback = _partner_callback("admin:partner:100:balance:zero:confirm", message)
    await confirm_partner_balance_zero(callback, session_factory)

    async with session_factory() as session:
        assert await get_partner_balance(session, 100) == Decimal("0.00")
        adjustments = list(await session.scalars(select(PartnerBalanceAdjustment)))
    assert [(adjustment.amount, adjustment.title) for adjustment in adjustments] == [
        (Decimal("150.75"), "Bonus"),
        (Decimal("-150.75"), "Вывод"),
    ]
    assert "Баланс: 0.00 ₽" in message.edit_text.await_args.args[0]
    assert not any(call.args == ("Админ-панель:",) for call in message.answer.await_args_list)


@pytest.mark.asyncio
async def test_zero_balance_alert_does_not_create_adjustment(session_factory):
    await _create_partner(session_factory)
    callback = _partner_callback("admin:partner:100:balance:zero")

    await request_partner_balance_zero(callback, session_factory)

    callback.answer.assert_awaited_once_with("Баланс уже 0.", show_alert=True)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(PartnerBalanceAdjustment)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["words", "0", "-12.50"])
async def test_partner_balance_amount_rejects_invalid_values(text):
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=900))
    await state.set_state(PartnerBalanceForm.waiting_amount)
    message = SimpleNamespace(text=text, answer=AsyncMock())

    await receive_partner_balance_amount(message, state)

    assert await state.get_state() == PartnerBalanceForm.waiting_amount.state
    assert "положительную сумму" in message.answer.await_args.args[0]
    await storage.close()


@pytest.mark.asyncio
async def test_partner_balance_amount_accepts_comma_and_advances_to_title():
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=900))
    await state.set_state(PartnerBalanceForm.waiting_amount)
    message = SimpleNamespace(text="500,50", answer=AsyncMock())

    await receive_partner_balance_amount(message, state)

    assert await state.get_state() == PartnerBalanceForm.waiting_title.state
    assert (await state.get_data())["amount"] == "500.50"
    await storage.close()


@pytest.mark.asyncio
async def test_partner_balance_title_requires_nonempty_value():
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=900))
    await state.set_state(PartnerBalanceForm.waiting_title)
    message = SimpleNamespace(text="  ", answer=AsyncMock())

    await receive_partner_balance_title(message, state, None)

    assert await state.get_state() == PartnerBalanceForm.waiting_title.state
    assert "непустое название" in message.answer.await_args.args[0]
    await storage.close()


@pytest.mark.asyncio
async def test_partner_balance_add_creates_exact_adjustment_and_returns_card(session_factory):
    await _create_partner(session_factory)
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=900))
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())

    await request_partner_balance_add(_partner_callback("admin:partner:100:balance:add", message), state)
    await receive_partner_balance_amount(SimpleNamespace(text="500,50", answer=AsyncMock()), state)
    title_message = SimpleNamespace(text="Доплата", from_user=SimpleNamespace(id=900), answer=AsyncMock())
    await receive_partner_balance_title(title_message, state, session_factory)

    async with session_factory() as session:
        adjustment = await session.scalar(select(PartnerBalanceAdjustment))
    assert (adjustment.amount, adjustment.title) == (Decimal("500.50"), "Доплата")
    assert await state.get_state() is None
    assert any("Баланс: 500.50 ₽" in call.args[0] for call in title_message.answer.await_args_list)
    assert not any(call.args == ("Админ-панель:",) for call in title_message.answer.await_args_list)
    await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("form", [PartnerBalanceForm.waiting_amount, PartnerBalanceForm.waiting_title])
async def test_partner_balance_cancel_clears_state_without_adjustment(form, session_factory):
    await _create_partner(session_factory)
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=900))
    await state.set_state(form)
    await state.update_data(partner_telegram_id=100, amount="1")
    message = SimpleNamespace(text="❌ Отмена", from_user=SimpleNamespace(id=900), answer=AsyncMock())

    if form == PartnerBalanceForm.waiting_amount:
        await receive_partner_balance_amount(message, state)
    else:
        await receive_partner_balance_title(message, state, session_factory)

    assert await state.get_state() is None
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(PartnerBalanceAdjustment)) == 0
    await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("viewer", "role", "can_manage", "can_add_cards"),
    [(900, "admin", False, False), (900, "admin", True, True), (901, "owner", False, True)],
)
async def test_admins_menu_shows_management_controls_only_to_authorized_viewers(
    session_factory, viewer, role, can_manage, can_add_cards
):
    async with session_factory() as session:
        rows = [
            Admin(telegram_id=viewer, role=role, can_manage_admins=can_manage),
            Admin(telegram_id=100, role="admin"),
            Admin(telegram_id=101, role="admin", revoked_at=datetime.now(timezone.utc)),
        ]
        if role != "owner":
            rows.append(Admin(telegram_id=901, role="owner"))
        session.add_all(rows)
        await session.commit()
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=viewer), message=SimpleNamespace(edit_text=AsyncMock()), answer=AsyncMock()
    )

    await admins_menu(callback, session_factory)

    keyboard = callback.message.edit_text.await_args.kwargs["reply_markup"]
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert any("Администраторы:" in call.args[0] for call in callback.message.edit_text.await_args_list)
    assert ("➕ Добавить администратора" in [button.text for button in buttons]) is can_add_cards
    card_ids = {button.callback_data for button in buttons if button.callback_data.endswith(":card")}
    expected_ids = {100}
    if role == "admin":
        expected_ids.add(viewer)
    assert card_ids == ({f"admin:admins:{telegram_id}:card" for telegram_id in expected_ids} if can_add_cards else set())


@pytest.mark.asyncio
async def test_admin_grant_flow_creates_permissions_clears_state_and_returns_admins_list(session_factory):
    async with session_factory() as session:
        session.add_all([Admin(telegram_id=900, role="owner"), User(telegram_id=100)])
        await session.commit()
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=900))
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    callback = _partner_callback("admin:admins:add", message)

    await add_admin_start(callback, state, session_factory)
    assert await state.get_state() == AdminGrantForm.waiting_user.state
    await receive_admin_user(SimpleNamespace(text="100", answer=AsyncMock()), state)
    await choose_admin_manage_permission(
        _partner_callback("admin:admins:grant:manage_admins:yes", message), state, session_factory
    )
    await choose_admin_payout_permission(
        _partner_callback("admin:admins:grant:payouts:no", message), state, session_factory
    )

    async with session_factory() as session:
        admin = await session.scalar(select(Admin).where(Admin.telegram_id == 100))
    assert (admin.can_manage_admins, admin.can_manage_payouts) == (True, False)
    assert await state.get_state() is None
    assert "Администраторы:" in message.edit_text.await_args.args[0]
    assert not any(call.args == ("Админ-панель:",) for call in message.answer.await_args_list)
    await storage.close()


@pytest.mark.asyncio
async def test_admin_grant_flow_queues_user_who_never_started(session_factory):
    async with session_factory() as session:
        session.add(Admin(telegram_id=900, role="owner"))
        await session.commit()
    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=900))
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    callback = _partner_callback("admin:admins:add", message)

    await add_admin_start(callback, state, session_factory)
    await receive_admin_user(SimpleNamespace(text="100", answer=AsyncMock()), state)
    await choose_admin_manage_permission(
        _partner_callback("admin:admins:grant:manage_admins:no", message), state, session_factory
    )
    await choose_admin_payout_permission(
        _partner_callback("admin:admins:grant:payouts:yes", message), state, session_factory
    )

    async with session_factory() as session:
        grant = await session.get(PendingAdminGrant, 100)
    assert (
        grant.requested_by_admin_telegram_id,
        grant.can_manage_admins,
        grant.can_manage_payouts,
    ) == (900, False, True)
    assert await state.get_state() is None
    assert any("Заявка сохранена" in call.args[0] for call in message.answer.await_args_list)
    assert any(
        call.args == ("Админ-панель:",)
        and call.kwargs["reply_markup"] == _admin_menu_keyboard()
        for call in message.answer.await_args_list
    )
    await storage.close()


@pytest.mark.asyncio
async def test_toggle_admin_permission_changes_flag_and_denies_unprivileged_viewer(session_factory):
    async with session_factory() as session:
        session.add_all([
            Admin(telegram_id=900, role="owner"),
            Admin(telegram_id=901, role="admin", can_manage_admins=False),
            Admin(telegram_id=100, role="admin", can_manage_payouts=False),
        ])
        await session.commit()
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    await toggle_admin_permission(
        _partner_callback("admin:admins:100:toggle:payouts", message), session_factory
    )
    async with session_factory() as session:
        assert (await session.scalar(select(Admin).where(Admin.telegram_id == 100))).can_manage_payouts is True

    denied = SimpleNamespace(
        data="admin:admins:100:toggle:payouts", from_user=SimpleNamespace(id=901),
        message=message, answer=AsyncMock(),
    )
    await toggle_admin_permission(denied, session_factory)
    denied.answer.assert_awaited_once_with("Недостаточно прав.", show_alert=True)
    async with session_factory() as session:
        assert (await session.scalar(select(Admin).where(Admin.telegram_id == 100))).can_manage_payouts is True


@pytest.mark.asyncio
async def test_confirm_admin_revoke_disables_admin_and_handles_owner(session_factory):
    async with session_factory() as session:
        session.add_all([Admin(telegram_id=900, role="owner"), Admin(telegram_id=100, role="admin")])
        await session.commit()
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    await confirm_admin_revoke(_partner_callback("admin:admins:100:revoke:confirm", message), session_factory)
    async with session_factory() as session:
        assert await is_admin(session, 100) is False

    owner_callback = _partner_callback("admin:admins:900:revoke:confirm", message)
    await confirm_admin_revoke(owner_callback, session_factory)
    owner_callback.answer.assert_awaited_once_with("Нельзя отозвать owner-а.", show_alert=True)


@pytest.mark.asyncio
async def test_revoked_partner_moves_to_archive_and_can_be_restored(session_factory):
    await _create_partner(session_factory, telegram_id=100)

    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    await confirm_partner_revoke(_partner_callback("admin:partner:100:revoke:confirm", message), session_factory)

    active, _, active_total, _ = await _get_partners_page(session_factory, 0)
    archived, _, archived_total, _ = await _get_partners_page(session_factory, 0, revoked=True)
    assert [p.telegram_id for p in active] == []
    assert active_total == 0
    assert [p.telegram_id for p in archived] == [100]
    assert archived_total == 1

    list_callback = _partner_callback("admin:partners:archived:0", message)
    await archived_partners_page(list_callback, session_factory)
    assert "Архив рефоводов" in message.edit_text.await_args.args[0]

    await unrevoke_partner_handler(_partner_callback("admin:partner:100:unrevoke", message), session_factory)

    active, _, active_total, _ = await _get_partners_page(session_factory, 0)
    archived, _, archived_total, _ = await _get_partners_page(session_factory, 0, revoked=True)
    assert [p.telegram_id for p in active] == [100]
    assert active_total == 1
    assert archived == []
    assert archived_total == 0

    card_text = message.edit_text.await_args.args[0]
    card_keyboard = message.edit_text.await_args.kwargs["reply_markup"]
    assert "отозван" not in card_text
    button_texts = [button.text for row in card_keyboard.inline_keyboard for button in row]
    assert "🚫 Отозвать" in button_texts
    assert "♻️ Восстановить" not in button_texts


def test_partner_card_balance_buttons_require_payout_permission():
    partner = ReferralPartner(telegram_id=100, referral_code="partner", approved_by_admin_id=1)
    denied = _partner_card_keyboard(partner, viewer_can_manage_payouts=False)
    allowed = _partner_card_keyboard(partner, viewer_can_manage_payouts=True)
    denied_text = [button.text for row in denied.inline_keyboard for button in row]
    allowed_text = [button.text for row in allowed.inline_keyboard for button in row]
    assert "💵 Обнулить баланс (Вывод)" not in denied_text
    assert "➕ Добавить средства" not in denied_text
    assert {"💵 Обнулить баланс (Вывод)", "➕ Добавить средства"} <= set(allowed_text)


def test_partner_card_bonus_clear_button_is_shown_only_for_active_bonus():
    today = datetime.now(timezone.utc).date()
    active_partner = ReferralPartner(
        telegram_id=100,
        referral_code="partner",
        approved_by_admin_id=1,
        bonus_rate=Decimal("25.50"),
        bonus_rate_until=today,
    )
    inactive_partner = ReferralPartner(
        telegram_id=101,
        referral_code="partner-inactive",
        approved_by_admin_id=1,
    )

    active_buttons = [
        button for row in _partner_card_keyboard(
            active_partner, viewer_can_manage_payouts=False
        ).inline_keyboard for button in row
    ]
    inactive_buttons = [
        button for row in _partner_card_keyboard(
            inactive_partner, viewer_can_manage_payouts=False
        ).inline_keyboard for button in row
    ]

    assert any(
        button.text == "❌ Отменить бонус"
        and button.callback_data == "admin:partner:100:bonus:clear"
        for button in active_buttons
    )
    assert all(button.text != "❌ Отменить бонус" for button in inactive_buttons)


@pytest.mark.asyncio
async def test_clear_partner_bonus_handler_clears_bonus_and_rerenders_card(session_factory):
    await _create_partner(session_factory)
    async with session_factory() as session:
        partner = await session.scalar(
            select(ReferralPartner).where(ReferralPartner.telegram_id == 100)
        )
        partner.bonus_rate = Decimal("25.50")
        partner.bonus_rate_until = datetime.now(timezone.utc).date()
        await session.commit()
    message = SimpleNamespace(answer=AsyncMock(), edit_text=AsyncMock())
    callback = _partner_callback("admin:partner:100:bonus:clear", message)

    await clear_partner_bonus_handler(callback, session_factory)

    async with session_factory() as session:
        partner = await session.scalar(
            select(ReferralPartner).where(ReferralPartner.telegram_id == 100)
        )
        assert partner.bonus_rate is None
        assert partner.bonus_rate_until is None
    assert "Бонусная ставка:" not in message.edit_text.await_args.args[0]
    callback.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_balance_actions_deny_admin_without_payout_permission(session_factory):
    await _create_partner(session_factory)
    async with session_factory() as session:
        viewer = await session.scalar(select(Admin).where(Admin.telegram_id == 900))
        viewer.role = "admin"
        viewer.can_manage_payouts = False
        await session.commit()
    zero = _partner_callback("admin:partner:100:balance:zero")
    await request_partner_balance_zero(zero, session_factory)
    zero.answer.assert_awaited_once_with("Недостаточно прав для управления выплатами.", show_alert=True)

    storage = MemoryStorage()
    state = FSMContext(storage, StorageKey(bot_id=1, chat_id=1, user_id=900))
    add = _partner_callback("admin:partner:100:balance:add")
    await request_partner_balance_add(add, state, session_factory)
    add.answer.assert_awaited_once_with("Недостаточно прав для управления выплатами.", show_alert=True)
    assert await state.get_state() is None
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(PartnerBalanceAdjustment)) == 0
    await storage.close()


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False
