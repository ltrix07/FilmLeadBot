from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from types import SimpleNamespace

from app.bot.routers.partner import PartnerTriggerFilter
from app.db.models import (
    Admin,
    Campaign,
    CampaignStatus,
    ReferralEvent,
    ReferralPartner,
    ReferralSubscription,
    Sponsor,
    SponsorType,
    User,
)
from app.services.partners import (
    approve_partner,
    format_partner_label,
    get_partner_stats,
    revoke_partner,
)


async def _admin_and_user(session_factory, user_id: int = 100, admin_id: int = 800) -> None:
    async with session_factory() as session:
        session.add_all([Admin(telegram_id=admin_id), User(telegram_id=user_id)])
        await session.commit()


@pytest.mark.asyncio
async def test_approve_partner_requires_started_user(session_factory):
    async with session_factory() as session:
        session.add(Admin(telegram_id=800))
        await session.commit()
    async with session_factory() as session:
        with pytest.raises(LookupError, match="user_not_started"):
            await approve_partner(session, 100, 800)


@pytest.mark.asyncio
async def test_approve_partner_is_idempotent_and_reactivates_with_same_code(session_factory):
    await _admin_and_user(session_factory)
    async with session_factory() as session:
        partner, is_new = await approve_partner(session, 100, 800)
        assert is_new is True
        assert partner.referral_code
        code = partner.referral_code

    async with session_factory() as session:
        repeated, is_new = await approve_partner(session, 100, 800)
        count = await session.scalar(select(func.count()).select_from(ReferralPartner))
        assert is_new is False
        assert repeated.referral_code == code
        assert count == 1
        repeated.revoked_at = datetime.now(timezone.utc)
        await session.commit()

    async with session_factory() as session:
        reactivated, is_new = await approve_partner(session, 100, 800)
        assert is_new is False
        assert reactivated.revoked_at is None
        assert reactivated.referral_code == code


@pytest.mark.asyncio
async def test_revoke_partner_marks_existing_and_ignores_missing(session_factory):
    await _admin_and_user(session_factory)
    async with session_factory() as session:
        await approve_partner(session, 100, 800)
        partner = await revoke_partner(session, 100)
        assert partner is not None
        assert partner.revoked_at is not None
        assert await revoke_partner(session, 999) is None


@pytest.mark.asyncio
async def test_partner_stats_counts_started_and_campaign_credits(session_factory):
    await _admin_and_user(session_factory)
    async with session_factory() as session:
        await approve_partner(session, 100, 800)
        session.add_all([User(telegram_id=user_id) for user_id in (101, 102, 103)])
        await session.flush()
        session.add_all([
            Sponsor(chat_id=-100, title="Sponsor", type=SponsorType.CHANNEL),
            ReferralEvent(referred_user_telegram_id=101, referrer_telegram_id=100),
            ReferralEvent(
                referred_user_telegram_id=102, referrer_telegram_id=100,
                confirmed_at=datetime.now(timezone.utc),
            ),
            ReferralEvent(
                referred_user_telegram_id=103, referrer_telegram_id=100,
                confirmed_at=datetime.now(timezone.utc),
            ),
        ])
        await session.flush()
        campaign = Campaign(
            sponsor_chat_id=-100, limit_original=10, limit_current=10, status=CampaignStatus.ACTIVE
        )
        session.add(campaign)
        await session.flush()
        session.add_all([
            ReferralSubscription(campaign_id=campaign.id, referrer_telegram_id=100, referred_user_telegram_id=101),
            ReferralSubscription(campaign_id=campaign.id, referrer_telegram_id=100, referred_user_telegram_id=102),
        ])
        await session.commit()
        assert await get_partner_stats(session, 100) == (3, 2)


@pytest.mark.asyncio
async def test_partner_stats_excludes_own_channel_credits(session_factory):
    async with session_factory() as session:
        session.add_all([User(telegram_id=user_id) for user_id in (100, 101, 102)])
        await session.flush()
        session.add_all([
            Sponsor(chat_id=-110, title="Own", type=SponsorType.CHANNEL, own_channel=True),
            Sponsor(chat_id=-111, title="Partner", type=SponsorType.CHANNEL),
            ReferralEvent(referred_user_telegram_id=101, referrer_telegram_id=100),
            ReferralEvent(referred_user_telegram_id=102, referrer_telegram_id=100),
        ])
        await session.flush()
        own_campaign = Campaign(
            sponsor_chat_id=-110, limit_original=10, limit_current=10, status=CampaignStatus.ACTIVE
        )
        partner_campaign = Campaign(
            sponsor_chat_id=-111, limit_original=10, limit_current=10, status=CampaignStatus.ACTIVE
        )
        session.add_all([own_campaign, partner_campaign])
        await session.flush()
        session.add_all([
            ReferralSubscription(campaign_id=own_campaign.id, referrer_telegram_id=100, referred_user_telegram_id=101),
            ReferralSubscription(campaign_id=partner_campaign.id, referrer_telegram_id=100, referred_user_telegram_id=102),
        ])
        await session.commit()
        assert await get_partner_stats(session, 100) == (2, 1)


@pytest.mark.asyncio
async def test_partner_trigger_filter_matches_only_non_revoked_partners(session_factory):
    await _admin_and_user(session_factory)
    trigger = PartnerTriggerFilter()
    message = SimpleNamespace(text="партнёр", from_user=SimpleNamespace(id=100))
    assert await trigger(message, session_factory) is False

    async with session_factory() as session:
        await approve_partner(session, 100, 800)
    assert await trigger(message, session_factory) is True
    assert await trigger(SimpleNamespace(text="не партнёр", from_user=SimpleNamespace(id=100)), session_factory) is False

    async with session_factory() as session:
        await revoke_partner(session, 100)
    assert await trigger(message, session_factory) is False


@pytest.mark.parametrize(
    ("username", "full_name", "expected"),
    [
        ("partner", "Partner Name", "@partner"),
        (None, "Partner Name", "Partner Name (#100)"),
        (None, None, "#100"),
    ],
)
def test_format_partner_label_prefers_username_then_full_name(username, full_name, expected):
    partner = ReferralPartner(telegram_id=100, referral_code="code", approved_by_admin_id=1)
    user = User(telegram_id=100, username=username, full_name=full_name)

    assert format_partner_label(partner, user) == expected
