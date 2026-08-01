from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, DateTime, Enum as SqlEnum, ForeignKey, Index, Integer, func
from sqlalchemy.orm import Mapped, relationship, mapped_column

from app.db.base import Base


class CampaignStatus(str, Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (Index("ix_campaigns_status", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    sponsor_chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("sponsors.chat_id"))
    limit_original: Mapped[int] = mapped_column(Integer)
    limit_current: Mapped[int] = mapped_column(Integer)
    counter: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[CampaignStatus] = mapped_column(
        SqlEnum(CampaignStatus, native_enum=False, validate_strings=True)
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by_admin_id: Mapped[int | None] = mapped_column(ForeignKey("admins.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    sponsor: Mapped["Sponsor"] = relationship()
    completions: Mapped[list["CampaignCompletion"]] = relationship()
