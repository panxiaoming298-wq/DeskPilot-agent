"""Add incremental database-side DAG ready projection.

Revision ID: 0021_incremental_ready
Revises: 0020_cluster_dag_admission
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_incremental_ready"
down_revision: str | None = "0020_cluster_dag_admission"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_effect_dag_ready_states",
        sa.Column("graph_id", sa.String(length=68), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("rebuilt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "revision >= 1 AND event_seq >= 1",
            name="ck_effect_dag_ready_states_versions",
        ),
        sa.ForeignKeyConstraint(
            ["graph_id"],
            ["tool_effect_graphs.graph_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("graph_id"),
    )
    op.create_index(
        "ix_effect_dag_ready_states_event",
        "tool_effect_dag_ready_states",
        ["graph_id", "event_seq"],
    )
    op.create_table(
        "tool_effect_dag_ready_nodes",
        sa.Column("node_id", sa.String(length=68), nullable=False),
        sa.Column("graph_id", sa.String(length=68), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("remaining_predecessors", sa.Integer(), nullable=False),
        sa.Column("unresolved_branches", sa.Integer(), nullable=False),
        sa.Column("branch_rejected", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("proof_digest", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0 AND remaining_predecessors >= 0 "
            "AND unresolved_branches >= 0 AND revision >= 1",
            name="ck_effect_dag_ready_nodes_counters",
        ),
        sa.ForeignKeyConstraint(
            ["graph_id"],
            ["tool_effect_graphs.graph_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["tool_effect_nodes.node_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("node_id"),
        sa.UniqueConstraint(
            "graph_id",
            "ordinal",
            name="uq_effect_dag_ready_nodes_ordinal",
        ),
    )
    op.create_index(
        "ix_effect_dag_ready_nodes_query",
        "tool_effect_dag_ready_nodes",
        [
            "graph_id",
            "branch_rejected",
            "remaining_predecessors",
            "unresolved_branches",
            "ordinal",
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_effect_dag_ready_nodes_query",
        table_name="tool_effect_dag_ready_nodes",
    )
    op.drop_table("tool_effect_dag_ready_nodes")
    op.drop_index(
        "ix_effect_dag_ready_states_event",
        table_name="tool_effect_dag_ready_states",
    )
    op.drop_table("tool_effect_dag_ready_states")
