"""add paper_account table

Revision ID: 8d1826978b1c
Revises: 21bf011bc093
Create Date: 2026-07-20 22:14:11.649646
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8d1826978b1c"
down_revision: str | None = "21bf011bc093"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cash", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("paper_account")
