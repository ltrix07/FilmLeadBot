"""add referral subscriptions

Revision ID: 7a2b4c6d8e0f
Revises: 9b4e2d7f1a6c
Create Date: 2026-08-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "7a2b4c6d8e0f"
down_revision: str | None = "9b4e2d7f1a6c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "referral_subscriptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("referrer_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("referred_user_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.ForeignKeyConstraint(["referrer_telegram_id"], ["users.telegram_id"]),
        sa.ForeignKeyConstraint(["referred_user_telegram_id"], ["users.telegram_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id",
            "referred_user_telegram_id",
            name="uq_referral_subscription_campaign_lead",
        ),
    )
    op.create_index(
        "ix_referral_subscriptions_referrer_telegram_id",
        "referral_subscriptions",
        ["referrer_telegram_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_referral_subscriptions_referrer_telegram_id", table_name="referral_subscriptions")
    op.drop_table("referral_subscriptions")
