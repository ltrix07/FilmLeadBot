import asyncio

import pytest
from sqlalchemy import func, select

from app.db.models import WelcomeMessage
from app.services.settings import get_welcome_message, set_welcome_message


@pytest.mark.asyncio
async def test_set_welcome_message_upserts_singleton_and_updates_timestamp(session_factory):
    async with session_factory() as session:
        first = await set_welcome_message(session, -1001, 10, "First")
        first_updated_at = first.updated_at

    await asyncio.sleep(0.01)

    async with session_factory() as session:
        second = await set_welcome_message(session, -1002, 20, "Second")
        current = await get_welcome_message(session)
        count = await session.scalar(select(func.count()).select_from(WelcomeMessage))

    assert second.id == 1
    assert current is not None
    assert (current.source_chat_id, current.source_message_id, current.preview) == (-1002, 20, "Second")
    assert count == 1
    assert current.updated_at > first_updated_at
