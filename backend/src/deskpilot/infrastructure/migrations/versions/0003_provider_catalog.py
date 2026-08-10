"""Add versioned public Provider catalog persistence.

Revision ID: 0003_provider_catalog
Revises: 0002_transactional_outbox
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_provider_catalog"
down_revision: str | None = "0002_transactional_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_provider_catalog_state",
        sa.Column("catalog_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("default_provider_id", sa.String(length=64), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("catalog_id"),
    )
    op.create_table(
        "model_provider_catalog_entries",
        sa.Column("catalog_id", sa.String(length=32), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("descriptor", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["catalog_id"],
            ["model_provider_catalog_state.catalog_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("catalog_id", "provider_id"),
        sa.UniqueConstraint(
            "catalog_id",
            "ordinal",
            name="uq_model_provider_catalog_entry_ordinal",
        ),
    )
    op.create_index(
        "ix_model_provider_catalog_entries_enabled",
        "model_provider_catalog_entries",
        ["enabled"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_model_provider_catalog_entries_enabled",
        table_name="model_provider_catalog_entries",
    )
    op.drop_table("model_provider_catalog_entries")
    op.drop_table("model_provider_catalog_state")
