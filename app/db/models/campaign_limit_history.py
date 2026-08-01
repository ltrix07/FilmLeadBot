from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CampaignLimitHistory(Base):
    __tablename__ = "campaign_limit_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"))
    old_limit: Mapped[int] = mapped_column(Integer)
    new_limit: Mapped[int] = mapped_column(Integer)
    changed_by_admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id"))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
