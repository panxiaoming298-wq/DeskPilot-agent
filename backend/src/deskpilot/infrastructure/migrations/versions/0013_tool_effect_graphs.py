"""Add versioned Tool effect graphs and atomic transition journal.

Revision ID: 0013_tool_effect_graphs
Revises: 0012_task_runtime_checkpoints
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_tool_effect_graphs"
down_revision: str | None = "0012_task_runtime_checkpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_effect_graphs",
        sa.Column("graph_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("execution_mode", sa.String(length=16), nullable=False),
        sa.Column("current_node_id", sa.String(length=68), nullable=True),
        sa.Column("failure_node_id", sa.String(length=68), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("last_event_seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'compensating', 'succeeded', 'compensated', "
            "'failed', 'blocked_unknown', 'blocked_non_compensable')",
            name="ck_tool_effect_graphs_status",
        ),
        sa.CheckConstraint(
            "execution_mode IN ('forward', 'compensating')",
            name="ck_tool_effect_graphs_execution_mode",
        ),
        sa.CheckConstraint(
            "revision >= 1 AND last_event_seq >= 1",
            name="ck_tool_effect_graphs_positive_versions",
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("graph_id"),
        sa.UniqueConstraint("task_id", name="uq_tool_effect_graphs_task_id"),
    )
    op.create_index(
        "ix_tool_effect_graphs_status",
        "tool_effect_graphs",
        ["status", "updated_at"],
    )
    op.create_table(
        "tool_effect_nodes",
        sa.Column("node_id", sa.String(length=68), nullable=False),
        sa.Column("graph_id", sa.String(length=68), nullable=False),
        sa.Column("node_key", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("step_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("tool_version", sa.String(length=32), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("compensation_strategy", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("last_event_seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'waiting_approval', 'running', "
            "'succeeded', 'failed', 'unknown', 'compensating', 'compensated', "
            "'compensation_failed', 'compensation_unknown')",
            name="ck_tool_effect_nodes_status",
        ),
        sa.CheckConstraint(
            "compensation_strategy IN ('none', 'receipt_bound_reverse')",
            name="ck_tool_effect_nodes_compensation_strategy",
        ),
        sa.CheckConstraint(
            "ordinal >= 0 AND revision >= 1 AND last_event_seq >= 1",
            name="ck_tool_effect_nodes_positive_versions",
        ),
        sa.ForeignKeyConstraint(["graph_id"], ["tool_effect_graphs.graph_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("node_id"),
        sa.UniqueConstraint("graph_id", "node_key", name="uq_tool_effect_nodes_key"),
        sa.UniqueConstraint("graph_id", "ordinal", name="uq_tool_effect_nodes_ordinal"),
    )
    op.create_index(
        "ix_tool_effect_nodes_graph_status",
        "tool_effect_nodes",
        ["graph_id", "status"],
    )
    op.create_table(
        "tool_effect_edges",
        sa.Column("edge_id", sa.String(length=68), nullable=False),
        sa.Column("graph_id", sa.String(length=68), nullable=False),
        sa.Column("from_node_id", sa.String(length=68), nullable=False),
        sa.Column("to_node_id", sa.String(length=68), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "kind IN ('success', 'compensation_order')",
            name="ck_tool_effect_edges_kind",
        ),
        sa.ForeignKeyConstraint(["graph_id"], ["tool_effect_graphs.graph_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["from_node_id"], ["tool_effect_nodes.node_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["to_node_id"], ["tool_effect_nodes.node_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("edge_id"),
        sa.UniqueConstraint(
            "graph_id",
            "from_node_id",
            "to_node_id",
            "kind",
            name="uq_tool_effect_edges_identity",
        ),
    )
    op.create_table(
        "tool_effect_attempts",
        sa.Column("attempt_id", sa.String(length=68), nullable=False),
        sa.Column("node_id", sa.String(length=68), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("call_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("effect_id", sa.String(length=68), nullable=True),
        sa.Column("last_event_seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('forward', 'compensation')",
            name="ck_tool_effect_attempts_kind",
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'running', 'succeeded', 'failed', 'cancelled', 'unknown')",
            name="ck_tool_effect_attempts_status",
        ),
        sa.CheckConstraint(
            "attempt >= 1 AND last_event_seq >= 1",
            name="ck_tool_effect_attempts_positive_versions",
        ),
        sa.ForeignKeyConstraint(["call_id"], ["tool_calls.call_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["tool_effect_nodes.node_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint("call_id", name="uq_tool_effect_attempts_call_id"),
        sa.UniqueConstraint(
            "node_id",
            "kind",
            "attempt",
            name="uq_tool_effect_attempts_node_kind_attempt",
        ),
    )
    op.create_index(
        "ix_tool_effect_attempts_node_status",
        "tool_effect_attempts",
        ["node_id", "status"],
    )
    op.create_table(
        "tool_effects",
        sa.Column("effect_id", sa.String(length=68), nullable=False),
        sa.Column("node_id", sa.String(length=68), nullable=False),
        sa.Column("attempt_id", sa.String(length=68), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("receipt_id", sa.String(length=68), nullable=True),
        sa.Column("compensates_effect_id", sa.String(length=68), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('forward', 'compensation')",
            name="ck_tool_effects_kind",
        ),
        sa.CheckConstraint(
            "state IN ('applied', 'compensated', 'compensation_applied')",
            name="ck_tool_effects_state",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["tool_effect_attempts.attempt_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["compensates_effect_id"], ["tool_effects.effect_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["node_id"], ["tool_effect_nodes.node_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["receipt_id"], ["tool_commit_receipts.receipt_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("effect_id"),
        sa.UniqueConstraint("attempt_id", name="uq_tool_effects_attempt_id"),
    )
    op.create_index("ix_tool_effects_node_state", "tool_effects", ["node_id", "state"])
    op.create_table(
        "tool_effect_transitions",
        sa.Column("transition_id", sa.String(length=68), nullable=False),
        sa.Column("graph_id", sa.String(length=68), nullable=False),
        sa.Column("node_id", sa.String(length=68), nullable=False),
        sa.Column("attempt_id", sa.String(length=68), nullable=True),
        sa.Column("event_id", sa.String(length=40), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("transition_kind", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=False),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("graph_from_status", sa.String(length=32), nullable=False),
        sa.Column("graph_to_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["tool_effect_attempts.attempt_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["event_id"], ["task_events.event_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["graph_id"], ["tool_effect_graphs.graph_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["node_id"], ["tool_effect_nodes.node_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("transition_id"),
        sa.UniqueConstraint("event_id", name="uq_tool_effect_transitions_event_id"),
        sa.UniqueConstraint("graph_id", "event_seq", name="uq_tool_effect_transitions_graph_seq"),
    )
    op.create_index(
        "ix_tool_effect_transitions_graph_seq",
        "tool_effect_transitions",
        ["graph_id", "event_seq"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_effect_transitions_graph_seq", table_name="tool_effect_transitions")
    op.drop_table("tool_effect_transitions")
    op.drop_index("ix_tool_effects_node_state", table_name="tool_effects")
    op.drop_table("tool_effects")
    op.drop_index("ix_tool_effect_attempts_node_status", table_name="tool_effect_attempts")
    op.drop_table("tool_effect_attempts")
    op.drop_table("tool_effect_edges")
    op.drop_index("ix_tool_effect_nodes_graph_status", table_name="tool_effect_nodes")
    op.drop_table("tool_effect_nodes")
    op.drop_index("ix_tool_effect_graphs_status", table_name="tool_effect_graphs")
    op.drop_table("tool_effect_graphs")
