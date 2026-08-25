"""persist verified workspace coding evidence deliveries

Revision ID: 0058_workspace_coding_deliveries
Revises: 0057_task_loop_deferred_binding
Create Date: 2026-08-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0058_workspace_coding_deliveries"
down_revision: str | None = "0057_task_loop_deferred_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_coding_deliveries",
        sa.Column("delivery_id", sa.String(length=68), nullable=False),
        sa.Column("execution_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("run_id", sa.String(length=68), nullable=False),
        sa.Column("plan_id", sa.String(length=68), nullable=False),
        sa.Column("plan_manifest_digest", sa.String(length=64), nullable=False),
        sa.Column("changed_file_count", sa.Integer(), nullable=False),
        sa.Column("test_run_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("rollback_available", sa.Boolean(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("delivery_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "changed_file_count = 2 AND test_run_count BETWEEN 1 AND 2 "
            "AND failure_count BETWEEN 0 AND 1",
            name="ck_workspace_coding_delivery_counts",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["task_loop_executions.execution_id"],
            name="fk_workspace_coding_delivery_execution",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_workspace_coding_delivery_task",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["task_execution_runs.run_id"],
            name="fk_workspace_coding_delivery_run",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "delivery_id",
            name="pk_workspace_coding_deliveries",
        ),
        sa.UniqueConstraint(
            "execution_id",
            name="uq_workspace_coding_delivery_execution",
        ),
        sa.UniqueConstraint(
            "delivery_digest",
            name="uq_workspace_coding_delivery_digest",
        ),
    )
    op.create_index(
        "ix_workspace_coding_deliveries_task",
        "workspace_coding_deliveries",
        ["task_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "workspace coding evidence deliveries",
                "SELECT 1 FROM workspace_coding_deliveries LIMIT 1",
            ),
        ),
    )
    op.drop_index(
        "ix_workspace_coding_deliveries_task",
        table_name="workspace_coding_deliveries",
    )
    op.drop_table("workspace_coding_deliveries")
