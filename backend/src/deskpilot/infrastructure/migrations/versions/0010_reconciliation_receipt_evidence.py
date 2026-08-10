"""Add append-only Runner receipt evidence for unknown Tool calls.

Revision ID: 0010_reconciliation_receipt_evidence
Revises: 0009_tool_commit_receipts
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_reconciliation_receipt_evidence"
down_revision: str | None = "0009_tool_commit_receipts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_reconciliation_evidence",
        sa.Column("evidence_id", sa.String(length=40), nullable=False),
        sa.Column("reconciliation_id", sa.String(length=140), nullable=False),
        sa.Column("evidence_digest", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("queried_runner_id", sa.String(length=128), nullable=True),
        sa.Column("receipt_id", sa.String(length=68), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('commit_receipt', 'no_receipt', 'query_failed')",
            name="ck_tool_reconciliation_evidence_kind",
        ),
        sa.CheckConstraint(
            "(kind = 'commit_receipt' AND receipt_id IS NOT NULL "
            "AND error_code IS NULL) OR "
            "(kind = 'no_receipt' AND receipt_id IS NULL "
            "AND error_code IS NULL) OR "
            "(kind = 'query_failed' AND receipt_id IS NULL "
            "AND error_code IS NOT NULL)",
            name="ck_tool_reconciliation_evidence_payload",
        ),
        sa.ForeignKeyConstraint(
            ["reconciliation_id"],
            ["tool_reconciliations.reconciliation_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["receipt_id"],
            ["tool_commit_receipts.receipt_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("evidence_id"),
        sa.UniqueConstraint(
            "reconciliation_id",
            "evidence_digest",
            name="uq_tool_reconciliation_evidence_digest",
        ),
    )
    op.create_index(
        "ix_tool_reconciliation_evidence_observed",
        "tool_reconciliation_evidence",
        ["reconciliation_id", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tool_reconciliation_evidence_observed",
        table_name="tool_reconciliation_evidence",
    )
    op.drop_table("tool_reconciliation_evidence")
