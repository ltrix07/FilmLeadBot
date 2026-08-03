from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PricingSettings, WelcomeMessage


async def get_subscription_price(session: AsyncSession) -> Decimal:
    settings = await session.get(PricingSettings, 1)
    return settings.price_per_subscription if settings is not None else Decimal("0")


async def set_subscription_price(session: AsyncSession, price: Decimal) -> None:
    statement = insert(PricingSettings).values(id=1, price_per_subscription=price)
    statement = statement.on_conflict_do_update(
        index_elements=[PricingSettings.id],
        set_={
            "price_per_subscription": statement.excluded.price_per_subscription,
            "updated_at": func.now(),
        },
    )
    await session.execute(statement)
    await session.commit()


async def get_welcome_message(session: AsyncSession) -> WelcomeMessage | None:
    """Return the configured welcome message, or None if not customized yet."""
    return await session.get(WelcomeMessage, 1)


async def set_welcome_message(
    session: AsyncSession, source_chat_id: int, source_message_id: int, preview: str
) -> WelcomeMessage:
    """Create or replace the singleton welcome message."""
    statement = insert(WelcomeMessage).values(
        id=1,
        source_chat_id=source_chat_id,
        source_message_id=source_message_id,
        preview=preview,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[WelcomeMessage.id],
        set_={
            "source_chat_id": statement.excluded.source_chat_id,
            "source_message_id": statement.excluded.source_message_id,
            "preview": statement.excluded.preview,
            "updated_at": func.now(),
        },
    )
    await session.execute(statement)
    await session.commit()
    welcome = await session.get(WelcomeMessage, 1)
    assert welcome is not None
    return welcome
