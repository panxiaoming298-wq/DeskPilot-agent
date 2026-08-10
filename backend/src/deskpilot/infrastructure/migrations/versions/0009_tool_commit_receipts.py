"""Add control-plane projections for durable Tool commit receipts.

Revision ID: 0009_tool_commit_receipts
Revises: 0008_tool_reconciliation
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_tool_commit_receipts"
down_revision: str | None = "0008_tool_reconciliation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_commit_receipts",
        sa.Column("receipt_id", sa.String(length=68), nullable=False),
        sa.Column("call_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=200), nullable=False),
        sa.Column("tool_version", sa.String(length=32), nullable=False),
        sa.Column("authorization_id", sa.String(length=80), nullable=False),
        sa.Column("approval_id", sa.String(length=128), nullable=False),
        sa.Column("preview_hash", sa.String(length=64), nullable=False),
        sa.Column("prepare_digest", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key_digest", sa.String(length=64), nullable=False),
        sa.Column("resource_versions_before", sa.JSON(), nullable=False),
        sa.Column("resource_versions_after", sa.JSON(), nullable=False),
        sa.Column("commit_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receipt_recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("projected_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["call_id"],
            ["tool_calls.call_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("receipt_id"),
        sa.UniqueConstraint("call_id", name="uq_tool_commit_receipts_call_id"),
    )


def downgrade() -> None:
    op.drop_table("tool_commit_receipts")
