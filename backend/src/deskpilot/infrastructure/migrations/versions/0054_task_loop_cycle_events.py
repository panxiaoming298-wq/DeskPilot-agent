"""add durable task-loop cycle evidence

Revision ID: 0054_task_loop_cycle_events
Revises: 0053_task_loop_execution
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0054_task_loop_cycle_events"
down_revision: str | Sequence[str] | None = "0053_task_loop_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_loop_capability_approvals",
        sa.Column("approval_id", sa.String(length=69), nullable=False),
        sa.Column("execution_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=68), nullable=False),
        sa.Column("node_id", sa.String(length=68), nullable=False),
        sa.Column("node_binding_id", sa.String(length=68), nullable=False),
        sa.Column("attempt_id", sa.String(length=68), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("input_binding_digest", sa.String(length=64), nullable=False),
        sa.Column("executor_manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("preview_schema_digest", sa.String(length=64), nullable=False),
        sa.Column("preview_manifest", sa.JSON(), nullable=False),
        sa.Column("confirmation_digest", sa.String(length=64), nullable=False),
        sa.Column("requested_execution_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_digest", sa.String(length=64), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("approval_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt >= 1 AND plan_generation BETWEEN 1 AND 3 AND "
            "requested_execution_revision >= 2 AND revision BETWEEN 1 AND 3",
            name="ck_task_loop_capability_approval_versions",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'consumed')",
            name="ck_task_loop_capability_approval_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND revision = 1 AND approved_at IS NULL AND "
            "consumed_at IS NULL AND result_digest IS NULL) OR "
            "(status = 'approved' AND revision = 2 AND approved_at IS NOT NULL AND "
            "consumed_at IS NULL AND result_digest IS NULL) OR "
            "(status = 'consumed' AND revision = 3 AND approved_at IS NOT NULL AND "
            "consumed_at IS NOT NULL AND result_digest IS NOT NULL)",
            name="ck_task_loop_capability_approval_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["task_loop_executions.execution_id"],
            name="fk_task_loop_capability_approvals_execution",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_task_loop_capability_approvals_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["task_execution_runs.run_id"],
            name="fk_task_loop_capability_approvals_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["task_execution_nodes.node_id"],
            name="fk_task_loop_capability_approvals_node",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["node_binding_id"],
            ["model_planner_node_bindings.node_binding_id"],
            name="fk_task_loop_capability_approvals_binding",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["task_loop_node_attempts.attempt_id"],
            name="fk_task_loop_capability_approvals_attempt",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("approval_id"),
        sa.UniqueConstraint(
            "execution_id", "node_id", name="uq_task_loop_capability_approval_node"
        ),
        sa.UniqueConstraint(
            "attempt_id", name="uq_task_loop_capability_approval_attempt"
        ),
        sa.UniqueConstraint(
            "approval_digest", name="uq_task_loop_capability_approval_digest"
        ),
    )
    op.create_index(
        "ix_task_loop_capability_approvals_pending",
        "task_loop_capability_approvals",
        ["task_id", "status", "updated_at"],
        unique=False,
    )
    op.create_table(
        "task_loop_cycle_events",
        sa.Column("event_id", sa.String(length=68), nullable=False),
        sa.Column("execution_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_event_digest", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("source_progress_digest", sa.String(length=64), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("evidence_manifest", sa.JSON(), nullable=False),
        sa.Column("evidence_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("event_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence >= 1 AND plan_generation BETWEEN 1 AND 3",
            name="ck_task_loop_cycle_event_versions",
        ),
        sa.CheckConstraint(
            "kind IN ('no_progress_observed', 'no_progress_terminated', "
            "'budget_exhausted', 'repair_started', 'repair_completed')",
            name="ck_task_loop_cycle_event_kind",
        ),
        sa.CheckConstraint(
            "(sequence = 1 AND previous_event_digest IS NULL) OR "
            "(sequence > 1 AND previous_event_digest IS NOT NULL)",
            name="ck_task_loop_cycle_event_chain_root",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["task_loop_executions.execution_id"],
            name="fk_task_loop_cycle_events_execution",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_task_loop_cycle_events_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id", "previous_event_digest"],
            [
                "task_loop_cycle_events.execution_id",
                "task_loop_cycle_events.event_digest",
            ],
            name="fk_task_loop_cycle_event_previous",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "execution_id",
            "sequence",
            name="uq_task_loop_cycle_event_sequence",
        ),
        sa.UniqueConstraint(
            "event_digest",
            name="uq_task_loop_cycle_event_digest",
        ),
        sa.UniqueConstraint(
            "execution_id",
            "event_digest",
            name="uq_task_loop_cycle_event_chain_target",
        ),
    )
    op.create_index(
        "ix_task_loop_cycle_events_progress",
        "task_loop_cycle_events",
        ["execution_id", "source_progress_digest", "kind", "sequence"],
        unique=False,
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "task-loop capability approvals",
                "SELECT 1 FROM task_loop_capability_approvals LIMIT 1",
            ),
            (
                "task-loop cycle events",
                "SELECT 1 FROM task_loop_cycle_events LIMIT 1",
            ),
        ),
    )
    op.drop_index(
        "ix_task_loop_cycle_events_progress",
        table_name="task_loop_cycle_events",
    )
    op.drop_table("task_loop_cycle_events")
    op.drop_index(
        "ix_task_loop_capability_approvals_pending",
        table_name="task_loop_capability_approvals",
    )
    op.drop_table("task_loop_capability_approvals")
