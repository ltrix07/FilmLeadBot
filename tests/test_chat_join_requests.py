from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.bot.chat_join_requests import record_sponsor_join_request
from app.db.models import Sponsor, SponsorJoinRequest, SponsorType, User


@pytest.mark.asyncio
async def test_join_request_is_recorded_once_only_for_request_mode_sponsors(session_factory):
    async with session_factory() as session:
        session.add_all(
            [
                User(telegram_id=10),
                Sponsor(
                    chat_id=-100,
                    title="Requests",
                    type=SponsorType.CHANNEL,
                    request_mode=True,
                ),
                Sponsor(chat_id=-101, title="Regular", type=SponsorType.CHANNEL),
            ]
        )
        await session.commit()

    request_event = SimpleNamespace(
        chat=SimpleNamespace(id=-100), from_user=SimpleNamespace(id=10)
    )
    await record_sponsor_join_request(request_event, session_factory)
    await record_sponsor_join_request(request_event, session_factory)
    await record_sponsor_join_request(
        SimpleNamespace(chat=SimpleNamespace(id=-101), from_user=SimpleNamespace(id=10)),
        session_factory,
    )
    await record_sponsor_join_request(
        SimpleNamespace(chat=SimpleNamespace(id=-999), from_user=SimpleNamespace(id=10)),
        session_factory,
    )

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(SponsorJoinRequest)) == 1
        assert await session.scalar(select(SponsorJoinRequest.sponsor_chat_id)) == -100
