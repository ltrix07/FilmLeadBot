from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from cachetools import TTLCache
from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models import (
    Campaign,
    CampaignCompletion,
    CampaignStatus,
    ReferralEvent,
    ReferralSubscription,
    Sponsor,
    SponsorJoinRequest,
)
from app.services.partners import is_active_partner
from app.services.settings import get_subscription_price

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccessResult:
    passed: bool
    is_partner: bool
    subscribed_sponsors: list[Sponsor]
    missing_sponsors: list[Sponsor]
    newly_completed_campaigns: list[Campaign]
    errored_campaigns: list[Campaign]


class SubscriptionAccessService:
    def __init__(self, ttl_seconds: int) -> None:
        self._membership_cache: TTLCache[tuple[int, frozenset[int]], dict[int, bool]] = TTLCache(
            maxsize=10_000, ttl=ttl_seconds
        )

    async def evaluate_user_access(
        self,
        session: AsyncSession,
        bot: Bot,
        telegram_id: int,
        *,
        force_refresh: bool = False,
    ) -> AccessResult:
        if await is_active_partner(session, telegram_id):
            return AccessResult(True, True, [], [], [], [])

        campaigns = list(
            (
                await session.scalars(
                    select(Campaign)
                    .where(Campaign.status == CampaignStatus.ACTIVE)
                    .options(joinedload(Campaign.sponsor))
                )
            ).unique()
        )
        if not campaigns:
            return AccessResult(True, False, [], [], [], [])

        cache_key = (telegram_id, frozenset(campaign.id for campaign in campaigns))
        memberships = None if force_refresh else self._membership_cache.get(cache_key)
        errored_campaigns: list[Campaign] = []

        if memberships is None:
            memberships = {}
            sponsors = {campaign.sponsor.chat_id: campaign.sponsor for campaign in campaigns}
            inaccessible_sponsor_ids: set[int] = set()
            request_mode_sponsor_ids = {
                sponsor_chat_id
                for sponsor_chat_id, sponsor in sponsors.items()
                if sponsor.request_mode
            }
            if request_mode_sponsor_ids:
                requested_ids = set(
                    await session.scalars(
                        select(SponsorJoinRequest.sponsor_chat_id).where(
                            SponsorJoinRequest.user_telegram_id == telegram_id,
                            SponsorJoinRequest.sponsor_chat_id.in_(request_mode_sponsor_ids),
                        )
                    )
                )
                memberships.update(
                    {
                        sponsor_chat_id: sponsor_chat_id in requested_ids
                        for sponsor_chat_id in request_mode_sponsor_ids
                    }
                )

            for sponsor_chat_id, sponsor in sponsors.items():
                if sponsor.request_mode:
                    continue
                try:
                    member = await bot.get_chat_member(sponsor_chat_id, telegram_id)
                except (TelegramBadRequest, TelegramForbiddenError) as error:
                    logger.warning(
                        "Bot cannot check sponsor membership: sponsor_chat_id=%s error=%s",
                        sponsor_chat_id,
                        error,
                    )
                    inaccessible_sponsor_ids.add(sponsor_chat_id)
                    sponsor.bot_has_access = False
                    await session.execute(
                        update(Sponsor)
                        .where(Sponsor.chat_id == sponsor_chat_id)
                        .values(bot_has_access=False)
                    )
                    continue
                memberships[sponsor_chat_id] = member.status not in {
                    ChatMemberStatus.LEFT,
                    ChatMemberStatus.KICKED,
                }

            if inaccessible_sponsor_ids:
                errored_campaigns = [
                    campaign
                    for campaign in campaigns
                    if campaign.sponsor_chat_id in inaccessible_sponsor_ids
                ]
                error_ids = [campaign.id for campaign in errored_campaigns]
                await session.execute(
                    update(Campaign)
                    .where(
                        Campaign.id.in_(error_ids),
                        Campaign.status == CampaignStatus.ACTIVE,
                    )
                    .values(status=CampaignStatus.ERROR)
                )
                for campaign in errored_campaigns:
                    campaign.status = CampaignStatus.ERROR
                campaigns = [
                    campaign
                    for campaign in campaigns
                    if campaign.sponsor_chat_id not in inaccessible_sponsor_ids
                ]
            self._membership_cache[cache_key] = memberships

        sponsors_by_id = {campaign.sponsor.chat_id: campaign.sponsor for campaign in campaigns}
        missing_sponsors = [
            sponsor
            for sponsor_chat_id, sponsor in sponsors_by_id.items()
            if not memberships.get(sponsor_chat_id, False)
        ]
        subscribed_sponsors = [
            sponsor
            for sponsor_chat_id, sponsor in sponsors_by_id.items()
            if memberships.get(sponsor_chat_id, False)
        ]
        passed = not missing_sponsors
        newly_completed_campaigns: list[Campaign] = []

        if passed:
            newly_completed_campaigns = await _record_campaign_completions(
                session, [campaign.id for campaign in campaigns], telegram_id
            )
            await session.execute(
                update(ReferralEvent)
                .where(
                    ReferralEvent.referred_user_telegram_id == telegram_id,
                    ReferralEvent.confirmed_at.is_(None),
                )
                .values(confirmed_at=func.now())
            )

        # Error-state changes must persist even when another sponsor is missing.
        if passed or errored_campaigns:
            await session.commit()

        return AccessResult(
            passed=passed,
            is_partner=False,
            subscribed_sponsors=subscribed_sponsors,
            missing_sponsors=missing_sponsors,
            newly_completed_campaigns=newly_completed_campaigns,
            errored_campaigns=errored_campaigns,
        )


