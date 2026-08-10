"""Add protected Provider runtime configuration and append-only audit history.

Revision ID: 0004_provider_runtime_config
Revises: 0003_provider_catalog
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_provider_runtime_config"
down_revision: str | None = "0003_provider_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_provider_runtime_configs",
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("config_kind", sa.String(length=64), nullable=False),
        sa.Column("payload_schema_version", sa.Integer(), nullable=False),
        sa.Column("protection_scheme", sa.String(length=64), nullable=False),
        sa.Column("protected_payload", sa.LargeBinary(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider_id"),
    )
    op.create_table(
        "model_provider_config_audit_events",
        sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=40), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("changed_fields", sa.JSON(), nullable=False),
        sa.Column("credential_disposition", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("sequence"),
        sa.UniqueConstraint(
            "event_id",
            name="uq_model_provider_config_audit_event_id",
        ),
    )
    op.create_index(
        "ix_model_provider_config_audit_provider_sequence",
        "model_provider_config_audit_events",
        ["provider_id", "sequence"],
    )
    op.create_index(
        "ix_model_provider_config_audit_occurred_at",
        "model_provider_config_audit_events",
        ["occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_model_provider_config_audit_occurred_at",
        table_name="model_provider_config_audit_events",
    )
    op.drop_index(
        "ix_model_provider_config_audit_provider_sequence",
        table_name="model_provider_config_audit_events",
    )
    op.drop_table("model_provider_config_audit_events")
    op.drop_table("model_provider_runtime_configs")
