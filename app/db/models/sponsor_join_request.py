from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SponsorJoinRequest(Base):
    """A recorded join request remains valid even if its owner later rejects it."""

    __tablename__ = "sponsor_join_requests"
    __table_args__ = (
        UniqueConstraint(
            "sponsor_chat_id",
            "user_telegram_id",
            name="uq_sponsor_join_request_sponsor_user",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sponsor_chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sponsors.chat_id"))
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
