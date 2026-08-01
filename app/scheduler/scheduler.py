from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.services.campaign_jobs import check_sponsor_access, launch_scheduled_campaigns


def create_scheduler(bot: Bot, session_factory) -> AsyncIOScheduler:
    """Create the periodic campaign maintenance scheduler."""
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        launch_scheduled_campaigns, "interval", seconds=60,
        args=[bot, session_factory], id="launch_scheduled_campaigns",
    )
    scheduler.add_job(
        check_sponsor_access, "interval", seconds=300,
        args=[bot, session_factory], id="check_sponsor_access",
    )
    return scheduler
