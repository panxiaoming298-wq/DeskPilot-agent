"""Add immutable bounded Agent replan generation lineage.

Revision ID: 0047_agent_replans
Revises: 0046_agent_task_graph_capability_inputs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0047_agent_replans"
down_revision: str | None = "0046_agent_task_graph_capability_inputs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_replans",
        sa.Column("replan_id", sa.String(68), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_run_id",
            sa.String(68),
            sa.ForeignKey("task_execution_runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_plan_generation", sa.Integer(), nullable=False),
        sa.Column("source_plan_digest", sa.String(64), nullable=False),
        sa.Column(
            "target_run_id",
            sa.String(68),
            sa.ForeignKey("task_execution_runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("target_plan_generation", sa.Integer(), nullable=False),
        sa.Column("target_plan_digest", sa.String(64), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("contract_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("replan_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_plan_generation >= 1 AND target_plan_generation = source_plan_generation + 1",
            name="ck_agent_replan_generation",
        ),
        sa.CheckConstraint("status IN ('activated')", name="ck_agent_replan_status"),
        sa.UniqueConstraint("source_run_id", name="uq_agent_replan_source_run"),
        sa.UniqueConstraint("target_run_id", name="uq_agent_replan_target_run"),
        sa.UniqueConstraint(
            "task_id", "target_plan_generation", name="uq_agent_replan_target_generation"
        ),
    )
    op.create_index(
        "ix_agent_replans_task",
        "agent_replans",
        ["task_id", "target_plan_generation"],
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(("persisted Agent replan lineage", "SELECT 1 FROM agent_replans LIMIT 1"),),
    )
    op.drop_index("ix_agent_replans_task", table_name="agent_replans")
    op.drop_table("agent_replans")
