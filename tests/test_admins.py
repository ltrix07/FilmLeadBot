from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from types import SimpleNamespace

from app.bot.routers.admin import (
    PartnerForm,
    SponsorForm,
    _admin_menu_keyboard,
    _process_partner_user,
    _process_sponsor_chat,
    receive_partner_user,
    receive_sponsor_chat,
)
from app.db.models import Admin, PendingPartnerGrant, SponsorType, User
from app.services.admins import (
    add_admin,
    bot_status_allows_access,
    can_manage_admins,
    can_manage_payouts,
    ensure_seed_admins,
    is_admin,
    revoke_admin,
    set_admin_permissions,
)


class _State:
    def __init__(self) -> None:
        self.data = {}
        self.current_state = None

    async def update_data(self, **data) -> None:
        self.data.update(data)

    async def set_state(self, state) -> None:
        self.current_state = state

    async def clear(self) -> None:
        self.data.clear()
        self.current_state = None

    async def get_state(self):
        return self.current_state


class _Message:
    def __init__(self, text: str | None = None, user_id: int = 800) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.answers = []

    async def answer(self, text, **kwargs) -> None:
        self.answers.append((text, kwargs))


@pytest.mark.asyncio
async def test_seed_admins_is_idempotent(session_factory):
    async with session_factory() as session:
        await ensure_seed_admins(session, [100, 200])

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Admin)) == 2
        assert await is_admin(session, 100) is True
        assert await is_admin(session, 999) is False

    async with session_factory() as session:
        await ensure_seed_admins(session, [100, 200])

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Admin)) == 2


@pytest.mark.asyncio
async def test_admin_permission_defaults_and_owner_seed_backfill_values(session_factory):
    async with session_factory() as session:
        session.add(Admin(telegram_id=10, role="owner", can_manage_admins=True, can_manage_payouts=True))
        session.add(Admin(telegram_id=20, role="admin"))
        await session.commit()

    async with session_factory() as session:
        owner = await session.scalar(select(Admin).where(Admin.telegram_id == 10))
        admin = await session.scalar(select(Admin).where(Admin.telegram_id == 20))
    # Existing owners receive these values from the migration data backfill.
    assert (owner.can_manage_admins, owner.can_manage_payouts) == (True, True)
    assert admin.can_manage_admins is False
    assert admin.can_manage_payouts is False
    assert admin.revoked_at is None


def test_admin_permissions_migration_contains_owner_backfill():
    migration_path = (
        "alembic/versions/b4e8f2a1c6d9_add_admin_permissions.py"
    )
    with open(migration_path, encoding="utf-8") as migration:
        source = migration.read()

    assert "can_manage_admins" in source
    assert "can_manage_payouts" in source
    assert "UPDATE admins SET can_manage_admins = true, can_manage_payouts = true WHERE role = 'owner'" in source


@pytest.mark.asyncio
async def test_revoked_admin_is_not_admin(session_factory):
    revoked_at = datetime.now(timezone.utc)
    async with session_factory() as session:
        session.add(Admin(telegram_id=100, role="admin", revoked_at=revoked_at))
        await session.commit()

    async with session_factory() as session:
        assert await is_admin(session, 100) is False


@pytest.mark.parametrize(
    ("role", "manage_admins", "manage_payouts", "expected_admins", "expected_payouts"),
    [
        ("owner", False, False, True, True),
        ("admin", True, False, True, False),
        ("admin", False, True, False, True),
    ],
)
def test_permission_helpers_respect_role_and_flags(
    role, manage_admins, manage_payouts, expected_admins, expected_payouts
):
    admin = Admin(
        telegram_id=100,
        role=role,
        can_manage_admins=manage_admins,
        can_manage_payouts=manage_payouts,
    )
    assert can_manage_admins(admin) is expected_admins
    assert can_manage_payouts(admin) is expected_payouts


@pytest.mark.asyncio
async def test_add_admin_creates_updates_and_reactivates_single_record(session_factory):
    async with session_factory() as session:
        session.add(User(telegram_id=100))
        await session.commit()
        created = await add_admin(session, 100, can_manage_admins=True, can_manage_payouts=False)
        assert created.role == "admin"
        assert (created.can_manage_admins, created.can_manage_payouts) == (True, False)
        await revoke_admin(session, 100)
        updated = await add_admin(session, 100, can_manage_admins=False, can_manage_payouts=True)
        assert updated.revoked_at is None
        assert (updated.can_manage_admins, updated.can_manage_payouts) == (False, True)

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Admin)) == 1


