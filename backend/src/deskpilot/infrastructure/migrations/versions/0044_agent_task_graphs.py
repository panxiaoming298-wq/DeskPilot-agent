"""Add server-bound dynamic Agent task graphs.

Revision ID: 0044_agent_task_graphs
Revises: 0043_agent_delegations
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0044_agent_task_graphs"
down_revision: str | None = "0043_agent_delegations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_decisions") as batch:
        batch.drop_constraint("ck_agent_decision_kind", type_="check")
        batch.create_check_constraint(
            "ck_agent_decision_kind",
            "kind IN ('request_route', 'submit_result', 'needs_user_input', "
            "'propose_handoff', 'propose_task_graph')",
        )
    op.create_table(
        "agent_task_graphs",
        sa.Column("graph_id", sa.String(68), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(68),
            sa.ForeignKey("task_execution_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_invocation_id",
            sa.String(68),
            sa.ForeignKey("agent_invocations.invocation_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "parent_node_id",
            sa.String(68),
            sa.ForeignKey("task_execution_nodes.node_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "decision_id",
            sa.String(68),
            sa.ForeignKey("agent_decisions.decision_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("binding_id", sa.String(68), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("graph_digest", sa.String(64), nullable=False),
        sa.Column("node_count", sa.Integer(), nullable=False),
        sa.Column("max_depth", sa.Integer(), nullable=False),
        sa.Column("observation_id", sa.String(68), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "node_count >= 1 AND node_count <= 8", name="ck_agent_task_graph_nodes"
        ),
        sa.CheckConstraint(
            "max_depth >= 1 AND max_depth <= 8", name="ck_agent_task_graph_depth"
        ),
        sa.CheckConstraint(
            "status IN ('running', 'verified', 'consumed', 'cancelled', 'failed')",
            name="ck_agent_task_graph_status",
        ),
    )
    op.create_index(
        "ix_agent_task_graphs_run", "agent_task_graphs", ["run_id", "status"]
    )
    op.create_table(
        "agent_task_graph_nodes",
        sa.Column(
            "graph_id",
            sa.String(68),
            sa.ForeignKey("agent_task_graphs.graph_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("local_key", sa.String(64), primary_key=True),
        sa.Column(
            "child_node_id",
            sa.String(68),
            sa.ForeignKey("task_execution_nodes.node_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "child_invocation_id",
            sa.String(68),
            sa.ForeignKey("agent_invocations.invocation_id", ondelete="RESTRICT"),
            nullable=True,
            unique=True,
        ),
        sa.Column("binding_id", sa.String(68), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("budget_allocation", sa.JSON(), nullable=False),
        sa.Column("child_result_id", sa.String(68), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('waiting_child', 'child_verified', 'consumed', 'cancelled', 'failed')",
            name="ck_agent_task_graph_node_status",
        ),
        sa.UniqueConstraint("child_node_id", name="uq_agent_task_graph_child_node"),
    )
    op.create_index(
        "ix_agent_task_graph_nodes_status",
        "agent_task_graph_nodes",
        ["graph_id", "status"],
    )
    op.create_table(
        "workspace_agent_results",
        sa.Column(
            "invocation_id",
            sa.String(68),
            sa.ForeignKey("agent_invocations.invocation_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "run_id",
            sa.String(68),
            sa.ForeignKey("task_execution_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("result_kind", sa.String(16), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("result_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "result_kind IN ('file', 'directory')",
            name="ck_workspace_agent_result_kind",
        ),
    )
    op.create_index(
        "ix_workspace_agent_results_run",
        "workspace_agent_results",
        ["run_id", "created_at"],
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "persisted workspace Agent result proof",
                "SELECT 1 FROM workspace_agent_results LIMIT 1",
            ),
            (
                "persisted Agent task graph node proof",
                "SELECT 1 FROM agent_task_graph_nodes LIMIT 1",
            ),
            (
                "persisted Agent task graph proof",
                "SELECT 1 FROM agent_task_graphs LIMIT 1",
            ),
            (
                "agent decision kind 'propose_task_graph'",
                "SELECT 1 FROM agent_decisions "
                "WHERE kind = 'propose_task_graph' LIMIT 1",
            ),
        ),
    )
    op.drop_index("ix_workspace_agent_results_run", table_name="workspace_agent_results")
    op.drop_table("workspace_agent_results")
    op.drop_index("ix_agent_task_graph_nodes_status", table_name="agent_task_graph_nodes")
    op.drop_table("agent_task_graph_nodes")
    op.drop_index("ix_agent_task_graphs_run", table_name="agent_task_graphs")
    op.drop_table("agent_task_graphs")
    with op.batch_alter_table("agent_decisions") as batch:
        batch.drop_constraint("ck_agent_decision_kind", type_="check")
        batch.create_check_constraint(
            "ck_agent_decision_kind",
            "kind IN ('request_route', 'submit_result', 'needs_user_input', "
            "'propose_handoff')",
        )
