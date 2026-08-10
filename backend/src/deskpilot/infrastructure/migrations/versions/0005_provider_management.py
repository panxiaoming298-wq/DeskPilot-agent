"""Add persistent Provider management idempotency receipts.

Revision ID: 0005_provider_management
Revises: 0004_provider_runtime_config
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_provider_management"
down_revision: str | None = "0004_provider_runtime_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_provider_idempotency_records",
        sa.Column("key_digest", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=80), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key_digest"),
    )
    op.create_index(
        "ix_model_provider_idempotency_expires_at",
        "model_provider_idempotency_records",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_model_provider_idempotency_expires_at",
        table_name="model_provider_idempotency_records",
    )
    op.drop_table("model_provider_idempotency_records")
