from datetime import datetime, timezone
import secrets

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ReferralEvent, ReferralPartner, User
from app.services.admins import get_admin_id


def get_partner_menu_extra_lines() -> list[str]:
    """Extra menu lines for an active partner's cabinet."""
    return ["Доступен кабинет партнёра — используй кнопки ниже."]


def get_partner_menu_keyboard() -> InlineKeyboardMarkup:
    """Build the partner-cabinet controls shown alongside the regular menu."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Моя реф. ссылка", callback_data="partner:link")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="partner:stats")],
        [InlineKeyboardButton(text="📄 Скачать коды/названия", callback_data="partner:codes_export")],
    ])


async def get_partner_stats(session: AsyncSession, telegram_id: int) -> tuple[int, int]:
    """Return referrals started and referrals confirmed for a partner."""
    base_filter = ReferralEvent.referrer_telegram_id == telegram_id
    started = await session.scalar(
        select(func.count()).select_from(ReferralEvent).where(base_filter)
    )
    confirmed = await session.scalar(
        select(func.count()).select_from(ReferralEvent).where(
            base_filter, ReferralEvent.confirmed_at.is_not(None)
        )
    )
    return int(started or 0), int(confirmed or 0)


async def is_active_partner(session: AsyncSession, telegram_id: int) -> bool:
    """Return whether a Telegram user has an active referral-partner record."""
    statement = select(ReferralPartner.id).where(
        ReferralPartner.telegram_id == telegram_id,
        ReferralPartner.activated_at.is_not(None),
        ReferralPartner.revoked_at.is_(None),
    )
    return (await session.scalar(statement)) is not None


async def approve_partner(
    session: AsyncSession, telegram_id: int, admin_telegram_id: int
) -> tuple[ReferralPartner, bool]:
    """Whitelist a user as a referral partner, preserving an existing code."""
    partner = await session.scalar(
        select(ReferralPartner).where(ReferralPartner.telegram_id == telegram_id)
    )
    if partner is not None:
        if partner.revoked_at is not None:
            partner.revoked_at = None
            await session.commit()
        return partner, False

    if await session.get(User, telegram_id) is None:
        raise LookupError("user_not_started")

    referral_code = ""
    for _ in range(5):
        candidate = secrets.token_urlsafe(6)
        existing = await session.scalar(
            select(ReferralPartner.id).where(ReferralPartner.referral_code == candidate)
        )
        if existing is None:
            referral_code = candidate
            break
    if not referral_code:
        raise RuntimeError("Could not generate a unique referral code")

    partner = ReferralPartner(
        telegram_id=telegram_id,
        referral_code=referral_code,
        approved_by_admin_id=await get_admin_id(session, admin_telegram_id),
        activated_at=None,
        revoked_at=None,
    )
    session.add(partner)
    await session.commit()
    return partner, True


async def revoke_partner(session: AsyncSession, telegram_id: int) -> ReferralPartner | None:
    """Revoke a partner's whitelist entry, if it exists."""
    partner = await session.scalar(
        select(ReferralPartner).where(ReferralPartner.telegram_id == telegram_id)
    )
    if partner is None:
        return None
    if partner.revoked_at is None:
        partner.revoked_at = datetime.now(timezone.utc)
        await session.commit()
    return partner


async def list_partners(session: AsyncSession) -> list[ReferralPartner]:
    """Return all referral partners, newest approvals first."""
    return list(
        (await session.scalars(
            select(ReferralPartner).order_by(ReferralPartner.approved_at.desc())
        )).all()
    )
