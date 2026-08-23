"""Add typed Agent task graph result references and output binding.

Revision ID: 0045_agent_task_graph_result_refs
Revises: 0044_agent_task_graphs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0045_agent_task_graph_result_refs"
down_revision: str | None = "0044_agent_task_graphs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_task_graphs") as batch:
        batch.add_column(sa.Column("output_local_key", sa.String(64), nullable=True))
        batch.add_column(sa.Column("output_node_id", sa.String(68), nullable=True))
    with op.batch_alter_table("agent_task_graph_nodes") as batch:
        batch.add_column(sa.Column("result_ref_manifest", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("result_ref_digest", sa.String(64), nullable=True))


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "persisted Agent task graph result reference",
                "SELECT 1 FROM agent_task_graph_nodes "
                "WHERE result_ref_manifest IS NOT NULL "
                "OR result_ref_digest IS NOT NULL LIMIT 1",
            ),
            (
                "persisted Agent task graph output binding",
                "SELECT 1 FROM agent_task_graphs "
                "WHERE output_local_key IS NOT NULL "
                "OR output_node_id IS NOT NULL LIMIT 1",
            ),
        ),
    )
    with op.batch_alter_table("agent_task_graph_nodes") as batch:
        batch.drop_column("result_ref_digest")
        batch.drop_column("result_ref_manifest")
    with op.batch_alter_table("agent_task_graphs") as batch:
        batch.drop_column("output_node_id")
        batch.drop_column("output_local_key")
