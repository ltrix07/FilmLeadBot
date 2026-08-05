"""add partner bonus rate

Revision ID: c7d3e9f2a4b6
Revises: 6f2a8c4d9e1b
Create Date: 2026-08-05
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c7d3e9f2a4b6"
down_revision: str | None = "6f2a8c4d9e1b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("referral_partners", "pending_partner_grants"):
        op.add_column(table, sa.Column("bonus_rate", sa.Numeric(12, 2), nullable=True))
        op.add_column(table, sa.Column("bonus_rate_until", sa.Date(), nullable=True))


def downgrade() -> None:
    for table in ("pending_partner_grants", "referral_partners"):
        op.drop_column(table, "bonus_rate_until")
        op.drop_column(table, "bonus_rate")
