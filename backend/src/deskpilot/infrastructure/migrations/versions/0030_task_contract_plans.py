"""Add immutable Task Contract and Executable Plan generations.

Revision ID: 0030_task_contract_plans
Revises: 0029_evaluation_traces
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_task_contract_plans"
down_revision: str | None = "0029_evaluation_traces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_planning_states",
        sa.Column(
            "task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("active_contract_version", sa.Integer(), nullable=False),
        sa.Column("active_contract_digest", sa.String(64), nullable=False),
        sa.Column("active_plan_generation", sa.Integer(), nullable=False),
        sa.Column("active_plan_digest", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "active_contract_version >= 1 AND active_plan_generation >= 1 AND revision >= 1",
            name="ck_task_planning_state_versions",
        ),
    )
    op.create_table(
        "task_contract_versions",
        sa.Column(
            "task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("contract_id", sa.String(40), nullable=False),
        sa.Column("previous_contract_digest", sa.String(64), nullable=True),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("contract_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_task_contract_version"),
        sa.UniqueConstraint("contract_id", "version", name="uq_task_contract_identity"),
    )
    op.create_index(
        "ix_task_contract_versions_task", "task_contract_versions", ["task_id", "version"]
    )
    op.create_table(
        "task_plan_generations",
        sa.Column(
            "task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("generation", sa.Integer(), primary_key=True),
        sa.Column("plan_id", sa.String(68), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("contract_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("plan_manifest_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_task_plan_generation"),
        sa.CheckConstraint(
            "status IN ('active', 'superseded')", name="ck_task_plan_status"
        ),
        sa.UniqueConstraint("plan_id", name="uq_task_plan_id"),
    )
    op.create_index(
        "ix_task_plan_generations_task", "task_plan_generations", ["task_id", "generation"]
    )


def downgrade() -> None:
    op.drop_index("ix_task_plan_generations_task", table_name="task_plan_generations")
    op.drop_table("task_plan_generations")
    op.drop_index("ix_task_contract_versions_task", table_name="task_contract_versions")
    op.drop_table("task_contract_versions")
    op.drop_table("task_planning_states")
