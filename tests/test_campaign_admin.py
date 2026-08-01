from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.bot.routers.admin import cancel_campaign, update_campaign_limit
from app.db.models import Admin, Campaign, CampaignLimitHistory, CampaignStatus, Sponsor, SponsorType
from app.services.notifications import format_campaign_completed, format_campaign_error


async def _campaign_with_admin(session_factory, *, counter: int = 2) -> tuple[int, int]:
    async with session_factory() as session:
        admin = Admin(telegram_id=700)
        sponsor = Sponsor(chat_id=-700, title="Cinema", username="cinema", type=SponsorType.CHANNEL)
        campaign = Campaign(
            sponsor_chat_id=sponsor.chat_id, limit_original=10, limit_current=10,
            counter=counter, status=CampaignStatus.ACTIVE,
        )
        session.add_all([admin, sponsor, campaign])
        await session.commit()
        return campaign.id, admin.id


def test_campaign_notification_formats():
    sponsor = Sponsor(chat_id=-1, title="Кино", username=None, type=SponsorType.CHANNEL)
    campaign = Campaign(
        sponsor_chat_id=-1, limit_original=10, limit_current=8, counter=8,
        status=CampaignStatus.COMPLETED, completed_at=datetime(2026, 12, 25, 18, 0, tzinfo=timezone.utc),
    )
    assert format_campaign_completed(campaign, sponsor) == (
        "Кампания завершена.\n\nКанал: Кино\nПлан: 8\nЗасчитано: 8\nДата завершения: 25.12.2026 18:00"
    )
    assert format_campaign_error(campaign, sponsor) == (
        "Кампания приостановлена из-за потери доступа бота к каналу.\n\nКанал: Кино\nЗасчитано: 8 из 8"
    )


@pytest.mark.asyncio
async def test_update_campaign_limit_records_history(session_factory):
    campaign_id, admin_id = await _campaign_with_admin(session_factory)
    async with session_factory() as session:
        campaign = await update_campaign_limit(session, campaign_id, 15, 700)
        assert campaign is not None
    async with session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
        history = await session.scalar(select(CampaignLimitHistory))
    assert campaign.limit_current == 15
    assert history.old_limit == 10
    assert history.new_limit == 15
    assert history.changed_by_admin_id == admin_id


@pytest.mark.asyncio
async def test_confirm_lower_limit_completes_campaign(session_factory):
    campaign_id, _ = await _campaign_with_admin(session_factory, counter=7)
    async with session_factory() as session:
        await update_campaign_limit(session, campaign_id, 5, 700, complete=True)
    async with session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
    assert campaign.limit_current == 5
    assert campaign.status is CampaignStatus.COMPLETED
    assert campaign.completed_at is not None


@pytest.mark.asyncio
async def test_manual_cancel_preserves_campaign_progress(session_factory):
    campaign_id, admin_id = await _campaign_with_admin(session_factory, counter=4)
    async with session_factory() as session:
        await cancel_campaign(session, campaign_id, 700)
    async with session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
    assert campaign.status is CampaignStatus.CANCELLED
    assert campaign.cancelled_at is not None
    assert campaign.cancelled_by_admin_id == admin_id
    assert campaign.limit_original == 10
    assert campaign.counter == 4
