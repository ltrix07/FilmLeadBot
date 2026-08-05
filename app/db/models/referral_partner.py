from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ReferralPartner(Base):
    __tablename__ = "referral_partners"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), unique=True)
    referral_code: Mapped[str] = mapped_column(String, unique=True)
    approved_by_admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id"))
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bonus_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    bonus_rate_until: Mapped[date | None] = mapped_column(Date)
