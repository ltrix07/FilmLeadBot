from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.bot.routers.start import _ensure_user_and_referral
from app.db.models import (
    Admin,
    Campaign,
    CampaignStatus,
    PartnerBalanceAdjustment,
    ReferralEvent,
    ReferralSubscription,
    Sponsor,
    SponsorType,
    User,
)
from app.services.partner_balance import (
    add_partner_balance_adjustment,
    get_partner_balance,
    get_partner_balance_history,
    zero_out_partner_balance,
)
from app.services.settings import get_subscription_price, set_subscription_price
from app.services.subscription import _record_campaign_completions


async def _campaign(session, chat_id: int) -> Campaign:
    session.add(Sponsor(chat_id=chat_id, title=str(chat_id), type=SponsorType.CHANNEL))
    campaign = Campaign(
        sponsor_chat_id=chat_id,
        limit_original=10,
        limit_current=10,
        status=CampaignStatus.ACTIVE,
    )
    session.add(campaign)
    await session.flush()
    return campaign


@pytest.mark.asyncio
async def test_start_captures_and_refreshes_telegram_profile(session_factory):
    async with session_factory() as session:
        await _ensure_user_and_referral(session, 100, None, "old_name", "Old Name")
    async with session_factory() as session:
        await _ensure_user_and_referral(session, 100, None, "new_name", "New Name")
    async with session_factory() as session:
        user = await session.get(User, 100)
        assert (user.username, user.full_name) == ("new_name", "New Name")


@pytest.mark.asyncio
async def test_price_is_snapshotted_on_credit_and_not_recalculated(session_factory):
    async with session_factory() as session:
        session.add_all([User(telegram_id=1), User(telegram_id=2)])
        await session.flush()
        session.add(ReferralEvent(referred_user_telegram_id=2, referrer_telegram_id=1))
        campaign = await _campaign(session, -101)
        await set_subscription_price(session, Decimal("12.50"))
        await _record_campaign_completions(session, [campaign.id], 2)
        await session.commit()

    async with session_factory() as session:
        await set_subscription_price(session, Decimal("99.00"))
        credit = await session.scalar(select(ReferralSubscription))
        assert credit.price_at_credit == Decimal("12.50")
        assert await get_partner_balance(session, 1) == Decimal("12.50")
        assert await get_subscription_price(session) == Decimal("99.00")


@pytest.mark.asyncio
async def test_balance_adjustments_zero_out_and_history(session_factory):
    today = datetime.now(timezone.utc).date()
    async with session_factory() as session:
        session.add_all([Admin(telegram_id=900), User(telegram_id=1), User(telegram_id=2), User(telegram_id=3)])
        first = await _campaign(session, -102)
        second = await _campaign(session, -103)
        session.add_all([
            ReferralSubscription(
                campaign_id=first.id,
                referrer_telegram_id=1,
                referred_user_telegram_id=2,
                price_at_credit=Decimal("10.00"),
            ),
            ReferralSubscription(
                campaign_id=second.id,
                referrer_telegram_id=1,
                referred_user_telegram_id=3,
                price_at_credit=Decimal("15.00"),
            ),
        ])
        await session.commit()
        await add_partner_balance_adjustment(session, 1, 900, Decimal("5.00"), "Бонус")
        assert await get_partner_balance(session, 1) == Decimal("30.00")
        assert await zero_out_partner_balance(session, 1, 900) == Decimal("30.00")
        assert await zero_out_partner_balance(session, 1, 900) == Decimal("0")
        assert await get_partner_balance(session, 1) == Decimal("0.00")
        adjustments = list(await session.scalars(select(PartnerBalanceAdjustment)))
        assert len(adjustments) == 2
        assert await get_partner_balance_history(session, 1) == [
            f"{today:%d.%m}: Подписок — 2, +25.00 ₽",
            f"{today:%d.%m}: Бонус — +5.00 ₽",
            f"{today:%d.%m}: Вывод — -30.00 ₽",
        ]
        assert await get_partner_balance_history(session, 999) == ["История пуста."]


@pytest.mark.asyncio
async def test_balance_and_history_dynamically_exclude_own_channel_credits(session_factory):
    today = datetime.now(timezone.utc).date()
    async with session_factory() as session:
        session.add_all([User(telegram_id=1), User(telegram_id=2), User(telegram_id=3)])
        await session.flush()
        own = await _campaign(session, -104)
        ordinary = await _campaign(session, -105)
        session.add_all([
            ReferralSubscription(campaign_id=own.id, referrer_telegram_id=1, referred_user_telegram_id=2, price_at_credit=Decimal("10.00")),
            ReferralSubscription(campaign_id=ordinary.id, referrer_telegram_id=1, referred_user_telegram_id=3, price_at_credit=Decimal("15.00")),
        ])
        await session.commit()
        assert await get_partner_balance(session, 1) == Decimal("25.00")

        sponsor = await session.get(Sponsor, -104)
        sponsor.own_channel = True
        await session.commit()
        assert await get_partner_balance(session, 1) == Decimal("15.00")
        assert await get_partner_balance_history(session, 1) == [
            f"{today:%d.%m}: Подписок — 1, +15.00 ₽"
        ]

        sponsor.own_channel = False
        await session.commit()
        assert await get_partner_balance(session, 1) == Decimal("25.00")
