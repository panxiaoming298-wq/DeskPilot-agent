"""Create the initial task and event schema.

Revision ID: 0001_initial_schema
Revises: None
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create a new database or adopt the schema from the pre-Alembic release."""
    connection = op.get_bind()
    existing_tables = set(inspect(connection).get_table_names())

    if "tasks" not in existing_tables:
        op.create_table(
            "tasks",
            sa.Column("task_id", sa.String(length=40), nullable=False),
            sa.Column("conversation_id", sa.String(length=40), nullable=True),
            sa.Column("goal", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("mode", sa.String(length=32), nullable=False),
            sa.Column("privacy_mode", sa.String(length=32), nullable=False),
            sa.Column("constraints", sa.JSON(), nullable=False),
            sa.Column("last_event_seq", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("task_id"),
        )
        op.create_index("ix_tasks_conversation_id", "tasks", ["conversation_id"])
        op.create_index("ix_tasks_status", "tasks", ["status"])

    if "task_events" not in existing_tables:
        op.create_table(
            "task_events",
            sa.Column("event_id", sa.String(length=40), nullable=False),
            sa.Column("task_id", sa.String(length=40), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("type", sa.String(length=80), nullable=False),
            sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
            sa.Column("trace_id", sa.String(length=40), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("event_id"),
            sa.UniqueConstraint("task_id", "seq", name="uq_task_event_seq"),
        )
        op.create_index("ix_task_events_task_id", "task_events", ["task_id"])
        op.create_index("ix_task_events_trace_id", "task_events", ["trace_id"])
        op.create_index("ix_task_events_type", "task_events", ["type"])


def downgrade() -> None:
    op.drop_index("ix_task_events_type", table_name="task_events")
    op.drop_index("ix_task_events_trace_id", table_name="task_events")
    op.drop_index("ix_task_events_task_id", table_name="task_events")
    op.drop_table("task_events")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_index("ix_tasks_conversation_id", table_name="tasks")
    op.drop_table("tasks")
