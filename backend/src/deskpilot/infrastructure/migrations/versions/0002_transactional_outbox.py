"""Add the transactional task event outbox.

Revision ID: 0002_transactional_outbox
Revises: 0001_initial_schema
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_transactional_outbox"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox_messages",
        sa.Column("message_id", sa.String(length=40), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("event_id", sa.String(length=40), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("topic", sa.String(length=80), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["task_events.event_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("message_id"),
        sa.UniqueConstraint("event_id", name="uq_outbox_event_id"),
    )
    op.create_index(
        "ix_outbox_messages_task_id",
        "outbox_messages",
        ["task_id"],
    )
    op.create_index(
        "ix_outbox_pending",
        "outbox_messages",
        ["published_at", "available_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_pending", table_name="outbox_messages")
    op.drop_index("ix_outbox_messages_task_id", table_name="outbox_messages")
    op.drop_table("outbox_messages")
