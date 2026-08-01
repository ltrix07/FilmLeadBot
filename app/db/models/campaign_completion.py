from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CampaignCompletion(Base):
    __tablename__ = "campaign_completions"
    __table_args__ = (UniqueConstraint("campaign_id", "user_telegram_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"))
    user_telegram_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
