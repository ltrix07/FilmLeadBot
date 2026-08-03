from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.bot.routers.start import _ensure_user_and_referral
from app.db.models import Admin, PendingAdminGrant, PendingPartnerGrant, ReferralPartner
from app.services.admins import queue_pending_admin_grant
from app.services.partners import queue_pending_partner_grant


@pytest.mark.asyncio
async def test_queue_pending_partner_grant_upserts_existing_grant(session_factory):
    async with session_factory() as session:
        session.add_all([Admin(telegram_id=800), Admin(telegram_id=801)])
        await session.commit()
        await queue_pending_partner_grant(session, 100, 800)
        grant = await queue_pending_partner_grant(session, 100, 801)

        assert grant.requested_by_admin_telegram_id == 801
        assert await session.scalar(
            select(func.count()).select_from(PendingPartnerGrant)
        ) == 1


@pytest.mark.asyncio
async def test_queue_pending_admin_grant_upserts_permissions(session_factory):
    async with session_factory() as session:
        session.add_all([Admin(telegram_id=800), Admin(telegram_id=801)])
        await session.commit()
        await queue_pending_admin_grant(
            session, 100, 800, can_manage_admins=False, can_manage_payouts=True
        )
        grant = await queue_pending_admin_grant(
            session, 100, 801, can_manage_admins=True, can_manage_payouts=False
        )

        assert (
            grant.requested_by_admin_telegram_id,
            grant.can_manage_admins,
            grant.can_manage_payouts,
        ) == (801, True, False)
        assert await session.scalar(select(func.count()).select_from(PendingAdminGrant)) == 1


@pytest.mark.asyncio
async def test_first_start_applies_pending_partner_grant_and_notifies_requester(session_factory):
    async with session_factory() as session:
        requester = Admin(telegram_id=800)
        session.add(requester)
        await session.commit()
        await queue_pending_partner_grant(session, 100, 800)

        bot = AsyncMock()
        await _ensure_user_and_referral(session, 100, None, bot=bot)

        partner = await session.scalar(
            select(ReferralPartner).where(ReferralPartner.telegram_id == 100)
        )
        assert partner is not None
        assert partner.approved_by_admin_id == requester.id
        assert await session.get(PendingPartnerGrant, 100) is None
        bot.send_message.assert_awaited_once_with(
            800, "Рефовод #100 запустил бота — права выданы автоматически."
        )


@pytest.mark.asyncio
async def test_first_start_applies_pending_admin_grant_and_notifies_requester(session_factory):
    async with session_factory() as session:
        session.add(Admin(telegram_id=800))
        await session.commit()
        await queue_pending_admin_grant(
            session, 100, 800, can_manage_admins=True, can_manage_payouts=False
        )

        bot = AsyncMock()
        await _ensure_user_and_referral(session, 100, None, bot=bot)

        admin = await session.scalar(select(Admin).where(Admin.telegram_id == 100))
        assert admin is not None
        assert (admin.can_manage_admins, admin.can_manage_payouts) == (True, False)
        assert await session.get(PendingAdminGrant, 100) is None
        bot.send_message.assert_awaited_once_with(
            800,
            "Администратор #100: права выданы автоматически "
            "(админы: да, выплаты: нет).",
        )


@pytest.mark.asyncio
async def test_normal_first_start_does_not_create_grants_or_notify(session_factory):
    async with session_factory() as session:
        bot = AsyncMock()
        await _ensure_user_and_referral(session, 100, None, bot=bot)

        assert await session.scalar(select(func.count()).select_from(ReferralPartner)) == 0
        assert await session.scalar(select(func.count()).select_from(Admin)) == 0
        bot.send_message.assert_not_awaited()
