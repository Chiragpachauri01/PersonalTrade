"""add instrument name and news instrument tags

Revision ID: 75d3cb675810
Revises: a89d90acf864
Create Date: 2026-07-21 23:47:33.800857
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "75d3cb675810"
down_revision: str | None = "a89d90acf864"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_instrument_tags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("news_item_id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.ForeignKeyConstraint(["news_item_id"], ["news_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("news_item_id", "instrument_id", name="uq_news_instrument_tag"),
    )
    with op.batch_alter_table("news_instrument_tags", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_news_instrument_tags_instrument_id"), ["instrument_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_news_instrument_tags_news_item_id"), ["news_item_id"], unique=False
        )

    with op.batch_alter_table("instruments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("name", sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("instruments", schema=None) as batch_op:
        batch_op.drop_column("name")

    with op.batch_alter_table("news_instrument_tags", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_news_instrument_tags_news_item_id"))
        batch_op.drop_index(batch_op.f("ix_news_instrument_tags_instrument_id"))

    op.drop_table("news_instrument_tags")
