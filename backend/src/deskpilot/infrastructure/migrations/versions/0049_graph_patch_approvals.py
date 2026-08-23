"""Persist dynamic graph Patch approval proofs and typed results.

Revision ID: 0049_agent_graph_patch_approvals
Revises: 0048_agent_test_capability_inputs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0049_agent_graph_patch_approvals"
down_revision: str | None = "0048_agent_test_capability_inputs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_task_graph_nodes") as batch:
        batch.add_column(sa.Column("approval_manifest", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("approval_digest", sa.String(length=64), nullable=True))
    with op.batch_alter_table("workspace_agent_results") as batch:
        batch.drop_constraint("ck_workspace_agent_result_kind", type_="check")
        batch.create_check_constraint(
            "ck_workspace_agent_result_kind",
            "result_kind IN ('file', 'directory', 'python_test', 'node_test', 'patch_test')",
        )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "workspace Patch test result proof",
                "SELECT 1 FROM workspace_agent_results "
                "WHERE result_kind = 'patch_test' LIMIT 1",
            ),
            (
                "dynamic graph Patch approval proof",
                "SELECT 1 FROM agent_task_graph_nodes "
                "WHERE approval_manifest IS NOT NULL "
                "OR approval_digest IS NOT NULL LIMIT 1",
            ),
        ),
    )
    with op.batch_alter_table("workspace_agent_results") as batch:
        batch.drop_constraint("ck_workspace_agent_result_kind", type_="check")
        batch.create_check_constraint(
            "ck_workspace_agent_result_kind",
            "result_kind IN ('file', 'directory', 'python_test', 'node_test')",
        )
    with op.batch_alter_table("agent_task_graph_nodes") as batch:
        batch.drop_column("approval_digest")
        batch.drop_column("approval_manifest")
