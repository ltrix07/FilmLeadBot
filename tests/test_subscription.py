import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import func, select

from app.db.models import (
    Admin,
    Campaign,
    CampaignCompletion,
    CampaignStatus,
    ReferralEvent,
    ReferralPartner,
    ReferralSubscription,
    Sponsor,
    SponsorJoinRequest,
    SponsorType,
    User,
)
from app.services.subscription import SubscriptionAccessService, _record_campaign_completions
from app.services.partners import get_partner_stats


async def add_campaigns(session_factory, count: int = 2) -> list[Campaign]:
    async with session_factory() as session:
        campaigns = []
        for index in range(count):
            chat_id = -100_000 - index
            session.add(Sponsor(chat_id=chat_id, title=f"Sponsor {index}", type=SponsorType.CHANNEL))
            campaigns.append(
                Campaign(
                    sponsor_chat_id=chat_id,
                    limit_original=10,
                    limit_current=10,
                    status=CampaignStatus.ACTIVE,
                )
            )
        session.add_all(campaigns)
        await session.commit()
        return campaigns


async def add_user(session_factory, telegram_id: int) -> None:
    async with session_factory() as session:
        session.add(User(telegram_id=telegram_id))
        await session.commit()


def member(status: ChatMemberStatus):
    result = AsyncMock()
    result.status = status
    return result


@pytest.mark.asyncio
async def test_active_partner_bypasses_bot(session_factory):
    await add_user(session_factory, 1)
    async with session_factory() as session:
        admin = Admin(telegram_id=99, role="owner")
        session.add_all([admin, User(telegram_id=2)])
        await session.flush()
        session.add(
            ReferralPartner(
                telegram_id=1,
                referral_code="partner",
                approved_by_admin_id=admin.id,
                activated_at=datetime.now(timezone.utc),
            )
        )
        session.add(ReferralEvent(referred_user_telegram_id=1, referrer_telegram_id=2))
        await session.commit()

    bot = AsyncMock()
    async with session_factory() as session:
        result = await SubscriptionAccessService(60).evaluate_user_access(session, bot, 1)
    assert result.passed
    assert result.is_partner
    bot.get_chat_member.assert_not_awaited()
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ReferralSubscription)) == 0


@pytest.mark.asyncio
async def test_referral_credit_is_recorded_once_per_campaign(session_factory):
    campaigns = await add_campaigns(session_factory, count=1)
    await add_user(session_factory, 100)
    await add_user(session_factory, 2)
    async with session_factory() as session:
        session.add(ReferralEvent(referred_user_telegram_id=2, referrer_telegram_id=100))
        await session.commit()

    bot = AsyncMock()
    bot.get_chat_member.return_value = member(ChatMemberStatus.MEMBER)
    service = SubscriptionAccessService(ttl_seconds=60)
    async with session_factory() as session:
        assert (await service.evaluate_user_access(session, bot, 2)).passed
    async with session_factory() as session:
        assert (await service.evaluate_user_access(session, bot, 2)).passed
        credits = list(await session.scalars(select(ReferralSubscription)))
    assert [(credit.campaign_id, credit.referred_user_telegram_id) for credit in credits] == [
        (campaigns[0].id, 2)
    ]
    async with session_factory() as session:
        assert await get_partner_stats(session, 100) == (1, 1)

    async with session_factory() as session:
        sponsor = Sponsor(chat_id=-100_001, title="Sponsor B", type=SponsorType.CHANNEL)
        campaign_b = Campaign(
            sponsor_chat_id=sponsor.chat_id,
            limit_original=10,
            limit_current=10,
            status=CampaignStatus.ACTIVE,
        )
        session.add_all([
            sponsor,
            campaign_b,
        ])
        await session.commit()
        campaign_b_id = campaign_b.id

    async with session_factory() as session:
        assert (await service.evaluate_user_access(session, bot, 2)).passed
        credits = list(
            await session.scalars(
                select(ReferralSubscription).order_by(ReferralSubscription.campaign_id)
            )
        )
    assert [(credit.campaign_id, credit.referred_user_telegram_id) for credit in credits] == [
        (campaigns[0].id, 2),
        (campaign_b_id, 2),
    ]
    async with session_factory() as session:
        assert await get_partner_stats(session, 100) == (1, 2)


@pytest.mark.asyncio
async def test_user_without_referrer_does_not_receive_referral_credit(session_factory):
    await add_campaigns(session_factory, count=1)
    await add_user(session_factory, 2)
    bot = AsyncMock()
    bot.get_chat_member.return_value = member(ChatMemberStatus.MEMBER)

    async with session_factory() as session:
        assert (await SubscriptionAccessService(60).evaluate_user_access(session, bot, 2)).passed
        assert await session.scalar(select(func.count()).select_from(ReferralSubscription)) == 0


