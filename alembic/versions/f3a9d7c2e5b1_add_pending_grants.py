"""add pending partner and administrator grants

Revision ID: f3a9d7c2e5b1
Revises: e5f7a9b2c4d6
Create Date: 2026-08-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "f3a9d7c2e5b1"
down_revision: str | None = "e5f7a9b2c4d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_partner_grants",
        sa.Column("telegram_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("requested_by_admin_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["requested_by_admin_telegram_id"], ["admins.telegram_id"]),
        sa.PrimaryKeyConstraint("telegram_id"),
    )
    op.create_table(
        "pending_admin_grants",
        sa.Column("telegram_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("requested_by_admin_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("can_manage_admins", sa.Boolean(), nullable=False),
        sa.Column("can_manage_payouts", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["requested_by_admin_telegram_id"], ["admins.telegram_id"]),
        sa.PrimaryKeyConstraint("telegram_id"),
    )


def downgrade() -> None:
    op.drop_table("pending_admin_grants")
    op.drop_table("pending_partner_grants")
