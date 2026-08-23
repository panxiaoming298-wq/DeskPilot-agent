"""Add server-bound capability inputs to Agent task graph nodes.

Revision ID: 0046_agent_task_graph_capability_inputs
Revises: 0045_agent_task_graph_result_refs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0046_agent_task_graph_capability_inputs"
down_revision: str | None = "0045_agent_task_graph_result_refs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_task_graph_nodes") as batch:
        batch.add_column(sa.Column("input_manifest", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("input_digest", sa.String(64), nullable=True))


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "persisted Agent graph capability input binding",
                "SELECT 1 FROM agent_task_graph_nodes "
                "WHERE input_manifest IS NOT NULL "
                "OR input_digest IS NOT NULL LIMIT 1",
            ),
        ),
    )
    with op.batch_alter_table("agent_task_graph_nodes") as batch:
        batch.drop_column("input_digest")
        batch.drop_column("input_manifest")
