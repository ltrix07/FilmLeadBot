from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MovieCode, MovieCodeAudit, MovieCodeStatus
from app.services.admins import get_admin_id


async def build_active_codes_export_xlsx(session: AsyncSession) -> bytes:
    """Export active movie codes and titles as an XLSX workbook."""
    movie_codes = list((await session.scalars(
        select(MovieCode).where(MovieCode.status == MovieCodeStatus.ACTIVE).order_by(MovieCode.code)
    )).all())
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["code", "title"])
    for cell in worksheet[1]:
        cell.font = Font(bold=True)
    for movie_code in movie_codes:
        worksheet.append([movie_code.code, movie_code.title])
    for cell in worksheet["A"]:
        cell.number_format = "@"
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


async def build_all_codes_export_xlsx(session: AsyncSession) -> bytes:
    """Export every movie code (active and inactive) for admin review."""
    movie_codes = list((await session.scalars(select(MovieCode).order_by(MovieCode.code))).all())
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["code", "title", "status"])
    for cell in worksheet[1]:
        cell.font = Font(bold=True)
    for movie_code in movie_codes:
        worksheet.append([movie_code.code, movie_code.title, movie_code.status.value])
    for cell in worksheet["A"]:
        cell.number_format = "@"
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


async def create_code(
    session: AsyncSession, code: str, title: str, admin_telegram_id: int, *, source: str = "manual"
) -> MovieCode:
    if await session.get(MovieCode, code) is not None:
        raise ValueError(f"Movie code {code!r} already exists")
    movie_code = MovieCode(code=code, title=title, status=MovieCodeStatus.ACTIVE)
    session.add(movie_code)
    session.add(MovieCodeAudit(
        code=code, action="create", old_title=None, new_title=title, source=source,
        changed_by_admin_id=await get_admin_id(session, admin_telegram_id),
    ))
    await session.commit()
    return movie_code


async def update_title(
    session: AsyncSession, code: str, new_title: str, admin_telegram_id: int, *, source: str = "manual"
) -> MovieCode:
    movie_code = await session.get(MovieCode, code)
    if movie_code is None:
        raise LookupError(f"Movie code {code!r} is missing")
    old_title = movie_code.title
    movie_code.title = new_title
    session.add(MovieCodeAudit(
        code=code, action="update", old_title=old_title, new_title=new_title, source=source,
        changed_by_admin_id=await get_admin_id(session, admin_telegram_id),
    ))
    await session.commit()
    return movie_code


async def deactivate_code(
    session: AsyncSession, code: str, admin_telegram_id: int, *, source: str = "manual"
) -> MovieCode:
    movie_code = await session.get(MovieCode, code)
    if movie_code is None:
        raise LookupError(f"Movie code {code!r} is missing")
    if movie_code.status is MovieCodeStatus.INACTIVE:
        return movie_code
    movie_code.status = MovieCodeStatus.INACTIVE
    session.add(MovieCodeAudit(
        code=code, action="deactivate", old_title=movie_code.title, new_title=movie_code.title, source=source,
        changed_by_admin_id=await get_admin_id(session, admin_telegram_id),
    ))
    await session.commit()
    return movie_code


async def restore_code(
    session: AsyncSession, code: str, admin_telegram_id: int, new_title: str | None, *, source: str = "manual"
) -> MovieCode:
    movie_code = await session.get(MovieCode, code)
    if movie_code is None:
        raise LookupError(f"Movie code {code!r} is missing")
    if movie_code.status is MovieCodeStatus.ACTIVE:
        raise ValueError(f"Movie code {code!r} is already active")
    old_title = movie_code.title
    movie_code.status = MovieCodeStatus.ACTIVE
    if new_title is not None:
        movie_code.title = new_title
    session.add(MovieCodeAudit(
        code=code, action="restore", old_title=old_title, new_title=movie_code.title, source=source,
        changed_by_admin_id=await get_admin_id(session, admin_telegram_id),
    ))
    await session.commit()
    return movie_code


async def get_code_history(session: AsyncSession, code: str) -> list[MovieCodeAudit]:
    return list((await session.scalars(
        select(MovieCodeAudit).where(MovieCodeAudit.code == code).order_by(MovieCodeAudit.changed_at.desc())
    )).all())