@pytest.mark.asyncio
async def test_subscribed_user_is_counted_once_and_cache_can_refresh(session_factory):
    campaigns = await add_campaigns(session_factory)
    await add_user(session_factory, 2)
    bot = AsyncMock()
    bot.get_chat_member.return_value = member(ChatMemberStatus.MEMBER)
    service = SubscriptionAccessService(ttl_seconds=60)

    async with session_factory() as session:
        first = await service.evaluate_user_access(session, bot, 2)
    assert first.passed
    assert bot.get_chat_member.await_count == 2

    async with session_factory() as session:
        second = await service.evaluate_user_access(session, bot, 2)
    assert second.passed
    assert bot.get_chat_member.await_count == 2

    async with session_factory() as session:
        await service.evaluate_user_access(session, bot, 2, force_refresh=True)
    assert bot.get_chat_member.await_count == 4

    async with session_factory() as session:
        completion_count = await session.scalar(select(func.count()).select_from(CampaignCompletion))
        counters = list(await session.scalars(select(Campaign.counter).order_by(Campaign.id)))
    assert completion_count == 2
    assert counters == [1, 1]
    assert len(campaigns) == 2


@pytest.mark.asyncio
async def test_missing_sponsor_does_not_count_user(session_factory):
    await add_campaigns(session_factory)
    await add_user(session_factory, 3)
    bot = AsyncMock()
    bot.get_chat_member.side_effect = [member(ChatMemberStatus.MEMBER), member(ChatMemberStatus.LEFT)]

    async with session_factory() as session:
        result = await SubscriptionAccessService(60).evaluate_user_access(session, bot, 3)
    assert not result.passed
    assert len(result.missing_sponsors) == 1

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CampaignCompletion)) == 0


@pytest.mark.asyncio
async def test_request_mode_membership_uses_recorded_request_without_bot_check(session_factory):
    async with session_factory() as session:
        session.add_all([User(telegram_id=20), User(telegram_id=21)])
        regular = Sponsor(chat_id=-300, title="Regular", type=SponsorType.CHANNEL)
        requested = Sponsor(
            chat_id=-301,
            title="Requested",
            type=SponsorType.CHANNEL,
            request_mode=True,
        )
        session.add_all(
            [
                regular,
                requested,
                Campaign(
                    sponsor_chat_id=regular.chat_id,
                    limit_original=10,
                    limit_current=10,
                    status=CampaignStatus.ACTIVE,
                ),
                Campaign(
                    sponsor_chat_id=requested.chat_id,
                    limit_original=10,
                    limit_current=10,
                    status=CampaignStatus.ACTIVE,
                ),
            ]
        )
        await session.flush()
        session.add(SponsorJoinRequest(sponsor_chat_id=requested.chat_id, user_telegram_id=20))
        await session.commit()

    bot = AsyncMock()
    bot.get_chat_member.return_value = member(ChatMemberStatus.MEMBER)
    service = SubscriptionAccessService(ttl_seconds=60)
    async with session_factory() as session:
        assert (await service.evaluate_user_access(session, bot, 20)).passed
    bot.get_chat_member.assert_awaited_once_with(-300, 20)

    async with session_factory() as session:
        result = await service.evaluate_user_access(session, bot, 21)
    assert not result.passed
    assert [sponsor.chat_id for sponsor in result.missing_sponsors] == [-301]
    assert bot.get_chat_member.await_args_list[-1].args == (-300, 21)


@pytest.mark.asyncio
async def test_forbidden_sponsor_is_disabled_and_does_not_block(session_factory):
    await add_campaigns(session_factory)
    await add_user(session_factory, 4)
    bot = AsyncMock()
    bot.get_chat_member.side_effect = [
        member(ChatMemberStatus.MEMBER),
        TelegramForbiddenError(method=AsyncMock(), message="forbidden"),
    ]

    async with session_factory() as session:
        result = await SubscriptionAccessService(60).evaluate_user_access(session, bot, 4)
    assert result.passed
    assert len(result.errored_campaigns) == 1

    async with session_factory() as session:
        errored = await session.scalar(select(Campaign).where(Campaign.status == CampaignStatus.ERROR))
        assert errored is not None
        assert not (await session.get(Sponsor, errored.sponsor_chat_id)).bot_has_access


@pytest.mark.asyncio
async def test_concurrent_quota_never_exceeds_limit(session_factory):
    async with session_factory() as setup_session:
        setup_session.add(Sponsor(chat_id=-200_000, title="Limited", type=SponsorType.CHANNEL))
        campaign = Campaign(
            sponsor_chat_id=-200_000,
            limit_original=5,
            limit_current=5,
            status=CampaignStatus.ACTIVE,
        )
        setup_session.add(campaign)
        setup_session.add_all(User(telegram_id=100 + index) for index in range(20))
        await setup_session.commit()
        campaign_id = campaign.id

    async def record(telegram_id: int) -> None:
        async with session_factory() as session:
            await _record_campaign_completions(session, [campaign_id], telegram_id)
            await session.commit()

    await asyncio.gather(*(record(100 + index) for index in range(20)))
    async with session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
        count = await session.scalar(
            select(func.count()).select_from(CampaignCompletion).where(CampaignCompletion.campaign_id == campaign_id)
        )
    assert campaign.counter == 5
    assert campaign.status == CampaignStatus.COMPLETED
    assert count == 5
