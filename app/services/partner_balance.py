from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Campaign, PartnerBalanceAdjustment, ReferralSubscription, Sponsor
from app.services.admins import get_admin_id


async def get_partner_balance(session: AsyncSession, telegram_id: int) -> Decimal:
    credits = await session.scalar(
        select(func.coalesce(func.sum(ReferralSubscription.price_at_credit), 0))
        .select_from(ReferralSubscription)
        .join(Campaign, Campaign.id == ReferralSubscription.campaign_id)
        .join(Sponsor, Sponsor.chat_id == Campaign.sponsor_chat_id)
        .where(
            ReferralSubscription.referrer_telegram_id == telegram_id,
            ReferralSubscription.price_at_credit.is_not(None),
            Sponsor.own_channel.is_(False),
        )
    )
    adjustments = await session.scalar(
        select(func.coalesce(func.sum(PartnerBalanceAdjustment.amount), 0)).where(
            PartnerBalanceAdjustment.partner_telegram_id == telegram_id
        )
    )
    return Decimal(credits or 0) + Decimal(adjustments or 0)


async def add_partner_balance_adjustment(
    session: AsyncSession,
    partner_telegram_id: int,
    admin_telegram_id: int,
    amount: Decimal,
    title: str,
) -> PartnerBalanceAdjustment:
    adjustment = PartnerBalanceAdjustment(
        partner_telegram_id=partner_telegram_id,
        admin_id=await get_admin_id(session, admin_telegram_id),
        amount=amount,
        title=title,
    )
    session.add(adjustment)
    await session.commit()
    return adjustment


async def zero_out_partner_balance(
    session: AsyncSession, partner_telegram_id: int, admin_telegram_id: int
) -> Decimal:
    balance = await get_partner_balance(session, partner_telegram_id)
    if balance <= 0:
        return Decimal("0")
    await add_partner_balance_adjustment(
        session, partner_telegram_id, admin_telegram_id, -balance, "Вывод"
    )
    return balance


async def get_partner_balance_history(
    session: AsyncSession, telegram_id: int, days: int = 30
) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    subscription_day = func.date(func.timezone("UTC", ReferralSubscription.created_at))
    adjustment_day = func.date(func.timezone("UTC", PartnerBalanceAdjustment.created_at))

    credits = (await session.execute(
        select(
            subscription_day.label("day"),
            func.count(ReferralSubscription.id).label("count"),
            func.coalesce(func.sum(ReferralSubscription.price_at_credit), 0).label("amount"),
        )
        .select_from(ReferralSubscription)
        .join(Campaign, Campaign.id == ReferralSubscription.campaign_id)
        .join(Sponsor, Sponsor.chat_id == Campaign.sponsor_chat_id)
        .where(
            ReferralSubscription.referrer_telegram_id == telegram_id,
            ReferralSubscription.price_at_credit.is_not(None),
            ReferralSubscription.created_at >= cutoff,
            Sponsor.own_channel.is_(False),
        )
        .group_by(subscription_day)
        .order_by(subscription_day)
    )).all()
    adjustments = (await session.execute(
        select(
            adjustment_day.label("day"),
            PartnerBalanceAdjustment.title,
            PartnerBalanceAdjustment.amount,
        )
        .where(
            PartnerBalanceAdjustment.partner_telegram_id == telegram_id,
            PartnerBalanceAdjustment.created_at >= cutoff,
        )
        .order_by(adjustment_day, PartnerBalanceAdjustment.created_at, PartnerBalanceAdjustment.id)
    )).all()

    entries: dict[object, list[tuple[int, str]]] = {}
    for day, count, amount in credits:
        entries.setdefault(day, []).append(
            (0, f"{day:%d.%m}: Подписок — {count}, +{Decimal(amount):.2f} ₽")
        )
    for day, title, amount in adjustments:
        entries.setdefault(day, []).append(
            (1, f"{day:%d.%m}: {title} — {Decimal(amount):+.2f} ₽")
        )

    if not entries:
        return ["История пуста."]
    return [line for day in sorted(entries) for _, line in entries[day]]
