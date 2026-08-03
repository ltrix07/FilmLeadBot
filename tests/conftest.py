import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
import app.db.models  # noqa: F401 - register all mapped tables with Base metadata


TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://filmtraffic:filmtraffic@localhost:5433/filmtraffic_test",
)


@pytest.fixture
async def session_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            table_names = ", ".join(
                table.name for table in reversed(Base.metadata.sorted_tables)
            )
            await connection.execute(text(f"TRUNCATE TABLE {table_names} CASCADE"))
    except OSError as error:
        await engine.dispose()
        pytest.skip(f"PostgreSQL is unavailable: {error}")
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
