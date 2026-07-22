"""add soak_periods table

Revision ID: f1a2b3c4d5e6
Revises: c3f5a1d8e2b4
Create Date: 2026-07-22 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "c3f5a1d8e2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "soak_periods",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("target_days", sa.Integer(), nullable=False),
        sa.Column("baseline_backtest_run_id", sa.Integer(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("end_reason", sa.String(length=256), nullable=True),
        sa.Column("notes", sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(["baseline_backtest_run_id"], ["backtest_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("soak_periods")
