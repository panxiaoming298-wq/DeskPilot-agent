"""Add database-time claims, Outbox fencing, and DAG ready-set proofs.

Revision ID: 0015_database_claims_dag
Revises: 0014_graph_lease_recovery
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_database_claims_dag"
down_revision: str | None = "0014_graph_lease_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table_name, owner_length, prefix in (
        ("outbox_messages", 96, "outbox"),
        ("tool_effect_nodes", 96, "effect_nodes"),
    ):
        op.add_column(
            table_name,
            sa.Column("claim_owner_id", sa.String(length=owner_length), nullable=True),
        )
        op.add_column(
            table_name,
            sa.Column("claim_acquired_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column(
            table_name,
            sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column(
            table_name,
            sa.Column(
                "claim_fencing_token",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        op.create_index(
            f"ix_{prefix}_claim_expires_at",
            table_name,
            ["claim_expires_at"],
        )

    op.create_index(
        "ix_outbox_claimable",
        "outbox_messages",
        ["published_at", "available_at", "claim_expires_at", "created_at"],
    )
    op.create_table(
        "tool_effect_ready_set_checkpoints",
        sa.Column("checkpoint_id", sa.String(length=68), nullable=False),
        sa.Column("graph_id", sa.String(length=68), nullable=False),
        sa.Column("graph_revision", sa.Integer(), nullable=False),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("ready_node_ids", sa.JSON(), nullable=False),
        sa.Column("predecessor_proof", sa.JSON(), nullable=False),
        sa.Column("proof_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["graph_id"],
            ["tool_effect_graphs.graph_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("checkpoint_id"),
        sa.UniqueConstraint(
            "graph_id",
            "graph_revision",
            "proof_digest",
            name="uq_tool_effect_ready_checkpoint_proof",
        ),
    )
    op.create_index(
        "ix_tool_effect_ready_checkpoint_latest",
        "tool_effect_ready_set_checkpoints",
        ["graph_id", "graph_revision", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_effect_ready_checkpoint_latest",
        table_name="tool_effect_ready_set_checkpoints",
    )
    op.drop_table("tool_effect_ready_set_checkpoints")
    op.drop_index("ix_outbox_claimable", table_name="outbox_messages")
    for table_name, prefix in (
        ("tool_effect_nodes", "effect_nodes"),
        ("outbox_messages", "outbox"),
    ):
        op.drop_index(f"ix_{prefix}_claim_expires_at", table_name=table_name)
        op.drop_column(table_name, "claim_fencing_token")
        op.drop_column(table_name, "claim_expires_at")
        op.drop_column(table_name, "claim_acquired_at")
        op.drop_column(table_name, "claim_owner_id")
