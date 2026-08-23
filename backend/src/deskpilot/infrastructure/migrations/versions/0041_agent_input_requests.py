"""Add reusable Workspace Agent Loop input pause and resume proof.

Revision ID: 0041_agent_input_requests
Revises: 0040_durable_agent_model_loop
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from deskpilot.infrastructure.migrations.downgrade_guard import (
    refuse_downgrade_if_rows,
)

revision: str = "0041_agent_input_requests"
down_revision: str | None = "0040_durable_agent_model_loop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("turn_routes") as batch:
        batch.drop_constraint("ck_turn_route_status", type_="check")
        batch.create_check_constraint(
            "ck_turn_route_status",
            "status IN ('ready', 'running', 'needs_user_action', 'succeeded', "
            "'failed', 'not_applicable', 'waiting_user_input')",
        )
    with op.batch_alter_table("task_execution_nodes") as batch:
        batch.drop_constraint("ck_execution_node_status", type_="check")
        batch.create_check_constraint(
            "ck_execution_node_status",
            "status IN ('pending', 'ready', 'claimed', 'running', "
            "'awaiting_verification', 'verified', 'cancelled', 'failed', 'waiting_user')",
        )
    with op.batch_alter_table("agent_invocations") as batch:
        batch.drop_constraint("ck_agent_invocation_execution_status", type_="check")
        batch.create_check_constraint(
            "ck_agent_invocation_execution_status",
            "execution_status IN ('created', 'running', 'result_submitted', "
            "'failed_retryable', 'failed_terminal', 'cancelled', 'expired', 'waiting_user')",
        )
    with op.batch_alter_table("agent_decisions") as batch:
        batch.drop_constraint("ck_agent_decision_kind", type_="check")
        batch.create_check_constraint(
            "ck_agent_decision_kind",
            "kind IN ('request_route', 'submit_result', 'needs_user_input')",
        )
    op.create_table(
        "agent_input_requests",
        sa.Column("input_request_id", sa.String(68), primary_key=True),
        sa.Column(
            "invocation_id",
            sa.String(68),
            sa.ForeignKey("agent_invocations.invocation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "decision_id",
            sa.String(68),
            sa.ForeignKey("agent_decisions.decision_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("question_code", sa.String(100), nullable=False),
        sa.Column("question", sa.String(300), nullable=False),
        sa.Column("blocking_fields", sa.JSON(), nullable=False),
        sa.Column("answer_schema", sa.String(100), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "resolved_task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("answer_digest", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'resolved', 'cancelled')",
            name="ck_agent_input_request_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND resolved_task_id IS NULL AND answer_digest IS NULL "
            "AND resolved_at IS NULL) OR "
            "(status <> 'pending' AND resolved_at IS NOT NULL)",
            name="ck_agent_input_request_resolution",
        ),
    )
    op.create_index(
        "ix_agent_input_requests_invocation_id",
        "agent_input_requests",
        ["invocation_id"],
    )


def downgrade() -> None:
    refuse_downgrade_if_rows(
        op.get_bind(),
        revision=revision,
        checks=(
            (
                "persisted agent input request proof",
                "SELECT 1 FROM agent_input_requests LIMIT 1",
            ),
            (
                "agent decision kind 'needs_user_input'",
                "SELECT 1 FROM agent_decisions "
                "WHERE kind = 'needs_user_input' LIMIT 1",
            ),
            (
                "agent invocation status 'waiting_user'",
                "SELECT 1 FROM agent_invocations "
                "WHERE execution_status = 'waiting_user' LIMIT 1",
            ),
            (
                "execution node status 'waiting_user'",
                "SELECT 1 FROM task_execution_nodes "
                "WHERE status = 'waiting_user' LIMIT 1",
            ),
            (
                "turn route status 'waiting_user_input'",
                "SELECT 1 FROM turn_routes "
                "WHERE status = 'waiting_user_input' LIMIT 1",
            ),
        ),
    )
    op.drop_index("ix_agent_input_requests_invocation_id", table_name="agent_input_requests")
    op.drop_table("agent_input_requests")
    with op.batch_alter_table("agent_decisions") as batch:
        batch.drop_constraint("ck_agent_decision_kind", type_="check")
        batch.create_check_constraint(
            "ck_agent_decision_kind",
            "kind IN ('request_route', 'submit_result')",
        )
    with op.batch_alter_table("agent_invocations") as batch:
        batch.drop_constraint("ck_agent_invocation_execution_status", type_="check")
        batch.create_check_constraint(
            "ck_agent_invocation_execution_status",
            "execution_status IN ('created', 'running', 'result_submitted', "
            "'failed_retryable', 'failed_terminal', 'cancelled', 'expired')",
        )
    with op.batch_alter_table("task_execution_nodes") as batch:
        batch.drop_constraint("ck_execution_node_status", type_="check")
        batch.create_check_constraint(
            "ck_execution_node_status",
            "status IN ('pending', 'ready', 'claimed', 'running', "
            "'awaiting_verification', 'verified', 'cancelled', 'failed')",
        )
    with op.batch_alter_table("turn_routes") as batch:
        batch.drop_constraint("ck_turn_route_status", type_="check")
        batch.create_check_constraint(
            "ck_turn_route_status",
            "status IN ('ready', 'running', 'needs_user_action', 'succeeded', "
            "'failed', 'not_applicable')",
        )
