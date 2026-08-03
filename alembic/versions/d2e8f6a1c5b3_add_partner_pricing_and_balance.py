"""add partner pricing and balance

Revision ID: d2e8f6a1c5b3
Revises: 7a2b4c6d8e0f
Create Date: 2026-08-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "d2e8f6a1c5b3"
down_revision: str | None = "7a2b4c6d8e0f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("username", sa.String(), nullable=True))
    op.add_column("users", sa.Column("full_name", sa.String(), nullable=True))
    op.create_table(
        "pricing_settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("price_per_subscription", sa.Numeric(12, 2), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column(
        "referral_subscriptions", sa.Column("price_at_credit", sa.Numeric(12, 2), nullable=True)
    )
    op.create_table(
        "partner_balance_adjustments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("partner_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("admin_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["admins.id"]),
        sa.ForeignKeyConstraint(["partner_telegram_id"], ["users.telegram_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_partner_balance_adjustments_partner_telegram_id",
        "partner_balance_adjustments",
        ["partner_telegram_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_partner_balance_adjustments_partner_telegram_id", table_name="partner_balance_adjustments")
    op.drop_table("partner_balance_adjustments")
    op.drop_column("referral_subscriptions", "price_at_credit")
    op.drop_table("pricing_settings")
    op.drop_column("users", "full_name")
    op.drop_column("users", "username")
