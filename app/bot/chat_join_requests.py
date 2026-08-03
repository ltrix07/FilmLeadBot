from aiogram import Router
from aiogram.types import ChatJoinRequest
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Sponsor, SponsorJoinRequest


chat_join_requests_router = Router(name="chat_join_requests")


@chat_join_requests_router.chat_join_request()
async def record_sponsor_join_request(event: ChatJoinRequest, session_factory) -> None:
    """Record a request only for sponsors configured to use join requests."""
    async with session_factory() as session:
        sponsor = await session.scalar(
            select(Sponsor).where(
                Sponsor.chat_id == event.chat.id,
                Sponsor.request_mode.is_(True),
            )
        )
        if sponsor is None:
            return

        await session.execute(
            insert(SponsorJoinRequest)
            .values(
                sponsor_chat_id=event.chat.id,
                user_telegram_id=event.from_user.id,
            )
            .on_conflict_do_nothing(
                index_elements=("sponsor_chat_id", "user_telegram_id")
            )
        )
        await session.commit()
