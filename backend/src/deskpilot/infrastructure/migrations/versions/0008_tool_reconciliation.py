"""Add unknown-call reconciliation and durable idempotency receipts.

Revision ID: 0008_tool_reconciliation
Revises: 0007_policy_approvals
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_tool_reconciliation"
down_revision: str | None = "0007_policy_approvals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_idempotency_receipts",
        sa.Column("receipt_id", sa.String(length=140), nullable=False),
        sa.Column("call_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("tool_version", sa.String(length=32), nullable=False),
        sa.Column("key_digest", sa.String(length=64), nullable=False),
        sa.Column("arguments_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["call_id"],
            ["tool_calls.call_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("receipt_id"),
        sa.UniqueConstraint(
            "tool_name",
            "tool_version",
            "key_digest",
            name="uq_tool_idempotency_receipts_scope_key",
        ),
        sa.UniqueConstraint(
            "call_id",
            name="uq_tool_idempotency_receipts_call_id",
        ),
    )

    op.create_table(
        "tool_reconciliations",
        sa.Column("reconciliation_id", sa.String(length=140), nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=False),
        sa.Column("call_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("evidence_summary", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.String(length=100), nullable=True),
        sa.Column("unknown_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("new_attempt_task_id", sa.String(length=40), nullable=True),
        sa.Column("new_attempt_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'resolved')",
            name="ck_tool_reconciliations_status",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ("
            "'confirmed_succeeded', 'confirmed_failed', "
            "'confirmed_no_effect', 'accepted_unknown')",
            name="ck_tool_reconciliations_outcome",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND outcome IS NULL AND resolved_at IS NULL) OR "
            "(status = 'resolved' AND outcome IS NOT NULL AND resolved_at IS NOT NULL)",
            name="ck_tool_reconciliations_resolution",
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
        sa.ForeignKeyConstraint(
            ["new_attempt_task_id"],
            ["tasks.task_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("reconciliation_id"),
        sa.UniqueConstraint("call_id", name="uq_tool_reconciliations_call_id"),
        sa.UniqueConstraint(
            "new_attempt_task_id",
            name="uq_tool_reconciliations_new_attempt_task_id",
        ),
    )
    op.create_index(
        "ix_tool_reconciliations_status_unknown_at",
        "tool_reconciliations",
        ["status", "unknown_at"],
    )
    op.create_index(
        "ix_tool_reconciliations_task_status",
        "tool_reconciliations",
        ["task_id", "status"],
    )

    op.create_table(
        "tool_reconciliation_idempotency_records",
        sa.Column("key_digest", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=80), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("reconciliation_id", sa.String(length=140), nullable=False),
        sa.Column("created_task_id", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["reconciliation_id"],
            ["tool_reconciliations.reconciliation_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_task_id"],
            ["tasks.task_id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("key_digest"),
    )
    op.create_index(
        "ix_tool_reconciliation_idempotency_reconciliation",
        "tool_reconciliation_idempotency_records",
        ["reconciliation_id", "created_at"],
    )

    # Adopt existing uncertain calls without rewriting their terminal ledger state.
    op.execute(
        sa.text(
            """
            INSERT INTO tool_reconciliations (
                reconciliation_id, task_id, call_id, status, outcome,
                evidence_summary, resolved_by, unknown_at, resolved_at,
                new_attempt_task_id, new_attempt_created_at, updated_at
            )
            SELECT
                'rec_' || call_id, task_id, call_id, 'pending', NULL,
                NULL, NULL, COALESCE(finished_at, updated_at), NULL,
                NULL, NULL, updated_at
            FROM tool_calls
            WHERE status = 'unknown'
            """
        )
    )
    # For any legacy duplicate key, the earliest durable call owns the receipt.
    op.execute(
        sa.text(
            """
            INSERT INTO tool_idempotency_receipts (
                receipt_id, call_id, tool_name, tool_version,
                key_digest, arguments_digest, created_at
            )
            SELECT
                'tir_' || current.call_id,
                current.call_id,
                current.tool_name,
                current.tool_version,
                current.idempotency_key_digest,
                current.arguments_digest,
                current.requested_at
            FROM tool_calls AS current
            WHERE current.idempotency = 'key_required'
              AND current.idempotency_key_digest IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM tool_calls AS earlier
                  WHERE earlier.idempotency = 'key_required'
                    AND earlier.idempotency_key_digest = current.idempotency_key_digest
                    AND earlier.tool_name = current.tool_name
                    AND earlier.tool_version = current.tool_version
                    AND (
                        earlier.requested_at < current.requested_at
                        OR (
                            earlier.requested_at = current.requested_at
                            AND earlier.call_id < current.call_id
                        )
                    )
              )
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_reconciliation_idempotency_reconciliation",
        table_name="tool_reconciliation_idempotency_records",
    )
    op.drop_table("tool_reconciliation_idempotency_records")
    op.drop_index(
        "ix_tool_reconciliations_task_status",
        table_name="tool_reconciliations",
    )
    op.drop_index(
        "ix_tool_reconciliations_status_unknown_at",
        table_name="tool_reconciliations",
    )
    op.drop_table("tool_reconciliations")
    op.drop_table("tool_idempotency_receipts")
