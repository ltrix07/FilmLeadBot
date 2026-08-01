from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message

from app.services.admins import is_admin


class IsAdmin(Filter):
    async def __call__(self, event: Message | CallbackQuery, session_factory) -> bool:
        async with session_factory() as session:
            return await is_admin(session, event.from_user.id)
