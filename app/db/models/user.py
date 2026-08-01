from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    referrer_telegram_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
