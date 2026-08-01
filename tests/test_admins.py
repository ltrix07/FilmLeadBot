import pytest
from sqlalchemy import func, select

from app.db.models import Admin, SponsorType
from app.services.admins import bot_status_allows_access, ensure_seed_admins, is_admin


@pytest.mark.asyncio
async def test_seed_admins_is_idempotent(session_factory):
    async with session_factory() as session:
        await ensure_seed_admins(session, [100, 200])

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Admin)) == 2
        assert await is_admin(session, 100) is True
        assert await is_admin(session, 999) is False

    async with session_factory() as session:
        await ensure_seed_admins(session, [100, 200])

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Admin)) == 2


@pytest.mark.parametrize(
    ("chat_type", "status", "expected"),
    [
        (SponsorType.CHANNEL, "administrator", True),
        (SponsorType.CHANNEL, "member", False),
        (SponsorType.GROUP, "member", True),
        (SponsorType.SUPERGROUP, "left", False),
    ],
)
def test_bot_status_allows_access(chat_type, status, expected):
    assert bot_status_allows_access(chat_type, status) is expected
