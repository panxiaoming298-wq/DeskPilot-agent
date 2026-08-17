"""Add protected and versioned long-term memory.

Revision ID: 0034_long_term_memory
Revises: 0033_context_working_memory
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_long_term_memory"
down_revision: str | None = "0033_context_working_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "long_term_memory_proposals",
        sa.Column("proposal_id", sa.String(68), primary_key=True),
        sa.Column("memory_key", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("value_scheme", sa.String(64), nullable=False),
        sa.Column("value_payload", sa.LargeBinary(), nullable=True),
        sa.Column("value_digest", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(100), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(16), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("classification", sa.String(16), nullable=False),
        sa.Column("confidence_micros", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("proposal_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('proposal', 'pending_confirmation', 'confirmed', 'rejected')",
            name="ck_long_term_memory_proposal_status",
        ),
    )
    op.create_index(
        "ix_long_term_memory_proposals_memory_key", "long_term_memory_proposals", ["memory_key"]
    )
    op.create_index(
        "ix_long_term_memory_proposals_status",
        "long_term_memory_proposals",
        ["status", "created_at"],
    )
    op.create_table(
        "long_term_memory_items",
        sa.Column("memory_id", sa.String(68), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.String(68),
            sa.ForeignKey("long_term_memory_proposals.proposal_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("memory_key", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("value_scheme", sa.String(64), nullable=False),
        sa.Column("value_payload", sa.LargeBinary(), nullable=True),
        sa.Column("value_digest", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(100), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(16), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("classification", sa.String(16), nullable=False),
        sa.Column("confidence_micros", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("item_digest", sa.String(64), nullable=False),
        sa.Column("supersedes_memory_id", sa.String(68), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'conflict', 'expired', 'deleted')",
            name="ck_long_term_memory_item_status",
        ),
        sa.UniqueConstraint("memory_key", "kind", "version", name="uq_long_term_memory_version"),
    )
    op.create_index(
        "ix_long_term_memory_items_memory_key", "long_term_memory_items", ["memory_key"]
    )
    op.create_index(
        "ix_long_term_memory_recall",
        "long_term_memory_items",
        ["scope", "status", "kind", "created_at"],
    )
    op.create_table(
        "long_term_memory_conflicts",
        sa.Column("conflict_id", sa.String(68), primary_key=True),
        sa.Column("memory_key", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("memory_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("selected_memory_id", sa.String(68), nullable=True),
        sa.Column("conflict_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_long_term_memory_conflicts_memory_key", "long_term_memory_conflicts", ["memory_key"]
    )
    op.create_table(
        "long_term_memory_tombstones",
        sa.Column("tombstone_id", sa.String(68), primary_key=True),
        sa.Column("memory_id", sa.String(68), nullable=False, unique=True),
        sa.Column("memory_key_digest", sa.String(64), nullable=False),
        sa.Column("value_digest", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "long_term_memory_usage",
        sa.Column("usage_id", sa.String(68), primary_key=True),
        sa.Column("memory_id", sa.String(68), nullable=False),
        sa.Column("memory_version", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.String(40), nullable=False),
        sa.Column("invocation_id", sa.String(68), nullable=False),
        sa.Column("context_manifest_id", sa.String(68), nullable=False),
        sa.Column("agent_id", sa.String(100), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("provider_location", sa.String(16), nullable=False),
        sa.Column("purpose", sa.String(100), nullable=False),
        sa.Column("supplied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_reference", sa.String(100), nullable=False),
        sa.UniqueConstraint("memory_id", "context_manifest_id", name="uq_memory_manifest_usage"),
    )
    op.create_index(
        "ix_long_term_memory_usage_memory", "long_term_memory_usage", ["memory_id", "supplied_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_long_term_memory_usage_memory", table_name="long_term_memory_usage")
    op.drop_table("long_term_memory_usage")
    op.drop_table("long_term_memory_tombstones")
    op.drop_index(
        "ix_long_term_memory_conflicts_memory_key", table_name="long_term_memory_conflicts"
    )
    op.drop_table("long_term_memory_conflicts")
    op.drop_index("ix_long_term_memory_recall", table_name="long_term_memory_items")
    op.drop_index("ix_long_term_memory_items_memory_key", table_name="long_term_memory_items")
    op.drop_table("long_term_memory_items")
    op.drop_index("ix_long_term_memory_proposals_status", table_name="long_term_memory_proposals")
    op.drop_index(
        "ix_long_term_memory_proposals_memory_key", table_name="long_term_memory_proposals"
    )
    op.drop_table("long_term_memory_proposals")
