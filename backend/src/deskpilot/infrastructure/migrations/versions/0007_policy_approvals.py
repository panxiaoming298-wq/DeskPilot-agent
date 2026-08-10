"""Add durable, exact-binding tool approvals.

Revision ID: 0007_policy_approvals
Revises: 0006_tool_call_persistence
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_policy_approvals"
down_revision: str | None = "0006_tool_call_persistence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tool_calls") as batch_op:
        batch_op.add_column(sa.Column("policy_decision_id", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("policy_revision", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("policy_effect", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("resource_scope_digest", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("policy_event_id", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("authorization_id", sa.String(length=80), nullable=True))
        batch_op.create_check_constraint(
            "ck_tool_calls_policy_effect",
            "policy_effect IS NULL OR policy_effect IN ('allow', 'deny', 'require_approval')",
        )
        batch_op.create_foreign_key(
            "fk_tool_calls_policy_event_id_task_events",
            "task_events",
            ["policy_event_id"],
            ["event_id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(length=40), nullable=False),
        sa.Column("decision_id", sa.String(length=80), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("call_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("tool_version", sa.String(length=32), nullable=False),
        sa.Column("risk_level", sa.String(length=2), nullable=False),
        sa.Column("policy_decision", sa.String(length=16), nullable=False),
        sa.Column("policy_rule_id", sa.String(length=100), nullable=False),
        sa.Column("policy_revision", sa.String(length=64), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("contract_digest", sa.String(length=64), nullable=False),
        sa.Column("arguments_digest", sa.String(length=64), nullable=False),
        sa.Column("binding_digest", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("resource_scope", sa.JSON(), nullable=False),
        sa.Column("consequences", sa.JSON(), nullable=False),
        sa.Column("reversible", sa.Boolean(), nullable=False),
        sa.Column("data_egress", sa.JSON(), nullable=False),
        sa.Column("expected_resource_versions", sa.JSON(), nullable=False),
        sa.Column("preview_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=True),
        sa.Column("resolved_by", sa.String(length=100), nullable=True),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled')",
            name="ck_approvals_status",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('approved', 'rejected')",
            name="ck_approvals_decision",
        ),
        sa.CheckConstraint(
            "policy_decision IN ('allow', 'deny', 'require_approval')",
            name="ck_approvals_policy_decision",
        ),
        sa.CheckConstraint(
            "scope IS NULL OR scope = 'once'",
            name="ck_approvals_scope",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["call_id"],
            ["tool_calls.call_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("approval_id"),
        sa.UniqueConstraint("call_id", name="uq_approvals_call_id"),
    )
    op.create_index(
        "ix_approvals_status_expires_at",
        "approvals",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_approvals_task_status",
        "approvals",
        ["task_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_approvals_task_status", table_name="approvals")
    op.drop_index("ix_approvals_status_expires_at", table_name="approvals")
    op.drop_table("approvals")
    with op.batch_alter_table("tool_calls") as batch_op:
        batch_op.drop_constraint(
            "fk_tool_calls_policy_event_id_task_events",
            type_="foreignkey",
        )
        batch_op.drop_constraint("ck_tool_calls_policy_effect", type_="check")
        batch_op.drop_column("authorization_id")
        batch_op.drop_column("policy_event_id")
        batch_op.drop_column("resource_scope_digest")
        batch_op.drop_column("policy_effect")
        batch_op.drop_column("policy_revision")
        batch_op.drop_column("policy_decision_id")
