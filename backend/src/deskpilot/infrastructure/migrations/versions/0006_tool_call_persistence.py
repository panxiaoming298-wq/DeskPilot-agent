"""Add the durable Tool Runner call ledger.

Revision ID: 0006_tool_call_persistence
Revises: 0005_provider_management
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_tool_call_persistence"
down_revision: str | None = "0005_provider_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_calls",
        sa.Column("call_id", sa.String(length=128), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("step_id", sa.String(length=128), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("tool_version", sa.String(length=32), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("arguments_digest", sa.String(length=64), nullable=False),
        sa.Column("idempotency", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key_digest", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("runner_id", sa.String(length=128), nullable=True),
        sa.Column("resolution_source", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("terminal_event_id", sa.String(length=40), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('requested', 'running', 'succeeded', 'failed', "
            "'cancelled', 'unknown')",
            name="ck_tool_calls_status",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["terminal_event_id"],
            ["task_events.event_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("call_id"),
        sa.UniqueConstraint(
            "task_id",
            "step_id",
            "attempt",
            name="uq_tool_calls_task_step_attempt",
        ),
        sa.UniqueConstraint(
            "terminal_event_id",
            name="uq_tool_calls_terminal_event_id",
        ),
    )
    op.create_index(
        "ix_tool_calls_task_status",
        "tool_calls",
        ["task_id", "status"],
    )
    op.create_index(
        "ix_tool_calls_recovery",
        "tool_calls",
        ["status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_calls_recovery", table_name="tool_calls")
    op.drop_index("ix_tool_calls_task_status", table_name="tool_calls")
    op.drop_table("tool_calls")
