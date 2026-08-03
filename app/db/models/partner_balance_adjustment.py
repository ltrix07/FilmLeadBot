from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PartnerBalanceAdjustment(Base):
    __tablename__ = "partner_balance_adjustments"
    __table_args__ = (Index("ix_partner_balance_adjustments_partner_telegram_id", "partner_telegram_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    partner_telegram_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    title: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
