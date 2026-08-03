from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PendingPartnerGrant(Base):
    __tablename__ = "pending_partner_grants"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    requested_by_admin_telegram_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("admins.telegram_id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
