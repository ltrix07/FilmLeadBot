from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Sponsor


async def list_sponsors(
    session: AsyncSession, *, archived: bool = False, limit: int | None = None, offset: int = 0,
) -> list[Sponsor]:
    """List either active sponsors or archived sponsors, alphabetically."""
    statement = (
        select(Sponsor)
        .where(Sponsor.archived_at.is_not(None) if archived else Sponsor.archived_at.is_(None))
        .order_by(Sponsor.title)
        .offset(offset)
    )
    if limit is not None:
        statement = statement.limit(limit)
    return list((await session.scalars(statement)).all())


async def count_sponsors(session: AsyncSession, *, archived: bool = False) -> int:
    statement = select(func.count()).select_from(Sponsor).where(
        Sponsor.archived_at.is_not(None) if archived else Sponsor.archived_at.is_(None)
    )
    return int(await session.scalar(statement) or 0)


async def archive_sponsor(session: AsyncSession, chat_id: int) -> Sponsor | None:
    sponsor = await session.get(Sponsor, chat_id)
    if sponsor is None:
        return None
    if sponsor.archived_at is None:
        sponsor.archived_at = datetime.now(timezone.utc)
        await session.commit()
    return sponsor


async def unarchive_sponsor(session: AsyncSession, chat_id: int) -> Sponsor | None:
    sponsor = await session.get(Sponsor, chat_id)
    if sponsor is None:
        return None
    if sponsor.archived_at is not None:
        sponsor.archived_at = None
        await session.commit()
    return sponsor
