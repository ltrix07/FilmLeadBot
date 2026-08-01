from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, SponsorType


async def ensure_seed_admins(session: AsyncSession, telegram_ids: list[int]) -> None:
    """Create bootstrap owners without changing any existing administrator."""
    if not telegram_ids:
        return

    rows = [{"telegram_id": telegram_id, "role": "owner"} for telegram_id in set(telegram_ids)]
    statement = insert(Admin).values(rows)
    await session.execute(
        statement.on_conflict_do_nothing(index_elements=[Admin.telegram_id])
    )
    await session.commit()


async def is_admin(session: AsyncSession, telegram_id: int) -> bool:
    return (
        await session.scalar(select(Admin.telegram_id).where(Admin.telegram_id == telegram_id))
    ) is not None


async def get_admin_id(session: AsyncSession, telegram_id: int) -> int:
    admin_id = await session.scalar(select(Admin.id).where(Admin.telegram_id == telegram_id))
    if admin_id is None:
        raise LookupError(f"Administrator {telegram_id} is missing")
    return admin_id


def bot_status_allows_access(chat_type: SponsorType, status: str) -> bool:
    """Return whether the bot has enough membership rights for a sponsor chat."""
    normalized_status = getattr(status, "value", status)
    if chat_type is SponsorType.CHANNEL:
        return normalized_status in {"administrator", "creator"}
    return normalized_status in {"member", "administrator", "creator"}
