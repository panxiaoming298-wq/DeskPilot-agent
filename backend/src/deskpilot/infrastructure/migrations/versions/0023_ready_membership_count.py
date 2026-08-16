"""Project exact ready membership counts.

Revision ID: 0023_ready_membership
Revises: 0022_effect_runtime_ops
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_ready_membership"
down_revision: str | None = "0022_effect_runtime_ops"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tool_effect_dag_ready_states") as batch:
        batch.add_column(
            sa.Column(
                "membership_version",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "projected_node_count",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column(
                "ready_node_count",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
    with op.batch_alter_table("tool_effect_dag_ready_nodes") as batch:
        batch.add_column(
            sa.Column(
                "membership_ready",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )

    op.execute(
        sa.text(
            "UPDATE tool_effect_dag_ready_nodes AS ready "
            "SET membership_ready = CASE WHEN "
            "ready.branch_rejected IS FALSE "
            "AND ready.remaining_predecessors = 0 "
            "AND ready.unresolved_branches = 0 "
            "AND EXISTS ("
            "SELECT 1 FROM tool_effect_nodes AS node "
            "WHERE node.node_id = ready.node_id "
            "AND node.status IN ('pending', 'active') "
            "AND (node.claim_owner_id IS NULL "
            "OR node.claim_expires_at IS NULL "
            "OR node.claim_expires_at <= CURRENT_TIMESTAMP)"
            ") THEN TRUE ELSE FALSE END"
        )
    )
    op.execute(
        sa.text(
            "UPDATE tool_effect_dag_ready_states AS state "
            "SET projected_node_count = ("
            "SELECT COUNT(*) FROM tool_effect_dag_ready_nodes AS ready "
            "WHERE ready.graph_id = state.graph_id"
            "), ready_node_count = ("
            "SELECT COUNT(*) FROM tool_effect_dag_ready_nodes AS ready "
            "WHERE ready.graph_id = state.graph_id AND ready.membership_ready IS TRUE"
            ")"
        )
    )

    with op.batch_alter_table("tool_effect_dag_ready_states") as batch:
        batch.drop_constraint(
            "ck_effect_dag_ready_states_versions",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_effect_dag_ready_states_versions",
            "revision >= 1 AND event_seq >= 1 "
            "AND membership_version IN (0, 1) "
            "AND projected_node_count >= 0 AND ready_node_count >= 0 "
            "AND ready_node_count <= projected_node_count",
        )
    op.create_index(
        "ix_effect_dag_ready_nodes_membership",
        "tool_effect_dag_ready_nodes",
        ["graph_id", "membership_ready", "ordinal"],
    )
    op.create_index(
        "ix_effect_nodes_graph_claim_expires",
        "tool_effect_nodes",
        ["graph_id", "claim_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_effect_nodes_graph_claim_expires",
        table_name="tool_effect_nodes",
    )
    op.drop_index(
        "ix_effect_dag_ready_nodes_membership",
        table_name="tool_effect_dag_ready_nodes",
    )
    with op.batch_alter_table("tool_effect_dag_ready_states") as batch:
        batch.drop_constraint(
            "ck_effect_dag_ready_states_versions",
            type_="check",
        )
        batch.create_check_constraint(
            "ck_effect_dag_ready_states_versions",
            "revision >= 1 AND event_seq >= 1",
        )
    with op.batch_alter_table("tool_effect_dag_ready_nodes") as batch:
        batch.drop_column("membership_ready")
    with op.batch_alter_table("tool_effect_dag_ready_states") as batch:
        batch.drop_column("ready_node_count")
        batch.drop_column("projected_node_count")
        batch.drop_column("membership_version")
