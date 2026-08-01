from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import select

from app.db.models import MovieCode, MovieCodeStatus
from app.services.subscription import AccessResult

menu_router = Router(name="menu")


@menu_router.message(F.text)
async def handle_movie_code(message: Message, session_factory, access_result: AccessResult) -> None:
    code = message.text.strip()
    async with session_factory() as session:
        movie = await session.scalar(
            select(MovieCode).where(
                MovieCode.code == code, MovieCode.status == MovieCodeStatus.ACTIVE
            )
        )
    if movie is None:
        await message.answer(f"Код «{code}» не найден.")
    else:
        await message.answer(movie.title)
