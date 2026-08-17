"""Add content-addressed local knowledge artifacts and chunks.

Revision ID: 0027_local_knowledge
Revises: 0026_alert_notifications
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_local_knowledge"
down_revision: str | None = "0026_alert_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_artifacts",
        sa.Column("artifact_id", sa.String(68), primary_key=True),
        sa.Column("content_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("parser_version", sa.String(64), nullable=False),
        sa.Column("chunker_version", sa.String(64), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("manifest_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "byte_size >= 0 AND chunk_count >= 1", name="ck_knowledge_artifacts_values"
        ),
    )
    op.create_table(
        "knowledge_sources",
        sa.Column("source_id", sa.String(68), primary_key=True),
        sa.Column("canonical_path", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "artifact_id",
            sa.String(68),
            sa.ForeignKey("knowledge_artifacts.artifact_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_knowledge_sources_artifact_id", "knowledge_sources", ["artifact_id"])
    op.create_table(
        "knowledge_chunks",
        sa.Column("chunk_id", sa.String(68), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(68),
            sa.ForeignKey("knowledge_artifacts.artifact_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("locator", sa.String(80), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_digest", sa.String(64), nullable=False),
        sa.Column("proof_digest", sa.String(64), nullable=False),
        sa.UniqueConstraint("artifact_id", "ordinal", name="uq_knowledge_chunks_ordinal"),
    )
    op.create_index("ix_knowledge_chunks_artifact", "knowledge_chunks", ["artifact_id", "ordinal"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_artifact", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index("ix_knowledge_sources_artifact_id", table_name="knowledge_sources")
    op.drop_table("knowledge_sources")
    op.drop_table("knowledge_artifacts")
