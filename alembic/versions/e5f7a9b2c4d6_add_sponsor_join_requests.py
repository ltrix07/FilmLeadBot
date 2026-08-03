"""add sponsor join requests

Revision ID: e5f7a9b2c4d6
Revises: b4e8f2a1c6d9
Create Date: 2026-08-03
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "e5f7a9b2c4d6"
down_revision: str | None = "b4e8f2a1c6d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "sponsors",
        sa.Column("request_mode", sa.Boolean(), server_default="false", nullable=False),
    )
    op.create_table(
        "sponsor_join_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("sponsor_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["sponsor_chat_id"], ["sponsors.chat_id"]),
        sa.ForeignKeyConstraint(["user_telegram_id"], ["users.telegram_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sponsor_chat_id",
            "user_telegram_id",
            name="uq_sponsor_join_request_sponsor_user",
        ),
    )


def downgrade() -> None:
    op.drop_table("sponsor_join_requests")
    op.drop_column("sponsors", "request_mode")
