"""Add durable, server-adjudicated parent/child Agent delegations.

Revision ID: 0043_agent_delegations
Revises: 0042_workbench_runtime_items
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0043_agent_delegations"
down_revision: str | None = "0042_workbench_runtime_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("task_execution_nodes") as batch:
        batch.add_column(sa.Column("handoff_parent_node_id", sa.String(68), nullable=True))
        batch.create_foreign_key(
            "fk_execution_node_handoff_parent",
            "task_execution_nodes",
            ["handoff_parent_node_id"],
            ["node_id"],
            ondelete="RESTRICT",
        )
        batch.drop_constraint("ck_execution_node_status", type_="check")
        batch.create_check_constraint(
            "ck_execution_node_status",
            "status IN ('pending', 'ready', 'claimed', 'running', "
            "'awaiting_verification', 'verified', 'cancelled', 'failed', 'waiting_user', "
            "'waiting_children')",
        )
    with op.batch_alter_table("agent_invocations") as batch:
        batch.add_column(sa.Column("parent_invocation_id", sa.String(68), nullable=True))
        batch.create_foreign_key(
            "fk_agent_invocation_parent",
            "agent_invocations",
            ["parent_invocation_id"],
            ["invocation_id"],
            ondelete="RESTRICT",
        )
        batch.drop_constraint("ck_agent_invocation_execution_status", type_="check")
        batch.create_check_constraint(
            "ck_agent_invocation_execution_status",
            "execution_status IN ('created', 'running', 'result_submitted', "
            "'failed_retryable', 'failed_terminal', 'cancelled', 'expired', 'waiting_user', "
            "'waiting_children')",
        )
    op.create_index(
        "ix_agent_invocations_parent_invocation_id",
        "agent_invocations",
        ["parent_invocation_id"],
    )
    with op.batch_alter_table("agent_decisions") as batch:
        batch.drop_constraint("ck_agent_decision_kind", type_="check")
        batch.create_check_constraint(
            "ck_agent_decision_kind",
            "kind IN ('request_route', 'submit_result', 'needs_user_input', "
            "'propose_handoff')",
        )
    with op.batch_alter_table("agent_observations") as batch:
        batch.drop_constraint("ck_agent_observation_state", type_="check")
        batch.create_check_constraint(
            "ck_agent_observation_state",
            "source_kind IN ('route', 'handoff') AND status IN ('succeeded', 'failed')",
        )
    op.create_table(
        "agent_delegations",
        sa.Column("delegation_id", sa.String(68), primary_key=True),
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
        ),
        sa.Column(
            "child_invocation_id",
            sa.String(68),
            sa.ForeignKey("agent_invocations.invocation_id", ondelete="RESTRICT"),
            nullable=True,
            unique=True,
        ),
        sa.Column(
            "parent_node_id",
            sa.String(68),
            sa.ForeignKey("task_execution_nodes.node_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "child_node_id",
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
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("proposal_manifest", sa.JSON(), nullable=False),
        sa.Column("proposal_digest", sa.String(64), nullable=False),
        sa.Column("budget_allocation", sa.JSON(), nullable=False),
        sa.Column("child_result_id", sa.String(68), nullable=True),
        sa.Column("observation_id", sa.String(68), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("depth >= 1 AND depth <= 10", name="ck_agent_delegation_depth"),
        sa.CheckConstraint(
            "status IN ('waiting_child', 'child_verified', 'consumed', 'cancelled', 'failed')",
            name="ck_agent_delegation_status",
        ),
        sa.UniqueConstraint(
            "run_id", "child_node_id", name="uq_agent_delegation_child_node"
        ),
    )
    op.create_index(
        "ix_agent_delegations_parent",
        "agent_delegations",
        ["parent_invocation_id", "status"],
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "persisted agent delegation proof",
                "SELECT 1 FROM agent_delegations LIMIT 1",
            ),
            (
                "execution node handoff parent binding",
                "SELECT 1 FROM task_execution_nodes "
                "WHERE handoff_parent_node_id IS NOT NULL LIMIT 1",
            ),
            (
                "agent invocation parent binding",
                "SELECT 1 FROM agent_invocations "
                "WHERE parent_invocation_id IS NOT NULL LIMIT 1",
            ),
            (
                "agent observation source kind 'handoff'",
                "SELECT 1 FROM agent_observations "
                "WHERE source_kind = 'handoff' LIMIT 1",
            ),
            (
                "agent decision kind 'propose_handoff'",
                "SELECT 1 FROM agent_decisions "
                "WHERE kind = 'propose_handoff' LIMIT 1",
            ),
            (
                "agent invocation status 'waiting_children'",
                "SELECT 1 FROM agent_invocations "
                "WHERE execution_status = 'waiting_children' LIMIT 1",
            ),
            (
                "execution node status 'waiting_children'",
                "SELECT 1 FROM task_execution_nodes "
                "WHERE status = 'waiting_children' LIMIT 1",
            ),
        ),
    )
    op.drop_index("ix_agent_delegations_parent", table_name="agent_delegations")
    op.drop_table("agent_delegations")
    with op.batch_alter_table("agent_observations") as batch:
        batch.drop_constraint("ck_agent_observation_state", type_="check")
        batch.create_check_constraint(
            "ck_agent_observation_state",
            "source_kind IN ('route') AND status IN ('succeeded', 'failed')",
        )
    with op.batch_alter_table("agent_decisions") as batch:
        batch.drop_constraint("ck_agent_decision_kind", type_="check")
        batch.create_check_constraint(
            "ck_agent_decision_kind",
            "kind IN ('request_route', 'submit_result', 'needs_user_input')",
        )
    op.drop_index(
        "ix_agent_invocations_parent_invocation_id",
        table_name="agent_invocations",
    )
    with op.batch_alter_table("agent_invocations") as batch:
        batch.drop_constraint("fk_agent_invocation_parent", type_="foreignkey")
        batch.drop_constraint("ck_agent_invocation_execution_status", type_="check")
        batch.create_check_constraint(
            "ck_agent_invocation_execution_status",
            "execution_status IN ('created', 'running', 'result_submitted', "
            "'failed_retryable', 'failed_terminal', 'cancelled', 'expired', 'waiting_user')",
        )
        batch.drop_column("parent_invocation_id")
    with op.batch_alter_table("task_execution_nodes") as batch:
        batch.drop_constraint("fk_execution_node_handoff_parent", type_="foreignkey")
        batch.drop_constraint("ck_execution_node_status", type_="check")
        batch.create_check_constraint(
            "ck_execution_node_status",
            "status IN ('pending', 'ready', 'claimed', 'running', "
            "'awaiting_verification', 'verified', 'cancelled', 'failed', 'waiting_user')",
        )
        batch.drop_column("handoff_parent_node_id")
