from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.routers.menu import handle_movie_code
from app.db.models import (
    Admin,
    Campaign,
    CampaignStatus,
    MovieCode,
    MovieCodeStatus,
    ReferralEvent,
    ReferralPartner,
    ReferralSubscription,
    Sponsor,
    SponsorType,
    User,
)
from app.services.stats import get_overview_stats, get_top_codes, get_top_partners


@pytest.mark.asyncio
async def test_movie_code_lookup_increments_only_for_active_codes(session_factory):
    async with session_factory() as session:
        session.add_all([
            MovieCode(code="active", title="Active", status=MovieCodeStatus.ACTIVE),
            MovieCode(code="inactive", title="Inactive", status=MovieCodeStatus.INACTIVE),
        ])
        await session.commit()

    message = SimpleNamespace(text="active", answer=AsyncMock())
    await handle_movie_code(message, session_factory, None)
    await handle_movie_code(message, session_factory, None)
    for code in ("missing", "inactive"):
        await handle_movie_code(SimpleNamespace(text=code, answer=AsyncMock()), session_factory, None)

    async with session_factory() as session:
        active = await session.get(MovieCode, "active")
        inactive = await session.get(MovieCode, "inactive")
    assert active.lookup_count == 2
    assert inactive.lookup_count == 0


@pytest.mark.asyncio
async def test_overview_and_top_code_stats(session_factory):
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        admin = Admin(telegram_id=900)
        session.add(admin)
        session.add_all([
            User(telegram_id=1),
            User(telegram_id=2, blocked_at=now),
            User(telegram_id=3),
        ])
        session.add_all([
            Sponsor(
                chat_id=10,
                title="Available",
                type=SponsorType.CHANNEL,
                bot_has_access=True,
                own_channel=True,
            ),
            Sponsor(chat_id=11, title="Unavailable", type=SponsorType.GROUP, bot_has_access=False),
        ])
        await session.flush()
        session.add_all([
            Campaign(sponsor_chat_id=10, limit_original=10, limit_current=10, status=CampaignStatus.ACTIVE),
            Campaign(sponsor_chat_id=11, limit_original=10, limit_current=10, status=CampaignStatus.PAUSED),
            MovieCode(code="popular", title="Popular", status=MovieCodeStatus.ACTIVE, lookup_count=5),
            MovieCode(code="unused", title="Unused", status=MovieCodeStatus.ACTIVE, lookup_count=0),
            MovieCode(code="hidden", title="Hidden", status=MovieCodeStatus.INACTIVE, lookup_count=10),
            ReferralPartner(
                telegram_id=1,
                referral_code="active-partner",
                approved_by_admin_id=admin.id,
                activated_at=now,
            ),
            ReferralPartner(
                telegram_id=2,
                referral_code="inactive-partner",
                approved_by_admin_id=admin.id,
            ),
        ])
        await session.commit()

    async with session_factory() as session:
        overview = await get_overview_stats(session)
        codes = await get_top_codes(session)

    assert overview.users_total == 3
    assert overview.users_blocked == 1
    assert overview.sponsors_total == 2
    assert overview.sponsors_with_access == 1
    assert overview.campaigns_total == 2
    assert overview.campaigns_active == 1
    assert overview.codes_active == 2
    assert overview.codes_inactive == 1
    assert overview.partners_total == 2
    assert overview.partners_active == 1
    assert [code.code for code in codes] == ["popular"]


@pytest.mark.asyncio
async def test_top_partners_orders_by_confirmed_and_respects_limit(session_factory):
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        admin = Admin(telegram_id=900)
        session.add(admin)
        session.add_all(User(telegram_id=user_id) for user_id in range(1, 8))
        await session.flush()
        session.add_all([
            Sponsor(chat_id=12, title="Referral sponsor", type=SponsorType.CHANNEL),
            ReferralPartner(telegram_id=1, referral_code="one", approved_by_admin_id=admin.id),
            ReferralPartner(telegram_id=2, referral_code="two", approved_by_admin_id=admin.id),
            ReferralEvent(referred_user_telegram_id=3, referrer_telegram_id=1, confirmed_at=now),
            ReferralEvent(referred_user_telegram_id=4, referrer_telegram_id=1),
            ReferralEvent(referred_user_telegram_id=5, referrer_telegram_id=2, confirmed_at=now),
            ReferralEvent(referred_user_telegram_id=6, referrer_telegram_id=2, confirmed_at=now),
        ])
        await session.flush()
        campaign = Campaign(
            sponsor_chat_id=12, limit_original=10, limit_current=10, status=CampaignStatus.ACTIVE
        )
        session.add(campaign)
        await session.flush()
        session.add_all([
            ReferralSubscription(campaign_id=campaign.id, referrer_telegram_id=1, referred_user_telegram_id=3),
            ReferralSubscription(campaign_id=campaign.id, referrer_telegram_id=1, referred_user_telegram_id=4),
            ReferralSubscription(campaign_id=campaign.id, referrer_telegram_id=2, referred_user_telegram_id=5),
        ])
        await session.commit()

    async with session_factory() as session:
        partners = await get_top_partners(session, limit=1)

    assert [(partner.telegram_id, started, confirmed) for partner, started, confirmed in partners] == [
        (1, 2, 2)
    ]


@pytest.mark.asyncio
async def test_top_partners_excludes_own_channel_credits(session_factory):
    async with session_factory() as session:
        admin = Admin(telegram_id=900)
        session.add(admin)
        session.add_all(User(telegram_id=user_id) for user_id in range(1, 9))
        session.add_all([
            Sponsor(chat_id=13, title="Own", type=SponsorType.CHANNEL, own_channel=True),
            Sponsor(chat_id=14, title="Partner", type=SponsorType.CHANNEL),
        ])
        await session.flush()
        session.add_all([
            ReferralPartner(telegram_id=1, referral_code="one", approved_by_admin_id=admin.id),
            ReferralPartner(telegram_id=2, referral_code="two", approved_by_admin_id=admin.id),
        ])
        own_campaign = Campaign(sponsor_chat_id=13, limit_original=10, limit_current=10, status=CampaignStatus.ACTIVE)
        partner_campaign = Campaign(sponsor_chat_id=14, limit_original=10, limit_current=10, status=CampaignStatus.ACTIVE)
        session.add_all([own_campaign, partner_campaign])
        await session.flush()
        session.add_all([
            ReferralSubscription(campaign_id=own_campaign.id, referrer_telegram_id=1, referred_user_telegram_id=user_id)
            for user_id in (3, 4, 5)
        ] + [
            ReferralSubscription(campaign_id=partner_campaign.id, referrer_telegram_id=2, referred_user_telegram_id=user_id)
            for user_id in (6, 7)
        ])
        await session.commit()

    async with session_factory() as session:
        partners = await get_top_partners(session, limit=1)
    assert [(partner.telegram_id, confirmed) for partner, _, confirmed in partners] == [(2, 2)]
