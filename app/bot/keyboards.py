from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.models import Sponsor


def sponsor_url(sponsor: Sponsor) -> str | None:
    if sponsor.username:
        return f"https://t.me/{sponsor.username.lstrip('@')}"
    return sponsor.invite_link


def build_gate_keyboard(
    subscribed: list[Sponsor], missing: list[Sponsor]
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for sponsor in subscribed:
        url = sponsor_url(sponsor)
        if url:
            builder.button(text=f"✅ {sponsor.title}", url=url)
        else:
            builder.button(text=f"✅ {sponsor.title}", callback_data="sponsor_link_missing")
    for sponsor in missing:
        url = sponsor_url(sponsor)
        if url:
            builder.button(text=f"Подписаться: {sponsor.title}", url=url)
        else:
            builder.button(text=f"Подписаться: {sponsor.title}", callback_data="sponsor_link_missing")
    builder.button(text="✅ Проверить подписку", callback_data="check_subscription")
    builder.adjust(1)
    return builder.as_markup()
