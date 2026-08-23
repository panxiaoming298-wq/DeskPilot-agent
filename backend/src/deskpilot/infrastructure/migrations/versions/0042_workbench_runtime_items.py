"""Add durable server-side Workbench advancement queue.

Revision ID: 0042_workbench_runtime_items
Revises: 0041_agent_input_requests
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0042_workbench_runtime_items"
down_revision: str | None = "0041_agent_input_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workbench_runtime_items",
        sa.Column("work_item_id", sa.String(68), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("consecutive_failure_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_projection_digest", sa.String(64), nullable=True),
        sa.Column("claim_owner_id", sa.String(128), nullable=True),
        sa.Column("claim_fencing_token", sa.Integer(), nullable=False),
        sa.Column("claim_acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(100), nullable=True),
        sa.Column("last_error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("action = 'advance'", name="ck_workbench_runtime_item_action"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'applied', 'cancelled', 'dead_letter')",
            name="ck_workbench_runtime_item_status",
        ),
        sa.CheckConstraint(
            "revision >= 1 AND attempt_count >= 0 AND consecutive_failure_count >= 0 "
            "AND claim_fencing_token >= 0",
            name="ck_workbench_runtime_item_counters",
        ),
        sa.UniqueConstraint(
            "task_id",
            "action",
            name="uq_workbench_runtime_item_task_action",
        ),
    )
    op.create_index(
        "ix_workbench_runtime_items_ready",
        "workbench_runtime_items",
        ["status", "available_at", "created_at"],
    )
    op.create_index(
        "ix_workbench_runtime_items_lease",
        "workbench_runtime_items",
        ["status", "claim_expires_at"],
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "persisted Workbench runtime item",
                "SELECT 1 FROM workbench_runtime_items LIMIT 1",
            ),
        ),
    )
    op.drop_index(
        "ix_workbench_runtime_items_lease",
        table_name="workbench_runtime_items",
    )
    op.drop_index(
        "ix_workbench_runtime_items_ready",
        table_name="workbench_runtime_items",
    )
    op.drop_table("workbench_runtime_items")
