"""insider tips

Revision ID: c93f10ab7742
Revises: a1c4e7b92f10
Created: 2026-09-10 00:30:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import stockgame.server.db.base

revision = "c93f10ab7742"
down_revision = "a1c4e7b92f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "insider_tips",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("public_id", sa.String(length=22), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("stock_id", sa.Integer(), nullable=False),
        sa.Column("fee_cents", sa.Integer(), nullable=False),
        sa.Column("promised_pct", sa.Float(), nullable=False),
        sa.Column("actual_pct", sa.Float(), nullable=False),
        sa.Column("genuine", sa.Boolean(), nullable=False),
        sa.Column("applies_at", stockgame.server.db.base.UTCDateTime(), nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False),
        sa.Column("created_at", stockgame.server.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
    )
    op.create_index("ix_insider_tips_user_id", "insider_tips", ["user_id"])
    op.create_index("ix_insider_tips_stock_id", "insider_tips", ["stock_id"])
    op.create_index("ix_insider_tips_applies_at", "insider_tips", ["applies_at"])
    op.create_index("ix_insider_tips_applied", "insider_tips", ["applied"])


def downgrade() -> None:
    op.drop_table("insider_tips")
