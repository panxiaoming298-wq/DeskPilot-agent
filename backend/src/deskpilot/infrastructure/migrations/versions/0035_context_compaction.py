"""Add source-bound context compaction snapshots.

Revision ID: 0035_context_compaction
Revises: 0034_long_term_memory
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_context_compaction"
down_revision: str | None = "0034_long_term_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "compaction_snapshots",
        sa.Column("snapshot_id", sa.String(68), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.String(40), nullable=True),
        sa.Column(
            "parent_snapshot_id",
            sa.String(68),
            sa.ForeignKey("compaction_snapshots.snapshot_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("source_set_digest", sa.String(64), nullable=False),
        sa.Column("structured_fields", sa.JSON(), nullable=False),
        sa.Column("narrative_summary", sa.Text(), nullable=True),
        sa.Column("coverage_manifest", sa.JSON(), nullable=False),
        sa.Column("compressor_version", sa.String(64), nullable=False),
        sa.Column("classification", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("snapshot_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stale_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'conflict', 'stale')",
            name="ck_compaction_snapshot_status",
        ),
        sa.CheckConstraint(
            "classification IN ('public', 'internal', 'sensitive')",
            name="ck_compaction_snapshot_classification",
        ),
    )
    op.create_index("ix_compaction_snapshots_task_id", "compaction_snapshots", ["task_id"])
    op.create_index(
        "ix_compaction_snapshots_task",
        "compaction_snapshots",
        ["task_id", "status", "created_at"],
    )
    op.create_table(
        "compaction_source_refs",
        sa.Column(
            "snapshot_id",
            sa.String(68),
            sa.ForeignKey("compaction_snapshots.snapshot_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("source_type", sa.String(100), nullable=False),
        sa.Column("source_ref", sa.String(500), nullable=False),
        sa.Column("source_version", sa.String(100), nullable=False),
        sa.Column("content_digest", sa.String(64), nullable=False),
        sa.Column("authority_class", sa.String(32), nullable=False),
        sa.Column("classification", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'stale', 'deleted', 'out_of_scope')",
            name="ck_compaction_source_status",
        ),
    )
    op.create_table(
        "compaction_coverage_items",
        sa.Column(
            "snapshot_id",
            sa.String(68),
            sa.ForeignKey("compaction_snapshots.snapshot_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("field_kind", sa.String(32), nullable=False),
        sa.Column("value_digest", sa.String(64), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "status IN ('covered', 'conflict', 'stale')",
            name="ck_compaction_coverage_status",
        ),
    )


def downgrade() -> None:
    op.drop_table("compaction_coverage_items")
    op.drop_table("compaction_source_refs")
    op.drop_index("ix_compaction_snapshots_task", table_name="compaction_snapshots")
    op.drop_index("ix_compaction_snapshots_task_id", table_name="compaction_snapshots")
    op.drop_table("compaction_snapshots")
