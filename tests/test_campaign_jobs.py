from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select

from app.db.models import Campaign, CampaignStatus, Sponsor, SponsorType
from app.services import campaign_jobs


async def _add_campaign(session_factory, *, status, scheduled_at=None, chat_id=-900):
    async with session_factory() as session:
        sponsor = Sponsor(
            chat_id=chat_id, title=f"Cinema {chat_id}", username=None, type=SponsorType.CHANNEL
        )
        campaign = Campaign(
            sponsor_chat_id=chat_id, limit_original=10, limit_current=10, counter=0,
            status=status, scheduled_at=scheduled_at,
        )
        session.add_all([sponsor, campaign])
        await session.commit()
        return campaign.id


@pytest.mark.asyncio
async def test_launch_scheduled_campaigns_only_launches_due_campaigns(session_factory, monkeypatch):
    now = datetime.now(timezone.utc)
    due_id = await _add_campaign(
        session_factory, status=CampaignStatus.SCHEDULED, scheduled_at=now - timedelta(minutes=1)
    )
    future_id = await _add_campaign(
        session_factory, status=CampaignStatus.SCHEDULED, scheduled_at=now + timedelta(minutes=1), chat_id=-901
    )
    notify = AsyncMock()
    monkeypatch.setattr(campaign_jobs, "notify_admins", notify)

    await campaign_jobs.launch_scheduled_campaigns(AsyncMock(), session_factory)

    async with session_factory() as session:
        due, future = await session.get(Campaign, due_id), await session.get(Campaign, future_id)
    assert due.status is CampaignStatus.ACTIVE
    assert due.started_at is not None
    assert future.status is CampaignStatus.SCHEDULED
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_launch_scheduled_campaigns_notifies_when_six_are_active(session_factory, monkeypatch):
    now = datetime.now(timezone.utc)
    for index in range(5):
        await _add_campaign(session_factory, status=CampaignStatus.ACTIVE, chat_id=-910 - index)
    await _add_campaign(session_factory, status=CampaignStatus.SCHEDULED, scheduled_at=now - timedelta(minutes=1), chat_id=-920)
    notify = AsyncMock()
    monkeypatch.setattr(campaign_jobs, "notify_admins", notify)

    await campaign_jobs.launch_scheduled_campaigns(AsyncMock(), session_factory)

    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_sponsor_access_errors_inaccessible_campaigns(session_factory, monkeypatch):
    campaign_id = await _add_campaign(session_factory, status=CampaignStatus.ACTIVE)
    bot = AsyncMock()
    bot.id = 1
    bot.get_chat_member.side_effect = TelegramForbiddenError(method=AsyncMock(), message="forbidden")
    notify = AsyncMock()
    monkeypatch.setattr(campaign_jobs, "notify_admins", notify)

    await campaign_jobs.check_sponsor_access(bot, session_factory)

    async with session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
        sponsor = await session.get(Sponsor, -900)
    assert campaign.status is CampaignStatus.ERROR
    assert sponsor.bot_has_access is False
    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_sponsor_access_preserves_accessible_campaign(session_factory, monkeypatch):
    campaign_id = await _add_campaign(session_factory, status=CampaignStatus.ACTIVE)
    bot = AsyncMock()
    bot.id = 1
    bot.get_chat_member.return_value = SimpleNamespace(status="administrator")
    notify = AsyncMock()
    monkeypatch.setattr(campaign_jobs, "notify_admins", notify)

    await campaign_jobs.check_sponsor_access(bot, session_factory)

    async with session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
        sponsor = await session.get(Sponsor, -900)
    assert campaign.status is CampaignStatus.ACTIVE
    assert sponsor.bot_has_access is True
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_attempt_resume_campaign_checks_live_access(session_factory):
    campaign_id = await _add_campaign(session_factory, status=CampaignStatus.ERROR)
    bot = AsyncMock()
    bot.id = 1
    bot.get_chat_member.return_value = SimpleNamespace(status="administrator")
    async with session_factory() as session:
        assert await campaign_jobs.attempt_resume_campaign(session, bot, campaign_id) is True
    async with session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
    assert campaign.status is CampaignStatus.ACTIVE


@pytest.mark.asyncio
async def test_attempt_resume_campaign_keeps_error_without_access(session_factory):
    campaign_id = await _add_campaign(session_factory, status=CampaignStatus.ERROR)
    bot = AsyncMock()
    bot.id = 1
    bot.get_chat_member.side_effect = TelegramForbiddenError(method=AsyncMock(), message="forbidden")
    async with session_factory() as session:
        assert await campaign_jobs.attempt_resume_campaign(session, bot, campaign_id) is False
    async with session_factory() as session:
        campaign = await session.get(Campaign, campaign_id)
    assert campaign.status is CampaignStatus.ERROR
