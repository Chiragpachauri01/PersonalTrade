"""add kill_switch_state table

Revision ID: 21bf011bc093
Revises: b5bb1075a581
Create Date: 2026-07-20 21:54:48.017253
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "21bf011bc093"
down_revision: str | None = "b5bb1075a581"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kill_switch_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tripped", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=True),
        sa.Column("tripped_at", sa.DateTime(), nullable=True),
        sa.Column("consecutive_errors", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("kill_switch_state")
