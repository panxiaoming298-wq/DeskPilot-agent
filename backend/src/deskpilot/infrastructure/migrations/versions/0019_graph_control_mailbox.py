"""Add owner-targeted durable effect graph control mailbox.

Revision ID: 0019_graph_control_mailbox
Revises: 0018_branch_decision_proofs
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_graph_control_mailbox"
down_revision: str | None = "0018_branch_decision_proofs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_effect_graph_controls",
        sa.Column("control_id", sa.String(length=68), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("graph_id", sa.String(length=68), nullable=False),
        sa.Column("command", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("requested_by", sa.String(length=80), nullable=False),
        sa.Column("target_owner_id", sa.String(length=80), nullable=True),
        sa.Column("target_fencing_token", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.String(length=120), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_owner_id", sa.String(length=80), nullable=True),
        sa.Column("claim_acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "claim_fencing_token",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("applied_graph_fencing_token", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "command IN ('cancel')",
            name="ck_tool_effect_graph_controls_command",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'applied', 'superseded')",
            name="ck_tool_effect_graph_controls_status",
        ),
        sa.CheckConstraint(
            "revision >= 1 AND attempt_count >= 0 AND claim_fencing_token >= 0",
            name="ck_tool_effect_graph_controls_positive_versions",
        ),
        sa.CheckConstraint(
            "(target_owner_id IS NULL AND target_fencing_token IS NULL) OR "
            "(target_owner_id IS NOT NULL AND target_fencing_token >= 1)",
            name="ck_tool_effect_graph_controls_target_pair",
        ),
        sa.ForeignKeyConstraint(
            ["graph_id"],
            ["tool_effect_graphs.graph_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("control_id"),
        sa.UniqueConstraint(
            "graph_id",
            "command",
            name="uq_tool_effect_graph_controls_command",
        ),
    )
    op.create_index(
        "ix_tool_effect_graph_controls_route",
        "tool_effect_graph_controls",
        ["status", "target_owner_id", "available_at"],
    )
    op.create_index(
        "ix_tool_effect_graph_controls_claim_expiry",
        "tool_effect_graph_controls",
        ["claim_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_effect_graph_controls_claim_expiry",
        table_name="tool_effect_graph_controls",
    )
    op.drop_index(
        "ix_tool_effect_graph_controls_route",
        table_name="tool_effect_graph_controls",
    )
    op.drop_table("tool_effect_graph_controls")
