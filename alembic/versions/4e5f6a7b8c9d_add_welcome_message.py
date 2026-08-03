"""add editable welcome message

Revision ID: 4e5f6a7b8c9d
Revises: c1b5a3f9d2e4
Create Date: 2026-08-01
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "4e5f6a7b8c9d"
down_revision: str | None = "c1b5a3f9d2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "welcome_message",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("source_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("preview", sa.String(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_welcome_message")),
    )


def downgrade() -> None:
    op.drop_table("welcome_message")
