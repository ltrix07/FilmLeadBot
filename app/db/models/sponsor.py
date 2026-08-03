from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, Boolean, DateTime, Enum as SqlEnum, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SponsorType(str, Enum):
    CHANNEL = "channel"
    GROUP = "group"
    SUPERGROUP = "supergroup"


class Sponsor(Base):
    __tablename__ = "sponsors"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String)
    invite_link: Mapped[str | None] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    type: Mapped[SponsorType] = mapped_column(
        SqlEnum(SponsorType, native_enum=False, validate_strings=True),
    )
    bot_has_access: Mapped[bool] = mapped_column(Boolean, default=False)
    own_channel: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    request_mode: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