async def _record_campaign_completions(
    session: AsyncSession, campaign_ids: list[int], telegram_id: int
) -> list[Campaign]:
    """Record first-time completions and reserve campaign capacity atomically."""
    completed: list[Campaign] = []
    referrer_telegram_id = await session.scalar(
        select(ReferralEvent.referrer_telegram_id).where(
            ReferralEvent.referred_user_telegram_id == telegram_id
        )
    )
    subscription_price = await get_subscription_price(session)
    for campaign_id in campaign_ids:
        inserted = await session.execute(
            insert(CampaignCompletion)
            .values(campaign_id=campaign_id, user_telegram_id=telegram_id)
            .on_conflict_do_nothing(index_elements=("campaign_id", "user_telegram_id"))
        )
        if inserted.rowcount == 0:
            continue

        reservation = await session.execute(
            update(Campaign)
            .where(
                Campaign.id == campaign_id,
                Campaign.status == CampaignStatus.ACTIVE,
                Campaign.counter < Campaign.limit_current,
            )
            .values(counter=Campaign.counter + 1)
            .returning(Campaign.id, Campaign.counter, Campaign.limit_current)
        )
        row = reservation.one_or_none()
        if row is None:
            # The quota was filled concurrently.  Do not retain a completion
            # that was not admitted to the campaign quota.
            await session.execute(
                CampaignCompletion.__table__.delete().where(
                    and_(
                        CampaignCompletion.campaign_id == campaign_id,
                        CampaignCompletion.user_telegram_id == telegram_id,
                    )
                )
            )
            continue

        if referrer_telegram_id is not None:
            await session.execute(
                insert(ReferralSubscription)
                .values(
                    campaign_id=campaign_id,
                    referrer_telegram_id=referrer_telegram_id,
                    referred_user_telegram_id=telegram_id,
                    price_at_credit=subscription_price,
                )
                .on_conflict_do_nothing(
                    index_elements=("campaign_id", "referred_user_telegram_id")
                )
            )

        if row.counter >= row.limit_current:
            campaign = await session.scalar(
                update(Campaign)
                .where(
                    Campaign.id == campaign_id,
                    Campaign.status == CampaignStatus.ACTIVE,
                )
                .values(status=CampaignStatus.COMPLETED, completed_at=func.now())
                .returning(Campaign)
            )
            if campaign is not None:
                completed.append(campaign)
    return completed