@pytest.mark.asyncio
async def test_add_admin_rejects_owner_and_user_who_never_started(session_factory):
    async with session_factory() as session:
        session.add_all([User(telegram_id=100), Admin(telegram_id=100, role="owner")])
        await session.commit()
        with pytest.raises(LookupError, match="already_owner"):
            await add_admin(session, 100, can_manage_admins=False, can_manage_payouts=False)
        with pytest.raises(LookupError, match="user_not_started"):
            await add_admin(session, 200, can_manage_admins=False, can_manage_payouts=False)


@pytest.mark.asyncio
async def test_set_admin_permissions_updates_only_requested_fields_and_protects_owner(session_factory):
    async with session_factory() as session:
        session.add_all([
            Admin(telegram_id=100, role="admin", can_manage_admins=False, can_manage_payouts=False),
            Admin(telegram_id=200, role="owner"),
        ])
        await session.commit()
        changed = await set_admin_permissions(session, 100, can_manage_admins=True)
        assert (changed.can_manage_admins, changed.can_manage_payouts) == (True, False)
        changed = await set_admin_permissions(session, 100, can_manage_payouts=True)
        assert (changed.can_manage_admins, changed.can_manage_payouts) == (True, True)
        assert await set_admin_permissions(session, 999, can_manage_admins=True) is None
        with pytest.raises(LookupError, match="cannot_modify_owner"):
            await set_admin_permissions(session, 200, can_manage_admins=False)


@pytest.mark.asyncio
async def test_revoke_admin_is_idempotent_and_protects_owner(session_factory):
    async with session_factory() as session:
        session.add_all([Admin(telegram_id=100, role="admin"), Admin(telegram_id=200, role="owner")])
        await session.commit()
        revoked = await revoke_admin(session, 100)
        first_revoked_at = revoked.revoked_at
        assert first_revoked_at is not None
        assert (await revoke_admin(session, 100)).revoked_at == first_revoked_at
        assert await revoke_admin(session, 999) is None
        with pytest.raises(LookupError, match="cannot_revoke_owner"):
            await revoke_admin(session, 200)


@pytest.mark.parametrize(
    ("chat_type", "status", "expected"),
    [
        (SponsorType.CHANNEL, "administrator", True),
        (SponsorType.CHANNEL, "member", False),
        (SponsorType.GROUP, "member", True),
        (SponsorType.SUPERGROUP, "left", False),
    ],
)
def test_bot_status_allows_access(chat_type, status, expected):
    assert bot_status_allows_access(chat_type, status) is expected


@pytest.mark.asyncio
async def test_process_sponsor_chat_saves_selected_chat_and_asks_for_request_mode():
    state = _State()
    message = _Message()
    chat = SimpleNamespace(id=-100, type="channel", title="Cinema", username="cinema")
    bot = SimpleNamespace(
        id=42,
        get_chat_member=lambda *_: _return(SimpleNamespace(status="administrator")),
    )

    await _process_sponsor_chat(chat, message, state, bot)

    assert state.data == {
        "chat_id": -100,
        "title": "Cinema",
        "username": "cinema",
        "type": "channel",
    }
    assert state.current_state == SponsorForm.waiting_request_mode


@pytest.mark.asyncio
async def test_process_partner_user_approves_selected_user(session_factory):
    async with session_factory() as session:
        session.add_all([Admin(telegram_id=800), User(telegram_id=100)])
        await session.commit()

    state = _State()
    message = _Message(user_id=800)
    await _process_partner_user(100, message, state, session_factory)

    assert state.current_state is None
    assert any("Рефовод #100 добавлен" in text for text, _ in message.answers)


@pytest.mark.asyncio
async def test_process_partner_user_queues_grant_for_user_who_never_started(session_factory):
    async with session_factory() as session:
        session.add(Admin(telegram_id=800, role="owner"))
        await session.commit()

    state = _State()
    await state.set_state(PartnerForm.waiting_user)
    message = _Message(user_id=800)
    await _process_partner_user(100, message, state, session_factory)

    async with session_factory() as session:
        grant = await session.get(PendingPartnerGrant, 100)
    assert grant.requested_by_admin_telegram_id == 800
    assert await state.get_state() is None
    assert any("Заявка сохранена" in text for text, _ in message.answers)
    assert any(
        text == "Админ-панель:" and kwargs["reply_markup"] == _admin_menu_keyboard()
        for text, kwargs in message.answers
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "form_state", "kwargs"),
    [
        (receive_sponsor_chat, SponsorForm.waiting_chat, {"bot": SimpleNamespace()}),
        (receive_partner_user, PartnerForm.waiting_user, {"session_factory": None}),
    ],
)
async def test_reply_keyboard_cancel_clears_form_state(handler, form_state, kwargs):
    state = _State()
    state.current_state = form_state
    message = _Message(text="❌ Отмена")

    await handler(message, state, **kwargs)

    assert state.current_state is None
    assert message.answers[0][0] == "Отменено."


async def _return(value):
    return value
