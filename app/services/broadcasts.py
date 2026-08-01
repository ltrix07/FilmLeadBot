import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Broadcast, BroadcastStatus, User
from app.services.admins import get_admin_id


logger = logging.getLogger(__name__)


async def get_broadcast_recipients(session: AsyncSession) -> list[int]:
    return list(
        (await session.scalars(select(User.telegram_id).where(User.blocked_at.is_(None)))).all()
    )


async def create_broadcast(
    session: AsyncSession,
    admin_telegram_id: int,
    source_chat_id: int,
    source_message_id: int,
    preview: str,
) -> Broadcast:
    broadcast = Broadcast(
        admin_id=await get_admin_id(session, admin_telegram_id),
        content=preview,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        status=BroadcastStatus.SENDING,
    )
    session.add(broadcast)
    await session.commit()
    await session.refresh(broadcast)
    return broadcast


async def mark_broadcast_completed(
    session: AsyncSession, broadcast_id: int, sent_count: int, failed_count: int
) -> None:
    broadcast = await session.get(Broadcast, broadcast_id)
    if broadcast is None:
        return
    broadcast.status = BroadcastStatus.COMPLETED
    broadcast.completed_at = datetime.now(timezone.utc)
    broadcast.sent_count = sent_count
    broadcast.failed_count = failed_count
    await session.commit()


async def mark_users_blocked(session: AsyncSession, telegram_ids: list[int]) -> None:
    if not telegram_ids:
        return
    await session.execute(
        update(User)
        .where(User.telegram_id.in_(telegram_ids), User.blocked_at.is_(None))
        .values(blocked_at=datetime.now(timezone.utc))
    )
    await session.commit()


async def run_broadcast(
    bot: Bot,
    session_factory: Any,
    broadcast_id: int,
    source_chat_id: int,
    source_message_id: int,
    recipients: list[int],
    notify_telegram_id: int,
) -> None:
    sent = 0
    failed = 0
    newly_blocked: list[int] = []

    for telegram_id in recipients:
        try:
            await bot.copy_message(
                chat_id=telegram_id, from_chat_id=source_chat_id, message_id=source_message_id
            )
        except TelegramForbiddenError:
            failed += 1
            newly_blocked.append(telegram_id)
        except TelegramRetryAfter as error:
            await asyncio.sleep(error.retry_after)
            try:
                await bot.copy_message(
                    chat_id=telegram_id, from_chat_id=source_chat_id, message_id=source_message_id
                )
            except TelegramForbiddenError:
                failed += 1
                newly_blocked.append(telegram_id)
            except TelegramAPIError:
                failed += 1
                logger.warning("Broadcast %s failed for user %s after retry", broadcast_id, telegram_id)
            else:
                sent += 1
        except TelegramAPIError:
            failed += 1
            logger.warning("Broadcast %s failed for user %s", broadcast_id, telegram_id)
        else:
            sent += 1
        await asyncio.sleep(0.05)

    async with session_factory() as session:
        await mark_users_blocked(session, newly_blocked)
    async with session_factory() as session:
        await mark_broadcast_completed(session, broadcast_id, sent, failed)
    await bot.send_message(
        notify_telegram_id,
        f"Рассылка завершена. Отправлено: {sent}. Не доставлено: {failed}.",
    )
