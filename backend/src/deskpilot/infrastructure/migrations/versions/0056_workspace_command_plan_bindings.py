"""persist exact workspace command Plan bindings

Revision ID: 0056_workspace_command_plan_bindings
Revises: 0055_planner_only_single_task_loop
Create Date: 2026-08-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0056_workspace_command_plan_bindings"
down_revision: str | None = "0055_planner_only_single_task_loop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_command_plan_bindings",
        sa.Column("binding_id", sa.String(length=68), nullable=False),
        sa.Column("draft_id", sa.String(length=68), nullable=False),
        sa.Column("loop_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("group_ordinal", sa.Integer(), nullable=False),
        sa.Column("expected_plan_id", sa.String(length=68), nullable=False),
        sa.Column(
            "expected_plan_manifest_digest",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("command_plan_id", sa.String(length=68), nullable=False),
        sa.Column("plan_generation", sa.Integer(), nullable=False),
        sa.Column("project_path", sa.String(length=32_767), nullable=False),
        sa.Column("ecosystem", sa.String(length=16), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("catalog_digest", sa.String(length=64), nullable=False),
        sa.Column("step_count", sa.Integer(), nullable=False),
        sa.Column("command_plan_manifest", sa.JSON(), nullable=False),
        sa.Column("command_plan_digest", sa.String(length=64), nullable=False),
        sa.Column("mappings_manifest", sa.JSON(), nullable=False),
        sa.Column("mappings_digest", sa.String(length=64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("binding_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "group_ordinal BETWEEN 1 AND 8 AND step_count BETWEEN 1 AND 6 "
            "AND plan_generation = 1",
            name="ck_workspace_command_plan_binding_bounds",
        ),
        sa.CheckConstraint(
            "ecosystem IN ('python', 'node')",
            name="ck_workspace_command_plan_binding_ecosystem",
        ),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["model_planner_drafts.draft_id"],
            name="fk_workspace_command_plan_binding_draft",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["loop_id"],
            ["task_loops.loop_id"],
            name="fk_workspace_command_plan_binding_loop",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            name="fk_workspace_command_plan_binding_task",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "binding_id",
            name="pk_workspace_command_plan_bindings",
        ),
        sa.UniqueConstraint(
            "draft_id",
            "group_ordinal",
            name="uq_workspace_command_plan_binding_group",
        ),
        sa.UniqueConstraint(
            "binding_digest",
            name="uq_workspace_command_plan_binding_digest",
        ),
    )
    op.create_index(
        "ix_workspace_command_plan_bindings_draft",
        "workspace_command_plan_bindings",
        ["draft_id", "group_ordinal"],
    )
    op.create_index(
        "ix_workspace_command_plan_bindings_task",
        "workspace_command_plan_bindings",
        ["task_id", "created_at"],
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "workspace command Plan bindings",
                "SELECT 1 FROM workspace_command_plan_bindings LIMIT 1",
            ),
        ),
    )
    op.drop_index(
        "ix_workspace_command_plan_bindings_task",
        table_name="workspace_command_plan_bindings",
    )
    op.drop_index(
        "ix_workspace_command_plan_bindings_draft",
        table_name="workspace_command_plan_bindings",
    )
    op.drop_table("workspace_command_plan_bindings")
