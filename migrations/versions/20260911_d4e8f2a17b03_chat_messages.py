"""chat messages

Revision ID: d4e8f2a17b03
Revises: c93f10ab7742
Created: 2026-09-11 00:00:00.000000+00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

import stockgame.server.db.base

revision = "d4e8f2a17b03"
down_revision = "c93f10ab7742"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=20), nullable=False),
        sa.Column("body", sa.String(length=240), nullable=False),
        sa.Column("created_at", stockgame.server.db.base.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_messages_user_id", "chat_messages", ["user_id"])
    op.create_index("ix_chat_messages_created_at", "chat_messages", ["created_at"])


def downgrade() -> None:
    op.drop_table("chat_messages")
