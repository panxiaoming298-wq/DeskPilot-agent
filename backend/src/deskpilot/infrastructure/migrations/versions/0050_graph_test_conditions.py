"""Persist server-adjudicated dynamic graph test-result conditions.

Revision ID: 0050_agent_graph_test_conditions
Revises: 0049_agent_graph_patch_approvals
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0050_agent_graph_test_conditions"
down_revision: str | None = "0049_agent_graph_patch_approvals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_execution_edges") as batch:
        batch.drop_constraint("ck_execution_edge_requirement", type_="check")
        batch.add_column(sa.Column("condition_manifest", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("condition_digest", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("decision_manifest", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("decision_digest", sa.String(length=64), nullable=True))
        batch.create_check_constraint(
            "ck_execution_edge_requirement",
            "requirement IN ('verified', 'server_condition')",
        )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "server-adjudicated condition edge",
                "SELECT 1 FROM task_execution_edges "
                "WHERE requirement = 'server_condition' LIMIT 1",
            ),
            (
                "server-adjudicated condition or decision proof",
                "SELECT 1 FROM task_execution_edges "
                "WHERE condition_manifest IS NOT NULL "
                "OR condition_digest IS NOT NULL "
                "OR decision_manifest IS NOT NULL "
                "OR decision_digest IS NOT NULL LIMIT 1",
            ),
        ),
    )
    with op.batch_alter_table("task_execution_edges") as batch:
        batch.drop_constraint("ck_execution_edge_requirement", type_="check")
        batch.drop_column("decision_digest")
        batch.drop_column("decision_manifest")
        batch.drop_column("condition_digest")
        batch.drop_column("condition_manifest")
        batch.create_check_constraint(
            "ck_execution_edge_requirement",
            "requirement IN ('verified')",
        )
