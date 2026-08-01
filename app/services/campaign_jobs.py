from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import func, select, update
from sqlalchemy.orm import joinedload

from app.db.models import Campaign, CampaignStatus, Sponsor
from app.services.admins import bot_status_allows_access
from app.services.notifications import format_campaign_error, notify_admins

logger = logging.getLogger(__name__)


async def launch_scheduled_campaigns(bot: Bot, session_factory) -> None:
    """Launch campaigns whose scheduled start time has arrived."""
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        campaigns = list(
            (await session.scalars(
                select(Campaign).where(
                    Campaign.status == CampaignStatus.SCHEDULED,
                    Campaign.scheduled_at <= now,
                )
            )).all()
        )
        for campaign in campaigns:
            campaign.status = CampaignStatus.ACTIVE
            campaign.started_at = now
        await session.commit()
        active_count = await session.scalar(
            select(func.count()).select_from(Campaign).where(Campaign.status == CampaignStatus.ACTIVE)
        )

    if campaigns and active_count > 5:
        await notify_admins(
            bot,
            session_factory,
            f"Автоматически запущено по расписанию кампаний: {len(campaigns)}. "
            f"Сейчас одновременно активны {active_count} кампаний — это может снижать "
            "конверсию пользователей.",
        )


async def check_sponsor_access(bot: Bot, session_factory) -> None:
    """Proactively mark active campaigns as errored when bot access is lost."""
    async with session_factory() as session:
        sponsors = list(
            (await session.scalars(
                select(Sponsor)
                .join(Campaign, Campaign.sponsor_chat_id == Sponsor.chat_id)
                .where(Campaign.status == CampaignStatus.ACTIVE)
                .distinct()
            )).all()
        )

    for sponsor in sponsors:
        try:
            try:
                member = await bot.get_chat_member(sponsor.chat_id, bot.id)
                accessible = bot_status_allows_access(sponsor.type, member.status)
            except (TelegramBadRequest, TelegramForbiddenError):
                accessible = False
            except TelegramAPIError as error:
                logger.warning("Could not check bot access for sponsor %s: %s", sponsor.chat_id, error)
                continue

            async with session_factory() as session:
                current_sponsor = await session.get(Sponsor, sponsor.chat_id)
                if current_sponsor is None:
                    continue
                current_sponsor.bot_has_access = accessible
                campaigns: list[Campaign] = []
                if not accessible:
                    campaigns = list(
                        (await session.scalars(
                            update(Campaign)
                            .where(
                                Campaign.sponsor_chat_id == sponsor.chat_id,
                                Campaign.status == CampaignStatus.ACTIVE,
                            )
                            .values(status=CampaignStatus.ERROR)
                            .returning(Campaign)
                        )).all()
                    )
                await session.commit()

            for campaign in campaigns:
                await notify_admins(bot, session_factory, format_campaign_error(campaign, sponsor))
        except Exception:
            logger.exception("Failed to check access for sponsor %s", sponsor.chat_id)


async def attempt_resume_campaign(session, bot: Bot, campaign_id: int) -> bool:
    """Restore an errored or paused campaign if the bot can access its sponsor chat."""
    campaign = await session.scalar(
        select(Campaign).where(Campaign.id == campaign_id).options(joinedload(Campaign.sponsor))
    )
    if campaign is None:
        return False

    sponsor = campaign.sponsor
    try:
        member = await bot.get_chat_member(sponsor.chat_id, bot.id)
        accessible = bot_status_allows_access(sponsor.type, member.status)
    except (TelegramBadRequest, TelegramForbiddenError):
        accessible = False
    if not accessible:
        return False

    sponsor.bot_has_access = True
    resumed = await session.scalar(
        update(Campaign)
        .where(
            Campaign.id == campaign_id,
            Campaign.status.in_((CampaignStatus.ERROR, CampaignStatus.PAUSED)),
        )
        .values(status=CampaignStatus.ACTIVE)
        .returning(Campaign.id)
    )
    if resumed is None:
        await session.rollback()
        return False
    await session.commit()
    return True
