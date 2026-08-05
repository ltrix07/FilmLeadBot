from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import update

from app.db.models import MovieCode, MovieCodeStatus
from app.services.subscription import AccessResult

menu_router = Router(name="menu")

NEXT_REQUEST_EMOJI_ID = "5334917262207889846"


@menu_router.message(F.text)
async def handle_movie_code(message: Message, session_factory, access_result: AccessResult) -> None:
    code = message.text.strip()
    async with session_factory() as session:
        movie = await session.scalar(
            update(MovieCode)
            .where(MovieCode.code == code, MovieCode.status == MovieCodeStatus.ACTIVE)
            .values(lookup_count=MovieCode.lookup_count + 1)
            .returning(MovieCode)
        )
        await session.commit()
    if movie is None:
        await message.answer(
            f"🍎 Код «{code}» не найден. Возможно - была допущена ошибка, "
            "проверьте правильность написания номера."
        )
    else:
        await message.answer(movie.title)
        await message.answer(
            "Приятного просмотра! Буду ждать - Ваш следующий запрос "
            f'<tg-emoji emoji-id="{NEXT_REQUEST_EMOJI_ID}">⏳</tg-emoji>.',
            parse_mode="HTML",
        )
