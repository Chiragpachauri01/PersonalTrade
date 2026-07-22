"""add upstox_tokens table

Revision ID: c3f5a1d8e2b4
Revises: 75d3cb675810
Create Date: 2026-07-22 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f5a1d8e2b4"
down_revision: str | None = "75d3cb675810"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "upstox_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("encrypted_access_token", sa.Text(), nullable=False),
        sa.Column("obtained_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("upstox_tokens")
