from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ReferralEvent(Base):
    __tablename__ = "referral_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    referred_user_telegram_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), unique=True
    )
    referrer_telegram_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
