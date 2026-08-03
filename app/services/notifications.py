from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy import select

from app.db.models import Admin, Campaign, Sponsor

logger = logging.getLogger(__name__)


async def notify_admin(bot: Bot, telegram_id: int, text: str) -> None:
    """Deliver a notification to one administrator without failing the caller."""
    try:
        await bot.send_message(telegram_id, text)
    except Exception as error:  # Telegram errors are individual delivery failures.
        logger.warning("Could not notify admin %s: %s", telegram_id, error)


async def notify_admins(bot: Bot, session_factory, text: str) -> None:
    """Deliver a notification to every administrator without aborting on one failure."""
    async with session_factory() as session:
        telegram_ids = list(await session.scalars(select(Admin.telegram_id)))
    for telegram_id in telegram_ids:
        try:
            await bot.send_message(telegram_id, text)
        except Exception as error:  # Telegram errors are individual delivery failures.
            logger.warning("Could not notify admin %s: %s", telegram_id, error)


def format_campaign_completed(campaign: Campaign, sponsor: Sponsor) -> str:
    completed_at = campaign.completed_at
    return (
        "Кампания завершена.\n\n"
        f"Канал: {sponsor.username or sponsor.title}\n"
        f"План: {campaign.limit_current}\n"
        f"Засчитано: {campaign.counter}\n"
        f"Дата завершения: {completed_at:%d.%m.%Y %H:%M}"
    )


def format_campaign_error(campaign: Campaign, sponsor: Sponsor) -> str:
    return (
        "Кампания приостановлена из-за потери доступа бота к каналу.\n\n"
        f"Канал: {sponsor.username or sponsor.title}\n"
        f"Засчитано: {campaign.counter} из {campaign.limit_current}"
    )
