from __future__ import annotations

import asyncio
import datetime
import json
import logging

from app.bot import create_bot, create_dispatcher
from app.config import Settings
from app.db.session import create_engine, create_session_factory
from app.scheduler import create_scheduler
from app.services.admins import ensure_seed_admins
from app.services.subscription import SubscriptionAccessService


class JsonFormatter(logging.Formatter):
    """Small dependency-free JSON formatter for application logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in logging.LogRecord("", 0, "", 0, "", (), None).__dict__:
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)


async def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger(__name__)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        await ensure_seed_admins(session, settings.admin_ids)
    bot = create_bot(settings.bot_token)
    subscription_service = SubscriptionAccessService(settings.subscription_cache_ttl_seconds)
    dispatcher = create_dispatcher(subscription_service)
    scheduler = create_scheduler(bot, session_factory)
    scheduler.start()
    logger.info("application_started", extra={"admin_count": len(settings.admin_ids)})

    try:
        await dispatcher.start_polling(
            bot,
            session_factory=session_factory,
            subscription_service=subscription_service,
            settings=settings,
        )
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await engine.dispose()
        logger.info("application_stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
