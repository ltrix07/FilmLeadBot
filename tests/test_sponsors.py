from datetime import datetime, timezone

import pytest

from app.db.models import Sponsor, SponsorType
from app.bot.routers.admin import (
    _get_sponsors_page,
    _sponsor_card_keyboard,
    _sponsor_card_text,
    _sponsors_page_keyboard,
)
from app.services.sponsors import archive_sponsor, count_sponsors, list_sponsors, unarchive_sponsor


@pytest.mark.asyncio
async def test_list_sponsors_separates_archived_records_and_paginates(session_factory):
    async with session_factory() as session:
        session.add_all([
            Sponsor(chat_id=-1, title="Bravo", type=SponsorType.CHANNEL),
            Sponsor(chat_id=-2, title="Alpha", type=SponsorType.CHANNEL),
            Sponsor(chat_id=-3, title="Archived", type=SponsorType.CHANNEL, archived_at=datetime.now(timezone.utc)),
        ])
        await session.commit()

    async with session_factory() as session:
        assert [sponsor.title for sponsor in await list_sponsors(session, limit=1)] == ["Alpha"]
        assert [sponsor.title for sponsor in await list_sponsors(session, offset=1)] == ["Bravo"]
        assert [sponsor.title for sponsor in await list_sponsors(session, archived=True)] == ["Archived"]
        assert await count_sponsors(session) == 2
        assert await count_sponsors(session, archived=True) == 1


@pytest.mark.asyncio
async def test_archive_and_unarchive_sponsor_are_safe_and_idempotent(session_factory):
    async with session_factory() as session:
        session.add(Sponsor(chat_id=-1, title="Sponsor", type=SponsorType.CHANNEL))
        await session.commit()

        archived = await archive_sponsor(session, -1)
        assert archived is not None and archived.archived_at is not None
        archived_at = archived.archived_at
        assert (await archive_sponsor(session, -1)).archived_at == archived_at
        assert await archive_sponsor(session, -404) is None

        restored = await unarchive_sponsor(session, -1)
        assert restored is not None and restored.archived_at is None
        assert await unarchive_sponsor(session, -404) is None


@pytest.mark.asyncio
async def test_sponsor_menu_paginates_and_archived_card_links_to_archive(session_factory):
    async with session_factory() as session:
        session.add_all([
            Sponsor(chat_id=-index, title=f"Sponsor {index:02}", type=SponsorType.CHANNEL)
            for index in range(15)
        ])
        session.add(Sponsor(
            chat_id=-100, title="Archived", type=SponsorType.CHANNEL,
            archived_at=datetime.now(timezone.utc),
        ))
        await session.commit()

    first, total_pages = await _get_sponsors_page(session_factory, 0, archived=False)
    second, _ = await _get_sponsors_page(session_factory, 1, archived=False)
    first_buttons = [button.text for row in _sponsors_page_keyboard(first, 0, total_pages, archived=False).inline_keyboard for button in row]
    second_buttons = [button.text for row in _sponsors_page_keyboard(second, 1, total_pages, archived=False).inline_keyboard for button in row]
    assert len(first) == 10
    assert len(second) == 5
    assert "▶️ След" in first_buttons and "◀️ Пред" not in first_buttons
    assert "◀️ Пред" in second_buttons and "▶️ След" not in second_buttons

    async with session_factory() as session:
        archived = await session.get(Sponsor, -100)
    assert archived is not None
    assert "· в архиве" in _sponsor_card_text(archived)
    archived_buttons = [button for row in _sponsor_card_keyboard(archived).inline_keyboard for button in row]
    assert any(button.text == "♻️ Разархивировать" for button in archived_buttons)
    assert any(button.callback_data == "admin:sponsors:archived:0" for button in archived_buttons)
