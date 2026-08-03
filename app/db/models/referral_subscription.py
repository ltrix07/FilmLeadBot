from datetime import datetime

from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Numeric, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ReferralSubscription(Base):
    """A referral credit earned when a lead completes a campaign."""

    __tablename__ = "referral_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "referred_user_telegram_id",
            name="uq_referral_subscription_campaign_lead",
        ),
        Index("ix_referral_subscriptions_referrer_telegram_id", "referrer_telegram_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"))
    referrer_telegram_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    referred_user_telegram_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    price_at_credit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
