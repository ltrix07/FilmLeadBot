from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.bot.keyboards import build_gate_keyboard, sponsor_url
from app.bot.routers.start import _ensure_user_and_referral
from app.db.models import Admin, ReferralEvent, ReferralPartner, Sponsor, SponsorType, User


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
