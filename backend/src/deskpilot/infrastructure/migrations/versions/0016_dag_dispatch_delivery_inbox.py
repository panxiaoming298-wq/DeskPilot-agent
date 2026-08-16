"""Add DAG reducer state, node heartbeats, delivery envelopes, Inbox, and DLQ.

Revision ID: 0016_dag_dispatch_delivery
Revises: 0015_database_claims_dag
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_dag_dispatch_delivery"
down_revision: str | None = "0015_database_claims_dag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tool_effect_graphs") as batch:
        batch.drop_constraint("ck_tool_effect_graphs_status", type_="check")
        batch.create_check_constraint(
            "ck_tool_effect_graphs_status",
            "status IN ('active', 'compensating', 'succeeded', 'compensated', "
            "'failed', 'cancelled', 'blocked_unknown', 'blocked_non_compensable')",
        )
        batch.add_column(
            sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
        )

    with op.batch_alter_table("tool_effect_nodes") as batch:
        batch.drop_constraint("ck_tool_effect_nodes_status", type_="check")
        batch.create_check_constraint(
            "ck_tool_effect_nodes_status",
            "status IN ('pending', 'active', 'waiting_approval', 'running', "
            "'succeeded', 'failed', 'unknown', 'compensating', 'compensated', "
            "'compensation_failed', 'compensation_unknown', 'skipped', 'cancelled')",
        )
        batch.add_column(sa.Column("claim_heartbeat_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("outbox_messages") as batch:
        batch.add_column(sa.Column("delivery_id", sa.String(length=40), nullable=True))
        batch.add_column(
            sa.Column("delivery_attempted_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("dead_letter_reason", sa.Text(), nullable=True))
        batch.create_unique_constraint("uq_outbox_delivery_id", ["delivery_id"])
        batch.create_index("ix_outbox_dead_lettered_at", ["dead_lettered_at"])

    op.create_table(
        "inbox_deliveries",
        sa.Column("inbox_id", sa.String(length=40), nullable=False),
        sa.Column("consumer_name", sa.String(length=96), nullable=False),
        sa.Column("message_id", sa.String(length=40), nullable=False),
        sa.Column("delivery_id", sa.String(length=40), nullable=False),
        sa.Column("topic", sa.String(length=80), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("inbox_id"),
        sa.UniqueConstraint("consumer_name", "message_id", name="uq_inbox_consumer_message"),
        sa.UniqueConstraint("consumer_name", "delivery_id", name="uq_inbox_consumer_delivery"),
    )
    op.create_index("ix_inbox_processed_at", "inbox_deliveries", ["processed_at"])

    op.create_table(
        "tool_effect_compensation_plans",
        sa.Column("plan_id", sa.String(length=68), nullable=False),
        sa.Column("graph_id", sa.String(length=68), nullable=False),
        sa.Column("graph_revision", sa.Integer(), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("waves", sa.JSON(), nullable=False),
        sa.Column("proof_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["graph_id"], ["tool_effect_graphs.graph_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("plan_id"),
        sa.UniqueConstraint(
            "graph_id",
            "graph_revision",
            "proof_digest",
            name="uq_tool_effect_compensation_plan_proof",
        ),
    )
    op.create_index(
        "ix_tool_effect_compensation_plan_latest",
        "tool_effect_compensation_plans",
        ["graph_id", "graph_revision", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_effect_compensation_plan_latest",
        table_name="tool_effect_compensation_plans",
    )
    op.drop_table("tool_effect_compensation_plans")
    op.drop_index("ix_inbox_processed_at", table_name="inbox_deliveries")
    op.drop_table("inbox_deliveries")

    with op.batch_alter_table("outbox_messages") as batch:
        batch.drop_index("ix_outbox_dead_lettered_at")
        batch.drop_constraint("uq_outbox_delivery_id", type_="unique")
        batch.drop_column("dead_letter_reason")
        batch.drop_column("dead_lettered_at")
        batch.drop_column("delivery_attempted_at")
        batch.drop_column("delivery_id")

    with op.batch_alter_table("tool_effect_nodes") as batch:
        batch.drop_column("claim_heartbeat_at")
        batch.drop_constraint("ck_tool_effect_nodes_status", type_="check")
        batch.create_check_constraint(
            "ck_tool_effect_nodes_status",
            "status IN ('pending', 'active', 'waiting_approval', 'running', "
            "'succeeded', 'failed', 'unknown', 'compensating', 'compensated', "
            "'compensation_failed', 'compensation_unknown')",
        )

    with op.batch_alter_table("tool_effect_graphs") as batch:
        batch.drop_column("cancel_requested_at")
        batch.drop_constraint("ck_tool_effect_graphs_status", type_="check")
        batch.create_check_constraint(
            "ck_tool_effect_graphs_status",
            "status IN ('active', 'compensating', 'succeeded', 'compensated', "
            "'failed', 'blocked_unknown', 'blocked_non_compensable')",
        )
