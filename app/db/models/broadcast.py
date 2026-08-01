from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, DateTime, Enum as SqlEnum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BroadcastStatus(str, Enum):
    DRAFT = "draft"
    SENDING = "sending"
    COMPLETED = "completed"
    FAILED = "failed"


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id"))
    content: Mapped[str] = mapped_column(String)
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[BroadcastStatus] = mapped_column(
        SqlEnum(BroadcastStatus, native_enum=False, validate_strings=True)
    )
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
