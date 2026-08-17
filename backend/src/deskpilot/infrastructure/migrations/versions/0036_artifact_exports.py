"""Add exact, independently confirmed Artifact export receipts.

Revision ID: 0036_artifact_exports
Revises: 0035_context_compaction
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_artifact_exports"
down_revision: str | None = "0035_context_compaction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifact_exports",
        sa.Column("export_id", sa.String(68), primary_key=True),
        sa.Column(
            "delivery_id",
            sa.String(68),
            sa.ForeignKey("delivery_manifests.delivery_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            sa.String(40),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.String(68),
            sa.ForeignKey("artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "revision_id",
            sa.String(68),
            sa.ForeignKey("artifact_revisions.revision_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_path", sa.String(32767), nullable=False),
        sa.Column("conflict_policy", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("confirmation_digest", sa.String(64), nullable=False),
        sa.Column("prepare_key_digest", sa.String(64), nullable=False),
        sa.Column("commit_key_digest", sa.String(64), nullable=True),
        sa.Column("receipt_digest", sa.String(64), nullable=True),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('prepared', 'committing', 'committed', 'failed')",
            name="ck_artifact_export_status",
        ),
        sa.CheckConstraint("byte_count >= 1", name="ck_artifact_export_byte_count"),
        sa.UniqueConstraint("delivery_id", "target_path", name="uq_artifact_export_target"),
        sa.UniqueConstraint("prepare_key_digest", name="uq_artifact_export_prepare_key"),
    )
    op.create_index("ix_artifact_exports_delivery_id", "artifact_exports", ["delivery_id"])
    op.create_index("ix_artifact_exports_task_id", "artifact_exports", ["task_id"])
    op.create_index("ix_artifact_exports_status", "artifact_exports", ["status"])


def downgrade() -> None:
    op.drop_index("ix_artifact_exports_status", table_name="artifact_exports")
    op.drop_index("ix_artifact_exports_task_id", table_name="artifact_exports")
    op.drop_index("ix_artifact_exports_delivery_id", table_name="artifact_exports")
    op.drop_table("artifact_exports")
