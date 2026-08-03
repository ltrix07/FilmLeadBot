from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Campaign,
    CampaignStatus,
    MovieCode,
    MovieCodeStatus,
    ReferralPartner,
    Sponsor,
    User,
)
from app.services.partners import get_partner_stats, list_partners


@dataclass(frozen=True)
class OverviewStats:
    users_total: int
    users_blocked: int
    sponsors_total: int
    sponsors_with_access: int
    campaigns_active: int
    campaigns_total: int
    codes_active: int
    codes_inactive: int
    partners_total: int
    partners_active: int


async def _count(session: AsyncSession, model, *filters) -> int:
    count = await session.scalar(select(func.count()).select_from(model).where(*filters))
    return int(count or 0)


async def get_overview_stats(session: AsyncSession) -> OverviewStats:
    return OverviewStats(
        users_total=await _count(session, User),
        users_blocked=await _count(session, User, User.blocked_at.is_not(None)),
        sponsors_total=await _count(session, Sponsor),
        sponsors_with_access=await _count(session, Sponsor, Sponsor.bot_has_access.is_(True)),
        campaigns_active=await _count(session, Campaign, Campaign.status == CampaignStatus.ACTIVE),
        campaigns_total=await _count(session, Campaign),
        codes_active=await _count(session, MovieCode, MovieCode.status == MovieCodeStatus.ACTIVE),
        codes_inactive=await _count(session, MovieCode, MovieCode.status == MovieCodeStatus.INACTIVE),
        partners_total=await _count(session, ReferralPartner),
        partners_active=await _count(
            session,
            ReferralPartner,
            ReferralPartner.revoked_at.is_(None),
            ReferralPartner.activated_at.is_not(None),
        ),
    )


async def get_top_partners(
    session: AsyncSession, limit: int = 10
) -> list[tuple[ReferralPartner, int, int]]:
    partners = await list_partners(session)
    results = [
        (partner, *(await get_partner_stats(session, partner.telegram_id)))
        for partner in partners
    ]
    return sorted(results, key=lambda item: item[2], reverse=True)[:limit]


async def get_top_codes(session: AsyncSession, limit: int = 10) -> list[MovieCode]:
    return list(
        (
            await session.scalars(
                select(MovieCode)
                .where(
                    MovieCode.status == MovieCodeStatus.ACTIVE,
                    MovieCode.lookup_count > 0,
                )
                .order_by(MovieCode.lookup_count.desc())
                .limit(limit)
            )
        ).all()
    )
