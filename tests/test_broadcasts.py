from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from app.db.models import Admin, Broadcast, BroadcastStatus, User
from app.services import broadcasts
from app.services import campaign_jobs


@pytest.mark.asyncio
async def test_recipients_exclude_blocked_users(session_factory):
    async with session_factory() as session:
        session.add_all([
            User(telegram_id=1),
            User(telegram_id=2, blocked_at=datetime.now(timezone.utc)),
        ])
        await session.commit()
        assert await broadcasts.get_broadcast_recipients(session) == [1]


@pytest.mark.asyncio
async def test_create_and_complete_broadcast(session_factory):
    async with session_factory() as session:
        session.add(Admin(telegram_id=10, role="owner"))
        await session.commit()
        broadcast = await broadcasts.create_broadcast(session, 10, 100, 200, "Preview")
        assert broadcast.status is BroadcastStatus.SENDING
        assert broadcast.source_chat_id == 100
        await broadcasts.mark_broadcast_completed(session, broadcast.id, 3, 1)

    async with session_factory() as session:
        saved = await session.get(Broadcast, broadcast.id)
        assert saved.status is BroadcastStatus.COMPLETED
        assert (saved.sent_count, saved.failed_count) == (3, 1)
        assert saved.completed_at is not None


@pytest.mark.asyncio
async def test_create_scheduled_broadcast(session_factory):
    scheduled_at = datetime(2026, 12, 25, 18, tzinfo=timezone.utc)
    async with session_factory() as session:
        session.add(Admin(telegram_id=10, role="owner"))
        await session.commit()
        broadcast = await broadcasts.create_broadcast(
            session, 10, 100, 200, "Preview",
            status=BroadcastStatus.SCHEDULED, scheduled_at=scheduled_at,
        )

    assert broadcast.status is BroadcastStatus.SCHEDULED
    assert broadcast.scheduled_at == scheduled_at


@pytest.mark.asyncio
async def test_launch_scheduled_broadcasts(session_factory, monkeypatch):
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        session.add_all([Admin(telegram_id=10, role="owner"), User(telegram_id=1)])
        await session.commit()
        due = await broadcasts.create_broadcast(
            session, 10, 100, 200, "Due", status=BroadcastStatus.SCHEDULED,
            scheduled_at=now - timedelta(minutes=1),
        )
        future = await broadcasts.create_broadcast(
            session, 10, 100, 201, "Future", status=BroadcastStatus.SCHEDULED,
            scheduled_at=now + timedelta(minutes=1),
        )

    async def fake_run_broadcast(*args):
        async with session_factory() as session:
            launched = await session.get(Broadcast, args[2])
            assert launched.status is BroadcastStatus.SENDING

    fake_run = AsyncMock(side_effect=fake_run_broadcast)
    monkeypatch.setattr(campaign_jobs, "run_broadcast", fake_run)
    await campaign_jobs.launch_scheduled_broadcasts(AsyncMock(), session_factory)

    fake_run.assert_awaited_once()
    async with session_factory() as session:
        assert (await session.get(Broadcast, due.id)).status is BroadcastStatus.SENDING
        assert (await session.get(Broadcast, future.id)).status is BroadcastStatus.SCHEDULED


@pytest.mark.asyncio
async def test_launch_scheduled_broadcast_without_recipients_completes(session_factory, monkeypatch):
    async with session_factory() as session:
        session.add(Admin(telegram_id=10, role="owner"))
        await session.commit()
        broadcast = await broadcasts.create_broadcast(
            session, 10, 100, 200, "Due", status=BroadcastStatus.SCHEDULED,
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )

    fake_run = AsyncMock()
    monkeypatch.setattr(campaign_jobs, "run_broadcast", fake_run)
    await campaign_jobs.launch_scheduled_broadcasts(AsyncMock(), session_factory)

    fake_run.assert_not_awaited()
    async with session_factory() as session:
        saved = await session.get(Broadcast, broadcast.id)
    assert saved.status is BroadcastStatus.COMPLETED
    assert (saved.sent_count, saved.failed_count) == (0, 0)


@pytest.mark.asyncio
async def test_cancel_scheduled_broadcast_only_cancels_pending(session_factory):
    async with session_factory() as session:
        session.add(Admin(telegram_id=10, role="owner"))
        await session.commit()
        scheduled = await broadcasts.create_broadcast(
            session, 10, 100, 200, "Scheduled", status=BroadcastStatus.SCHEDULED,
            scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        sending = await broadcasts.create_broadcast(session, 10, 100, 201, "Sending")
        cancelled = await broadcasts.cancel_scheduled_broadcast(session, scheduled.id)
        not_cancelled = await broadcasts.cancel_scheduled_broadcast(session, sending.id)

    assert cancelled is not None
    assert cancelled.status is BroadcastStatus.FAILED
    assert not_cancelled is None
    async with session_factory() as session:
        assert (await session.get(Broadcast, sending.id)).status is BroadcastStatus.SENDING


@pytest.mark.asyncio
async def test_mark_users_blocked_is_idempotent(session_factory):
    async with session_factory() as session:
        session.add_all([User(telegram_id=1), User(telegram_id=2)])
        await session.commit()
        await broadcasts.mark_users_blocked(session, [1, 2])
        first_timestamp = (await session.get(User, 1)).blocked_at
        await broadcasts.mark_users_blocked(session, [1, 2])
        await broadcasts.mark_users_blocked(session, [])
        assert (await session.get(User, 1)).blocked_at == first_timestamp
        assert (await session.get(User, 2)).blocked_at is not None


@pytest.mark.asyncio
async def test_run_broadcast_marks_forbidden_user_and_notifies(session_factory, monkeypatch):
    async with session_factory() as session:
        session.add_all([Admin(telegram_id=10, role="owner"), *(User(telegram_id=i) for i in (1, 2, 3))])
        await session.commit()
        broadcast = await broadcasts.create_broadcast(session, 10, 10, 20, "Preview")

    bot = AsyncMock()
    bot.copy_message.side_effect = [
        None,
        TelegramForbiddenError(SendMessage(chat_id=2, text="x"), "blocked"),
        None,
    ]
    monkeypatch.setattr(broadcasts.asyncio, "sleep", AsyncMock())

    await broadcasts.run_broadcast(bot, session_factory, broadcast.id, 10, 20, [1, 2, 3], 10)

    async with session_factory() as session:
        saved = await session.get(Broadcast, broadcast.id)
        blocked_user = await session.get(User, 2)
    assert (saved.sent_count, saved.failed_count) == (2, 1)
    assert saved.status is BroadcastStatus.COMPLETED
    assert blocked_user.blocked_at is not None
    bot.send_message.assert_awaited_once_with(
        10, "Рассылка завершена. Отправлено: 2. Не доставлено: 1."
    )
