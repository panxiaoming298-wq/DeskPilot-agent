"""Add conditional DAG edges and content-addressed branch decisions.

Revision ID: 0018_branch_decision_proofs
Revises: 0017_parallel_compensation
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_branch_decision_proofs"
down_revision: str | None = "0017_parallel_compensation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tool_effect_edges") as batch:
        batch.drop_constraint("ck_tool_effect_edges_kind", type_="check")
        batch.add_column(sa.Column("decision_key", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("expected_outcome", sa.String(length=64), nullable=True))
        batch.create_check_constraint(
            "ck_tool_effect_edges_kind",
            "kind IN ('success', 'conditional', 'compensation_order')",
        )
        batch.create_check_constraint(
            "ck_tool_effect_edges_branch_metadata",
            "(kind = 'conditional' AND decision_key IS NOT NULL AND "
            "expected_outcome IS NOT NULL) OR "
            "(kind <> 'conditional' AND decision_key IS NULL AND "
            "expected_outcome IS NULL)",
        )

    op.create_table(
        "tool_effect_branch_decisions",
        sa.Column("decision_id", sa.String(length=68), nullable=False),
        sa.Column("graph_id", sa.String(length=68), nullable=False),
        sa.Column("source_node_id", sa.String(length=68), nullable=False),
        sa.Column("decision_key", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("evidence_digest", sa.String(length=64), nullable=False),
        sa.Column("source_node_revision", sa.Integer(), nullable=False),
        sa.Column("source_event_seq", sa.Integer(), nullable=False),
        sa.Column("proof_digest", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=40), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_node_revision >= 1 AND source_event_seq >= 1 AND event_seq >= 1",
            name="ck_tool_effect_branch_decisions_positive_versions",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["task_events.event_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["graph_id"], ["tool_effect_graphs.graph_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_node_id"], ["tool_effect_nodes.node_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("decision_id"),
        sa.UniqueConstraint("event_id"),
        sa.UniqueConstraint(
            "graph_id",
            "source_node_id",
            "decision_key",
            name="uq_tool_effect_branch_decisions_key",
        ),
    )
    op.create_index(
        "ix_tool_effect_branch_decisions_graph_event",
        "tool_effect_branch_decisions",
        ["graph_id", "event_seq"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_effect_branch_decisions_graph_event",
        table_name="tool_effect_branch_decisions",
    )
    op.drop_table("tool_effect_branch_decisions")
    with op.batch_alter_table("tool_effect_edges") as batch:
        batch.drop_constraint("ck_tool_effect_edges_branch_metadata", type_="check")
        batch.drop_constraint("ck_tool_effect_edges_kind", type_="check")
        batch.drop_column("expected_outcome")
        batch.drop_column("decision_key")
        batch.create_check_constraint(
            "ck_tool_effect_edges_kind",
            "kind IN ('success', 'compensation_order')",
        )
