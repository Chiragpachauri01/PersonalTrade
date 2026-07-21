"""add trade realized_pnl and executed_at index

Revision ID: a89d90acf864
Revises: 8d1826978b1c
Create Date: 2026-07-21 21:23:19.766514
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a89d90acf864"
down_revision: str | None = "8d1826978b1c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("trades", schema=None) as batch_op:
        batch_op.add_column(sa.Column("realized_pnl", sa.String(length=40), nullable=True))
        batch_op.create_index(batch_op.f("ix_trades_executed_at"), ["executed_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("trades", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_trades_executed_at"))
        batch_op.drop_column("realized_pnl")
