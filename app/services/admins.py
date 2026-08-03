from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, SponsorType, User


async def ensure_seed_admins(session: AsyncSession, telegram_ids: list[int]) -> None:
    """Create bootstrap owners without changing any existing administrator."""
    if not telegram_ids:
        return

    rows = [
        {
            "telegram_id": telegram_id,
            "role": "owner",
            "can_manage_admins": True,
            "can_manage_payouts": True,
        }
        for telegram_id in set(telegram_ids)
    ]
    statement = insert(Admin).values(rows)
    await session.execute(
        statement.on_conflict_do_nothing(index_elements=[Admin.telegram_id])
    )
    await session.commit()


async def is_admin(session: AsyncSession, telegram_id: int) -> bool:
    return (
        await session.scalar(
            select(Admin.telegram_id).where(
                Admin.telegram_id == telegram_id, Admin.revoked_at.is_(None)
            )
        )
    ) is not None


def can_manage_admins(admin: Admin) -> bool:
    return admin.role == "owner" or admin.can_manage_admins


def can_manage_payouts(admin: Admin) -> bool:
    return admin.role == "owner" or admin.can_manage_payouts


async def get_admin(session: AsyncSession, telegram_id: int) -> Admin | None:
    return await session.scalar(select(Admin).where(Admin.telegram_id == telegram_id))


async def list_admins(session: AsyncSession) -> list[Admin]:
    return list(
        (await session.scalars(
            select(Admin).order_by(Admin.revoked_at.is_not(None), Admin.created_at)
        )).all()
    )


async def add_admin(
    session: AsyncSession, telegram_id: int, *, can_manage_admins: bool, can_manage_payouts: bool
) -> Admin:
    if await session.get(User, telegram_id) is None:
        raise LookupError("user_not_started")
    admin = await get_admin(session, telegram_id)
    if admin is not None and admin.role == "owner":
        raise LookupError("already_owner")
    if admin is None:
        admin = Admin(
            telegram_id=telegram_id,
            role="admin",
            can_manage_admins=can_manage_admins,
            can_manage_payouts=can_manage_payouts,
        )
        session.add(admin)
    else:
        admin.can_manage_admins = can_manage_admins
        admin.can_manage_payouts = can_manage_payouts
        admin.revoked_at = None
    await session.commit()
    return admin


async def set_admin_permissions(
    session: AsyncSession,
    telegram_id: int,
    *,
    can_manage_admins: bool | None = None,
    can_manage_payouts: bool | None = None,
) -> Admin | None:
    admin = await get_admin(session, telegram_id)
    if admin is None:
        return None
    if admin.role == "owner":
        raise LookupError("cannot_modify_owner")
    if can_manage_admins is not None:
        admin.can_manage_admins = can_manage_admins
    if can_manage_payouts is not None:
        admin.can_manage_payouts = can_manage_payouts
    await session.commit()
    return admin


async def revoke_admin(session: AsyncSession, telegram_id: int) -> Admin | None:
    admin = await get_admin(session, telegram_id)
    if admin is None:
        return None
    if admin.role == "owner":
        raise LookupError("cannot_revoke_owner")
    if admin.revoked_at is None:
        admin.revoked_at = datetime.now(timezone.utc)
        await session.commit()
    return admin


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
