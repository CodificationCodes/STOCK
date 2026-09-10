"""order settlement delay

Revision ID: a1c4e7b92f10
Revises: 8332bd9be143
Created: 2026-09-10 00:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import stockgame.server.db.base

revision = "a1c4e7b92f10"
down_revision = "8332bd9be143"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, so every order already in the book stays immediately eligible.
    op.add_column(
        "orders",
        sa.Column("execute_after", stockgame.server.db.base.UTCDateTime(), nullable=True),
    )
    op.create_index("ix_orders_execute_after", "orders", ["execute_after"])


def downgrade() -> None:
    op.drop_index("ix_orders_execute_after", table_name="orders")
    op.drop_column("orders", "execute_after")
